"""
The one place this app talks to Twilio.

Holds the process-wide SDK client, the translation of every SDK failure into
this app's own error type, and the mapping of Twilio's message statuses onto
this app's outcomes. Nothing here logs a phone number, a message body or a
credential.
"""
from __future__ import annotations

import atexit
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
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
    HttpRequest,
    HttpResponse,
    HttpxClient,
    RawError,
    UnsetType,
)
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumStatus, MessageEnumUpdateStatus

logger = logging.getLogger(__name__)

T = TypeVar('T')

DEFAULT_MESSAGING_BASE_URL = 'https://api.twilio.com'
LOOKUPS_BASE_URL = 'https://lookups.twilio.com'

# Transport failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

DONE = 'done'
PENDING = 'pending'
FAILED = 'failed'
UNKNOWN = 'unknown'


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class ProviderError(Exception):
    """A Twilio call did not give a usable answer.

    ``status_code`` is what this app's HTTP boundary answers with, and
    ``outcome_unknown`` says whether the call may have taken effect anyway.
    """

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool,
                 provider_status: int | None = None, provider_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.provider_code = provider_code


class ProviderNotConfigured(ProviderError):
    def __init__(self) -> None:
        super().__init__(503, 'SMS provider is not configured.', outcome_unknown=False)


class ProviderRejected(ProviderError):
    """Twilio refused the request itself (a 4xx that is about the request)."""


class ProviderUnavailable(ProviderError):
    """Our credentials, our quota, a 5xx, a transport failure or an unreadable answer."""


def _provider_code(error: RawError) -> tuple[int | None, str]:
    """Twilio's own error code and message from an error body, if it is readable JSON."""
    try:
        data = error.json()
    except ValueError:
        return None, ''
    if not isinstance(data, dict):
        return None, ''
    code = data.get('code')
    message = data.get('message')
    return (code if isinstance(code, int) else None), (message if isinstance(message, str) else '')


def call(operation: str, fn: Callable[[], T], *, is_write: bool) -> T:
    """Run one SDK call and translate every way it can fail into ``ProviderError``.

    The same ladder is used for every call site, reads and writes alike.
    """
    try:
        return fn()
    except ApiError as e:
        code, provider_message = (None, '')
        if isinstance(e.error, RawError):
            code, provider_message = _provider_code(e.error)
        logger.warning('twilio %s failed: HTTP %s (code %s)', operation, e.status_code, code)
        if e.status_code in (401, 403):
            raise ProviderUnavailable(
                502, 'SMS provider refused our credentials.', outcome_unknown=False,
                provider_status=e.status_code, provider_code=code) from e
        if e.status_code == 429:
            raise ProviderUnavailable(
                503, 'SMS provider is rate limiting us.', outcome_unknown=False,
                provider_status=e.status_code, provider_code=code) from e
        if 400 <= e.status_code < 500:
            raise ProviderRejected(
                e.status_code, provider_message or 'SMS provider rejected the request.',
                outcome_unknown=False, provider_status=e.status_code, provider_code=code) from e
        raise ProviderUnavailable(
            502, 'SMS provider unavailable.', outcome_unknown=is_write,
            provider_status=e.status_code, provider_code=code) from e
    except ValidationError as e:
        # A 2xx whose body we cannot read: for a write the outcome is unknown.
        logger.warning('twilio %s: unreadable response', operation)
        raise ProviderUnavailable(
            502, 'Unreadable answer from SMS provider.', outcome_unknown=is_write) from e
    except ValueError as e:
        logger.warning('twilio %s: non-JSON response', operation)
        raise ProviderUnavailable(
            502, 'Unreadable answer from SMS provider.', outcome_unknown=is_write) from e
    except NEVER_SENT as e:
        logger.warning('twilio %s: never sent (%s)', operation, type(e).__name__)
        raise ProviderUnavailable(
            502, 'SMS provider unreachable.', outcome_unknown=False) from e
    except httpx.RequestError as e:
        logger.warning('twilio %s: no response (%s)', operation, type(e).__name__)
        raise ProviderUnavailable(
            504, 'No response from SMS provider.', outcome_unknown=is_write) from e


# --------------------------------------------------------------------------
# Status mapping — the only place a Twilio status becomes one of ours
# --------------------------------------------------------------------------

def status_text(status: object) -> str:
    """The wire value of an (open) enum member, for storing and display."""
    if isinstance(status, UnsetType) or status is None:
        return ''
    if isinstance(status, MessageEnumStatus):
        return status.value
    return str(status)


