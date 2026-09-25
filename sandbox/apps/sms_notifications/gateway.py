"""
The only module that talks to Twilio, through the ``twilio_sdk`` client.

Messaging calls go to the SDK's ``default`` server (overridable verbatim with
``TWILIO_BASE_URL``); number lookups go to ``default4`` (Lookups), which that
setting does not govern.
"""

import atexit
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Iterator
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
    RawError,
    UnsetType,
)
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumStatus, MessageEnumUpdateStatus

from .models import Notification

log = logging.getLogger("apps.sms_notifications.twilio")

Outcome = Notification.Outcome

# Failures raised before the request left this process: nothing reached the provider.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_PHONE_IN_URL = re.compile(r"(\+|%2B)\d{4,}", re.IGNORECASE)


def redact_url(url: str) -> str:
    """Host and path only, with phone numbers masked; the query string is dropped."""
    parts = urlsplit(url)
    return parts.netloc + _PHONE_IN_URL.sub("[redacted]", parts.path)


class RedactingLoggingTransport:
    """Logs method, redacted URL, status and latency around the real transport -- never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            log.warning("twilio %s %s -> %s", request.method, redact_url(request.url), type(exc).__name__)
            raise
        log.info(
            "twilio %s %s -> %s (%.0f ms)",
            request.method,
            redact_url(request.url),
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(transport: HttpClient | None = None) -> TwilioSdkClient:
    sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    if not sid or not token:
        # Without credentials the SDK would silently send unauthenticated requests.
        raise ImproperlyConfigured("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    timeout = float(settings.SMS_NOTIFICATIONS_TIMEOUT_SECONDS)
    server_config: ServerConfigDict | None = None
    if settings.TWILIO_BASE_URL:
        server_config = {"default": {"base_url": settings.TWILIO_BASE_URL}}
    return TwilioSdkClient(
        server_config=server_config,
        timeout=timeout,
        custom_http_client=RedactingLoggingTransport(transport or HttpxClient(timeout=timeout)),
        account_sid_auth_token=BasicAuthCredentials(username=sid, password=token),
    )


_client_lock = threading.Lock()
_client: TwilioSdkClient | None = None
_client_pid: int | None = None


def get_client() -> TwilioSdkClient:
    """One long-lived client per process, built lazily (so it is created after any fork)."""
    global _client, _client_pid
    with _client_lock:
        if _client is None or _client_pid != os.getpid():
            _client = build_client()
            _client_pid = os.getpid()
        return _client


def set_client(client: TwilioSdkClient | None) -> None:
    """Replace the process client (tests inject one built over a stub transport)."""
    global _client, _client_pid
    with _client_lock:
        _client = client
        _client_pid = os.getpid() if client is not None else None


@atexit.register
def _close_client() -> None:
    if _client is not None and _client_pid == os.getpid():
        _client.close()


def account_sid() -> str:
    return str(settings.TWILIO_ACCOUNT_SID)


# ---------------------------------------------------------------------------
# Provider records, read into our own types (UNSET never leaves this module)
# ---------------------------------------------------------------------------


def _str(value: object) -> str | None:
    if value is None or isinstance(value, UnsetType):
        return None
    return str(value)


def _rfc1123(value: object) -> datetime | None:
    text = _str(value)
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ProviderMessage:
    sid: str | None
    status: str | None
    to: str | None
    from_: str | None
    direction: str | None
    body: str | None
    date_created: datetime | None
    date_sent: datetime | None
    error_code: int | None
    error_message: str | None


def read_message(m: ApiV2010AccountMessage) -> ProviderMessage:
    error_code = m.error_code if isinstance(m.error_code, int) else None
    return ProviderMessage(
        sid=_str(m.sid),
        status=_str(m.status),
        to=_str(m.to),
        from_=_str(m.from_),
        direction=_str(m.direction),
        body=_str(m.body),
        date_created=_rfc1123(m.date_created),
        date_sent=_rfc1123(m.date_sent),
        error_code=error_code,
        error_message=_str(m.error_message),
    )


# ---------------------------------------------------------------------------
# Status -> outcome: the one place a provider status becomes ours
# ---------------------------------------------------------------------------


def outcome_of_send(status: str | None) -> str:
    """Outcome of a message we asked to send (create_message / fetch_message status)."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return Outcome.DONE
        case (
            MessageEnumStatus.ACCEPTED
            | MessageEnumStatus.QUEUED
            | MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.SCHEDULED
            | MessageEnumStatus.PARTIALLY_DELIVERED
        ):
            return Outcome.PENDING
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED | MessageEnumStatus.CANCELED:
            return Outcome.FAILED
        case _:
            # receiving/received on an outbound send, a value newer than the SDK, or no status at all.
            return Outcome.UNKNOWN


