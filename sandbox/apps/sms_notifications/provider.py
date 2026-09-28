"""
Every Twilio call this app makes, and nothing else.

Signatures come from the contract sheet in ``twilio-sdk-plan.md``. Error
handling lives with the callers (``safe_write`` for writes, the services for
reads); nothing here catches.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from twilio_sdk.models import ApiV2010AccountMessage, LookupResponse
from twilio_sdk.models.enums import MessageEnumScheduleType, MessageEnumUpdateStatus

from .twilio_client import get_client


def _account_sid() -> str:
    return str(settings.TWILIO_ACCOUNT_SID)


def _from_number() -> str:
    return str(settings.TWILIO_FROM_NUMBER)


def lookup_number(phone_number: str, country_code: str | None = None) -> LookupResponse:
    # Basic lookup: validity and the canonical E.164 form.
    return get_client().lookups_v2_phone_number.fetch_phone_number3(
        phone_number, country_code=country_code
    )


def send_now(to: str, body: str) -> ApiV2010AccountMessage:
    return get_client().api20100401_message.create_message(
        _account_sid(), to, from_=_from_number(), body=body
    )


def send_scheduled(to: str, body: str, send_at: datetime) -> ApiV2010AccountMessage:
    # Scheduling is a Messaging Service feature; the sender stays this app's
    # own number so reconciliation by sender covers it.
    return get_client().api20100401_message.create_message(
        _account_sid(),
        to,
        from_=_from_number(),
        messaging_service_sid=str(settings.TWILIO_MESSAGING_SERVICE_SID),
        body=body,
        schedule_type=MessageEnumScheduleType.FIXED,
        send_at=send_at,
    )


def fetch_message(message_sid: str) -> ApiV2010AccountMessage:
    return get_client().api20100401_message.fetch_message(_account_sid(), message_sid)


def cancel_message(message_sid: str) -> ApiV2010AccountMessage:
    return get_client().api20100401_message.update_message(
        _account_sid(), message_sid, status=MessageEnumUpdateStatus.CANCELED
    )


def redact_message(message_sid: str) -> ApiV2010AccountMessage:
    # An empty body is the provider's redaction of the message text.
    return get_client().api20100401_message.update_message(
        _account_sid(), message_sid, body=""
    )


def find_message_by_code(to: str, code: str) -> ApiV2010AccountMessage | None:
    """The most recent messages to `to` whose text carries our reference code."""
    page = get_client().api20100401_message.list_message(
        _account_sid(), to=to, page_size=100
    )
    for message in page.messages or []:
        if isinstance(message.body, str) and code in message.body:
            return message
    return None


def messages_sent_from_us(
    on_or_after: datetime, on_or_before: datetime
) -> Iterator[ApiV2010AccountMessage]:
    """
    Every message sent from TWILIO_FROM_NUMBER with a sent date in the given
    (whole-day, inclusive) range - the filter is applied by the provider.
    """
    client = get_client()
    page_size: int | None = 1000
    page: int | None = None
    page_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        response = client.api20100401_message.list_message(
            _account_sid(),
            from_=_from_number(),
            date_sent_query_query=on_or_after,  # DateSent>
            date_sent_query=on_or_before,  # DateSent<
            page_size=page_size,
            page=page,
            page_token=page_token,
        )
        yield from response.messages or []
        next_uri = response.next_page_uri
        if not isinstance(next_uri, str) or not next_uri:
            return
        query = parse_qs(urlsplit(next_uri).query)
        tokens = query.get("PageToken")
        if not tokens or tokens[0] in seen_tokens:
            return
        page_token = tokens[0]
        seen_tokens.add(page_token)
        page = int(query["Page"][0]) if query.get("Page") else None
        page_size = int(query["PageSize"][0]) if query.get("PageSize") else page_size
