"""
The one place this app talks to Twilio.

Everything the rest of the app needs from the provider goes through the
functions at the bottom of this module. They return plain dataclasses (never
SDK models, whose optional members may hold the SDK's ``UNSET`` sentinel) and
raise only the ``ProviderError`` family defined here, so callers deal with one
failure type that says whether anything may have happened upstream.

The SDK performs no retries; nothing here retries a write either. Reads are
not retried automatically — callers decide.
"""

import atexit
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from pydantic import ValidationError
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import (
    ApiError,
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    RawError,
    UnsetType,
)
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumUpdateStatus

logger = logging.getLogger("order_notifications.twilio")

T = TypeVar("T")

#: Seconds before any single Twilio request is abandoned. This sits on the
#: request path of a shopper/operator API call, so it is far below the SDK's
#: 30s default.
REQUEST_TIMEOUT = 10.0

#: Upper bound on pages walked by one list; never depends on the provider.
MAX_PAGES = 50
PAGE_SIZE = 1000


# ---------------------------------------------------------------------------
# Failure types
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """A Twilio call did not produce a usable answer.

    ``status_code`` is what our own API should answer with; ``outcome_unknown``
    says whether the request may nevertheless have taken effect at Twilio.
    """

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown


class ProviderRejected(ProviderError):
    """Twilio answered with a 4xx that is about the request itself."""

    def __init__(self, provider_status: int, message: str) -> None:
        super().__init__(
            422 if provider_status in (400, 422) else provider_status,
            message,
            outcome_unknown=False,
        )
        self.provider_status = provider_status


class ProviderConfigError(ProviderError):
    """Our credentials/configuration were refused: never the caller's fault."""

    def __init__(self, message: str) -> None:
        super().__init__(502, message, outcome_unknown=False)


class ProviderUnavailable(ProviderError):
    pass


class ProviderUnreadable(ProviderError):
    """Twilio answered but the answer could not be read."""

    def __init__(self, message: str, *, outcome_unknown: bool = True) -> None:
        super().__init__(502, message, outcome_unknown=outcome_unknown)


class NumberNotUsable(Exception):
    """The provider does not consider the number a usable destination."""


# Failures raised before the request left this process: nothing happened.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

_CALLER_FAULT = (400, 404, 409, 422)


def _raw_detail(error: object) -> str:
    """A short, loggable description of a Twilio error body (no PII)."""
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return "HTTP %s" % error.status_code
        if isinstance(body, dict):
            # Twilio's messages can quote the number; mask before it goes anywhere.
            return _mask_url("Twilio error %s: %s" % (body.get("code"), body.get("message")))
    return "HTTP error"


def _call(operation: str, fn: Callable[[], T]) -> T:
    """Run one SDK call and translate every failure kind into ProviderError."""
    try:
        return fn()
    except ApiError as e:
        detail = _raw_detail(e.error)
        logger.warning("twilio %s failed: status=%s %s", operation, e.status_code, detail)
        if e.status_code in (401, 403):
            raise ProviderConfigError("Messaging provider refused our credentials.") from e
        if e.status_code == 429:
            raise ProviderUnavailable(
                503, "Messaging provider is rate-limiting us.", outcome_unknown=False
            ) from e
        if e.status_code in _CALLER_FAULT:
            raise ProviderRejected(e.status_code, detail) from e
        # 5xx (a write may still have landed) and every unmapped status.
        raise ProviderUnavailable(
            502, "Messaging provider error.", outcome_unknown=e.status_code >= 500
        ) from e
    except ValidationError as e:
        logger.error("twilio %s: unreadable response body", operation)
        raise ProviderUnreadable("Unreadable response from messaging provider.") from e
    except _NEVER_SENT as e:
        logger.warning("twilio %s: never sent (%s)", operation, type(e).__name__)
        raise ProviderUnavailable(
            502, "Messaging provider unreachable.", outcome_unknown=False
        ) from e
    except httpx.RequestError as e:
        logger.warning("twilio %s: no response (%s)", operation, type(e).__name__)
        raise ProviderUnavailable(
            504, "No response from messaging provider.", outcome_unknown=True
        ) from e
    except ValueError as e:
        # A non-JSON body where JSON was expected.
        logger.error("twilio %s: non-JSON response body", operation)
        raise ProviderUnreadable("Unreadable response from messaging provider.") from e


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"(%2B|\+)([\d ()%20-]{4,24}\d)")