def outcome_of_cancel(status: str | None) -> str:
    """Outcome of asking the provider to cancel a queued message."""
    match status:
        case MessageEnumStatus.CANCELED:
            return Outcome.DONE
        case MessageEnumStatus.SCHEDULED | MessageEnumStatus.ACCEPTED | MessageEnumStatus.QUEUED:
            return Outcome.PENDING
        case (
            MessageEnumStatus.SENDING
            | MessageEnumStatus.SENT
            | MessageEnumStatus.DELIVERED
            | MessageEnumStatus.READ
            | MessageEnumStatus.UNDELIVERED
            | MessageEnumStatus.FAILED
            | MessageEnumStatus.PARTIALLY_DELIVERED
        ):
            return Outcome.FAILED  # too late: the message already went out
        case _:
            return Outcome.UNKNOWN


# ---------------------------------------------------------------------------
# Error ladder: every SDK failure becomes one ProviderError
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool,
        provider_http_status: int | None = None,
        provider_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code  # what our API answers
        self.message = message
        self.outcome_unknown = outcome_unknown  # whether the provider may have acted
        self.provider_http_status = provider_http_status
        self.provider_code = provider_code


def twilio_error_code(error: object) -> int | None:
    if not isinstance(error, RawError):
        return None
    try:
        body = error.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, int) else None


def translate(exc: Exception) -> ProviderError:
    """Map an SDK/transport failure onto our boundary. Call only with exceptions from an SDK call."""
    if isinstance(exc, ApiError):
        status = exc.status_code
        code = twilio_error_code(exc.error)
        if status in (401, 403):
            return ProviderError(
                502, "The messaging provider refused this application's credentials.",
                outcome_unknown=False, provider_http_status=status, provider_code=code,
            )
        if status == 429:
            return ProviderError(
                503, "The messaging provider is rate limiting this application.",
                outcome_unknown=False, provider_http_status=status, provider_code=code,
            )
        if 400 <= status < 500:
            return ProviderError(
                status, "The messaging provider rejected the request.",
                outcome_unknown=False, provider_http_status=status, provider_code=code,
            )
        return ProviderError(
            502, "The messaging provider failed.", outcome_unknown=True, provider_http_status=status, provider_code=code
        )
    if isinstance(exc, NEVER_SENT):
        return ProviderError(502, "The messaging provider could not be reached.", outcome_unknown=False)
    if isinstance(exc, httpx.RequestError):
        return ProviderError(504, "The messaging provider did not answer.", outcome_unknown=True)
    if isinstance(exc, ValueError):  # pydantic ValidationError or a non-JSON body
        return ProviderError(502, "The messaging provider's answer could not be read.", outcome_unknown=True)
    raise exc


SDK_FAILURES = (ApiError, httpx.RequestError, ValueError)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NumberLookup:
    phone_number: str
    country_code: str
    national_format: str


class NumberNotUsable(Exception):
    pass


