"""
The Twilio boundary: the only module in this app that talks to the provider.

Everything above it deals in ``MessageSnapshot`` / ``LookupResult`` and one
failure type, ``ProviderError``, so no SDK type or SDK exception leaks into
services or views. Phone numbers and message bodies are never logged here.
"""
from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from pydantic import ValidationError
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import ApiError, BasicAuthCredentials, HttpClient, RawError, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage, ListMessageResponse, LookupResponse
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumStatus, MessageEnumUpdateStatus

logger = logging.getLogger("apps.sms_notifications.gateway")

T = TypeVar("T")

# Statuses after which a message will not change again.
FINAL_STATUSES = frozenset({"delivered", "read", "failed", "undelivered", "canceled"})
DELIVERED_STATUSES = frozenset({"delivered", "read"})
FAILED_STATUSES = frozenset({"failed", "undelivered"})
IN_FLIGHT_STATUSES = frozenset({"accepted", "scheduled", "queued", "sending", "sent"})

# Safety bound on list pagination; exceeding it fails loudly rather than
# returning a silently partial report.
MAX_LIST_PAGES = 500
LIST_PAGE_SIZE = 1000
# How many pages of recent messages to scan when settling an unknown outcome.
SETTLE_SCAN_PAGES = 3


class ProviderError(Exception):
    """
    A provider call that did not produce a usable answer.

    ``status_code`` is the HTTP status this application should answer with.
    ``outcome_unknown`` means the write may have taken effect upstream.
    ``rejected`` means the provider definitively refused the request (a 4xx
    that is about the request, not about our credentials or quota).
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        rejected: bool = False,
        provider_status: int | None = None,
        provider_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.rejected = rejected
        self.provider_status = provider_status
        self.provider_code = provider_code


@dataclass(frozen=True)
class MessageSnapshot:
    sid: str
    status: str | None
    error_code: int | None
    body: str | None
    to: str | None
    from_: str | None
    date_created: datetime | None
    date_sent: datetime | None


@dataclass(frozen=True)
class LookupResult:
    valid: bool
    phone_number: str | None
    country_code: str | None
    national_format: str | None


# ---------------------------------------------------------------------------
# Client lifetime
# ---------------------------------------------------------------------------

_client: TwilioSdkClient | None = None
_transport_override: HttpClient | None = None
_lock = threading.Lock()


def _setting(name: str) -> str:
    value = getattr(settings, name, "") or ""
    return str(value).strip()


def _build_client() -> TwilioSdkClient:
    account_sid = _setting("TWILIO_ACCOUNT_SID")
    auth_token = _setting("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        # Never build an unauthenticated client: the SDK would happily send
        # requests without credentials.
        raise ProviderError(503, "SMS provider is not configured.")
    base_url = _setting("TWILIO_BASE_URL")
    return TwilioSdkClient(
        account_sid_auth_token=BasicAuthCredentials(username=account_sid, password=auth_token),
        # TWILIO_BASE_URL governs the messaging API only (server "default");
        # Lookups (server "default5") always uses the provider's own host.
        server_config={"default": {"base_url": base_url}} if base_url else None,
        timeout=float(getattr(settings, "SMS_HTTP_TIMEOUT", 10.0)),
        custom_http_client=_transport_override,
    )


def get_client() -> TwilioSdkClient:
    """The process-wide client, built lazily (so after any worker fork)."""
    global _client
    with _lock:
        if _client is None:
            _client = _build_client()
        return _client


def reset_client(transport: HttpClient | None = None) -> None:
    """Close the current client; the next call builds a new one (tests inject a stub transport)."""
    global _client, _transport_override
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
        _transport_override = transport


atexit.register(reset_client)


def _account_sid() -> str:
    return _setting("TWILIO_ACCOUNT_SID")


def _from_number() -> str:
    number = _setting("TWILIO_FROM_NUMBER")
    if not number:
        raise ProviderError(503, "SMS sending number is not configured.")
    return number


# ---------------------------------------------------------------------------
# Error boundary
# ---------------------------------------------------------------------------

def _provider_code(raw: RawError) -> int | None:
    try:
        payload = raw.json()
    except ValueError:
        return None
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, int) else None


def _call(operation: str, fn: Callable[[], T]) -> T:
    """Run one SDK call and translate every failure kind into ProviderError."""
    try:
        return fn()
    except ProviderError:
        raise
    except ApiError as e:
        status = e.status_code
        code = _provider_code(e.error) if isinstance(e.error, RawError) else None
        logger.warning("twilio %s failed: http=%s code=%s", operation, status, code)
        if status in (401, 403):
            raise ProviderError(502, "The SMS provider refused our credentials.",
                                provider_status=status, provider_code=code) from e
        if status == 429:
            raise ProviderError(503, "The SMS provider is rate-limiting us.",
                                provider_status=status, provider_code=code) from e
        if 400 <= status < 500:
            raise ProviderError(422, "The SMS provider rejected the request.", rejected=True,
                                provider_status=status, provider_code=code) from e
        raise ProviderError(502, "The SMS provider is unavailable.", outcome_unknown=True,
                            provider_status=status, provider_code=code) from e
    except (ValidationError, ValueError) as e:
        logger.warning("twilio %s returned an unreadable response", operation)
        raise ProviderError(502, "Unreadable response from the SMS provider.", outcome_unknown=True) from e
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError) as e:
        logger.warning("twilio %s never sent: %s", operation, type(e).__name__)
        raise ProviderError(502, "Could not reach the SMS provider.") from e
    except httpx.RequestError as e:
        logger.warning("twilio %s got no reply: %s", operation, type(e).__name__)
        raise ProviderError(504, "No response from the SMS provider.", outcome_unknown=True) from e


# ---------------------------------------------------------------------------
# Mapping SDK models onto our own types (UNSET never escapes this module)
# ---------------------------------------------------------------------------

def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def parse_provider_time(value: object) -> datetime | None:
    """Twilio message dates are RFC 2822 strings; accept ISO 8601 as a fallback."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _status(message: ApiV2010AccountMessage) -> str | None:
    status = message.status
    if isinstance(status, MessageEnumStatus):
        return status.value
    return status if isinstance(status, str) else None  # open enum: unknown values pass through


