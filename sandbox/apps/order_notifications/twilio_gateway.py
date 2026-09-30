"""
The one place this application talks to Twilio.

Everything the rest of the app needs from the provider goes through
:class:`TwilioGateway`, which turns the SDK's four failure kinds (``ApiError``,
decode failures, transport errors that never left the process, and transport
errors whose outcome is unknown) into a single :class:`ProviderError`.

Phone numbers and message bodies are never logged from here.
"""
from __future__ import annotations

import atexit
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import ValidationError
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import ApiError, BasicAuthCredentials, HttpClient, RawError, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumUpdateStatus

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Provider statuses after which a message's outcome can no longer change.
FINAL_STATUSES = frozenset({"delivered", "undelivered", "failed", "canceled", "read", "received"})
# Provider statuses of a message that has not gone out (yet).
IN_FLIGHT_STATUSES = frozenset({"accepted", "queued", "sending", "scheduled"})

# Upper bound on pages walked for one reconciliation request.
MAX_LIST_PAGES = 50
LIST_PAGE_SIZE = 1000


class GatewayNotConfigured(Exception):
    """Raised when the Twilio settings needed to talk to the provider are absent."""


class ProviderError(Exception):
    """
    A call to Twilio did not produce a usable answer.

    ``status_code`` is the HTTP status this application should answer with.
    ``outcome_unknown`` is True when the request may have taken effect at the
    provider (so it must not be blindly repeated). ``rejected`` is True when the
    provider definitively refused the request, i.e. nothing happened.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        rejected: bool = False,
        provider_status: int | None = None,
        provider_code: int | None = None,
        provider_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.rejected = rejected
        self.provider_status = provider_status
        self.provider_code = provider_code
        self.provider_message = provider_message


@dataclass(frozen=True)
class MessageSnapshot:
    """What the provider says about one message, with UNSET resolved to None."""

    sid: str
    status: str | None
    from_number: str | None
    to_number: str | None
    body: str | None
    error_code: int | None
    error_message: str | None
    date_created: datetime | None
    date_sent: datetime | None


@dataclass(frozen=True)
class LookupResult:
    valid: bool
    e164: str | None
    reasons: list[str]


def _value(v: T | UnsetType | None) -> T | None:
    return None if isinstance(v, UnsetType) else v


def _rfc2822(v: str | UnsetType | None) -> datetime | None:
    raw = _value(v)
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _snapshot(message: ApiV2010AccountMessage) -> MessageSnapshot:
    sid = _value(message.sid)
    if not sid:
        # A 2xx without the identifier: we cannot name what (if anything) was created.
        raise ProviderError(502, "Twilio returned no message SID.", outcome_unknown=True)
    status = _value(message.status)
    return MessageSnapshot(
        sid=sid,
        status=str(status) if status is not None else None,
        from_number=_value(message.from_),
        to_number=_value(message.to),
        body=_value(message.body),
        error_code=_value(message.error_code),
        error_message=scrub(_value(message.error_message)),
        date_created=_rfc2822(message.date_created),
        date_sent=_rfc2822(message.date_sent),
    )


_PHONE_LIKE = re.compile(r"\+?\d[\d\s().-]{5,}\d")


def scrub(text: str | None) -> str | None:
    """Remove anything that looks like a phone number (provider messages echo them back)."""
    return _PHONE_LIKE.sub("[number]", text) if text else text


def _raw_error_details(error: RawError) -> tuple[int | None, str | None]:
    try:
        body: Any = error.json()
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    code = body.get("code")
    message = body.get("message")
    return (code if isinstance(code, int) else None, scrub(message) if isinstance(message, str) else None)


def _from_api_error(e: ApiError[Any]) -> ProviderError:
    status = e.status_code
    code, message = _raw_error_details(e.error) if isinstance(e.error, RawError) else (None, None)

    def err(http_status: int, text: str, *, rejected: bool = False, unknown: bool = False) -> ProviderError:
        return ProviderError(
            http_status, text, rejected=rejected, outcome_unknown=unknown,
            provider_status=status, provider_code=code, provider_message=message)

    if status in (401, 403):
        # Our credentials or our account: never the caller's fault.
        return err(502, "Twilio refused this application's credentials.", rejected=True)
    if status == 429:
        return err(503, "Twilio is rate-limiting this application.", rejected=True)
    if 400 <= status < 500:
        return err(422, message or "Twilio rejected the request.", rejected=True)
    return err(502, "Twilio is unavailable.", unknown=status >= 500)


def _call(operation: str, fn: Callable[[], T]) -> T:
    """Run one SDK call and translate every failure kind into ProviderError."""
    try:
        return fn()
    except ApiError as e:
        err = _from_api_error(e)
        logger.warning(
            "Twilio %s failed: HTTP %s code=%s", operation, err.provider_status, err.provider_code)
        raise err from e
    except (ValidationError, ValueError) as e:
        logger.error("Twilio %s returned an unreadable response", operation)
        raise ProviderError(502, "Twilio returned an unreadable response.", outcome_unknown=True) from e
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError) as e:
        logger.warning("Twilio %s not sent: %s", operation, type(e).__name__)
        raise ProviderError(502, "Could not reach Twilio.", rejected=True) from e
    except httpx.RequestError as e:
        logger.warning("Twilio %s outcome unknown: %s", operation, type(e).__name__)
        raise ProviderError(504, "No response from Twilio.", outcome_unknown=True) from e


class TwilioGateway:
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
        missing = [
            name for name, value in (
                ("TWILIO_ACCOUNT_SID", account_sid),
                ("TWILIO_AUTH_TOKEN", auth_token),
                ("TWILIO_FROM_NUMBER", from_number),
                ("TWILIO_MESSAGING_SERVICE_SID", messaging_service_sid),
            ) if not value
        ]
        if missing:
            raise GatewayNotConfigured("Missing Twilio settings: " + ", ".join(missing))
        self.account_sid = account_sid
        self.from_number = from_number
        self.messaging_service_sid = messaging_service_sid
        # Only the messaging API's server ("default", api.twilio.com) is overridden;
        # Lookups is served from its own host and keeps its default.
        server_config: ServerConfigDict | None = (
            {"default": {"base_url": base_url}} if base_url else None)
        credentials = BasicAuthCredentials(username=account_sid, password=auth_token)
        # Retries keep the SDK's default policy: idempotent GETs are retried on
        # 408/429/5xx and dropped connections; POSTs (send, cancel, redact) never are.
        if http_client is not None:
            self._client = TwilioSdkClient(
                server_config=server_config,
                timeout=timeout,
                custom_http_client=http_client,
                account_sid_auth_token=credentials,
            )
        else:
            self._client = TwilioSdkClient(
                server_config=server_config,
                timeout=timeout,
                account_sid_auth_token=credentials,
            )

    def close(self) -> None:
        self._client.close()

    # --- Lookups -------------------------------------------------------------

    def lookup_number(self, number: str, country_code: str | None = None) -> LookupResult:
        """Ask Twilio whether ``number`` is a usable destination and for its E.164 form."""
        try:
            if country_code:
                result = _call("lookup", lambda: self._client.lookups_v2_phone_number.fetch_phone_number2(
                    number, country_code=country_code))
            else:
                result = _call("lookup", lambda: self._client.lookups_v2_phone_number.fetch_phone_number2(number))
        except ProviderError as e:
            if e.provider_status == 404:
                # Lookups answers 404 for something that is not a phone number at all.
                return LookupResult(valid=False, e164=None, reasons=["NOT_A_NUMBER"])
            raise
        valid = _value(result.valid)
        if valid is None:
            raise ProviderError(502, "Twilio Lookup returned no validity verdict.", outcome_unknown=True)
        reasons = [str(r) for r in (_value(result.validation_errors) or [])]
        return LookupResult(valid=valid, e164=_value(result.phone_number), reasons=reasons)

    # --- Messages ------------------------------------------------------------

    def send_sms(self, to: str, body: str, *, send_at: datetime | None = None) -> MessageSnapshot:
        """
        Send ``body`` to ``to`` from this application's number.

        With ``send_at`` the message is scheduled with Twilio (Messaging Service
        scheduling) rather than held by this application.
        """
        messages = self._client.api20100401_message
        if send_at is None:
            message = _call("send", lambda: messages.create_message(
                self.account_sid, to, from_=self.from_number, body=body))
        else:
            when = send_at if send_at.tzinfo else send_at.replace(tzinfo=timezone.utc)
            message = _call("schedule", lambda: messages.create_message(
                self.account_sid,
                to,
                from_=self.from_number,
                messaging_service_sid=self.messaging_service_sid,
                schedule_type=MessageEnumScheduleType.FIXED,
                send_at=when,
                body=body,
            ))
        return _snapshot(message)

    def fetch(self, sid: str) -> MessageSnapshot:
        return _snapshot(_call("fetch", lambda: self._client.api20100401_message.fetch_message(
            self.account_sid, sid)))

    def cancel_scheduled(self, sid: str) -> MessageSnapshot:
        """Call off a scheduled message that has not gone out yet."""
        return _snapshot(_call("cancel", lambda: self._client.api20100401_message.update_message(
            self.account_sid, sid, status=MessageEnumUpdateStatus.CANCELED)))

    def redact(self, sid: str) -> MessageSnapshot:
        """Erase the message's text at the provider (the message record itself survives)."""
        snapshot = _snapshot(_call("redact", lambda: self._client.api20100401_message.update_message(
            self.account_sid, sid, body="")))
        if snapshot.body:
            raise ProviderError(502, "Twilio did not confirm the message text was erased.", outcome_unknown=True)
        return snapshot

    def list_sent_from_our_number(self, start: datetime, end: datetime) -> list[MessageSnapshot]:
        """
        The provider's record of messages sent from TWILIO_FROM_NUMBER within [start, end].

        The sender and date filters are applied by Twilio; the result is then
        trimmed to the exact instants, since the provider's date filter may be
        coarser than a date-time.
        """
        messages = self._client.api20100401_message
        found: list[MessageSnapshot] = []
        page: int | None = None
        page_token: str | None = None
        for _ in range(MAX_LIST_PAGES):
            p, tok = page, page_token
            response = _call("list", lambda: messages.list_message(
                self.account_sid,
                from_=self.from_number,
                date_sent_query_query=start,
                date_sent_query=end,
                page_size=LIST_PAGE_SIZE,
                page=p,
                page_token=tok,
            ))
            for message in _value(response.messages) or []:
                snap = _snapshot(message)
                if snap.date_sent is None or start <= snap.date_sent <= end:
                    found.append(snap)
            next_uri = _value(response.next_page_uri)
            if not next_uri:
                return found
            query = parse_qs(urlsplit(next_uri).query)
            tokens = query.get("PageToken")
            page_token = tokens[0] if tokens else None
            page_values = query.get("Page") or []
            page = int(page_values[0]) if page_values and page_values[0].isdigit() else None
            if not page_token:
                return found
        raise ProviderError(502, "Too many messages in range; narrow the date range.")


_lock = threading.Lock()
_gateway: TwilioGateway | None = None


def get_gateway() -> TwilioGateway:
    """
    The process-wide gateway, built on first use (i.e. after any worker fork)
    and closed at interpreter exit.
    """
    global _gateway
    if _gateway is None:
        with _lock:
            if _gateway is None:
                from django.conf import settings

                gateway = TwilioGateway(
                    account_sid=settings.TWILIO_ACCOUNT_SID,
                    auth_token=settings.TWILIO_AUTH_TOKEN,
                    from_number=settings.TWILIO_FROM_NUMBER,
                    messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
                    base_url=settings.TWILIO_BASE_URL,
                    timeout=settings.TWILIO_TIMEOUT,
                )
                atexit.register(gateway.close)
                _gateway = gateway
    return _gateway


def set_gateway(gateway: TwilioGateway | None) -> None:
    """Replace the process-wide gateway (used by tests)."""
    global _gateway
    with _lock:
        _gateway = gateway
