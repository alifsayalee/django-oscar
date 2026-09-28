"""
The one module that talks to the Twilio SDK.

Everything the rest of the app needs from Twilio goes through
:class:`TwilioGateway`, which turns SDK models into plain records (no
``UNSET`` leaks out) and SDK/transport failures into :class:`ProviderError`
subclasses that say whether a write can have reached the provider.

Phone numbers are never logged from here: the logging transport records the
method, host and status only, because the lookup path and the message-list
query string both carry the shopper's number.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import ValidationError
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import (
    ApiError, HttpClient, HttpRequest, HttpResponse, HttpxClient, RawError, RequestOptionsDict, UnsetType)
from twilio_sdk.models import ApiV2010AccountMessage, ListMessageResponse, LookupResponse
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumStatus, MessageEnumUpdateStatus

logger = logging.getLogger('apps.order_notifications.twilio')

Outcome = Literal['done', 'pending', 'failed', 'needs_review', 'unknown']

# Failures raised before the request left this process: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Twilio returns at most 1000 messages per page; the cap bounds a runaway walk.
LIST_PAGE_SIZE = 1000
MAX_LIST_PAGES = 200
FIND_PAGE_SIZE = 50

# A zero-argument SDK call, so one error ladder serves every operation.
_Call = Callable[[], ApiV2010AccountMessage]
_ListCall = Callable[[], ListMessageResponse]


class ProviderError(Exception):
    """A provider call that did not produce a usable answer."""

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False,
                 provider_status: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code          # what our own API answers with
        self.message = message
        self.outcome_unknown = outcome_unknown  # the write may have landed
        self.provider_status = provider_status  # the provider's HTTP status, when it answered


class WriteNotSent(ProviderError):
    """The request never left: the write certainly did not happen."""


class WriteRejected(ProviderError):
    """The provider answered with a refusal: the write did not happen."""


class WriteOutcomeUnknown(ProviderError):
    """Sent, but no readable answer: the write may have happened."""


class InvalidNumber(Exception):
    """The provider does not consider the number a usable destination."""


@dataclass(frozen=True)
class MessageRecord:
    sid: str
    status: str | None                 # the provider's wire value, e.g. "delivered"
    body: str | None
    to: str | None
    from_number: str | None
    date_sent: datetime | None
    date_created: datetime | None
    error_code: int | None


@dataclass(frozen=True)
class LookupResult:
    phone_number: str
    country_code: str


def reference_token(reference: str) -> str:
    """The short, stable token carried in a message body so the message can be found by reference."""
    return hashlib.sha256(reference.encode('utf-8')).hexdigest()[:10]


def body_with_reference(text: str, reference: str) -> str:
    return '%s (ref %s)' % (text, reference_token(reference))


def status_from_provider(status: str | None) -> Outcome:
    """Outcome of a *send*: done only once the provider reports delivery."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return 'done'
        case (MessageEnumStatus.ACCEPTED | MessageEnumStatus.SCHEDULED | MessageEnumStatus.QUEUED
              | MessageEnumStatus.SENDING | MessageEnumStatus.SENT):
            return 'pending'
        case (MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED
              | MessageEnumStatus.PARTIALLY_DELIVERED | MessageEnumStatus.CANCELED):
            # canceled: it was queued and then called off - not in effect.
            return 'failed'
        case _:
            # receiving/received are inbound and never ours; anything else is newer than this code.
            return 'unknown'


def cancel_outcome(status: str | None) -> Outcome:
    """Outcome of calling a scheduled message off: done means it will never reach the shopper."""
    match status:
        case MessageEnumStatus.CANCELED | MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return 'done'
        case (MessageEnumStatus.SENT | MessageEnumStatus.DELIVERED | MessageEnumStatus.READ
              | MessageEnumStatus.SENDING | MessageEnumStatus.QUEUED | MessageEnumStatus.PARTIALLY_DELIVERED):
            return 'failed'     # too late: it went out
        case _:
            return 'unknown'    # still scheduled/accepted, or a state not listed: check again


def redact_outcome(body: str | None) -> Outcome:
    """Outcome of a redaction, from the body the provider echoes back."""
    if body is None:
        return 'unknown'
    return 'done' if body == '' else 'needs_review'


def is_final_send_status(status: str | None) -> bool:
    return status_from_provider(status) in ('done', 'failed')


def _parse_provider_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _str(value: str | None | UnsetType) -> str | None:
    return None if isinstance(value, UnsetType) else value


def _record(message: ApiV2010AccountMessage) -> MessageRecord:
    sid = _str(message.sid)
    if not sid:
        # A 2xx that names nothing: whatever happened cannot be identified.
        raise WriteOutcomeUnknown(504, 'The provider answered without a message identifier.',
                                  outcome_unknown=True)
    status = message.status
    error_code = message.error_code
    return MessageRecord(
        sid=sid,
        status=None if isinstance(status, UnsetType) else str(status),
        body=_str(message.body),
        to=_str(message.to),
        from_number=_str(message.from_),
        date_sent=_parse_provider_time(_str(message.date_sent)),
        date_created=_parse_provider_time(_str(message.date_created)),
        error_code=None if isinstance(error_code, UnsetType) else error_code,
    )