def _snapshot(message: ApiV2010AccountMessage, operation: str) -> MessageSnapshot:
    sid = _str(message.sid)
    if not sid:
        # A 2xx without an identifier: the write may have happened and we cannot name it.
        raise ProviderError(502, f"{operation}: provider reply carried no message id.", outcome_unknown=True)
    error_code = message.error_code
    return MessageSnapshot(
        sid=sid,
        status=_status(message),
        error_code=error_code if isinstance(error_code, int) else None,
        body=_str(message.body),
        to=_str(message.to),
        from_=_str(message.from_),
        date_created=parse_provider_time(message.date_created),
        date_sent=parse_provider_time(message.date_sent),
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def lookup_number(phone_number: str, country_code: str | None = None) -> LookupResult:
    """Ask the provider whether a number is a usable destination, and for its canonical form."""
    def run() -> LookupResponse:
        client = get_client()
        if country_code:
            return client.lookups_v2_phone_number.fetch_phone_number2(phone_number, country_code=country_code)
        return client.lookups_v2_phone_number.fetch_phone_number2(phone_number)

    result = _call("lookup", run)
    valid = result.valid
    if not isinstance(valid, bool):
        raise ProviderError(502, "Lookup reply did not say whether the number is valid.")
    canonical = _str(result.phone_number)
    if valid and not canonical:
        raise ProviderError(502, "Lookup reply carried no canonical number.")
    return LookupResult(
        valid=valid,
        phone_number=canonical,
        country_code=_str(result.country_code),
        national_format=_str(result.national_format),
    )


def send_message(to: str, body: str) -> MessageSnapshot:
    from_number = _from_number()
    message = _call("create_message", lambda: get_client().api20100401_message.create_message(
        _account_sid(), to, from_=from_number, body=body))
    return _snapshot(message, "create_message")


def schedule_message(to: str, body: str, send_at: datetime) -> MessageSnapshot:
    """Queue a message with the provider for later delivery (requires a Messaging Service)."""
    service_sid = _setting("TWILIO_MESSAGING_SERVICE_SID")
    if not service_sid:
        raise ProviderError(503, "Scheduling needs TWILIO_MESSAGING_SERVICE_SID.")
    if send_at.tzinfo is None:
        raise ValueError("send_at must be timezone-aware")
    from_number = _from_number()
    message = _call("create_message(scheduled)", lambda: get_client().api20100401_message.create_message(
        _account_sid(), to,
        from_=from_number,
        messaging_service_sid=service_sid,
        schedule_type=MessageEnumScheduleType.FIXED,
        send_at=send_at,
        body=body,
    ))
    return _snapshot(message, "create_message(scheduled)")


def fetch_message(sid: str) -> MessageSnapshot:
    message = _call("fetch_message", lambda: get_client().api20100401_message.fetch_message(_account_sid(), sid))
    return _snapshot(message, "fetch_message")


def cancel_message(sid: str) -> MessageSnapshot:
    message = _call("update_message(cancel)", lambda: get_client().api20100401_message.update_message(
        _account_sid(), sid, status=MessageEnumUpdateStatus.CANCELED))
    return _snapshot(message, "update_message(cancel)")


def redact_message(sid: str) -> MessageSnapshot:
    """Dispose of a message's text at the provider (an empty body redacts it)."""
    message = _call("update_message(redact)", lambda: get_client().api20100401_message.update_message(
        _account_sid(), sid, body=""))
    return _snapshot(message, "update_message(redact)")


def _next_page(next_page_uri: object) -> tuple[int, str] | None:
    if not isinstance(next_page_uri, str) or not next_page_uri:
        return None
    query = parse_qs(urlsplit(next_page_uri).query)
    tokens = query.get("PageToken")
    pages = query.get("Page")
    if not tokens or not pages:
        return None
    try:
        return int(pages[0]), tokens[0]
    except ValueError:
        return None


def _iter_messages(max_pages: int, *, to: str | None = None, sent_after: datetime | None = None,
                   sent_before: datetime | None = None, page_size: int = LIST_PAGE_SIZE,
                   fail_when_truncated: bool = True) -> Iterable[MessageSnapshot]:
    from_number = _from_number()
    account_sid = _account_sid()
    page: int | None = None
    token: str | None = None
    for _ in range(max_pages):
        def run(page: int | None = page, token: str | None = token) -> ListMessageResponse:
            # Unset filters stay at the SDK's own None default and are not sent.
            return get_client().api20100401_message.list_message(
                account_sid,
                from_=from_number,               # only this application's own sending number
                to=to,
                date_sent_query_query=sent_after,  # wire: DateSent>
                date_sent_query=sent_before,       # wire: DateSent<
                page_size=page_size,
                page=page,
                page_token=token,
            )

        response = _call("list_message", run)
        if not isinstance(response.messages, UnsetType):
            for message in response.messages:
                yield _snapshot(message, "list_message")
        following = _next_page(response.next_page_uri)
        if following is None:
            return
        page, token = following
    if fail_when_truncated:
        raise ProviderError(502, "Provider message list exceeded the pagination bound.")


def list_messages_sent(start: datetime, end: datetime) -> list[MessageSnapshot]:
    """
    Every message the provider records as sent from TWILIO_FROM_NUMBER between
    ``start`` and ``end``. The provider's DateSent filter may match whole days,
    so the query is padded by a day each side and trimmed here.
    """
    padded = _iter_messages(MAX_LIST_PAGES, sent_after=start - timedelta(days=1),
                            sent_before=end + timedelta(days=1))
    return [m for m in padded if m.date_sent is not None and start <= m.date_sent <= end]


def find_created_message(to: str, body: str, created_since: datetime) -> list[MessageSnapshot]:
    """
    Candidates for a create whose outcome is unknown: messages from our number
    to ``to`` with exactly ``body``, created at or after ``created_since``.
    """
    candidates = []
    for message in _iter_messages(SETTLE_SCAN_PAGES, to=to, page_size=50, fail_when_truncated=False):
        if message.body == body and message.date_created is not None and message.date_created >= created_since:
            candidates.append(message)
    return candidates
