"""
Every Twilio call this app makes, and the one place provider failures become
``ProviderError``.

All in-scope operations are "Case B": ``ApiError.error`` is always ``RawError``.
Nothing here logs a phone number or a message body.
"""
from dataclasses import dataclass, field
from typing import TypedDict, TypeVar
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from pydantic import ValidationError
from twilio_sdk.core import ApiError, RawError, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage, ListMessageResponse
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumUpdateStatus

from .twilio_client import get_client

# Failures raised before the request left: nothing can have happened.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# How many recent messages to a destination are searched for a reference token.
LOOKUP_PAGE_SIZE = 100
LOOKUP_MAX_PAGES = 3
RECONCILE_PAGE_SIZE = 1000


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False,
                 provider_status: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code          # what our API answers
        self.message = message
        self.outcome_unknown = outcome_unknown  # whether anything may have happened upstream
        self.provider_status = provider_status  # the provider's HTTP status, if one arrived


def provider_error(status: int, error: RawError) -> ProviderError:
    """Map a provider HTTP failure (status + RawError body) onto our boundary."""
    code: object = None
    try:
        body = error.json()
        if isinstance(body, dict):
            code = body.get('code')
    except ValueError:
        pass
    if status in (401, 403):
        return ProviderError(502, 'The messaging provider refused our credentials.',
                             provider_status=status)
    if status == 429:
        return ProviderError(503, 'The messaging provider is rate-limiting us.',
                             provider_status=status)
    if 400 <= status < 500:
        suffix = ' (provider error %s)' % code if code else ''
        return ProviderError(status if status in (400, 404, 409) else 422,
                             'The messaging provider rejected the request%s.' % suffix,
                             provider_status=status)
    return ProviderError(502, 'The messaging provider is unavailable.',
                         outcome_unknown=True, provider_status=status)


