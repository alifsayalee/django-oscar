"""
The single boundary between this app and the Twilio SDK.

Everything that calls ``twilio_sdk`` lives here. SDK models never leave this
module: they are mapped into ``MessageSnapshot`` so ``UNSET`` never reaches the
database, a template or a JSON encoder.

Nothing here logs a phone number, a message body or a credential.
"""
import atexit
import base64
import hashlib
import logging
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from pydantic import ValidationError
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import ApiError, BasicAuthCredentials, RawError, RequestOptionsDict, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage, ListMessageResponse
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumUpdateStatus

from .errors import (
    InvalidRequest, NotificationError, OutcomeUnknown, ProviderError, ProviderNotConfigured,
    ProviderRejected)

logger = logging.getLogger('apps.sms_notifications')

# Failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

CLIENT_TIMEOUT_SECONDS = 10.0
FIND_PAGE_SIZE = 50
FIND_MAX_PAGES = 3
RECONCILE_PAGE_SIZE = 1000


@dataclass(frozen=True)
class MessageSnapshot:
    """What the provider told us about one message, with ``UNSET`` resolved to ``None``."""

    sid: str | None
    status: str | None
    body: str | None
    to: str | None
    from_number: str | None
    date_sent: datetime | None
    date_created: datetime | None
    error_code: int | None


class ProviderUnreadable(Exception):
    """A 2xx whose body we cannot read, or that lacks a member we depend on."""


def _value(v: object) -> object:
    return None if isinstance(v, UnsetType) else v


def _str_or_none(v: object) -> str | None:
    v = _value(v)
    return None if v is None else str(v)


