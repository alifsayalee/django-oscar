"""
The one place this application talks to Twilio.

Everything here goes through the generated ``twilio_sdk`` client. The SDK performs no retries and
no logging, and it does not wrap its transport's exceptions, so this module owns:

* the client's lifetime (one lazily built, process-wide sync client, closed at exit);
* a logging transport that never writes a phone number, a header or a body;
* the error ladder that turns SDK/transport failures into ``ProviderError`` values carrying the
  status this app answers with and whether the provider may have acted;
* ``status_from_provider`` - the single mapping from a provider message status to our outcome.

Provider *writes* are made by ``apps.order_sms.notifications`` through its safe-write path; the
write functions here are deliberately thin so that path sees ``ApiError`` and ``httpx`` errors
itself.
"""
from __future__ import annotations

import atexit
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import (
    ApiError, BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient, RawError,
    UnsetType)
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumStatus, MessageEnumUpdateStatus

log = logging.getLogger('apps.order_sms')

T = TypeVar('T')
Outcome = Literal['done', 'pending', 'failed', 'unknown']

# Transport failures raised before the request left: nothing can have reached the provider.
NEVER_SENT: tuple[type[Exception], ...] = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

_PHONE_LIKE = re.compile(r'(?:\+|%2[bB])?\d[\d\s().-]{4,}\d')


def scrub(text: str | None, limit: int = 255) -> str:
    """Remove anything that looks like a phone number from provider text before storing/logging."""
    if not text:
        return ''
    return _PHONE_LIKE.sub('***', text)[:limit]


# --------------------------------------------------------------------------------------------
# Failures, as this application reports them
# --------------------------------------------------------------------------------------------

class ProviderError(Exception):
    """A provider call did not give a usable answer.

    ``status_code`` is the HTTP status our API answers with; ``outcome_unknown`` says whether the
    provider may nevertheless have acted (only ever true for a write).
    """

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False,
                 provider_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_code = provider_code


class ProviderConfigError(ProviderError):
    """Our configuration or credentials are wrong - nothing the caller can fix."""


class NumberNotUsable(ProviderError):
    """The provider does not recognise the number as a usable destination."""


def provider_error_code(error: ApiError) -> int | None:
    """Twilio's own error code from a RawError body, when the body is readable JSON."""
    if not isinstance(error.error, RawError):   # every in-scope operation is Case B: RawError
        return None
    try:
        payload = error.error.json()
    except ValueError:
        return None
    code = payload.get('code') if isinstance(payload, dict) else None
    return code if isinstance(code, int) else None


def provider_error_detail(error: ApiError) -> str:
    if not isinstance(error.error, RawError):
        return f'HTTP {error.status_code}'
    try:
        payload = error.error.json()
    except ValueError:
        return f'HTTP {error.status_code}'
    message = payload.get('message') if isinstance(payload, dict) else None
    return scrub(message if isinstance(message, str) else f'HTTP {error.status_code}')


