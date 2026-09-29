"""
The one place this app talks to Twilio: the process-wide SDK client, the
calls it makes, and how their answers are read.
"""
import atexit
import email.utils
import logging
import threading
from datetime import datetime, timezone as dt_timezone
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import ValidationError
from twilio_sdk import ServerConfigDict, TwilioSdkClient
from twilio_sdk.core import ApiError, BasicAuthCredentials, HttpClient, RawError, RequestOptionsDict, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage, LookupResponse
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumStatus, MessageEnumUpdateStatus

from .models import Outcome

logger = logging.getLogger(__name__)

T = TypeVar('T')

# Transport failures raised before the request left: nothing can have reached Twilio.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# How far back the lookup-by-reference searches a destination's messages.
LOOKUP_PAGE_SIZE = 100
LOOKUP_MAX_PAGES = 3


class ProviderError(Exception):
    """
    A provider failure translated for our boundary: the status we answer
    with, and whether anything may have happened upstream.
    """

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False,
                 provider_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_code = provider_code


def _error_body(error: object) -> dict[str, Any]:
    if not isinstance(error, RawError):
        return {}
    try:
        body = error.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def provider_code(error: object) -> int | None:
    """Twilio's own error code from a ``RawError`` body, when it is JSON and carries one."""
    code = _error_body(error).get('code')
    return code if isinstance(code, int) else None


def provider_message(error: object) -> str:
    message = _error_body(error).get('message')
    return message if isinstance(message, str) else ''


def provider_error(status: int, error: object) -> ProviderError:
    """Map a provider error answer onto our boundary's status."""
    code = provider_code(error)
    if status in (401, 403):
        # Our credentials or our account: the caller did nothing wrong and cannot fix it.
        return ProviderError(502, 'The messaging provider refused our credentials.', provider_code=code)
    if status == 429:
        return ProviderError(503, 'The messaging provider is rate-limiting us.', provider_code=code)
    if 400 <= status < 500:
        return ProviderError(
            422, provider_message(error) or 'The messaging provider rejected the request.',
            provider_code=code)
    return ProviderError(502, 'The messaging provider is unavailable.', outcome_unknown=True, provider_code=code)


def translate(exc: Exception) -> ProviderError:
    """
    Translate a failure out of an SDK call into a ``ProviderError``. Used
    around reads; writes go through the safe write instead.
    """
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        return provider_error(exc.status_code, exc.error)
    if isinstance(exc, NEVER_SENT):
        return ProviderError(502, 'Could not reach the messaging provider.')
    if isinstance(exc, httpx.RequestError):
        return ProviderError(504, 'No response from the messaging provider.', outcome_unknown=True)
    if isinstance(exc, (ValidationError, ValueError)):
        return ProviderError(502, 'Unreadable response from the messaging provider.', outcome_unknown=True)
    raise exc


# Client lifetime
# ===============

_client: TwilioSdkClient | None = None
_client_lock = threading.Lock()


def build_client(transport: HttpClient | None = None) -> TwilioSdkClient:
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    if not account_sid or not auth_token:
        # Never build an unauthenticated client: every call would go out without credentials.
        raise ImproperlyConfigured('TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set.')
    server_config: ServerConfigDict = {}
    if settings.TWILIO_BASE_URL:
        # The messaging API (server "default") - sends, reads and reconciles messages.
        server_config['default'] = {'base_url': settings.TWILIO_BASE_URL}
    if settings.TWILIO_LOOKUPS_BASE_URL:
        server_config['default5'] = {'base_url': settings.TWILIO_LOOKUPS_BASE_URL}
    return TwilioSdkClient(
        server_config=server_config or None,
        timeout=settings.TWILIO_TIMEOUT_SECONDS,
        custom_http_client=transport,
        account_sid_auth_token=BasicAuthCredentials(username=account_sid, password=auth_token),
    )


def get_client() -> TwilioSdkClient:
    """
    The process-wide client, built on first use - so after any worker fork -
    and closed when the process exits.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: TwilioSdkClient | None) -> None:
    """Replace the process-wide client (tests hand in one with a fake transport)."""
    global _client
    with _client_lock:
        _client = client


def account_sid() -> str:
    return str(settings.TWILIO_ACCOUNT_SID)


# Reading answers
# ===============

def unset_to_none(value: T | UnsetType | None) -> T | None:
    return None if isinstance(value, UnsetType) else value


def parse_provider_time(value: str | None | UnsetType) -> datetime | None:
    """Twilio's RFC 2822 timestamps ("Tue, 29 Sep 2026 10:00:00 +0000") as aware datetimes."""
    text = unset_to_none(value)
    if not text:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def status_value(status: object) -> str:
    """The wire value of an open-enum status, or '' when absent."""
    if status is None or isinstance(status, UnsetType):
        return ''
    return status.value if isinstance(status, MessageEnumStatus) else str(status)