def parse_provider_time(value: str | None) -> datetime | None:
    """Twilio message dates are RFC 2822 strings; accept ISO-8601 too. Unreadable -> None."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def snapshot(message: ApiV2010AccountMessage) -> MessageSnapshot:
    error_code = _value(message.error_code)
    return MessageSnapshot(
        sid=_str_or_none(message.sid),
        status=_str_or_none(message.status),
        body=_str_or_none(message.body),
        to=_str_or_none(message.to),
        from_number=_str_or_none(message.from_),
        date_sent=parse_provider_time(_str_or_none(message.date_sent)),
        date_created=parse_provider_time(_str_or_none(message.date_created)),
        error_code=error_code if isinstance(error_code, int) else None,
    )


def require_readable(message: ApiV2010AccountMessage) -> MessageSnapshot:
    """No member of the message model is required, so a truncated 2xx decodes cleanly: check it."""
    snap = snapshot(message)
    if not snap.sid or not snap.status:
        raise ProviderUnreadable('message response lacks sid or status')
    return snap


def reference_token(reference: str) -> str:
    """A short, stable token derived from the claim reference; embedded in every message body."""
    digest = hashlib.sha256(reference.encode()).digest()
    return base64.b32encode(digest).decode()[:10]


def with_reference(body: str, reference: str) -> str:
    return '%s Ref %s' % (body, reference_token(reference))


def idempotency_header(reference: str) -> RequestOptionsDict:
    # The SDK sends a random Idempotency-Key per call; send the same one for every attempt at a reference.
    return {'extra_headers': {'Idempotency-Key': str(uuid.uuid5(uuid.NAMESPACE_URL, reference))}}


def translate_provider_failure(exc: BaseException, *, is_write: bool) -> NotificationError:
    """One mapping from an SDK/transport failure to our error type, used at every call site."""
    if isinstance(exc, ApiError):
        status = exc.status_code
        detail = _provider_message(exc.error)
        if status in (401, 403):
            return ProviderError('The SMS provider refused our credentials or account (%s).' % detail)
        if status == 429:
            return ProviderError('The SMS provider is rate-limiting us.', status_code=503)
        if 400 <= status < 500:
            return ProviderRejected('The SMS provider rejected the request (%s).' % detail)
        return ProviderError('The SMS provider is unavailable.', outcome_unknown=is_write,
                             status_code=504 if is_write else 502)
    if isinstance(exc, NEVER_SENT):
        return ProviderError('Could not reach the SMS provider; nothing was sent.')
    if isinstance(exc, httpx.RequestError):
        return ProviderError('No response from the SMS provider.', status_code=504, outcome_unknown=is_write)
    if isinstance(exc, (ValidationError, ValueError, ProviderUnreadable)):
        return ProviderError('Unreadable response from the SMS provider.', status_code=504,
                             outcome_unknown=is_write)
    return ProviderError('Unexpected SMS provider failure.')


def _provider_message(error: object) -> str:
    """Provider error code for logs/responses - never the raw body (it can echo a phone number)."""
    if isinstance(error, RawError):
        try:
            data = error.json()
        except ValueError:
            return 'HTTP %s' % error.status_code
        if isinstance(data, dict) and 'code' in data:
            return 'HTTP %s, code %s' % (error.status_code, data['code'])
        return 'HTTP %s' % error.status_code
    return type(error).__name__


class TwilioGateway:
    def __init__(self, client: TwilioSdkClient, *, account_sid: str, from_number: str,
                 messaging_service_sid: str) -> None:
        self._client = client
        self.account_sid = account_sid
        self.from_number = from_number
        self.messaging_service_sid = messaging_service_sid

    def close(self) -> None:
        self._client.close()

    # -- Lookup (server default4; not governed by TWILIO_BASE_URL) --------------------------

    def canonical_number(self, raw_number: str) -> tuple[str, str]:
        """Return (E.164 number, ISO country) for a usable destination; raise InvalidRequest otherwise."""
        try:
            result = self._client.lookups_v2_phone_number.fetch_phone_number3(raw_number)
        except ApiError as e:
            if e.status_code in (400, 404):
                raise InvalidRequest('The SMS provider does not recognise that phone number.',
                                     status_code=422, code='invalid_phone_number') from e
            raise translate_provider_failure(e, is_write=False) from e
        except (httpx.RequestError, ValidationError, ValueError) as e:
            raise translate_provider_failure(e, is_write=False) from e
        valid = _value(result.valid)
        canonical = _str_or_none(result.phone_number)
        if valid is not True or not canonical:
            raise InvalidRequest('The SMS provider does not consider that number a usable destination.',
                                 status_code=422, code='invalid_phone_number')
        return canonical, _str_or_none(result.country_code) or ''

    # -- Messages (server default, overridable by TWILIO_BASE_URL) --------------------------

    def send_message(self, *, to: str, body: str, reference: str,
                     send_at: datetime | None = None) -> ApiV2010AccountMessage:
        """
        Create a message. SDK and transport exceptions propagate unchanged: the
        caller (``safe_write``) decides what each one means for the outcome.
        """
        messages = self._client.api20100401_message
        if send_at is None:
            return messages.create_message(
                self.account_sid, to, from_=self.from_number, body=body,
                request_options=idempotency_header(reference))
        # Scheduling is a Messaging Service feature; From pins the sender so the
        # message is attributable to TWILIO_FROM_NUMBER in reconciliation.
        return messages.create_message(
            self.account_sid, to, from_=self.from_number, body=body,
            messaging_service_sid=self.messaging_service_sid,
            schedule_type=MessageEnumScheduleType.FIXED, send_at=send_at,
            request_options=idempotency_header(reference))

    def fetch_message(self, sid: str) -> ApiV2010AccountMessage:
        return self._client.api20100401_message.fetch_message(self.account_sid, sid)

    def cancel_message(self, sid: str) -> ApiV2010AccountMessage:
        return self._client.api20100401_message.update_message(
            self.account_sid, sid, status=MessageEnumUpdateStatus.CANCELED)

    def redact_message(self, sid: str) -> ApiV2010AccountMessage:
        return self._client.api20100401_message.update_message(self.account_sid, sid, body='')

    def find_message_by_token(self, *, to: str, reference: str) -> ApiV2010AccountMessage | None:
        """
        Look up a message we may have created, by the reference token embedded in
        its body. Filtered by destination only (a scheduled message may not carry
        its sender yet); the token makes the match exact.
        """
        token = 'Ref %s' % reference_token(reference)
        page: int | None = None
        page_token: str | None = None
        for _ in range(FIND_MAX_PAGES):
            listing = self._client.api20100401_message.list_message(
                self.account_sid, to=to, page_size=FIND_PAGE_SIZE, page=page, page_token=page_token)
            for message in _messages(listing):
                body = _str_or_none(message.body)
                if body and body.endswith(token):
                    return message
            next_page = _next_page(listing.next_page_uri)
            if next_page is None:
                return None
            page, page_token = next_page
        return None

    def messages_sent_from_us(self, first_day: datetime, after_last_day: datetime) -> Iterator[MessageSnapshot]:
        """
        Every message the provider holds from TWILIO_FROM_NUMBER with a sent date
        on/after ``first_day`` and on/before ``after_last_day`` (whole GMT days -
        the filter's own granularity). Follows every page.
        """
        page: int | None = None
        page_token: str | None = None
        while True:
            listing = self._client.api20100401_message.list_message(
                self.account_sid, from_=self.from_number,
                date_sent_query_query=first_day,      # wire "DateSent>"
                date_sent_query=after_last_day,       # wire "DateSent<"
                page_size=RECONCILE_PAGE_SIZE, page=page, page_token=page_token)
            for message in _messages(listing):
                yield snapshot(message)
            next_page = _next_page(listing.next_page_uri)
            if next_page is None:
                return
            page, page_token = next_page


def _messages(listing: ListMessageResponse) -> list[ApiV2010AccountMessage]:
    messages = listing.messages
    return [] if isinstance(messages, UnsetType) else messages


def _next_page(next_page_uri: object) -> tuple[int | None, str | None] | None:
    uri = _str_or_none(next_page_uri)
    if not uri:
        return None
    query = parse_qs(urlsplit(uri).query)
    tokens = query.get('PageToken', [])
    pages = query.get('Page', [])
    token = tokens[0] if tokens else None
    page = int(pages[0]) if pages and pages[0].isdigit() else None
    if token is None and page is None:
        return None
    return page, token


# -- Process-wide client ---------------------------------------------------------------------

_lock = threading.Lock()
_gateway: TwilioGateway | None = None


def build_gateway() -> TwilioGateway:
    account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '') or ''
    auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '') or ''
    from_number = getattr(settings, 'TWILIO_FROM_NUMBER', '') or ''
    service_sid = getattr(settings, 'TWILIO_MESSAGING_SERVICE_SID', '') or ''
    if not (account_sid and auth_token and from_number and service_sid):
        raise ProviderNotConfigured('SMS provider credentials are not configured.')
    base_url = getattr(settings, 'TWILIO_BASE_URL', None)
    server_config: ServerConfigDict | None = None
    if base_url:
        # Governs the messaging API (server "default") only; Lookup keeps its own host.
        server_config = {'default': {'base_url': base_url}}
    client = TwilioSdkClient(
        server_config=server_config,
        timeout=CLIENT_TIMEOUT_SECONDS,
        account_sid_auth_token=BasicAuthCredentials(username=account_sid, password=auth_token),
    )
    return TwilioGateway(client, account_sid=account_sid, from_number=from_number,
                         messaging_service_sid=service_sid)


def get_gateway() -> TwilioGateway:
    """The process's gateway, built on first use (after any worker fork) and reused."""
    global _gateway
    if _gateway is None:
        with _lock:
            if _gateway is None:
                _gateway = build_gateway()
                atexit.register(_gateway.close)
    return _gateway


def set_gateway(gateway: TwilioGateway | None) -> None:
    """Replace the process gateway (tests inject one built on a stub transport)."""
    global _gateway
    with _lock:
        _gateway = gateway


__all__ = [
    'MessageSnapshot', 'NEVER_SENT', 'OutcomeUnknown', 'ProviderUnreadable', 'TwilioGateway',
    'get_gateway', 'set_gateway', 'snapshot', 'require_readable', 'reference_token', 'with_reference',
    'translate_provider_failure', 'parse_provider_time',
]
