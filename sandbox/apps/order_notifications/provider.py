"""
The only module that calls the Twilio SDK.

Everything leaving this module is one of our own types: SDK models (and their
UNSET sentinels) never cross it, and every SDK failure is translated, in one
place, into a ``ProviderError`` that says what the caller should answer and
whether anything may have happened at the provider.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from pydantic import ValidationError
from twilio_sdk.core import ApiError, RawError, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import (
    MessageEnumDirection,
    MessageEnumScheduleType,
    MessageEnumStatus,
    MessageEnumUpdateStatus,
)

from .twilio_client import get_client

logger = logging.getLogger("apps.order_notifications.provider")

T = TypeVar("T")

# Failures raised before the request left this process: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Our own delivery outcomes.
DELIVERED = "delivered"
SENT = "sent"
PENDING = "pending"
SCHEDULED = "scheduled"
FAILED = "failed"
CANCELED = "canceled"
UNKNOWN = "unknown"

OUTBOUND_DIRECTIONS = (
    MessageEnumDirection.OUTBOUND_API,
    MessageEnumDirection.OUTBOUND_CALL,
    MessageEnumDirection.OUTBOUND_REPLY,
)


class ProviderError(Exception):
    """Base for every provider failure. ``http_status`` is what our API should
    answer; ``outcome_unknown`` says whether the provider may have acted."""

    def __init__(self, http_status: int, message: str, *, outcome_unknown: bool,
                 provider_status: int | None = None, provider_code: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.provider_code = provider_code


class ProviderRejected(ProviderError):
    """The provider refused the request (a caller-attributable 4xx). Nothing happened."""


class ProviderConfigError(ProviderError):
    """Our credentials/permissions were refused. Nothing happened."""


class ProviderUnavailable(ProviderError):
    """Outage, throttling, transport failure or an unreadable answer."""


def outcome_from_status(status: object) -> str:
    """The one place a provider message status becomes one of ours."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DELIVERED
        case MessageEnumStatus.SENT:
            return SENT
        case MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING | MessageEnumStatus.ACCEPTED:
            return PENDING
        case MessageEnumStatus.SCHEDULED:
            return SCHEDULED
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return FAILED
        case MessageEnumStatus.CANCELED:
            return CANCELED
        case (MessageEnumStatus.PARTIALLY_DELIVERED | MessageEnumStatus.RECEIVING
              | MessageEnumStatus.RECEIVED):
            return UNKNOWN
        case _:
            return UNKNOWN