def translate(exc: Exception) -> ProviderError:
    """The error ladder for provider *reads* (and for describing a failed write)."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        code = provider_error_code(exc)
        if exc.status_code in (401, 403):
            return ProviderConfigError(502, 'The messaging provider refused our credentials.',
                                       provider_code=code)
        if exc.status_code == 429:
            return ProviderError(503, 'The messaging provider is rate-limiting us; try again later.',
                                 provider_code=code)
        if exc.status_code in (400, 404, 409, 422):
            return ProviderError(exc.status_code, provider_error_detail(exc), provider_code=code)
        return ProviderError(502, 'The messaging provider failed.', provider_code=code,
                             outcome_unknown=exc.status_code >= 500)
    if isinstance(exc, NEVER_SENT):
        return ProviderError(502, 'The messaging provider could not be reached.')
    if isinstance(exc, httpx.RequestError):
        return ProviderError(504, 'The messaging provider did not answer.', outcome_unknown=True)
    if isinstance(exc, ValueError):  # pydantic ValidationError / non-JSON body
        return ProviderError(502, 'The messaging provider sent an unreadable answer.',
                             outcome_unknown=True)
    raise exc


def _transient(exc: Exception) -> bool:
    if isinstance(exc, ApiError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, httpx.RequestError)


def read(call: Callable[[], T]) -> T:
    """Run a provider *read*: safe to repeat, so one retry on a transient failure."""
    for attempt in (1, 2):
        try:
            return call()
        except (ApiError, httpx.RequestError) as exc:
            if attempt == 2 or not _transient(exc):
                raise translate(exc) from exc
            time.sleep(0.5)
        except ValueError as exc:
            raise translate(exc) from exc
    raise AssertionError('unreachable')


# --------------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------------

class LoggingTransport:
    """Wraps the SDK's transport: logs method, host, masked path, status and latency only."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        parts = urlsplit(request.url)
        path = _PHONE_LIKE.sub('***', parts.path)
        try:
            response = self._inner.send(request)
        except Exception as exc:
            log.warning('twilio %s %s%s -> %s', request.method, parts.netloc, path,
                        type(exc).__name__)
            raise
        log.info('twilio %s %s%s -> %s (%.0f ms)', request.method, parts.netloc, path,
                 response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


_client: TwilioSdkClient | None = None
_client_lock = threading.Lock()


def _build_client() -> TwilioSdkClient:
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    if not account_sid or not auth_token:
        raise ProviderConfigError(503, 'SMS notifications are not configured.')
    timeout = float(settings.TWILIO_TIMEOUT_SECONDS)
    base_url = settings.TWILIO_BASE_URL
    return TwilioSdkClient(
        # Messaging API calls resolve against server "default"; TWILIO_BASE_URL overrides only it.
        server_config={'default': {'base_url': base_url}} if base_url else None,
        timeout=timeout,
        custom_http_client=LoggingTransport(HttpxClient(timeout=timeout)),
        account_sid_auth_token=BasicAuthCredentials(username=account_sid, password=auth_token),
    )


def get_client() -> TwilioSdkClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _build_client()
    return _client


def set_client(client: TwilioSdkClient | None) -> None:
    """Replace the process-wide client (tests inject one built on a stub transport)."""
    global _client
    with _client_lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    set_client(None)


def account_sid() -> str:
    return str(settings.TWILIO_ACCOUNT_SID)


def from_number() -> str:
    number = settings.TWILIO_FROM_NUMBER
    if not number:
        raise ProviderConfigError(503, 'SMS notifications are not configured.')
    return str(number)


# --------------------------------------------------------------------------------------------
# Message state
# --------------------------------------------------------------------------------------------

def _str(value: str | None | UnsetType) -> str | None:
    return None if isinstance(value, UnsetType) else value


def _provider_time(value: str | None | UnsetType) -> datetime | None:
    text = _str(value)
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)   # the provider sends RFC 2822 dates
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MessageState:
    """What the provider says about one message, with UNSET resolved."""
    sid: str | None
    status: str
    body: str | None
    to: str | None
    from_: str | None
    direction: str
    date_created: datetime | None
    date_sent: datetime | None
    error_code: int | None
    error_message: str | None

    @classmethod
    def of(cls, message: ApiV2010AccountMessage) -> MessageState:
        status = '' if isinstance(message.status, UnsetType) else str(message.status)
        direction = '' if isinstance(message.direction, UnsetType) else str(message.direction)
        error_code = None if isinstance(message.error_code, UnsetType) else message.error_code
        return cls(
            sid=_str(message.sid), status=status, body=_str(message.body), to=_str(message.to),
            from_=_str(message.from_), direction=direction,
            date_created=_provider_time(message.date_created),
            date_sent=_provider_time(message.date_sent),
            error_code=error_code, error_message=_str(message.error_message))