def lookup_number(raw_number: str, country_code: str | None = None) -> NumberLookup:
    """Validate a destination with Lookups v1 and return the provider's canonical form.

    Raises NumberNotUsable when the provider does not know the number (404), ProviderError otherwise.
    """
    try:
        result = get_client().lookups_v1_phone_number_api.fetch_phone_number2(raw_number, country_code=country_code)
    except ApiError as e:
        if e.status_code == 404:
            raise NumberNotUsable() from e
        raise translate(e) from e
    except (httpx.RequestError, ValueError) as e:
        raise translate(e) from e
    canonical = _str(result.phone_number)
    if not canonical:
        raise ProviderError(502, "The messaging provider's answer could not be read.", outcome_unknown=False)
    return NumberLookup(
        phone_number=canonical,
        country_code=_str(result.country_code) or "",
        national_format=_str(result.national_format) or "",
    )


def send_message(to: str, body: str, send_at: datetime | None = None) -> ProviderMessage:
    """create_message. Raises the SDK's/httpx's own exceptions: the safe write classifies them."""
    messages = get_client().api20100401_message
    if send_at is None:
        created = messages.create_message(account_sid(), to, from_=settings.TWILIO_FROM_NUMBER, body=body)
    else:
        if not settings.TWILIO_MESSAGING_SERVICE_SID:
            raise ImproperlyConfigured("TWILIO_MESSAGING_SERVICE_SID is required to schedule messages")
        created = messages.create_message(
            account_sid(),
            to,
            from_=settings.TWILIO_FROM_NUMBER,
            messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=send_at,
            body=body,
        )
    return read_message(created)


def fetch_message(sid: str) -> ProviderMessage:
    return read_message(get_client().api20100401_message.fetch_message(account_sid(), sid))


def cancel_message(sid: str) -> ProviderMessage:
    return read_message(
        get_client().api20100401_message.update_message(account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED)
    )


def redact_message(sid: str) -> ProviderMessage:
    # Per the operation's docs, an empty body redacts the message text.
    return read_message(get_client().api20100401_message.update_message(account_sid(), sid, body=""))


def _next_page(next_page_uri: object) -> tuple[int, str] | None:
    uri = _str(next_page_uri)
    if not uri:
        return None
    query = parse_qs(urlsplit(uri).query)
    try:
        return int(query["Page"][0]), query["PageToken"][0]
    except (KeyError, IndexError, ValueError):
        return None


def iter_messages(
    *,
    to: str | None = None,
    from_: str | None = None,
    sent_on_or_after: datetime | None = None,
    sent_on_or_before: datetime | None = None,
    page_size: int = 1000,
    max_pages: int = 1000,
) -> Iterator[ProviderMessage]:
    """list_message, following every page (newest first). Raises SDK/httpx exceptions."""
    messages = get_client().api20100401_message
    page: int | None = None
    page_token: str | None = None
    for _ in range(max_pages):
        result = messages.list_message(
            account_sid(),
            to=to,
            from_=from_,
            date_sent_query_query=sent_on_or_after,  # wire "DateSent>"
            date_sent_query=sent_on_or_before,  # wire "DateSent<"
            page_size=page_size,
            page=page,
            page_token=page_token,
        )
        if not isinstance(result.messages, UnsetType):
            for m in result.messages:
                yield read_message(m)
        following = _next_page(result.next_page_uri)
        if following is None:
            return
        page, page_token = following
    raise ProviderError(502, "The provider's message list did not end.", outcome_unknown=False)


def find_by_token(to: str, token: str, *, created_since: datetime) -> ProviderMessage | None:
    """Find the message this app sent carrying ``token`` in its body (the lookup behind an unknown outcome)."""
    horizon = created_since - timedelta(minutes=10)  # tolerate clock skew
    for msg in iter_messages(to=to, page_size=100, max_pages=20):
        if msg.body and f"(ref {token})" in msg.body:
            return msg
        if msg.date_created is not None and msg.date_created < horizon:
            return None
    return None
