"""
The one module that talks to the Twilio SDK (``twilio_sdk``).

Everything that leaves this module is this app's own type: provider answers
become ``MessageRecord`` / ``LookupResult``, provider failures become
``ProviderError``. Transport and decode failures are left to propagate as the
library exceptions they are, so that the safe write (``safe_write.py``) can
tell a request that never left from one that may have landed.
"""

from __future__ import annotations

import atexit
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import (
    ApiError,
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    UnsetType,
)
from twilio_sdk.models import ApiV2010AccountMessage, LookupResponse
from twilio_sdk.models.enums import (
    MessageEnumScheduleType,
    MessageEnumStatus,
    MessageEnumUpdateStatus,
)

__all__ = ["MessageEnumStatus"]  # re-exported: services compares stored statuses against it

logger = logging.getLogger("apps.sms_notifications.twilio")

# Transport failures that happen before the request leaves this machine:
# nothing can have reached the provider.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

DEFAULT_TIMEOUT_SECONDS = 10.0


class ProviderError(Exception):
    """A provider failure, translated for this app's HTTP boundary."""

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown


def provider_error(status: int, error: object) -> ProviderError:
    """Map a provider error status onto the status this app answers with."""
    match status:
        case 401 | 403:
            # Our credentials or our account: the caller cannot fix it.
            return ProviderError(502, "The messaging provider refused this application's credentials.")
        case 429:
            return ProviderError(503, "The messaging provider is rate-limiting this application.")
        case s if 400 <= s < 500:
            return ProviderError(s, "The messaging provider rejected the request.")
        case _:
            return ProviderError(502, "The messaging provider is unavailable.", outcome_unknown=status >= 500)


def describe_raw_error(error: object) -> str:
    """Provider error code/message for logs. Never includes request data."""
    text = getattr(error, "text", None)
    if not callable(text):
        return type(error).__name__
    try:
        body = error.json()  # type: ignore[attr-defined]
    except ValueError:
        return "non-JSON error body"
    if isinstance(body, dict):
        return "code=%s message=%s" % (body.get("code"), mask_numbers(str(body.get("message"))))
    return "unrecognised error body"