def mask_number(number: str | None) -> str:
    """Render a phone number for logs/responses without disclosing it."""
    if not number:
        return ""
    return "%s***%s" % (number[:3], number[-2:])


_LOOKUP_PATH_RE = re.compile(r"(/PhoneNumbers/)[^/?]+")


def _mask_url(url: str) -> str:
    url = _LOOKUP_PATH_RE.sub(r"\1***", url)
    return _PHONE_RE.sub(lambda m: m.group(1) + "***" + m.group(2)[-2:], url)


class LoggingTransport:
    """Logs method, (masked) URL, status and latency — never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as e:
            logger.info(
                "twilio %s %s -> %s", request.method, _mask_url(request.url), type(e).__name__
            )
            raise
        logger.info(
            "twilio %s %s -> %s (%.0f ms)",
            request.method,
            _mask_url(request.url),
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


_client: TwilioSdkClient | None = None
_client_lock = threading.Lock()


def build_client(transport: HttpClient | None = None) -> TwilioSdkClient:
    """Build a client from Django settings; refuses to build one without credentials."""
    missing = [
        name
        for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
        if not getattr(settings, name, "")
    ]
    if missing:
        raise ProviderConfigError("Messaging is not configured (missing %s)." % ", ".join(missing))
    server_config: ServerConfigDict | None = None
    base_url = getattr(settings, "TWILIO_BASE_URL", "")
    if base_url:
        # Governs the messaging API (server "default") only; Lookup keeps its own host.
        server_config = {"default": {"base_url": base_url}}
    return TwilioSdkClient(
        server_config=server_config,
        account_sid_auth_token=BasicAuthCredentials(
            username=settings.TWILIO_ACCOUNT_SID, password=settings.TWILIO_AUTH_TOKEN
        ),
        timeout=REQUEST_TIMEOUT,
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=REQUEST_TIMEOUT)),
    )


def get_client() -> TwilioSdkClient:
    """The process-wide client, built lazily (after any worker fork)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


@atexit.register
def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


@contextmanager
def use_client(client: TwilioSdkClient) -> Iterator[TwilioSdkClient]:
    """Swap the process-wide client (tests, or credential rotation)."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    try:
        yield client
    finally:
        with _client_lock:
            _client = previous


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def _v(value: Any) -> Any:
    """Resolve the SDK's UNSET sentinel to None."""
    return None if isinstance(value, UnsetType) else value


def _rfc2822(value: Any) -> datetime | None:
    value = _v(value)
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Message:
    sid: str
    status: str | None
    to: str | None
    from_: str | None
    body: str | None
    error_code: int | None
    error_message: str | None
    date_created: datetime | None
    date_sent: datetime | None
    messaging_service_sid: str | None


def _message(value: ApiV2010AccountMessage, operation: str) -> Message:
    sid = _v(value.sid)
    if not isinstance(sid, str) or not sid:
        # Accepted or not, we cannot name what (if anything) was created.
        raise ProviderUnreadable("%s returned no message sid." % operation)
    status = _v(value.status)
    return Message(
        sid=sid,
        status=str(status) if status is not None else None,
        to=_v(value.to),
        from_=_v(value.from_),
        body=_v(value.body),
        error_code=_v(value.error_code),
        error_message=_v(value.error_message),
        date_created=_rfc2822(value.date_created),
        date_sent=_rfc2822(value.date_sent),
        messaging_service_sid=_v(value.messaging_service_sid),
    )


@dataclass(frozen=True)
class CanonicalNumber:
    phone_number: str
    country_code: str | None


@dataclass(frozen=True)
class MessagePage:
    messages: list[Message]
    truncated: bool


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _account() -> str:
    return str(settings.TWILIO_ACCOUNT_SID)


def lookup_number(raw_number: str, country_code: str | None = None) -> CanonicalNumber:
    """Ask the provider whether ``raw_number`` is a usable number, and for its E.164 form."""
    client = get_client()
    try:
        result = _call(
            "lookup",
            lambda: client.lookups_v1_phone_number_api.fetch_phone_number2(
                raw_number, country_code=country_code
            ),
        )
    except ProviderRejected as e:
        # Lookup answers 404 for anything it cannot resolve to a real number.
        raise NumberNotUsable(e.message) from e
    phone_number = _v(result.phone_number)
    if not isinstance(phone_number, str) or not phone_number.startswith("+"):
        raise ProviderUnreadable("Lookup returned no canonical number.", outcome_unknown=False)
    return CanonicalNumber(phone_number=phone_number, country_code=_v(result.country_code))