def status_from_provider(status: object) -> str:
    """What a message's status means for the send that created it."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return Outcome.DONE
        case (MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING | MessageEnumStatus.SENT
              | MessageEnumStatus.ACCEPTED | MessageEnumStatus.SCHEDULED):
            return Outcome.PENDING
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return Outcome.FAILED
        case MessageEnumStatus.CANCELED:
            # Accepted, then called off: it will never arrive.
            return Outcome.FAILED
        case MessageEnumStatus.PARTIALLY_DELIVERED:
            return Outcome.FAILED
        case _:
            # Inbound states, a value newer than this SDK, or no status at all.
            return Outcome.UNKNOWN


def cancel_outcome(status: object) -> str:
    """What a message's status means for calling it off."""
    match status:
        case MessageEnumStatus.CANCELED:
            return Outcome.DONE
        case MessageEnumStatus.QUEUED | MessageEnumStatus.SCHEDULED | MessageEnumStatus.ACCEPTED:
            return Outcome.PENDING
        case (MessageEnumStatus.SENDING | MessageEnumStatus.SENT | MessageEnumStatus.DELIVERED
              | MessageEnumStatus.READ | MessageEnumStatus.PARTIALLY_DELIVERED
              | MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED):
            # Too late: it went out, or was attempted.
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def redact_outcome(body: object) -> str:
    """What a message's body says about disposing of its content."""
    if isinstance(body, str) and body == '':
        return Outcome.DONE
    return Outcome.UNKNOWN


# Calls
# =====

def lookup_number(raw_number: str, country_code: str | None = None) -> LookupResponse:
    """
    Ask the provider whether ``raw_number`` is a usable destination. Raises
    ``ProviderError``.
    """
    try:
        return get_client().lookups_v2_phone_number.fetch_phone_number2(
            raw_number, country_code=country_code or None)
    except Exception as exc:
        raise translate(exc) from exc


def create_message(to: str, body: str, reference: str, *,
                   send_at: datetime | None = None) -> ApiV2010AccountMessage:
    client = get_client()
    # The same key on every attempt of this write (the SDK would otherwise send a random one).
    options: RequestOptionsDict = {'extra_headers': {'Idempotency-Key': reference}}
    messaging_service_sid = settings.TWILIO_MESSAGING_SERVICE_SID or None
    if send_at is not None:
        return client.api20100401_message.create_message(
            account_sid(), to, from_=settings.TWILIO_FROM_NUMBER,
            messaging_service_sid=messaging_service_sid, body=body,
            schedule_type=MessageEnumScheduleType.FIXED, send_at=send_at,
            request_options=options)
    return client.api20100401_message.create_message(
        account_sid(), to, from_=settings.TWILIO_FROM_NUMBER,
        messaging_service_sid=messaging_service_sid, body=body,
        request_options=options)


def fetch_message(sid: str) -> ApiV2010AccountMessage:
    return get_client().api20100401_message.fetch_message(account_sid(), sid)


def cancel_message(sid: str, reference: str) -> ApiV2010AccountMessage:
    return get_client().api20100401_message.update_message(
        account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED,
        request_options={'extra_headers': {'Idempotency-Key': reference}})


def redact_message(sid: str, reference: str) -> ApiV2010AccountMessage:
    return get_client().api20100401_message.update_message(
        account_sid(), sid, body='',
        request_options={'extra_headers': {'Idempotency-Key': reference}})


def page_token_of(next_page_uri: str | None | UnsetType) -> str | None:
    """The ``PageToken`` Twilio put on a list's next page, or None at the end."""
    uri = unset_to_none(next_page_uri)
    if not uri:
        return None
    tokens = parse_qs(urlsplit(uri).query).get('PageToken')
    return tokens[0] if tokens else None


def find_message_by_token(to: str, token: str) -> ApiV2010AccountMessage | None:
    """
    The message sent from our number to ``to`` whose body carries ``token``,
    or None. Searches newest-first over a bounded window.
    """
    client = get_client()
    page_token = None
    matches: list[ApiV2010AccountMessage] = []
    for _ in range(LOOKUP_MAX_PAGES):
        page = client.api20100401_message.list_message(
            account_sid(), to=to, from_=settings.TWILIO_FROM_NUMBER,
            page_size=LOOKUP_PAGE_SIZE, page_token=page_token)
        for message in unset_to_none(page.messages) or []:
            body = unset_to_none(message.body)
            if isinstance(body, str) and token in body:
                matches.append(message)
        page_token = page_token_of(page.next_page_uri)
        if matches or page_token is None:
            break
    if len(matches) > 1:
        logger.warning('Reference token %s matches %d provider messages', token, len(matches))
    # Newest first: the earliest one is the write this reference made.
    return matches[-1] if matches else None


def list_sent_messages(date_from: datetime, date_to: datetime) -> list[ApiV2010AccountMessage]:
    """
    Every message the provider records as sent from our own number
    (``TWILIO_FROM_NUMBER``) with a send date between ``date_from`` and
    ``date_to``. Follows every page.
    """
    client = get_client()
    messages: list[ApiV2010AccountMessage] = []
    page_token = None
    for _ in range(settings.SMS_RECONCILIATION_MAX_PAGES):
        page = client.api20100401_message.list_message(
            account_sid(), from_=settings.TWILIO_FROM_NUMBER,
            date_sent_query_query=date_from,   # DateSent>  (on or after)
            date_sent_query=date_to,           # DateSent<  (on or before)
            page_size=1000, page_token=page_token)
        messages.extend(unset_to_none(page.messages) or [])
        page_token = page_token_of(page.next_page_uri)
        if page_token is None:
            return messages
    raise ProviderError(502, 'The reconciliation range holds more messages than one report may cover.')


def lookup_is_usable(result: LookupResponse) -> bool:
    return result.valid is True and bool(unset_to_none(result.phone_number))