# --------------------------------------------------------------------------
# Configuration and the client
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str
    from_number: str
    messaging_service_sid: str
    base_url: str | None
    timeout: float

    @classmethod
    def from_settings(cls) -> TwilioConfig:
        values = {
            name: getattr(settings, name, "") or ""
            for name in (
                "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN",
                "TWILIO_FROM_NUMBER",
                "TWILIO_MESSAGING_SERVICE_SID",
            )
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            # An absent credential would otherwise send every request unauthenticated.
            raise ImproperlyConfigured("Missing Twilio settings: %s" % ", ".join(missing))
        return cls(
            account_sid=values["TWILIO_ACCOUNT_SID"],
            auth_token=values["TWILIO_AUTH_TOKEN"],
            from_number=values["TWILIO_FROM_NUMBER"],
            messaging_service_sid=values["TWILIO_MESSAGING_SERVICE_SID"],
            base_url=getattr(settings, "TWILIO_BASE_URL", None) or None,
            timeout=float(getattr(settings, "TWILIO_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
        )

    def __repr__(self) -> str:
        return "TwilioConfig(account_sid=%r, base_url=%r)" % (self.account_sid, self.base_url)


_NUMBER_RE = re.compile(r"(?:%2B|\+)?\d{6,}")


def mask_numbers(text: str) -> str:
    """Replace anything that looks like a phone number (or a long id) with ***."""
    return _NUMBER_RE.sub("***", text)


class LoggingTransport:
    """
    Wraps the SDK transport to log method, host, path and status.

    Query strings are dropped and digit runs masked: both can carry a
    shopper's phone number (``To=``, ``/PhoneNumbers/{n}``). Headers and
    bodies are never logged - the auth header carries the credential.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        parts = urlsplit(request.url)
        where = "%s%s" % (parts.netloc, mask_numbers(parts.path))
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning("twilio %s %s -> %s", request.method, where, type(exc).__name__)
            raise
        logger.info(
            "twilio %s %s -> %s (%.0f ms)",
            request.method,
            where,
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


_client: TwilioSdkClient | None = None
_client_config: TwilioConfig | None = None
_client_lock = threading.Lock()


def build_client(config: TwilioConfig, transport: HttpClient | None = None) -> TwilioSdkClient:
    server_config: ServerConfigDict | None = None
    if config.base_url:
        # TWILIO_BASE_URL governs the messaging API only: server "default"
        # (api.twilio.com), which serves every Messages operation this app
        # uses. Lookups live on "default4" and keep the provider's default.
        server_config = {"default": {"base_url": config.base_url}}
    return TwilioSdkClient(
        server_config=server_config,
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=config.timeout)),
        account_sid_auth_token=BasicAuthCredentials(username=config.account_sid, password=config.auth_token),
    )


def get_client() -> tuple[TwilioSdkClient, TwilioConfig]:
    """
    The process-wide client, built on first use (so after any worker fork)
    and reused - it owns a connection pool.
    """
    global _client, _client_config
    if _client is None:
        with _client_lock:
            if _client is None:
                config = TwilioConfig.from_settings()
                _client = build_client(config)
                _client_config = config
    assert _client_config is not None
    return _client, _client_config


def set_client_for_tests(client: TwilioSdkClient | None, config: TwilioConfig | None) -> None:
    global _client, _client_config
    with _client_lock:
        _client, _client_config = client, config


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


# --------------------------------------------------------------------------
# Provider records
# --------------------------------------------------------------------------


def _value(v: Any) -> Any:
    """UNSET -> None, so nothing outside this module ever sees the sentinel."""
    return None if isinstance(v, UnsetType) else v


def parse_provider_time(value: str | None) -> datetime | None:
    """Twilio message timestamps are RFC 2822, in GMT."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class MessageRecord:
    sid: str | None
    status: MessageEnumStatus | str | None
    body: str | None
    body_present: bool
    error_code: int | None
    date_sent: datetime | None
    date_created: datetime | None

    @property
    def status_text(self) -> str:
        if self.status is None:
            return ""
        return self.status.value if isinstance(self.status, MessageEnumStatus) else str(self.status)

    @classmethod
    def from_sdk(cls, message: ApiV2010AccountMessage) -> MessageRecord:
        return cls(
            sid=_value(message.sid),
            status=_value(message.status),
            body=_value(message.body),
            body_present=not isinstance(message.body, UnsetType),
            error_code=_value(message.error_code),
            date_sent=parse_provider_time(_value(message.date_sent)),
            date_created=parse_provider_time(_value(message.date_created)),
        )


# --------------------------------------------------------------------------
# Status -> outcome. Every member is listed by name; the default is unknown.
# --------------------------------------------------------------------------


def status_from_provider(status: object) -> str:
    """Outcome of a send, from the message's own status."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return "done"
        case (
            MessageEnumStatus.ACCEPTED
            | MessageEnumStatus.SCHEDULED
            | MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.PARTIALLY_DELIVERED
        ):
            # Accepted, not (yet) confirmed delivered. "sent" is the carrier
            # accepting it, not the handset receiving it.
            return "pending"
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return "failed"
        case MessageEnumStatus.CANCELED:
            # It was queued and then called off: not in effect.
            return "failed"
        case MessageEnumStatus.RECEIVING | MessageEnumStatus.RECEIVED:
            return "unknown"  # inbound states: never ours
        case _:
            return "unknown"


def cancel_outcome(status: object) -> str:
    """Outcome of calling off a scheduled message: canceled is its done."""
    match status:
        case MessageEnumStatus.CANCELED:
            return "done"
        case (
            MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.DELIVERED
            | MessageEnumStatus.READ
            | MessageEnumStatus.PARTIALLY_DELIVERED
            | MessageEnumStatus.FAILED
            | MessageEnumStatus.UNDELIVERED
        ):
            return "failed"  # too late: it already left the schedule
        case _:
            # scheduled / accepted (the call-off has not taken yet), or a
            # value we do not list: the next request checks again.
            return "unknown"


GONE = "__gone__"  # the provider no longer has the record


def redact_outcome(record: MessageRecord | str) -> str:
    """Outcome of disposing of a message's text at the provider."""
    if record == GONE:
        return "done"
    assert isinstance(record, MessageRecord)
    if not record.body_present:
        return "unknown"
    return "done" if not record.body else "failed"


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LookupResult:
    valid: bool
    phone_number: str | None
    country_code: str | None
    validation_errors: tuple[str, ...]


def lookup_number(raw: str) -> LookupResult:
    """Ask the provider whether ``raw`` is a usable number, and its canonical form."""
    client, _ = get_client()
    try:
        response: LookupResponse = client.lookups_v2_phone_number.fetch_phone_number3(raw)
    except ApiError as e:
        if e.status_code in (400, 404):
            # The provider could not make a number of it.
            return LookupResult(False, None, None, ("NOT_A_NUMBER",))
        logger.warning("twilio lookup refused: HTTP %s %s", e.status_code, describe_raw_error(e.error))
        raise provider_error(e.status_code, e.error) from e
    except ValueError as e:  # pydantic.ValidationError or non-JSON body
        raise ProviderError(502, "Unreadable response from the messaging provider.") from e
    except NEVER_SENT as e:
        raise ProviderError(502, "Could not reach the messaging provider.") from e
    except httpx.RequestError as e:
        raise ProviderError(504, "The messaging provider did not answer in time.") from e

    errors = _value(response.validation_errors) or []
    return LookupResult(
        valid=_value(response.valid) is True,
        phone_number=_value(response.phone_number),
        country_code=_value(response.country_code),
        validation_errors=tuple(e.value if hasattr(e, "value") else str(e) for e in errors),
    )


def create_message(to: str, body: str, *, send_at: datetime | None = None) -> MessageRecord:
    """
    One create call. Raises ApiError / httpx / ValueError untranslated: the
    safe write decides what each means for a write.
    """
    client, config = get_client()
    if send_at is None:
        message = client.api20100401_message.create_message(
            config.account_sid, to, from_=config.from_number, body=body
        )
    else:
        # Scheduling is a Messaging Service feature; From pins the sender to
        # this app's own number so reconciliation (From=...) sees it.
        message = client.api20100401_message.create_message(
            config.account_sid,
            to,
            from_=config.from_number,
            messaging_service_sid=config.messaging_service_sid,
            body=body,
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=send_at,
        )
    return MessageRecord.from_sdk(message)


def fetch_message(sid: str) -> MessageRecord | str:
    """The provider's record for ``sid``; ``GONE`` if it no longer has one."""
    client, config = get_client()
    try:
        return MessageRecord.from_sdk(client.api20100401_message.fetch_message(config.account_sid, sid))
    except ApiError as e:
        if e.status_code == 404:
            return GONE
        raise


def cancel_message(sid: str) -> MessageRecord | str:
    client, config = get_client()
    try:
        return MessageRecord.from_sdk(
            client.api20100401_message.update_message(
                config.account_sid, sid, status=MessageEnumUpdateStatus.CANCELED
            )
        )
    except ApiError as e:
        if e.status_code == 404:
            return GONE
        raise


def redact_message(sid: str) -> MessageRecord | str:
    client, config = get_client()
    try:
        # An empty body is the provider's documented way to redact the text.
        return MessageRecord.from_sdk(client.api20100401_message.update_message(config.account_sid, sid, body=""))
    except ApiError as e:
        if e.status_code == 404:
            return GONE
        raise


LOOKUP_PAGES = 3


def find_message_by_tag(to: str, tag: str) -> MessageRecord | None:
    """
    The message this app sent to ``to`` whose body carries ``tag``, or None.

    The provider has no field for a client reference, so the reference
    travels in the body. Most recent messages come first; a few pages cover
    any write whose answer we lost.
    """
    client, config = get_client()
    page_token: str | None = None
    page: int | None = None
    for _ in range(LOOKUP_PAGES):
        response = client.api20100401_message.list_message(
            config.account_sid,
            to=to,
            from_=config.from_number,
            page_size=100,
            page=page,
            page_token=page_token,
        )
        for message in _value(response.messages) or []:
            body = _value(message.body)
            if body and tag in body:
                return MessageRecord.from_sdk(message)
        next_page = next_page_params(_value(response.next_page_uri))
        if next_page is None:
            return None
        page, page_token = next_page
    return None


def next_page_params(next_page_uri: str | None) -> tuple[int | None, str | None] | None:
    if not next_page_uri:
        return None
    query = parse_qs(urlsplit(next_page_uri).query)
    token = query.get("PageToken", [None])[0]
    page_raw = query.get("Page", [None])[0]
    if token is None:
        return None
    return (int(page_raw) if page_raw and page_raw.isdigit() else None), token


MAX_RECONCILIATION_PAGES = 50


def list_messages_sent_from_us(start_day: datetime, end_day: datetime) -> list[MessageRecord]:
    """
    Every message the provider sent from this app's own number with a sent
    date between ``start_day`` (inclusive) and ``end_day`` (exclusive),
    whole-day granularity. The provider filters on From - the account
    carries traffic that is not this application's.
    """
    client, config = get_client()
    records: list[MessageRecord] = []
    page_token: str | None = None
    page: int | None = None
    for _ in range(MAX_RECONCILIATION_PAGES):
        response = client.api20100401_message.list_message(
            config.account_sid,
            from_=config.from_number,
            date_sent_query_query=start_day,  # wire: DateSent>
            date_sent_query=end_day,  # wire: DateSent<
            page_size=1000,
            page=page,
            page_token=page_token,
        )
        records.extend(MessageRecord.from_sdk(m) for m in _value(response.messages) or [])
        next_page = next_page_params(_value(response.next_page_uri))
        if next_page is None:
            return records
        page, page_token = next_page
    raise ProviderError(502, "Reconciliation range too large: narrow the date range.")
