"""
The one module that talks to Twilio.

Everything the rest of the app knows about the provider goes through here: the
client and its lifetime, the calls themselves, the mapping of provider message
statuses onto our own outcomes, and the translation of SDK failures into the
app's error types. It deliberately imports nothing from Django so it can be
type-checked strictly and tested against a stub transport.

Phone numbers are personal data: nothing in this module logs one. The logging
transport masks digit runs in request paths (Lookups carries the number in the
path) and never logs query strings, headers or bodies.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import (
    ApiError,
    BasicAuthCredentials,
    RequestOptions,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    RawError,
    UnsetType,
)
from twilio_sdk.models import ApiV2010AccountMessage, LookupsV1PhoneNumber
from twilio_sdk.models.enums import (
    MessageEnumDirection,
    MessageEnumScheduleType,
    MessageEnumStatus,
    MessageEnumUpdateStatus,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Base URLs from the SDK map's "Servers & auth" table, passed explicitly so a
#: deployment never relies on whatever the spec happened to list first.
DEFAULT_MESSAGING_BASE_URL = "https://api.twilio.com"
LOOKUPS_BASE_URL = "https://lookups.twilio.com"

#: Transport failures that happen before the request leaves: nothing can have landed.
NEVER_SENT: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

# Outcomes, shared with the service layer.
DONE = "done"
PENDING = "pending"
FAILED = "failed"
UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """A provider failure, already translated into what our API answers."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool,
        provider_status: int | None = None,
        provider_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.provider_code = provider_code


class ProviderRejected(ProviderError):
    def __init__(self, status: int, message: str, provider_status: int, provider_code: int | None) -> None:
        super().__init__(status, message, outcome_unknown=False, provider_status=provider_status,
                         provider_code=provider_code)