def status_from_provider(status: object) -> str:
    """Outcome of *sending* a message, from the message's status."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DONE
        case (MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING | MessageEnumStatus.SENT
              | MessageEnumStatus.ACCEPTED | MessageEnumStatus.SCHEDULED
              | MessageEnumStatus.PARTIALLY_DELIVERED):
            # Accepted by the provider but not (yet) confirmed on the handset.
            return PENDING
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return FAILED
        case MessageEnumStatus.CANCELED:
            # Called off before it went out: what was asked for is not in effect.
            return FAILED
        case _:
            # RECEIVING / RECEIVED (inbound values on an outbound send), a value
            # newer than this SDK, or no status at all.
            return UNKNOWN


def cancel_outcome_from_provider(status: object) -> str:
    """Outcome of *calling off* a scheduled message, from the message's status."""
    match status:
        case MessageEnumStatus.CANCELED:
            return DONE
        case MessageEnumStatus.SCHEDULED:
            return PENDING
        case (MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING | MessageEnumStatus.SENT
              | MessageEnumStatus.ACCEPTED | MessageEnumStatus.DELIVERED | MessageEnumStatus.READ
              | MessageEnumStatus.PARTIALLY_DELIVERED | MessageEnumStatus.FAILED
              | MessageEnumStatus.UNDELIVERED):
            # It already left the schedule: too late to call it off.
            return FAILED
        case _:
            return UNKNOWN


# --------------------------------------------------------------------------
# Reading a message
# --------------------------------------------------------------------------

def _text(value: object) -> str | None:
    if isinstance(value, UnsetType) or value is None:
        return None
    return str(value)


