"""
The one place this app talks to Twilio.

Everything the rest of the app needs from the provider goes through
:class:`TwilioGateway`, which wraps the generated ``twilio_sdk`` client and
translates every way a call can fail into a :class:`ProviderError` carrying
the status this app should answer with and whether the call may have taken
effect upstream (``outcome_unknown``).

Phone numbers and credentials are never logged from here.
"""

from __future__ import annotations

import email.utils
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import ApiError, BasicAuthCredentials, HttpClient, RawError, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import (
    MessageEnumDirection,
    MessageEnumScheduleType,
    MessageEnumStatus,
    MessageEnumUpdateStatus,
)

logger = logging.getLogger(__name__)

# Failures raised before the request left this process: nothing can have
# happened at the provider.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Our own view of a message's fate, derived from the provider's status.
OUTCOME_DELIVERED = "delivered"
OUTCOME_SENT = "sent"
OUTCOME_PENDING = "pending"
OUTCOME_SCHEDULED = "scheduled"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELED = "canceled"
OUTCOME_UNKNOWN = "unknown"

# Outcomes that can no longer change at the provider.
TERMINAL_OUTCOMES = frozenset({OUTCOME_DELIVERED, OUTCOME_FAILED, OUTCOME_CANCELED})

# Hard stop for list pagination, so a runaway range cannot loop forever.
MAX_LIST_PAGES = 200
LIST_PAGE_SIZE = 1000