def _raw_detail(error: object) -> str:
    """A short, number-free description of a provider error body."""
    if isinstance(error, RawError):
        try:
            payload = error.json()
        except ValueError:
            return 'HTTP %s' % error.status_code
        if isinstance(payload, dict):
            return 'Twilio error %s' % payload.get('code', 'unknown')
    return 'provider error'


def _read_error(e: ApiError) -> ProviderError:
    """Map a provider refusal on a READ to our error."""
    status = e.status_code
    detail = _raw_detail(e.error)
    if status in (401, 403):
        return ProviderError(502, 'The messaging provider refused our credentials (%s).' % detail,
                             provider_status=status)
    if status == 429:
        return ProviderError(503, 'The messaging provider is rate-limiting us.', provider_status=status)
    if status == 404:
        return ProviderError(404, 'The provider has no such record.', provider_status=status)
    if 400 <= status < 500:
        return ProviderError(502, 'The messaging provider rejected the request (%s).' % detail,
                             provider_status=status)
    return ProviderError(502, 'The messaging provider is unavailable.', provider_status=status)


def _write_error(e: ApiError) -> ProviderError:
    """Map a provider answer on a WRITE: a 4xx is a refusal, a 5xx may still have landed."""
    status = e.status_code
    detail = _raw_detail(e.error)
    if status >= 500:
        return WriteOutcomeUnknown(504, 'The messaging provider failed mid-request.', outcome_unknown=True,
                                   provider_status=status)
    if status in (401, 403):
        return WriteRejected(502, 'The messaging provider refused our credentials (%s).' % detail,
                             provider_status=status)
    if status == 429:
        return WriteRejected(503, 'The messaging provider is rate-limiting us.', provider_status=status)
    return WriteRejected(502, 'The messaging provider rejected the message (%s).' % detail,
                         provider_status=status)


class LoggingTransport:
    """Delegates to the SDK's httpx transport and logs method, host, status and duration only."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        host = urlsplit(request.url).netloc
        try:
            response = self._inner.send(request)
        except httpx.HTTPError as e:
            logger.warning('twilio %s %s -> %s', request.method, host, type(e).__name__)
            raise
        logger.info('twilio %s %s -> %s (%.0f ms)', request.method, host, response.status_code,
                    (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(*, account_sid: str, auth_token: str, base_url: str | None, timeout: float,
                 transport: HttpClient | None = None) -> TwilioSdkClient:
    """Build the long-lived SDK client. ``base_url`` overrides the messaging API's host only."""
    if not account_sid or not auth_token:
        # An omitted credential would send every request unauthenticated.
        raise ValueError('TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must both be configured.')
    server_config: ServerConfigDict = {}
    if base_url:
        server_config['default'] = {'base_url': base_url}
    return TwilioSdkClient(
        server_config=server_config,
        timeout=timeout,
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=timeout)),
        account_sid_auth_token={'username': account_sid, 'password': auth_token},
    )