class TwilioNotConfigured(ProviderError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(
            503, "SMS provider is not configured (missing: %s)." % ", ".join(missing), outcome_unknown=False
        )


class NumberNotUsable(Exception):
    """The provider does not recognise the number as a valid destination."""


def provider_error_details(error: ApiError[RawError]) -> tuple[int | None, str]:
    """Twilio's own error code and message from a RawError body, if it is readable JSON."""
    try:
        data = error.error.json()
    except ValueError:
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    code = data.get("code")
    message = data.get("message")
    return (code if isinstance(code, int) else None), (message if isinstance(message, str) else "")


def translate(exc: BaseException, *, is_write: bool) -> ProviderError:
    """The error ladder: one mapping from SDK failure kind to our answer, used at every call site."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        code, message = provider_error_details(exc)
        status = exc.status_code
        if status in (400, 404, 409, 422):
            return ProviderRejected(status, message or "The SMS provider rejected the request.", status, code)
        if status in (401, 403):
            return ProviderError(502, "SMS provider refused our credentials.", outcome_unknown=False,
                                 provider_status=status, provider_code=code)
        if status == 429:
            return ProviderError(503, "SMS provider is rate limiting us; try again later.", outcome_unknown=False,
                                 provider_status=status, provider_code=code)
        return ProviderError(502, "SMS provider error.", outcome_unknown=is_write and status >= 500,
                             provider_status=status, provider_code=code)
    if isinstance(exc, NEVER_SENT):
        return ProviderError(502, "SMS provider unreachable; nothing was sent.", outcome_unknown=False)
    if isinstance(exc, httpx.RequestError):
        return ProviderError(504, "No response from the SMS provider.", outcome_unknown=is_write)
    if isinstance(exc, ValueError):  # pydantic ValidationError, non-JSON body, missing required member
        return ProviderError(502, "Unreadable response from the SMS provider.", outcome_unknown=is_write)
    raise exc


# --------------------------------------------------------------------------
# Configuration and client
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayConfig:
    account_sid: str
    auth_token: str = field(repr=False)
    from_number: str
    messaging_service_sid: str
    base_url: str | None = None
    timeout: float = 10.0

    def missing(self, *, need_service: bool = False) -> list[str]:
        required = {
            "TWILIO_ACCOUNT_SID": self.account_sid,
            "TWILIO_AUTH_TOKEN": self.auth_token,
            "TWILIO_FROM_NUMBER": self.from_number,
        }
        if need_service:
            required["TWILIO_MESSAGING_SERVICE_SID"] = self.messaging_service_sid
        return [name for name, value in required.items() if not value]


_DIGIT_RUNS = re.compile(r"(?:\+|%2B)\d+|\d{7,}", re.IGNORECASE)


class RedactingLogTransport:
    """Logs method, host, masked path, status and latency — never query, headers or body."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        parts = urlsplit(request.url)
        path = _DIGIT_RUNS.sub("<redacted>", parts.path)
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning("twilio %s %s%s -> %s", request.method, parts.netloc, path, type(exc).__name__)
            raise
        logger.info(
            "twilio %s %s%s -> %s (%.0f ms)",
            request.method, parts.netloc, path, response.status_code, (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(config: GatewayConfig, transport: HttpClient | None = None) -> TwilioSdkClient:
    """Build the long-lived client. Refuses to build one without credentials (the SDK would send unauthenticated)."""
    missing = [m for m in config.missing() if m != "TWILIO_FROM_NUMBER"]
    if missing:
        raise TwilioNotConfigured(missing)
    return TwilioSdkClient(
        server_config={
            "default": {"base_url": config.base_url or DEFAULT_MESSAGING_BASE_URL},
            "default4": {"base_url": LOOKUPS_BASE_URL},
        },
        custom_http_client=transport or RedactingLogTransport(HttpxClient(timeout=config.timeout)),
        account_sid_auth_token=BasicAuthCredentials(username=config.account_sid, password=config.auth_token),
    )


# --------------------------------------------------------------------------
# Status mapping
# --------------------------------------------------------------------------


def status_from_provider(status: object) -> str:
    """The ONE place a provider message status becomes one of our outcomes, for a send."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DONE
        case (
            MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.ACCEPTED
            | MessageEnumStatus.SCHEDULED
            | MessageEnumStatus.PARTIALLY_DELIVERED
        ):
            return PENDING
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return FAILED
        case MessageEnumStatus.CANCELED:  # called off: not in effect
            return FAILED
        case MessageEnumStatus.RECEIVING | MessageEnumStatus.RECEIVED:
            return UNKNOWN  # inbound values are never expected for an outbound send
        case _:
            return UNKNOWN


def cancel_outcome_from_provider(status: object) -> str:
    """Outcome of a request to call off a scheduled message, read from the message's status."""
    match status:
        case MessageEnumStatus.CANCELED:
            return DONE
        case MessageEnumStatus.SCHEDULED | MessageEnumStatus.ACCEPTED:
            return PENDING  # still queued with the provider: the cancel has not taken effect yet
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
            return FAILED  # it already left the schedule: too late to call off
        case _:
            return UNKNOWN


# --------------------------------------------------------------------------
# Our view of a provider message (UNSET resolved before it leaves this module)
# --------------------------------------------------------------------------


def _opt(value: str | None | UnsetType) -> str | None:
    return None if isinstance(value, UnsetType) else value


def parse_provider_time(value: str | None | UnsetType) -> datetime | None:
    """Twilio's Message timestamps are RFC 1123 text, e.g. 'Thu, 24 Sep 2026 04:06:44 +0000'."""
    text = _opt(value)
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def is_outbound(direction: object) -> bool:
    """Only outbound records are sends. An inbound copy of one of our messages (a destination that
    routes back into this account) carries the same To/From/body and must never settle a send."""
    match direction:
        case MessageEnumDirection.OUTBOUND_API | MessageEnumDirection.OUTBOUND_CALL | MessageEnumDirection.OUTBOUND_REPLY:
            return True
        case _:
            return False


@dataclass(frozen=True)
class MessageView:
    sid: str
    outbound: bool
    status: str | None
    outcome: str
    error_code: int | None
    error_message: str | None
    body: str | None
    date_created: datetime | None
    date_sent: datetime | None

    @property
    def provider_time(self) -> datetime | None:
        """The provider's event time: when it was sent, or when it was created if it never was."""
        return self.date_sent or self.date_created


def to_view(message: ApiV2010AccountMessage) -> MessageView:
    """Map a decoded message; a message without a SID is unreadable (outcome unknown)."""
    sid = _opt(message.sid)
    if not sid:
        raise ValueError("provider message carried no sid")
    status = None if isinstance(message.status, UnsetType) else str(message.status)
    error_code = None if isinstance(message.error_code, UnsetType) else message.error_code
    return MessageView(
        sid=sid,
        outbound=is_outbound(message.direction),
        status=status,
        outcome=status_from_provider(message.status),
        error_code=error_code,
        error_message=_opt(message.error_message),
        body=_opt(message.body),
        date_created=parse_provider_time(message.date_created),
        date_sent=parse_provider_time(message.date_sent),
    )


# --------------------------------------------------------------------------
# Calls
# --------------------------------------------------------------------------


def _read(call: Callable[[], T]) -> T:
    """Reads are safe to repeat: one retry on a transient failure. Writes never come through here."""
    try:
        return call()
    except ApiError as exc:
        if exc.status_code not in (429, 500, 502, 503, 504):
            raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ReadTimeout):
        pass
    time.sleep(0.5)
    return call()


class Gateway:
    def __init__(self, client: TwilioSdkClient, config: GatewayConfig) -> None:
        self.client = client
        self.config = config

    def lookup_number(self, raw_number: str, country_code: str | None = None) -> tuple[str, str | None]:
        """Canonical E.164 form and ISO country; NumberNotUsable when the provider does not know the number."""
        try:
            result: LookupsV1PhoneNumber = _read(
                lambda: self.client.lookups_v1_phone_number_api.fetch_phone_number2(
                    raw_number, country_code=country_code
                )
            )
        except ApiError as exc:
            if exc.status_code in (400, 404):
                raise NumberNotUsable() from exc
            raise
        canonical = _opt(result.phone_number)
        if not canonical:
            raise ValueError("lookup returned no phone_number")
        return canonical, _opt(result.country_code)

    def create_message(
        self, to: str, body: str, idempotency_token: str, send_at: datetime | None = None
    ) -> MessageView:
        """One provider write. Raises the SDK's/httpx's own exceptions; the caller's safe write classifies them."""
        missing = self.config.missing(need_service=send_at is not None)
        if missing:
            raise TwilioNotConfigured(missing)
        options = RequestOptions(extra_headers={"Idempotency-Key": idempotency_token})
        if send_at is None:
            message = self.client.api20100401_message.create_message(
                self.config.account_sid, to, from_=self.config.from_number, body=body, request_options=options
            )
        else:
            message = self.client.api20100401_message.create_message(
                self.config.account_sid,
                to,
                from_=self.config.from_number,
                messaging_service_sid=self.config.messaging_service_sid,
                schedule_type=MessageEnumScheduleType.FIXED,
                send_at=send_at,
                body=body,
                request_options=options,
            )
        return to_view(message)

    def fetch_message(self, sid: str) -> MessageView:
        return to_view(_read(lambda: self.client.api20100401_message.fetch_message(self.config.account_sid, sid)))

    def cancel_message(self, sid: str) -> MessageView:
        return to_view(
            self.client.api20100401_message.update_message(
                self.config.account_sid, sid, status=MessageEnumUpdateStatus.CANCELED
            )
        )

    def redact_message(self, sid: str) -> MessageView:
        return to_view(self.client.api20100401_message.update_message(self.config.account_sid, sid, body=""))

    def find_by_token(
        self, to: str, token: str, not_before: datetime, max_pages: int = 4
    ) -> MessageView | None:
        """Lookup by the reference we sent: newest-first messages To+From, matched on 'Ref <token>' in the body."""
        needle = "Ref %s" % token
        for page in self._pages(to=to, max_pages=max_pages, page_size=50):
            for message in page:
                if is_outbound(message.direction) and needle in (_opt(message.body) or ""):
                    return to_view(message)
                created = parse_provider_time(message.date_created)
                if created is not None and created < not_before:
                    return None
        return None

    def list_sent_between(self, start: datetime, end: datetime, max_pages: int) -> list[MessageView]:
        """Every message sent From our configured number with DateSent in [start, end] (provider-side filter)."""
        views: list[MessageView] = []
        for page in self._pages(
            from_date=start, to_date=end, max_pages=max_pages, page_size=1000, raise_on_cap=True
        ):
            views.extend(to_view(m) for m in page)
        return views

    def _pages(
        self,
        *,
        max_pages: int,
        page_size: int,
        to: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        raise_on_cap: bool = False,
    ) -> Iterator[list[ApiV2010AccountMessage]]:
        page_token: str | None = None
        page_index: int | None = None
        for _ in range(max_pages):
            token, index = page_token, page_index
            response = _read(
                lambda: self.client.api20100401_message.list_message(
                    self.config.account_sid,
                    to=to,
                    from_=self.config.from_number,
                    date_sent_query_query=from_date,
                    date_sent_query=to_date,
                    page_size=page_size,
                    page=index,
                    page_token=token,
                )
            )
            messages = response.messages
            yield [] if isinstance(messages, UnsetType) else messages
            next_uri = _opt(response.next_page_uri)
            if not next_uri:
                return
            query = parse_qs(urlsplit(next_uri).query)
            page_token = query.get("PageToken", [""])[0] or None
            page_index = int(query.get("Page", ["0"])[0])
            if not page_token:
                return
        if raise_on_cap:
            raise PageCapExceeded()


class PageCapExceeded(Exception):
    """The provider has more pages than we are willing to walk for one report."""


__all__ = [
    "DONE", "PENDING", "FAILED", "UNKNOWN", "NEVER_SENT",
    "Gateway", "GatewayConfig", "MessageView", "NumberNotUsable", "PageCapExceeded",
    "ProviderError", "ProviderRejected", "TwilioNotConfigured",
    "build_client", "cancel_outcome_from_provider", "provider_error_details",
    "status_from_provider", "to_view", "translate",
]
