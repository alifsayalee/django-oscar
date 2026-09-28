"""
Twilio access for the SMS notifications app.

This is the only module that imports ``twilio_sdk``. It owns the client (one per process, built lazily after any
fork, closed at exit), the transport that logs without leaking numbers or credentials, the translation of provider
failures into :class:`ProviderError`, and one thin function per provider call the app makes.

Write functions (``send_sms``, ``cancel_scheduled_sms``, ``redact_sms``) let the SDK's and httpx's exceptions through
untouched: the safe write in :mod:`.safe_write` decides what each of them means. Read functions go through
:func:`read`, which maps every failure onto :class:`ProviderError`.
"""
from __future__ import annotations

import atexit
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import (
    UNSET, ApiError, BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient, RawError)
from twilio_sdk.models import ApiV2010AccountMessage, ListMessageResponse, LookupResponse
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumUpdateStatus

logger = logging.getLogger("apps.sms_notifications.provider")

DEFAULT_MESSAGING_BASE_URL = "https://api.twilio.com"
DEFAULT_LOOKUPS_BASE_URL = "https://lookups.twilio.com"

# Failures raised before the request left this process: nothing can have reached the provider.
NEVER_SENT: tuple[type[Exception], ...] = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError,
)

# Bounds on paging, so a runaway listing cannot hold a request forever.
LOOKUP_PAGE_SIZE = 100
LOOKUP_MAX_PAGES = 5
RECONCILE_PAGE_SIZE = 1000
RECONCILE_MAX_PAGES = 200

T = TypeVar("T")


class NotConfigured(Exception):
    """The Twilio settings this app needs are missing."""