class TwilioGateway:
    def __init__(self, client: TwilioSdkClient, *, account_sid: str, from_number: str,
                 messaging_service_sid: str) -> None:
        self._client = client
        self._account_sid = account_sid
        self.from_number = from_number
        self.messaging_service_sid = messaging_service_sid

    # -- lookups ---------------------------------------------------------------------------------

    def lookup_number(self, raw_number: str) -> LookupResult:
        """Ask the provider whether ``raw_number`` is a usable destination, and for its canonical form."""
        try:
            result: LookupResponse = self._client.lookups_v2_phone_number.fetch_phone_number3(raw_number)
        except ApiError as e:
            if e.status_code in (400, 404):
                raise InvalidNumber() from e
            raise _read_error(e) from e
        except NEVER_SENT as e:
            raise ProviderError(502, 'Could not reach the messaging provider.') from e
        except httpx.RequestError as e:
            raise ProviderError(504, 'The messaging provider did not answer.') from e
        except ValueError as e:     # pydantic's ValidationError and a non-JSON body
            raise ProviderError(502, 'Unreadable answer from the messaging provider.') from e
        valid = result.valid
        phone_number = _str(result.phone_number)
        if valid is not True or not phone_number:
            raise InvalidNumber()
        return LookupResult(phone_number=phone_number, country_code=_str(result.country_code) or '')

    # -- writes (each raises WriteNotSent / WriteRejected / WriteOutcomeUnknown) --------------------

    def _write(self, call: _Call) -> MessageRecord:
        try:
            message = call()
        except ApiError as e:
            raise _write_error(e) from e
        except NEVER_SENT as e:
            raise WriteNotSent(502, 'Could not reach the messaging provider.') from e
        except httpx.RequestError as e:
            raise WriteOutcomeUnknown(504, 'The messaging provider did not answer.', outcome_unknown=True) from e
        except (ValidationError, ValueError) as e:
            raise WriteOutcomeUnknown(504, 'Unreadable answer from the messaging provider.',
                                      outcome_unknown=True) from e
        return _record(message)

    @staticmethod
    def _options(idempotency_key: str) -> RequestOptionsDict:
        # Replaces the SDK's per-call random key with one derived from our reference.
        return {'extra_headers': {'Idempotency-Key': idempotency_key}}

    def send_message(self, to: str, body: str, *, idempotency_key: str) -> MessageRecord:
        return self._write(lambda: self._client.api20100401_message.create_message(
            self._account_sid, to, from_=self.from_number, body=body,
            request_options=self._options(idempotency_key)))

    def schedule_message(self, to: str, body: str, send_at: datetime, *, idempotency_key: str) -> MessageRecord:
        if not self.messaging_service_sid:
            raise WriteRejected(502, 'Scheduling needs TWILIO_MESSAGING_SERVICE_SID to be configured.')
        return self._write(lambda: self._client.api20100401_message.create_message(
            self._account_sid, to, from_=self.from_number, body=body,
            messaging_service_sid=self.messaging_service_sid,
            schedule_type=MessageEnumScheduleType.FIXED, send_at=send_at,
            request_options=self._options(idempotency_key)))

    def cancel_message(self, sid: str, *, idempotency_key: str) -> MessageRecord:
        return self._write(lambda: self._client.api20100401_message.update_message(
            self._account_sid, sid, status=MessageEnumUpdateStatus.CANCELED,
            request_options=self._options(idempotency_key)))

    def redact_message(self, sid: str, *, idempotency_key: str) -> MessageRecord:
        # An empty body is how the provider redacts a message's text.
        return self._write(lambda: self._client.api20100401_message.update_message(
            self._account_sid, sid, body='', request_options=self._options(idempotency_key)))

    # -- reads -----------------------------------------------------------------------------------

    def _read(self, call: _Call) -> MessageRecord:
        try:
            message = call()
        except ApiError as e:
            raise _read_error(e) from e
        except httpx.RequestError as e:
            raise ProviderError(502, 'Could not read from the messaging provider.') from e
        except (ValidationError, ValueError) as e:
            raise ProviderError(502, 'Unreadable answer from the messaging provider.') from e
        return _record(message)

    def fetch_message(self, sid: str) -> MessageRecord:
        return self._read(lambda: self._client.api20100401_message.fetch_message(self._account_sid, sid))

    def _list_page(self, call: _ListCall) -> ListMessageResponse:
        try:
            return call()
        except ApiError as e:
            raise _read_error(e) from e
        except httpx.RequestError as e:
            raise ProviderError(502, 'Could not read from the messaging provider.') from e
        except (ValidationError, ValueError) as e:
            raise ProviderError(502, 'Unreadable answer from the messaging provider.') from e

    def find_by_reference(self, to: str, reference: str) -> MessageRecord | None:
        """The message carrying ``reference`` in its body, among the latest sent to ``to``; None if none yet."""
        token = '(ref %s)' % reference_token(reference)
        page = self._list_page(lambda: self._client.api20100401_message.list_message(
            self._account_sid, to=to, page_size=FIND_PAGE_SIZE))
        messages = [] if isinstance(page.messages, UnsetType) else page.messages
        for message in messages:
            body = _str(message.body)
            if body and token in body:
                return _record(message)
        return None

    def list_sent_messages(self, start: datetime, end: datetime) -> list[MessageRecord]:
        """Every message sent from our own number with a send date in [start, end), across all pages.

        The provider filters on whole days, so the request is widened to day boundaries and the
        answer narrowed back to the exact window on the provider's own ``date_sent``.
        """
        day_start = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = end.astimezone(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=0)
        records: list[MessageRecord] = []
        page_token: str | None = None
        page_number: int | None = None
        for _ in range(MAX_LIST_PAGES):
            token, number = page_token, page_number
            page = self._list_page(lambda: self._client.api20100401_message.list_message(
                self._account_sid,
                from_=self.from_number,
                date_sent_query_query=day_start,     # wire DateSent>
                date_sent_query=day_end,             # wire DateSent<
                page_size=LIST_PAGE_SIZE,
                page=number,
                page_token=token))
            messages = [] if isinstance(page.messages, UnsetType) else page.messages
            for message in messages:
                record = _record(message)
                if record.date_sent is not None and start <= record.date_sent < end:
                    records.append(record)
            next_uri = _str(page.next_page_uri)
            if not next_uri:
                return records
            query = parse_qs(urlsplit(next_uri).query)
            tokens = query.get('PageToken')
            if not tokens:
                raise ProviderError(502, 'The provider returned a next page without a page token.')
            page_token = tokens[0]
            pages = query.get('Page')
            page_number = int(pages[0]) if pages else None
        raise ProviderError(502, 'Too many pages of messages for one report; narrow the date range.')