def _rfc1123(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MessageState:
    """What the provider says about one message."""

    sid: str
    status: str
    outcome: str
    cancel_outcome: str
    error_code: int | None
    error_message: str
    body: str | None
    to: str | None
    date_sent: datetime | None
    date_created: datetime | None

    @property
    def provider_time(self) -> datetime | None:
        return self.date_sent or self.date_created


def read_message(message: ApiV2010AccountMessage) -> MessageState:
    sid = _text(message.sid)
    if not sid:
        # No identifier: we cannot name what (if anything) was created.
        raise ProviderUnavailable(
            502, 'SMS provider answered without a message id.', outcome_unknown=True)
    error_code = message.error_code
    return MessageState(
        sid=sid,
        status=status_text(message.status),
        outcome=status_from_provider(message.status),
        cancel_outcome=cancel_outcome_from_provider(message.status),
        error_code=error_code if isinstance(error_code, int) else None,
        error_message=(_text(message.error_message) or '')[:255],
        body=_text(message.body),
        to=_text(message.to),
        date_sent=_rfc1123(message.date_sent),
        date_created=_rfc1123(message.date_created),
    )


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

_DIGITS = re.compile(r'(%2B|\+)?\d{6,}')


def _redact(url: str) -> str:
    parts = urlsplit(url)
    return f'{parts.netloc}{_DIGITS.sub("***", parts.path)}'


class LoggingTransport:
    """Logs method, host, redacted path, status and latency — never headers, query or bodies."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as e:
            logger.info('twilio %s %s -> %s', request.method, _redact(request.url), type(e).__name__)
            raise
        logger.info('twilio %s %s -> %s (%.0f ms)', request.method, _redact(request.url),
                    response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


_client: TwilioSdkClient | None = None
_client_lock = threading.Lock()


def is_configured() -> bool:
    return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN
                and settings.TWILIO_FROM_NUMBER)


def get_client() -> TwilioSdkClient:
    """The process-wide client, built lazily (so after any worker fork)."""
    global _client
    if not is_configured():
        raise ProviderNotConfigured()
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = TwilioSdkClient(
                    server_config={
                        'default': {'base_url': settings.TWILIO_BASE_URL or DEFAULT_MESSAGING_BASE_URL},
                        'default4': {'base_url': LOOKUPS_BASE_URL},
                    },
                    custom_http_client=LoggingTransport(
                        HttpxClient(timeout=settings.TWILIO_TIMEOUT_SECONDS)),
                    account_sid_auth_token=BasicAuthCredentials(
                        username=settings.TWILIO_ACCOUNT_SID,
                        password=settings.TWILIO_AUTH_TOKEN),
                )
                atexit.register(_client.close)
    return _client


def reset_client() -> None:
    """Close and forget the client (settings changed, or tests)."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None


def _account() -> str:
    return str(settings.TWILIO_ACCOUNT_SID)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CanonicalNumber:
    phone_number: str
    country_code: str


class NumberNotUsable(Exception):
    pass


def lookup_number(raw: str) -> CanonicalNumber:
    """Ask Twilio Lookup for the canonical E.164 form; a number it does not know is rejected."""
    client = get_client()
    try:
        result = call('lookup', lambda: client.lookups_v1_phone_number_api.fetch_phone_number2(raw),
                      is_write=False)
    except ProviderRejected as e:
        if e.provider_status in (400, 404):
            raise NumberNotUsable() from e
        raise
    phone_number = _text(result.phone_number)
    if not phone_number or not phone_number.startswith('+'):
        raise ProviderUnavailable(502, 'SMS provider returned no number.', outcome_unknown=False)
    return CanonicalNumber(phone_number=phone_number, country_code=_text(result.country_code) or '')


def send_message(*, to: str, body: str, reference: str,
                 send_at: datetime | None = None) -> MessageState:
    """Create a message (immediately, or scheduled at ``send_at``)."""
    client = get_client()
    options: Any = {'extra_headers': {'Idempotency-Key': reference}}
    if send_at is None:
        return read_message(call('create_message', lambda: client.api20100401_message.create_message(
            _account(), to, from_=settings.TWILIO_FROM_NUMBER, body=body,
            request_options=options), is_write=True))
    if not settings.TWILIO_MESSAGING_SERVICE_SID:
        raise ProviderRejected(
            503, 'Scheduling needs TWILIO_MESSAGING_SERVICE_SID.', outcome_unknown=False)
    return read_message(call('create_message', lambda: client.api20100401_message.create_message(
        _account(), to, from_=settings.TWILIO_FROM_NUMBER, body=body,
        messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
        schedule_type=MessageEnumScheduleType.FIXED, send_at=send_at,
        request_options=options), is_write=True))


def fetch_message(sid: str) -> MessageState:
    client = get_client()
    return read_message(call('fetch_message', lambda: client.api20100401_message.fetch_message(
        _account(), sid), is_write=False))


def cancel_message(sid: str) -> MessageState:
    client = get_client()
    return read_message(call('cancel_message', lambda: client.api20100401_message.update_message(
        _account(), sid, status=MessageEnumUpdateStatus.CANCELED), is_write=True))


def redact_message(sid: str) -> MessageState:
    client = get_client()
    return read_message(call('redact_message', lambda: client.api20100401_message.update_message(
        _account(), sid, body=''), is_write=True))


def _page_params(next_page_uri: str) -> tuple[str | None, int | None]:
    query = parse_qs(urlsplit(next_page_uri).query)
    token = query.get('PageToken', [None])[0]
    page = query.get('Page', [None])[0]
    return token, (int(page) if page and page.isdigit() else None)


def list_messages(*, to: str | None = None, sent_after: datetime | None = None,
                  sent_before: datetime | None = None, page_size: int = 1000,
                  max_pages: int | None = None) -> list[MessageState]:
    """Messages sent from this app's own number, following every page.

    ``max_pages=None`` reads the whole range; a cap stops early (used only for
    the recent-first lookup of one message).
    """
    client = get_client()
    results: list[MessageState] = []
    page_token: str | None = None
    page: int | None = None
    pages = 0
    while True:
        response = call('list_message', lambda: client.api20100401_message.list_message(
            _account(),
            to=to,
            from_=settings.TWILIO_FROM_NUMBER,
            date_sent_query_query=sent_after,
            date_sent_query=sent_before,
            page_size=page_size,
            page=page,
            page_token=page_token,
        ), is_write=False)
        messages = response.messages
        if isinstance(messages, UnsetType):
            raise ProviderUnavailable(502, 'SMS provider returned no message list.',
                                      outcome_unknown=False)
        for message in messages:
            try:
                results.append(read_message(message))
            except ProviderUnavailable:
                continue
        pages += 1
        next_uri = _text(response.next_page_uri)
        if not next_uri or (max_pages is not None and pages >= max_pages):
            return results
        page_token, page = _page_params(next_uri)
        if page_token is None:
            raise ProviderUnavailable(502, 'SMS provider returned an unreadable next page.',
                                      outcome_unknown=False)