def outcome_from_status(status: MessageEnumStatus | str | None) -> str:
    """The single mapping from a provider message status to our outcome."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return OUTCOME_DELIVERED
        case MessageEnumStatus.SENT:
            # Handed to the carrier; delivery not (yet) confirmed.
            return OUTCOME_SENT
        case MessageEnumStatus.QUEUED | MessageEnumStatus.ACCEPTED | MessageEnumStatus.SENDING:
            return OUTCOME_PENDING
        case MessageEnumStatus.PARTIALLY_DELIVERED:
            return OUTCOME_PENDING
        case MessageEnumStatus.SCHEDULED:
            return OUTCOME_SCHEDULED
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return OUTCOME_FAILED
        case MessageEnumStatus.CANCELED:
            return OUTCOME_CANCELED
        case _:
            # Inbound statuses, or a value newer than this SDK: neither done
            # nor failed.
            return OUTCOME_UNKNOWN


class ProviderError(Exception):
    """
    A provider call that did not produce a usable answer.

    ``http_status`` is what this app's endpoint should answer with;
    ``outcome_unknown`` says whether a write may nevertheless have landed.
    """

    def __init__(
        self,
        http_status: int,
        message: str,
        *,
        outcome_unknown: bool,
        provider_status: int | None = None,
        provider_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.provider_code = provider_code


class ProviderConfigError(ProviderError):
    """Our configuration or credentials are wrong; nothing was done."""


class ProviderRejected(ProviderError):
    """The provider refused the request itself (a definite "no")."""


class InvalidDestination(Exception):
    """The provider does not recognise the number as a usable destination."""


@dataclass(frozen=True)
class ProviderMessage:
    sid: str
    status: str | None
    outcome: str
    direction: str | None
    body: str | None
    error_code: int | None
    error_message: str | None
    date_sent: datetime | None
    date_created: datetime | None


@dataclass(frozen=True)
class LookedUpNumber:
    phone_number: str
    country_code: str | None


def _value(member: object) -> object:
    """Resolve an SDK ``UNSET`` to ``None`` so values can leave the SDK."""
    return None if isinstance(member, UnsetType) else member


def _str_or_none(member: object) -> str | None:
    value = _value(member)
    return value if isinstance(value, str) else None


def _parse_rfc1123(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _error_detail(error: RawError) -> tuple[int | None, str]:
    try:
        payload = error.json()
    except ValueError:
        return None, ""
    if not isinstance(payload, dict):
        return None, ""
    code = payload.get("code")
    message = payload.get("message")
    return (code if isinstance(code, int) else None, message if isinstance(message, str) else "")


def _to_message(message: ApiV2010AccountMessage) -> ProviderMessage:
    sid = _str_or_none(message.sid)
    if not sid:
        # A 2xx without an identifier: the call may have taken effect but we
        # cannot name what it created.
        raise ProviderError(502, "Provider response carried no message identifier.", outcome_unknown=True)
    status = _value(message.status)
    status_enum_or_str = status if isinstance(status, (MessageEnumStatus, str)) else None
    direction = _value(message.direction)
    error_code = _value(message.error_code)
    return ProviderMessage(
        sid=sid,
        status=str(status_enum_or_str) if status_enum_or_str is not None else None,
        outcome=outcome_from_status(status_enum_or_str),
        direction=str(direction) if isinstance(direction, (MessageEnumDirection, str)) else None,
        body=_str_or_none(message.body),
        error_code=error_code if isinstance(error_code, int) else None,
        error_message=_str_or_none(message.error_message),
        date_sent=_parse_rfc1123(_str_or_none(message.date_sent)),
        date_created=_parse_rfc1123(_str_or_none(message.date_created)),
    )


class TwilioGateway:
    """Sync wrapper over one long-lived ``TwilioSdkClient``."""

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        messaging_service_sid: str,
        base_url: str | None = None,
        timeout: float = 10.0,
        http_client: HttpClient | None = None,
    ) -> None:
        if not account_sid or not auth_token:
            raise ProviderConfigError(503, "Twilio credentials are not configured.", outcome_unknown=False)
        if not from_number:
            raise ProviderConfigError(503, "TWILIO_FROM_NUMBER is not configured.", outcome_unknown=False)
        self.account_sid = account_sid
        self.from_number = from_number
        self.messaging_service_sid = messaging_service_sid
        credentials = BasicAuthCredentials(username=account_sid, password=auth_token)
        # TWILIO_BASE_URL governs the messaging API only (the ``default``
        # server); Lookup keeps its own host.
        server_config: ServerConfigDict | None = {"default": {"base_url": base_url}} if base_url else None
        if http_client is not None:
            self._client = TwilioSdkClient(
                account_sid_auth_token=credentials,
                timeout=timeout,
                server_config=server_config,
                custom_http_client=http_client,
            )
        else:
            self._client = TwilioSdkClient(
                account_sid_auth_token=credentials, timeout=timeout, server_config=server_config
            )

    def close(self) -> None:
        self._client.close()

    @contextmanager
    def _boundary(self, operation: str, *, write: bool) -> Iterator[None]:
        """Translate every SDK failure kind into a ProviderError."""
        try:
            yield
        except ApiError as e:
            status = e.status_code
            code, detail = _error_detail(e.error) if isinstance(e.error, RawError) else (None, "")
            logger.warning("twilio %s failed: HTTP %s code=%s", operation, status, code)
            if status in (401, 403):
                raise ProviderConfigError(
                    502, "The messaging provider refused our credentials.", outcome_unknown=False,
                    provider_status=status, provider_code=code,
                ) from e
            if status == 429:
                raise ProviderError(
                    503, "The messaging provider is rate limiting us.", outcome_unknown=False,
                    provider_status=status, provider_code=code,
                ) from e
            if status in (400, 404, 409, 422):
                raise ProviderRejected(
                    status, detail or "The messaging provider rejected the request.", outcome_unknown=False,
                    provider_status=status, provider_code=code,
                ) from e
            raise ProviderError(
                502, "The messaging provider failed.", outcome_unknown=write and status >= 500,
                provider_status=status, provider_code=code,
            ) from e
        except NEVER_SENT as e:
            logger.warning("twilio %s not sent: %s", operation, type(e).__name__)
            raise ProviderError(502, "The messaging provider could not be reached.", outcome_unknown=False) from e
        except httpx.RequestError as e:
            logger.warning("twilio %s got no answer: %s", operation, type(e).__name__)
            raise ProviderError(504, "The messaging provider did not answer.", outcome_unknown=write) from e
        except ValueError as e:  # pydantic ValidationError, or a non-JSON body
            logger.warning("twilio %s returned an unreadable response", operation)
            raise ProviderError(502, "Unreadable response from the messaging provider.", outcome_unknown=write) from e

    # -- Lookup -------------------------------------------------------------

    def lookup_number(self, raw_number: str, country_code: str | None = None) -> LookedUpNumber:
        """
        Ask the provider whether ``raw_number`` is a usable phone number and
        return its canonical E.164 form. Raises InvalidDestination if not.
        """
        try:
            with self._boundary("lookup", write=False):
                result = self._client.lookups_v1_phone_number_api.fetch_phone_number2(
                    raw_number, country_code=country_code
                )
        except ProviderRejected as e:
            if e.provider_status in (400, 404):
                raise InvalidDestination() from e
            raise
        phone_number = _str_or_none(result.phone_number)
        if not phone_number or not phone_number.startswith("+"):
            raise InvalidDestination()
        return LookedUpNumber(phone_number=phone_number, country_code=_str_or_none(result.country_code))

    # -- Messages -----------------------------------------------------------

    def send(self, to: str, body: str) -> ProviderMessage:
        with self._boundary("send", write=True):
            message = self._client.api20100401_message.create_message(
                self.account_sid, to, from_=self.from_number, body=body
            )
        return _to_message(message)

    def schedule(self, to: str, body: str, send_at: datetime) -> ProviderMessage:
        """Queue ``body`` with the provider to be sent at ``send_at``."""
        if not self.messaging_service_sid:
            raise ProviderConfigError(503, "TWILIO_MESSAGING_SERVICE_SID is not configured.", outcome_unknown=False)
        with self._boundary("schedule", write=True):
            message = self._client.api20100401_message.create_message(
                self.account_sid,
                to,
                from_=self.from_number,
                messaging_service_sid=self.messaging_service_sid,
                body=body,
                schedule_type=MessageEnumScheduleType.FIXED,
                send_at=send_at.astimezone(timezone.utc),
            )
        return _to_message(message)

    def fetch(self, sid: str) -> ProviderMessage:
        with self._boundary("fetch", write=False):
            message = self._client.api20100401_message.fetch_message(self.account_sid, sid)
        return _to_message(message)

    def cancel(self, sid: str) -> ProviderMessage:
        """Call off a message that has not been sent yet (a scheduled one)."""
        with self._boundary("cancel", write=True):
            message = self._client.api20100401_message.update_message(
                self.account_sid, sid, status=MessageEnumUpdateStatus.CANCELED
            )
        return _to_message(message)

    def redact(self, sid: str) -> ProviderMessage:
        """Remove the message text at the provider; the record itself stays."""
        with self._boundary("redact", write=True):
            message = self._client.api20100401_message.update_message(self.account_sid, sid, body="")
        return _to_message(message)

    def _list(
        self,
        *,
        to: str | None = None,
        sent_after: datetime | None = None,
        sent_before: datetime | None = None,
        page_size: int = LIST_PAGE_SIZE,
        max_pages: int = MAX_LIST_PAGES,
        exhaustive: bool = True,
    ) -> Iterator[ProviderMessage]:
        """
        Messages sent from our number, following ``next_page_uri``. When
        ``exhaustive``, running past ``max_pages`` is an error rather than a
        silently partial answer.
        """
        page: int | None = None
        page_token: str | None = None
        for _ in range(max_pages):
            with self._boundary("list", write=False):
                response = self._client.api20100401_message.list_message(
                    self.account_sid,
                    to=to,
                    from_=self.from_number,
                    date_sent_query_query=sent_after,   # wire "DateSent>"
                    date_sent_query=sent_before,        # wire "DateSent<"
                    page_size=page_size,
                    page=page,
                    page_token=page_token,
                )
            messages = _value(response.messages)
            if not isinstance(messages, list):
                raise ProviderError(502, "Provider list response carried no messages.", outcome_unknown=False)
            for message in messages:
                yield _to_message(message)
            next_uri = _str_or_none(response.next_page_uri)
            if not next_uri:
                return
            query = parse_qs(urlparse(next_uri).query)
            token = query.get("PageToken", [None])[0]
            page_number = query.get("Page", [None])[0]
            if not token:
                return
            page_token = token
            page = int(page_number) if page_number and page_number.isdigit() else (page or 0) + 1
        if exhaustive:
            raise ProviderError(502, "Provider listing exceeded the page limit.", outcome_unknown=False)

    def list_sent(self, start: datetime, end: datetime) -> Iterator[ProviderMessage]:
        """
        The provider's own record of messages sent from TWILIO_FROM_NUMBER
        with a sent date in [start, end]. The provider filter is asked for
        the range; results are narrowed to the exact instants here.
        """
        for message in self._list(sent_after=start, sent_before=end):
            if message.direction == MessageEnumDirection.INBOUND.value:
                continue
            if message.date_sent is None or not (start <= message.date_sent <= end):
                continue
            yield message

    def find_by_reference(self, to: str, reference: str) -> ProviderMessage | None:
        """
        Find a message we created to ``to`` whose text carries ``reference``
        (used when a send's outcome is unknown). Scheduled messages have no
        sent date, so no date filter is applied; only recent pages are read.
        """
        for message in self._list(to=to, page_size=50, max_pages=3, exhaustive=False):
            if message.body and reference in message.body:
                return message
        return None