def send_message(to: str, body: str) -> Message:
    client = get_client()
    return _message(
        _call(
            "send",
            lambda: client.api20100401_message.create_message(
                _account(), to, from_=settings.TWILIO_FROM_NUMBER, body=body
            ),
        ),
        "create_message",
    )


def schedule_message(to: str, body: str, send_at: datetime) -> Message:
    """Queue ``body`` with the provider for delivery at ``send_at`` (aware datetime)."""
    if not settings.TWILIO_MESSAGING_SERVICE_SID:
        raise ProviderConfigError("Scheduling needs TWILIO_MESSAGING_SERVICE_SID.")
    client = get_client()
    return _message(
        _call(
            "schedule",
            lambda: client.api20100401_message.create_message(
                _account(),
                to,
                messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
                # Pin our own number from the service's pool, so the message is
                # attributable to this application's sender.
                from_=settings.TWILIO_FROM_NUMBER,
                schedule_type=MessageEnumScheduleType.FIXED,
                send_at=send_at,
                body=body,
            ),
        ),
        "create_message",
    )


def fetch_message(sid: str) -> Message | None:
    """The provider's current record of ``sid``; None only when the provider says 404."""
    client = get_client()
    try:
        value = _call("fetch", lambda: client.api20100401_message.fetch_message(_account(), sid))
    except ProviderRejected as e:
        if e.provider_status == 404:
            return None
        raise
    return _message(value, "fetch_message")


def cancel_message(sid: str) -> Message:
    client = get_client()
    return _message(
        _call(
            "cancel",
            lambda: client.api20100401_message.update_message(
                _account(), sid, status=MessageEnumUpdateStatus.CANCELED
            ),
        ),
        "update_message",
    )


def redact_message(sid: str) -> Message:
    """Erase the text of ``sid`` at the provider; the message record itself survives."""
    client = get_client()
    return _message(
        _call(
            "redact",
            lambda: client.api20100401_message.update_message(_account(), sid, body=""),
        ),
        "update_message",
    )


def _next_page(next_page_uri: str | None) -> tuple[int | None, str] | None:
    """(page, page_token) from a next_page_uri, or None at the last page."""
    if not next_page_uri:
        return None
    query = parse_qs(urlsplit(next_page_uri).query)
    tokens = query.get("PageToken") or []
    if not tokens or not tokens[0]:
        return None
    pages = query.get("Page") or []
    page = int(pages[0]) if pages and pages[0].isdigit() else None
    return (page, tokens[0])


def list_messages(
    *,
    sent_after: datetime | None = None,
    sent_before: datetime | None = None,
    to: str | None = None,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> MessagePage:
    """Messages sent from TWILIO_FROM_NUMBER, filtered provider-side, every page (bounded)."""
    client = get_client()
    from_number = str(settings.TWILIO_FROM_NUMBER)
    messages: list[Message] = []
    page: int | None = None
    token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(max_pages):
        result = _call(
            "list",
            lambda: client.api20100401_message.list_message(
                _account(),
                from_=from_number,
                to=to,
                date_sent_query_query=sent_after,  # DateSent>
                date_sent_query=sent_before,  # DateSent<
                page_size=page_size,
                page=page,
                page_token=token,
            ),
        )
        for value in _v(result.messages) or []:
            messages.append(_message(value, "list_message"))
        nxt = _next_page(_v(result.next_page_uri))
        if nxt is None:
            return MessagePage(messages=messages, truncated=False)
        page, token = nxt
        if token in seen_tokens:  # no progress: the provider keeps handing back the same page
            logger.error("twilio list: page token did not advance; stopping")
            return MessagePage(messages=messages, truncated=True)
        seen_tokens.add(token)
    logger.warning("twilio list: stopped at %s pages; result truncated", max_pages)
    return MessagePage(messages=messages, truncated=True)


def find_by_ref(to: str, ref: str, created_after: datetime) -> Message | None:
    """Find a message we may have created, by the reference embedded in its body."""
    page = list_messages(to=to, page_size=50, max_pages=2)
    window_start = created_after - timedelta(minutes=5)
    for message in page.messages:
        if message.body and ref in message.body:
            if message.date_created is None or message.date_created >= window_start:
                return message
    return None