def _set(value: T | None | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _parse_provider_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MessageState:
    sid: str
    provider_status: str
    outcome: str
    error_code: int | None
    error_message: str | None
    date_sent: datetime | None
    date_created: datetime | None
    body: str | None
    direction: str | None
    to: str | None  # kept in memory for matching only; never logged or stored beyond the contact row

    @property
    def is_outbound(self) -> bool:
        return self.direction in OUTBOUND_DIRECTIONS


@dataclass(frozen=True)
class LookupResult:
    e164: str
    country_code: str | None


@dataclass(frozen=True)
class MessagePage:
    messages: list[MessageState]
    truncated: bool
    pages: int


def _message_state(message: ApiV2010AccountMessage) -> MessageState:
    sid = _set(message.sid)
    if not isinstance(sid, str) or not sid:
        # Accepted or not, we cannot name what (if anything) was created.
        raise ProviderUnavailable(502, "Provider answer carried no message id.", outcome_unknown=True)
    status = _set(message.status)
    return MessageState(
        sid=sid,
        provider_status=str(status) if status is not None else "",
        outcome=outcome_from_status(status),
        error_code=_set(message.error_code),
        error_message=_set(message.error_message),
        date_sent=_parse_provider_time(_set(message.date_sent)),
        date_created=_parse_provider_time(_set(message.date_created)),
        body=_set(message.body),
        direction=str(d) if (d := _set(message.direction)) is not None else None,
        to=_set(message.to),
    )


def _provider_code(error: object) -> int | None:
    """The provider's numeric error code. Its message text is deliberately not
    kept: it can quote the destination number."""
    if isinstance(error, RawError):
        try:
            data = error.json()
        except ValueError:
            return None
        if isinstance(data, dict):
            code = data.get("code")
            return code if isinstance(code, int) else None
    return None


def _call(operation: str, fn: Callable[[], T], *, write: bool) -> T:
    """Run one SDK call and translate every failure kind. ``write`` marks calls
    whose effect may have landed even when no readable answer came back."""
    try:
        return fn()
    except ApiError as e:
        code = _provider_code(e.error)
        logger.warning("twilio %s rejected: HTTP %s code %s", operation, e.status_code, code)
        if e.status_code in (401, 403):
            raise ProviderConfigError(502, "Messaging provider refused our credentials.",
                                      outcome_unknown=False, provider_status=e.status_code,
                                      provider_code=code) from e
        if e.status_code == 429:
            raise ProviderUnavailable(503, "Messaging provider is throttling requests.",
                                      outcome_unknown=False, provider_status=429, provider_code=code) from e
        if e.status_code in (400, 404, 409, 422):
            raise ProviderRejected(e.status_code, f"Provider rejected the request (code {code}).", outcome_unknown=False,
                                   provider_status=e.status_code, provider_code=code) from e
        raise ProviderUnavailable(502, "Messaging provider failed.",
                                  outcome_unknown=write and e.status_code >= 500,
                                  provider_status=e.status_code, provider_code=code) from e
    except (ValidationError, ValueError) as e:  # a body that did not decode
        logger.error("twilio %s: unreadable provider response (%s)", operation, type(e).__name__)
        raise ProviderUnavailable(502, "Messaging provider answer was unreadable.",
                                  outcome_unknown=write) from e
    except NEVER_SENT as e:
        logger.warning("twilio %s: never sent (%s)", operation, type(e).__name__)
        raise ProviderUnavailable(502, "Messaging provider unreachable.", outcome_unknown=False) from e
    except httpx.RequestError as e:
        logger.warning("twilio %s: no response (%s)", operation, type(e).__name__)
        raise ProviderUnavailable(504, "Messaging provider did not answer.", outcome_unknown=write) from e


def lookup_number(raw: str) -> LookupResult:
    """Ask the provider whether ``raw`` is a usable phone number and for its
    canonical (E.164) form. A 404 from the provider means it is not."""
    client = get_client()
    result = _call("lookup", lambda: client.lookups_v1_phone_number_api.fetch_phone_number2(raw), write=False)
    e164 = _set(result.phone_number)
    if not isinstance(e164, str) or not e164:
        raise ProviderUnavailable(502, "Provider lookup answer carried no number.", outcome_unknown=False)
    return LookupResult(e164=e164, country_code=_set(result.country_code))


def send_sms(to: str, body: str, *, send_at: datetime | None = None) -> MessageState:
    """Create a message from TWILIO_FROM_NUMBER. With ``send_at`` the message
    is queued with the provider (scheduled through the messaging service)."""
    client = get_client()
    account = settings.TWILIO_ACCOUNT_SID
    if send_at is None:
        message = _call("create_message", lambda: client.api20100401_message.create_message(
            account, to, from_=settings.TWILIO_FROM_NUMBER, body=body), write=True)
    else:
        message = _call("create_message(scheduled)", lambda: client.api20100401_message.create_message(
            account, to,
            from_=settings.TWILIO_FROM_NUMBER,
            messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=send_at,
            body=body), write=True)
    return _message_state(message)


def fetch_message(sid: str) -> MessageState:
    client = get_client()
    message = _call("fetch_message", lambda: client.api20100401_message.fetch_message(
        settings.TWILIO_ACCOUNT_SID, sid), write=False)
    return _message_state(message)


def cancel_scheduled(sid: str) -> MessageState:
    client = get_client()
    message = _call("cancel_message", lambda: client.api20100401_message.update_message(
        settings.TWILIO_ACCOUNT_SID, sid, status=MessageEnumUpdateStatus.CANCELED), write=True)
    return _message_state(message)


def redact_body(sid: str) -> MessageState:
    client = get_client()
    message = _call("redact_message", lambda: client.api20100401_message.update_message(
        settings.TWILIO_ACCOUNT_SID, sid, body=""), write=True)
    return _message_state(message)


def _next_page_params(next_page_uri: str | None) -> tuple[str, int] | None:
    if not next_page_uri:
        return None
    query = parse_qs(urlsplit(next_page_uri).query)
    token = query.get("PageToken", [None])[0]
    page = query.get("Page", [None])[0]
    if not token or page is None or not page.isdigit():
        return None
    return token, int(page)


def list_sent_from(from_number: str, start: datetime, end: datetime, *,
                   page_size: int = 100, max_pages: int = 50) -> MessagePage:
    """Every message the provider holds from ``from_number`` with a send date
    inside the provider's (inclusive, possibly coarse) ``DateSent`` filter.
    Callers narrow to their exact window. Bounded; ``truncated`` says whether
    the cap, rather than the provider, ended the walk."""
    client = get_client()
    account = settings.TWILIO_ACCOUNT_SID
    collected: list[MessageState] = []
    cursor: tuple[str, int] | None = None
    seen_tokens: set[str] = set()
    pages = 0
    truncated = False
    while True:
        if pages >= max_pages:
            truncated = True
            break
        page_cursor = cursor

        result = _call("list_message", lambda: client.api20100401_message.list_message(
            account,
            from_=from_number,
            date_sent_query_query=start,
            date_sent_query=end,
            page_size=page_size,
            page_token=page_cursor[0] if page_cursor else None,
            page=page_cursor[1] if page_cursor else None,
        ), write=False)
        pages += 1
        messages = _set(result.messages) or []
        collected.extend(_message_state(m) for m in messages)
        cursor = _next_page_params(_set(result.next_page_uri))
        if cursor is None:
            break
        if cursor[0] in seen_tokens:  # no progress: the provider repeated a cursor
            truncated = True
            logger.error("twilio list_message: page token repeated; stopping")
            break
        seen_tokens.add(cursor[0])
    if truncated:
        logger.warning("twilio list_message: walk truncated after %d pages", pages)
    return MessagePage(messages=collected, truncated=truncated, pages=pages)


__all__ = [
    "CANCELED", "DELIVERED", "FAILED", "PENDING", "SCHEDULED", "SENT", "UNKNOWN",
    "LookupResult", "MessagePage", "MessageState", "ProviderConfigError", "ProviderError",
    "ProviderRejected", "ProviderUnavailable", "cancel_scheduled", "fetch_message", "list_sent_from",
    "lookup_number", "outcome_from_status", "redact_body", "send_sms",
]
