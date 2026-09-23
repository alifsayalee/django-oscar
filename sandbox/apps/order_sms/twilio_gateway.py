"""
The one place this app talks to Twilio.

Everything outside this module deals in :class:`MessageSnapshot` and the
:class:`ProviderError` family; no SDK type, SDK exception or httpx exception
leaks past it. Phone numbers are never logged: the transport logs method, host
and a masked path only, and provider error text is masked before it is logged
or stored.
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
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from pydantic import ValidationError
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import (
    ApiError,
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    RawError,
    RequestOptionsDict,
    UnsetType,
)
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import (
    MessageEnumDirection,
    MessageEnumScheduleType,
    MessageEnumStatus,
    MessageEnumUpdateStatus,
)

logger = logging.getLogger("apps.order_sms.twilio")

T = TypeVar("T")

# Seconds. Every call here sits on a user-facing request path.
DEFAULT_TIMEOUT = 10.0
# Bounds for the reconciliation page walk (page size is the provider's maximum).
LIST_PAGE_SIZE = 1000
LIST_MAX_PAGES = 50

# Outcomes a provider status maps onto. Local-only states live in models.py.
DELIVERED = "delivered"
PENDING = "pending"
SCHEDULED = "scheduled"
FAILED = "failed"
CANCELED = "canceled"
UNKNOWN = "unknown"

_NUMBERISH = re.compile(r"(?:%2B|\+)?\d{6,}")


def mask(text: str) -> str:
    """Blank out anything that looks like a phone number."""
    return _NUMBERISH.sub("***", text)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """
    A Twilio call did not produce a usable answer.

    ``http_status`` is what our own API should answer with; ``outcome_unknown``
    says whether a write may nevertheless have taken effect at the provider.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        outcome_unknown: bool,
        provider_status: int | None = None,
        provider_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.provider_code = provider_code


class TwilioNotConfigured(ProviderError):
    """Credentials or sending number missing from settings: nothing was sent."""


class ProviderConfigError(ProviderError):
    """Twilio refused *our* credentials (401/403): nothing was done."""


class ProviderRejected(ProviderError):
    """Twilio refused the request itself (400/404/409/422): nothing was done."""


class ProviderUnavailable(ProviderError):
    """Rate limited, or a transport failure."""


class ProviderFailure(ProviderError):
    """A 5xx or an unmapped status."""


class ProviderUnreadable(ProviderError):
    """A response arrived but could not be read."""


def _provider_code(error: object) -> int | None:
    if not isinstance(error, RawError):
        return None
    try:
        payload = error.json()
    except ValueError:
        return None
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, int) else None


def _translate(exc: Exception, *, write: bool) -> ProviderError:
    if isinstance(exc, ApiError):
        status = exc.status_code
        code = _provider_code(exc.error)
        detail = dict(provider_status=status, provider_code=code)
        if status in (401, 403):
            return ProviderConfigError(
                "Twilio refused our credentials.", http_status=502, outcome_unknown=False, **detail
            )
        if status == 429:
            return ProviderUnavailable(
                "Twilio rate limit reached.", http_status=503, outcome_unknown=False, **detail
            )
        if status in (400, 404, 409, 422):
            return ProviderRejected(
                "Twilio rejected the request.", http_status=status, outcome_unknown=False, **detail
            )
        return ProviderFailure(
            "Twilio failed to process the request.",
            http_status=502,
            outcome_unknown=write and status >= 500,
            **detail,
        )
    if isinstance(exc, (ValidationError, ValueError)):
        return ProviderUnreadable(
            "Twilio's response could not be read.", http_status=502, outcome_unknown=write
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)):
        return ProviderUnavailable("Twilio could not be reached.", http_status=502, outcome_unknown=False)
    if isinstance(exc, httpx.RequestError):
        return ProviderUnavailable("Twilio did not answer in time.", http_status=504, outcome_unknown=write)
    raise exc