def translate(exc: BaseException) -> ProviderError:
    """Turn any failure of an SDK call into a ProviderError (reads and ladders)."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        return provider_error(exc.status_code, exc.error)
    if isinstance(exc, NEVER_SENT):
        return ProviderError(502, 'The messaging provider could not be reached.')
    if isinstance(exc, httpx.RequestError):
        return ProviderError(504, 'The messaging provider did not answer.', outcome_unknown=True)
    if isinstance(exc, (ValidationError, ValueError)):
        return ProviderError(502, 'The messaging provider sent an unreadable answer.',
                             outcome_unknown=True)
    raise exc


T = TypeVar('T')


def _set(value: T | UnsetType | None) -> T | None:
    """UNSET/None -> None, anything else unchanged (never hand UNSET out)."""
    return None if isinstance(value, UnsetType) else value


# ---------------------------------------------------------------- lookup

@dataclass
class LookupResult:
    valid: bool
    phone_number: str | None
    country_code: str | None
    validation_errors: list[str] = field(default_factory=list)


def lookup_number(raw_number: str, country_code: str | None = None) -> LookupResult:
    """
    Ask the provider whether a number is a usable destination (Lookup v2, on
    its own host). ``country_code`` is only needed for a national-format number.
    """
    try:
        result = get_client().lookups_v2_phone_number.fetch_phone_number3(
            raw_number, country_code=country_code.upper() if country_code else None)
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc
    valid = _set(result.valid)
    if valid is None:
        raise ProviderError(502, 'The messaging provider did not say whether the number is valid.')
    errors = _set(result.validation_errors) or []
    return LookupResult(
        valid=bool(valid),
        phone_number=_set(result.phone_number),
        country_code=_set(result.country_code),
        validation_errors=[str(e) for e in errors])


# ---------------------------------------------------------------- messages

def create_sms(to: str, body: str, send_at: datetime | None = None) -> ApiV2010AccountMessage:
    """Send (or, with ``send_at``, schedule) a message. Raises SDK errors unmapped."""
    messages = get_client().api20100401_message
    if send_at is None:
        return messages.create_message(
            settings.TWILIO_ACCOUNT_SID, to, from_=settings.TWILIO_FROM_NUMBER, body=body)
    # Scheduling is for Messaging Services only; From pins our own number so the
    # reconciliation's From filter covers scheduled messages too.
    return messages.create_message(
        settings.TWILIO_ACCOUNT_SID, to,
        from_=settings.TWILIO_FROM_NUMBER,
        messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
        schedule_type=MessageEnumScheduleType.FIXED,
        send_at=send_at,
        body=body)


def fetch_sms(sid: str) -> ApiV2010AccountMessage:
    return get_client().api20100401_message.fetch_message(settings.TWILIO_ACCOUNT_SID, sid)


def cancel_sms(sid: str) -> ApiV2010AccountMessage:
    return get_client().api20100401_message.update_message(
        settings.TWILIO_ACCOUNT_SID, sid, status=MessageEnumUpdateStatus.CANCELED)


def redact_sms(sid: str) -> ApiV2010AccountMessage:
    # An empty body is the documented way to redact a message's text.
    return get_client().api20100401_message.update_message(
        settings.TWILIO_ACCOUNT_SID, sid, body='')


class Paging(TypedDict):
    page_token: str
    page: int | None


def _next_page(response: ListMessageResponse) -> Paging | None:
    """The Page/PageToken the provider put in next_page_uri, or None at the end."""
    uri = _set(response.next_page_uri)
    if not uri:
        return None
    query = parse_qs(urlsplit(uri).query)
    token = next(iter(query.get('PageToken', [])), None)
    page = next(iter(query.get('Page', [])), None)
    if not token:
        return None
    return {'page_token': token, 'page': int(page) if page and page.isdigit() else None}


def find_sms_by_token(to: str, token: str) -> ApiV2010AccountMessage | None:
    """
    The message we sent to ``to`` whose text carries ``token``, or None.

    The Message resource has no metadata field, so the reference travels in
    the text. Only messages from our own number count.
    """
    client = get_client()
    paging: Paging | None = None
    for _ in range(LOOKUP_MAX_PAGES):
        response = client.api20100401_message.list_message(
            settings.TWILIO_ACCOUNT_SID, to=to, page_size=LOOKUP_PAGE_SIZE,
            page_token=paging['page_token'] if paging else None,
            page=paging['page'] if paging else None)
        for message in _set(response.messages) or []:
            if token in (_set(message.body) or '') and _set(message.from_) in (
                    settings.TWILIO_FROM_NUMBER, None):
                return message
        paging = _next_page(response)
        if paging is None:
            return None
    return None


def list_sent_from_our_number(start: datetime, end: datetime) -> list[ApiV2010AccountMessage]:
    """
    Every message the provider holds as sent from TWILIO_FROM_NUMBER with a
    sent date in [start, end). The provider filters by our number and by whole
    days; the day-widened answer is narrowed back to the exact instants here.
    """
    client = get_client()
    day_from = datetime.combine(start.astimezone(timezone.utc).date(), datetime.min.time(),
                                tzinfo=timezone.utc)
    day_to = datetime.combine(end.astimezone(timezone.utc).date() + timedelta(days=1),
                              datetime.min.time(), tzinfo=timezone.utc)
    messages: list[ApiV2010AccountMessage] = []
    seen_tokens: set[str] = set()
    paging: Paging | None = None
    while True:
        response = client.api20100401_message.list_message(
            settings.TWILIO_ACCOUNT_SID,
            from_=settings.TWILIO_FROM_NUMBER,
            date_sent_query_query=day_from,     # DateSent>
            date_sent_query=day_to,             # DateSent<
            page_size=RECONCILE_PAGE_SIZE,
            page_token=paging['page_token'] if paging else None,
            page=paging['page'] if paging else None)
        messages.extend(_set(response.messages) or [])
        paging = _next_page(response)
        if paging is None or paging['page_token'] in seen_tokens:
            break
        seen_tokens.add(paging['page_token'])
    in_window = []
    for message in messages:
        sent = parse_provider_time(_set(message.date_sent))
        if sent is not None and start <= sent < end:
            in_window.append(message)
    return in_window


def parse_provider_time(value: str | None) -> datetime | None:
    """The provider's RFC 2822 timestamps (GMT) as aware datetimes."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def message_time(message: ApiV2010AccountMessage) -> datetime | None:
    """The provider's clock for a message: when it was sent, else when it was created."""
    return (parse_provider_time(_set(message.date_sent))
            or parse_provider_time(_set(message.date_created)))


def mask_number(number: str | None) -> str | None:
    if not number:
        return number
    return '%s…%s' % (number[:3], number[-2:])