class ProviderError(Exception):
    """A provider failure, translated once: the status our API answers with, and whether a write may have landed."""

    def __init__(
        self, status_code: int, message: str, *, outcome_unknown: bool = False, provider_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_code = provider_code


# ---------------------------------------------------------------------------------------------------------------------
# Transport and client
# ---------------------------------------------------------------------------------------------------------------------

_LOOKUP_SEGMENT = re.compile(r"(/PhoneNumbers/)[^/]*")
_PHONE_LIKE = re.compile(r"(?<![A-Za-z0-9])(%2B|\+)?[0-9]{6,}(?![A-Za-z0-9])")
_ACCOUNT_SID = re.compile(r"AC[0-9a-fA-F]{32}")


def redact_url(url: str) -> str:
    """Host and path only, with phone numbers and the account id masked. The query string is dropped entirely."""
    parts = urlsplit(url)
    path = _LOOKUP_SEGMENT.sub(r"\1***", parts.path)  # a Lookup path segment is whatever the caller typed
    path = _ACCOUNT_SID.sub("AC...", _PHONE_LIKE.sub("***", path))
    return "%s%s" % (parts.netloc, path)


class LoggingTransport:
    """Wraps the SDK's transport and logs method, redacted URL, status and duration - never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "twilio %s %s -> %s after %.0f ms",
                request.method, redact_url(request.url), type(exc).__name__, (time.monotonic() - started) * 1000,
            )
            raise
        logger.info(
            "twilio %s %s -> %s (%.0f ms)",
            request.method, redact_url(request.url), response.status_code, (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str
    from_number: str
    messaging_service_sid: str
    messaging_base_url: str
    lookups_base_url: str
    timeout: float


def get_config() -> TwilioConfig:
    account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", "") or ""
    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "") or ""
    from_number = getattr(settings, "TWILIO_FROM_NUMBER", "") or ""
    if not (account_sid and auth_token and from_number):
        raise NotConfigured("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER must all be set")
    return TwilioConfig(
        account_sid=account_sid,
        auth_token=auth_token,
        from_number=from_number,
        messaging_service_sid=getattr(settings, "TWILIO_MESSAGING_SERVICE_SID", "") or "",
        # TWILIO_BASE_URL governs the Messages API only, and is used verbatim when set.
        messaging_base_url=getattr(settings, "TWILIO_BASE_URL", "") or DEFAULT_MESSAGING_BASE_URL,
        lookups_base_url=getattr(settings, "TWILIO_LOOKUPS_BASE_URL", "") or DEFAULT_LOOKUPS_BASE_URL,
        timeout=float(getattr(settings, "TWILIO_TIMEOUT", 10.0)),
    )


_client: TwilioSdkClient | None = None
_client_config: TwilioConfig | None = None
_client_lock = threading.Lock()


def build_client(config: TwilioConfig, transport: HttpClient | None = None) -> TwilioSdkClient:
    return TwilioSdkClient(
        # Both servers this app uses, passed explicitly: "default" serves Messages, "default4" serves Lookup v2.
        server_config={
            "default": {"base_url": config.messaging_base_url},
            "default4": {"base_url": config.lookups_base_url},
        },
        custom_http_client=LoggingTransport(transport or HttpxClient(timeout=config.timeout)),
        account_sid_auth_token=BasicAuthCredentials(username=config.account_sid, password=config.auth_token),
    )


def get_client() -> tuple[TwilioSdkClient, TwilioConfig]:
    """The process-wide client, built on first use (so after any worker fork) and rebuilt if settings change."""
    global _client, _client_config
    config = get_config()
    with _client_lock:
        if _client is None or _client_config != config:
            if _client is not None:
                _client.close()
            _client = build_client(config)
            _client_config = config
        return _client, config


def set_client_for_tests(client: TwilioSdkClient | None, config: TwilioConfig | None = None) -> None:
    """Install a client built around a stub transport (or clear it)."""
    global _client, _client_config
    with _client_lock:
        _client = client
        _client_config = config


@atexit.register
def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


# ---------------------------------------------------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------------------------------------------------

def _twilio_code(error: RawError) -> int | None:
    try:
        body = error.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, int) else None


def provider_error(status: int, error: RawError) -> ProviderError:
    """The provider's refusal, as our status. Our credentials or quota are never the caller's fault."""
    code = _twilio_code(error)
    match status:
        case 401 | 403:
            return ProviderError(502, "The SMS provider refused this application's credentials.", provider_code=code)
        case 429:
            return ProviderError(503, "The SMS provider is rate-limiting this application.", provider_code=code)
        case s if 400 <= s < 500:
            return ProviderError(s, "The SMS provider rejected the request.", provider_code=code)
        case _:
            return ProviderError(502, "The SMS provider is unavailable.", outcome_unknown=True, provider_code=code)


def read(call: Callable[[], T]) -> T:
    """Run a provider *read* and translate every failure kind. Nothing is retried: the next request re-reads."""
    try:
        return call()
    except ApiError as e:
        raise provider_error(e.status_code, e.error) from e
    except ValueError as e:  # pydantic's ValidationError, or a non-JSON body
        raise ProviderError(502, "The SMS provider's answer could not be read.") from e
    except NEVER_SENT as e:
        raise ProviderError(502, "The SMS provider could not be reached.") from e
    except httpx.RequestError as e:
        raise ProviderError(504, "The SMS provider did not answer in time.") from e


def parse_provider_time(value: object) -> datetime | None:
    """The provider's RFC 2822 GMT timestamps, as aware datetimes; ``None`` when absent or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def has_sid(message: object) -> bool:
    return isinstance(message, ApiV2010AccountMessage) and isinstance(message.sid, str) and bool(message.sid)


def _next_page(response: ListMessageResponse) -> tuple[int | None, str | None] | None:
    uri = response.next_page_uri
    if not isinstance(uri, str) or not uri:
        return None
    query = parse_qs(urlsplit(uri).query)
    token = query.get("PageToken", [None])[0]
    page = query.get("Page", [None])[0]
    if token is None:
        return None
    return (int(page) if page and page.isdigit() else None), token


def _messages(response: ListMessageResponse) -> list[ApiV2010AccountMessage]:
    if response.messages is UNSET or not isinstance(response.messages, list):
        raise ProviderError(502, "The SMS provider's message list could not be read.")
    return response.messages


# ---------------------------------------------------------------------------------------------------------------------
# Writes (exceptions pass through to the safe write)
# ---------------------------------------------------------------------------------------------------------------------

def send_sms(to: str, body: str, *, send_at: datetime | None = None) -> ApiV2010AccountMessage:
    client, config = get_client()
    if send_at is None:
        return client.api20100401_message.create_message(
            config.account_sid, to, from_=config.from_number, body=body
        )
    if not config.messaging_service_sid:
        raise NotConfigured("TWILIO_MESSAGING_SERVICE_SID is required to queue a scheduled message")
    # Scheduling is a Messaging Service feature; From is still pinned to our number so reconciliation sees it.
    return client.api20100401_message.create_message(
        config.account_sid,
        to,
        from_=config.from_number,
        messaging_service_sid=config.messaging_service_sid,
        schedule_type=MessageEnumScheduleType.FIXED,
        send_at=send_at.astimezone(timezone.utc),
        body=body,
    )


def cancel_scheduled_sms(sid: str) -> ApiV2010AccountMessage:
    client, config = get_client()
    return client.api20100401_message.update_message(
        config.account_sid, sid, status=MessageEnumUpdateStatus.CANCELED
    )


def redact_sms(sid: str) -> ApiV2010AccountMessage:
    client, config = get_client()
    # An empty body is how the Messages API redacts a message's text; the record itself (status, times) survives.
    return client.api20100401_message.update_message(config.account_sid, sid, body="")


def fetch_sms(sid: str) -> ApiV2010AccountMessage:
    """Raw fetch (exceptions pass through) for use inside a safe write's lookup."""
    client, config = get_client()
    return client.api20100401_message.fetch_message(config.account_sid, sid)


def find_sms_by_token(to: str, token: str, since: datetime) -> ApiV2010AccountMessage | None:
    """
    The message we created carrying ``token`` in its body, or ``None``.

    Raw (exceptions pass through): the safe write treats a failed lookup as "not settled", never as "absent".
    """
    client, config = get_client()
    cutoff = since - timedelta(minutes=10)
    page: int | None = None
    token_param: str | None = None
    for _ in range(LOOKUP_MAX_PAGES):
        response = client.api20100401_message.list_message(
            config.account_sid, to=to, from_=config.from_number, page_size=LOOKUP_PAGE_SIZE,
            page=page, page_token=token_param,
        )
        for message in _messages(response):
            created = parse_provider_time(message.date_created)
            if isinstance(message.body, str) and token in message.body and (created is None or created >= cutoff):
                return message
        following = _next_page(response)
        if following is None:
            return None
        page, token_param = following
    # Ran out of pages without an answer: that proves nothing either way.
    raise ProviderError(504, "Lookup did not finish within its page budget.", outcome_unknown=True)


# ---------------------------------------------------------------------------------------------------------------------
# Reads (translated through ``read``)
# ---------------------------------------------------------------------------------------------------------------------

def lookup_number(raw: str) -> LookupResponse:
    client, _ = get_client()
    return read(lambda: client.lookups_v2_phone_number.fetch_phone_number3(raw))


def fetch_sms_checked(sid: str) -> ApiV2010AccountMessage:
    return read(lambda: fetch_sms(sid))


@dataclass(frozen=True)
class SentMessages:
    messages: list[ApiV2010AccountMessage]
    truncated: bool


def list_sent_sms(start: datetime, end: datetime) -> SentMessages:
    """
    Every message the provider sent from our configured number between ``start`` and ``end``.

    The provider filters by sent date on day boundaries, so the query is widened to whole days and the answer is
    narrowed back on the provider's own ``date_sent``. Pages are followed until the provider says there are no more.
    """
    client, config = get_client()
    day_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    last = end.astimezone(timezone.utc) + timedelta(days=1)
    day_end = datetime(last.year, last.month, last.day, tzinfo=timezone.utc)

    collected: list[ApiV2010AccountMessage] = []
    page: int | None = None
    token: str | None = None
    for _ in range(RECONCILE_MAX_PAGES):
        response = read(
            lambda: client.api20100401_message.list_message(
                config.account_sid,
                from_=config.from_number,
                date_sent_query_query=day_start,  # wire "DateSent>": on or after
                date_sent_query=day_end,  # wire "DateSent<": on or before
                page_size=RECONCILE_PAGE_SIZE,
                page=page,
                page_token=token,
            )
        )
        for message in _messages(response):
            sent = parse_provider_time(message.date_sent)
            if sent is not None and start <= sent <= end:
                collected.append(message)
        following = _next_page(response)
        if following is None:
            return SentMessages(collected, truncated=False)
        page, token = following
    return SentMessages(collected, truncated=True)