def _call(fn: Callable[[], T], *, write: bool, what: str) -> T:
    try:
        return fn()
    except (ApiError, ValidationError, ValueError, httpx.RequestError) as exc:
        error = _translate(exc, write=write)
        logger.warning(
            "twilio %s failed: %s (provider status=%s code=%s, outcome_unknown=%s)",
            what,
            type(error).__name__,
            error.provider_status,
            error.provider_code,
            error.outcome_unknown,
        )
        raise error from exc


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LoggingTransport:
    """Logs method, host, masked path and status. Never the query, headers or body."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        parts = urlsplit(request.url)
        where = f"{parts.netloc}{mask(parts.path)}"
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except httpx.RequestError as exc:
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
_client_lock = threading.Lock()


def _setting(name: str) -> str:
    value = getattr(settings, name, "")
    return value if isinstance(value, str) else ""


def build_client(http_client: HttpClient | None = None) -> TwilioSdkClient:
    missing = [
        name
        for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
        if not _setting(name)
    ]
    if missing:
        raise TwilioNotConfigured(
            "Twilio is not configured: missing " + ", ".join(missing),
            http_status=503,
            outcome_unknown=False,
        )
    base_url = _setting("TWILIO_BASE_URL")
    return TwilioSdkClient(
        # TWILIO_BASE_URL governs the messaging API (server "default") only.
        server_config={"default": {"base_url": base_url}} if base_url else None,
        timeout=DEFAULT_TIMEOUT,
        custom_http_client=http_client or LoggingTransport(HttpxClient(timeout=DEFAULT_TIMEOUT)),
        account_sid_auth_token=BasicAuthCredentials(
            username=_setting("TWILIO_ACCOUNT_SID"), password=_setting("TWILIO_AUTH_TOKEN")
        ),
    )


def get_client() -> TwilioSdkClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: TwilioSdkClient | None) -> None:
    """Replace the shared client (tests inject one built on a stub transport)."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    if previous is not None and previous is not client:
        previous.close()


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def outcome_for(status: str) -> str:
    """The one place a Twilio message status becomes ours. Unlisted values are unknown."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DELIVERED
        case (
            MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.ACCEPTED
        ):
            return PENDING
        case MessageEnumStatus.SCHEDULED:
            return SCHEDULED
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return FAILED
        case MessageEnumStatus.CANCELED:
            return CANCELED
        case _:
            return UNKNOWN


def _opt(value: Any) -> Any:
    return None if isinstance(value, UnsetType) else value


def _wire(value: Any) -> str | None:
    value = _opt(value)
    if value is None:
        return None
    return str(value.value) if hasattr(value, "value") else str(value)


def parse_provider_time(value: Any) -> datetime | None:
    value = _opt(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class MessageSnapshot:
    sid: str
    status: str
    outcome: str
    error_code: int | None
    error_message: str | None
    date_created: datetime | None
    date_sent: datetime | None
    direction: str | None
    from_number: str | None
    to_number: str | None
    body: str | None

    @property
    def provider_time(self) -> datetime | None:
        """When the provider acted on it: sent, else created."""
        return self.date_sent or self.date_created

    @property
    def is_outbound(self) -> bool:
        return self.direction in (
            MessageEnumDirection.OUTBOUND_API.value,
            MessageEnumDirection.OUTBOUND_CALL.value,
            MessageEnumDirection.OUTBOUND_REPLY.value,
        )


def _snapshot(message: ApiV2010AccountMessage) -> MessageSnapshot:
    sid = _opt(message.sid)
    if not isinstance(sid, str) or not sid:
        raise ProviderUnreadable(
            "Twilio returned a message without a SID.", http_status=502, outcome_unknown=True
        )
    status = _wire(message.status) or ""
    error_message = _opt(message.error_message)
    error_code = _opt(message.error_code)
    return MessageSnapshot(
        sid=sid,
        status=status,
        outcome=outcome_for(status),
        error_code=error_code if isinstance(error_code, int) else None,
        error_message=mask(error_message) if isinstance(error_message, str) else None,
        date_created=parse_provider_time(message.date_created),
        date_sent=parse_provider_time(message.date_sent),
        direction=_wire(message.direction),
        from_number=_opt(message.from_),
        to_number=_opt(message.to),
        body=_opt(message.body),
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _account_sid() -> str:
    return _setting("TWILIO_ACCOUNT_SID")


def lookup_number(number: str, country_code: str | None = None) -> tuple[str, str | None]:
    """Return (canonical E.164 number, ISO country) or raise ProviderRejected (404) when unusable."""
    client = get_client()
    result = _call(
        lambda: client.lookups_v1_phone_number_api.fetch_phone_number2(number, country_code=country_code),
        write=False,
        what="lookup",
    )
    canonical = _opt(result.phone_number)
    if not isinstance(canonical, str) or not canonical:
        raise ProviderUnreadable("Twilio returned no canonical number.", http_status=502, outcome_unknown=False)
    country = _opt(result.country_code)
    return canonical, country if isinstance(country, str) else None


def send_message(
    to: str, body: str, *, idempotency_key: str, send_at: datetime | None = None
) -> MessageSnapshot:
    """
    Send now, or - with ``send_at`` - queue it at Twilio for later (Messaging
    Service scheduling). Always from TWILIO_FROM_NUMBER, so reconciliation by
    that number sees every message this app sends.
    """
    client = get_client()
    options: RequestOptionsDict = {"extra_headers": {"idempotency-key": idempotency_key}}
    if send_at is None:
        call = lambda: client.api20100401_message.create_message(  # noqa: E731
            _account_sid(), to, from_=_setting("TWILIO_FROM_NUMBER"), body=body, request_options=options
        )
    else:
        service_sid = _setting("TWILIO_MESSAGING_SERVICE_SID")
        if not service_sid:
            raise TwilioNotConfigured(
                "Scheduling needs TWILIO_MESSAGING_SERVICE_SID.", http_status=503, outcome_unknown=False
            )
        call = lambda: client.api20100401_message.create_message(  # noqa: E731
            _account_sid(),
            to,
            from_=_setting("TWILIO_FROM_NUMBER"),
            messaging_service_sid=service_sid,
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=send_at,
            body=body,
            request_options=options,
        )
    return _snapshot(_call(call, write=True, what="send"))


def fetch_message(sid: str) -> MessageSnapshot:
    client = get_client()
    return _snapshot(
        _call(lambda: client.api20100401_message.fetch_message(_account_sid(), sid), write=False, what="fetch")
    )


def cancel_message(sid: str) -> MessageSnapshot:
    client = get_client()
    return _snapshot(
        _call(
            lambda: client.api20100401_message.update_message(
                _account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED
            ),
            write=True,
            what="cancel",
        )
    )


def redact_message(sid: str) -> MessageSnapshot:
    """Blank the message text at Twilio; the record and its status remain."""
    client = get_client()
    return _snapshot(
        _call(
            lambda: client.api20100401_message.update_message(_account_sid(), sid, body=""),
            write=True,
            what="redact",
        )
    )


@dataclass(frozen=True)
class MessageListing:
    messages: list[MessageSnapshot]
    truncated: bool
    pages: int


def _next_page(next_page_uri: Any) -> tuple[int | None, str | None]:
    uri = _opt(next_page_uri)
    if not isinstance(uri, str) or not uri:
        return None, None
    query = parse_qs(urlsplit(uri).query)
    tokens = query.get("PageToken") or []
    pages = query.get("Page") or []
    page = pages[0] if pages else ""
    return (int(page) if page.isdigit() else None), (tokens[0] if tokens else None)


def list_messages(
    *,
    sent_after: datetime,
    sent_before: datetime,
    to: str | None = None,
    page_size: int = LIST_PAGE_SIZE,
    max_pages: int = LIST_MAX_PAGES,
) -> MessageListing:
    """
    Messages from TWILIO_FROM_NUMBER (asked of Twilio, not filtered here) with
    DateSent in the given bounds. Bounded: ``truncated`` says the cap, not the
    provider, ended the walk.
    """
    client = get_client()
    from_number = _setting("TWILIO_FROM_NUMBER")
    messages: list[MessageSnapshot] = []
    page: int | None = None
    token: str | None = None
    for pages in range(1, max_pages + 1):
        result = _call(
            lambda: client.api20100401_message.list_message(
                _account_sid(),
                from_=from_number,
                to=to,
                date_sent_query_query=sent_after,
                date_sent_query=sent_before,
                page_size=page_size,
                page=page,
                page_token=token,
            ),
            write=False,
            what="list",
        )
        messages.extend(_snapshot(m) for m in (_opt(result.messages) or []))
        next_page, next_token = _next_page(result.next_page_uri)
        if next_token is None:
            return MessageListing(messages=messages, truncated=False, pages=pages)
        if next_token == token:
            logger.error("twilio list paging made no progress; stopping")
            return MessageListing(messages=messages, truncated=True, pages=pages)
        page, token = next_page, next_token
    logger.warning("twilio list stopped at the %s-page cap", max_pages)
    return MessageListing(messages=messages, truncated=True, pages=max_pages)