def status_from_provider(status: str) -> Outcome:
    """The ONE place a provider message status becomes this application's outcome."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return 'done'
        case (MessageEnumStatus.ACCEPTED | MessageEnumStatus.SCHEDULED | MessageEnumStatus.QUEUED
              | MessageEnumStatus.SENDING | MessageEnumStatus.SENT
              | MessageEnumStatus.PARTIALLY_DELIVERED):
            return 'pending'
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED | MessageEnumStatus.CANCELED:
            return 'failed'
        case _:  # receiving/received (inbound only), a value newer than the SDK, or none at all
            return 'unknown'


def call_off_outcome(status: str) -> Outcome:
    """Outcome of asking the provider to cancel a scheduled message, read from its status."""
    match status:
        case MessageEnumStatus.CANCELED | MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return 'done'      # it will not reach the shopper
        case MessageEnumStatus.SCHEDULED | MessageEnumStatus.ACCEPTED:
            return 'pending'   # still queued with the provider: the call-off has not taken yet
        case (MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING | MessageEnumStatus.SENT
              | MessageEnumStatus.DELIVERED | MessageEnumStatus.READ
              | MessageEnumStatus.PARTIALLY_DELIVERED):
            return 'failed'    # it already went out
        case _:
            return 'unknown'


# --------------------------------------------------------------------------------------------
# Provider operations
# --------------------------------------------------------------------------------------------

def lookup_number(raw: str) -> tuple[str, str]:
    """Ask the provider whether ``raw`` is a usable destination; return (E.164, country)."""
    client = get_client()
    try:
        result = read(lambda: client.lookups_v1_phone_number_api.fetch_phone_number2(raw))
    except ProviderError as exc:
        if exc.status_code in (400, 404):
            raise NumberNotUsable(422, 'The messaging provider does not recognise this number '
                                       'as a usable destination.') from exc
        raise
    number = _str(result.phone_number)
    if not number:
        raise ProviderError(502, 'The messaging provider sent an unreadable answer.')
    return number, _str(result.country_code) or ''


def send_message(to: str, body: str, *, send_at: datetime | None = None) -> MessageState:
    """Create a message. A write: raises ApiError/httpx errors untranslated for the safe write."""
    client = get_client()
    if send_at is None:
        message = client.api20100401_message.create_message(
            account_sid(), to, from_=from_number(), body=body)
    else:
        message = client.api20100401_message.create_message(
            account_sid(), to, from_=from_number(), body=body,
            messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
            schedule_type=MessageEnumScheduleType.FIXED, send_at=send_at)
    return MessageState.of(message)


def fetch_message(sid: str) -> MessageState:
    client = get_client()
    return MessageState.of(read(lambda: client.api20100401_message.fetch_message(account_sid(), sid)))


def cancel_message(sid: str) -> MessageState:
    """Call off a not-yet-sent message. Setting a fixed value: harmless to repeat."""
    client = get_client()
    return MessageState.of(client.api20100401_message.update_message(
        account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED))


def redact_message(sid: str) -> MessageState:
    """Erase a message's text at the provider (an empty body is the provider's redaction)."""
    client = get_client()
    return MessageState.of(client.api20100401_message.update_message(account_sid(), sid, body=''))


def _pages(fetch_page: Callable[[str | None, int | None], tuple[list[ApiV2010AccountMessage],
                                                                 str | None]],
           max_pages: int) -> Iterator[MessageState]:
    token: str | None = None
    page: int | None = None
    for _ in range(max_pages):
        messages, next_uri = fetch_page(token, page)
        for message in messages:
            yield MessageState.of(message)
        if not next_uri:
            return
        query = parse_qs(urlsplit(next_uri).query)
        tokens, pages = query.get('PageToken', []), query.get('Page', [])
        if not tokens:
            return
        token = tokens[0]
        page = int(pages[0]) if pages and pages[0].isdigit() else None
    raise ProviderError(502, 'Too many pages of messages from the provider.')


def find_message(to: str, tag: str, *, max_pages: int = 4) -> MessageState | None:
    """Look a message up by the reference tag it carries in its body (the may-have-landed check)."""
    client = get_client()
    sender = from_number()

    def fetch_page(token: str | None, page: int | None) -> tuple[list[ApiV2010AccountMessage],
                                                                 str | None]:
        response = read(lambda: client.api20100401_message.list_message(
            account_sid(), to=to, from_=sender, page_size=50, page_token=token, page=page))
        messages = [] if isinstance(response.messages, UnsetType) else response.messages
        next_uri = _str(response.next_page_uri)
        return messages, next_uri

    for state in _pages(fetch_page, max_pages):
        if state.body and tag in state.body and state.direction != 'inbound':
            return state
    return None


def list_sent_from(sender: str, start: datetime, end: datetime) -> Iterator[MessageState]:
    """Every message the provider holds from ``sender`` sent in [start - 1 day, end + 1 day].

    The provider's DateSent filter is date-granular per its docs, so the query is widened by a day
    on each side; the caller narrows back to the exact window.
    """
    client = get_client()
    after = start - timedelta(days=1)
    before = end + timedelta(days=1)

    def fetch_page(token: str | None, page: int | None) -> tuple[list[ApiV2010AccountMessage],
                                                                 str | None]:
        response = read(lambda: client.api20100401_message.list_message(
            account_sid(), from_=sender, date_sent_query_query=after, date_sent_query=before,
            page_size=1000, page_token=token, page=page))
        messages = [] if isinstance(response.messages, UnsetType) else response.messages
        return messages, _str(response.next_page_uri)

    return _pages(fetch_page, max_pages=1000)

