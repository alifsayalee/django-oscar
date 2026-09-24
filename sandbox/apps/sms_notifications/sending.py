"""
Everything that talks to Twilio goes through this module.

Every message this application creates goes through ``safe_send``: the
``Notification`` row is claimed (inserted and committed, with a unique
``reference``) *before* the provider is called, the call carries that
reference, a lost answer is settled by looking the message up by the reference
token embedded in its text, and the stored outcome comes from the status the
provider reported - never from the fact that it answered.

Phone numbers and message text are never logged here.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import ApiError, RequestOptionsDict
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import (
    MessageEnumDirection, MessageEnumScheduleType, MessageEnumStatus, MessageEnumUpdateStatus)

from . import outcomes
from .errors import ProviderError, ProviderRejected, ProviderUnavailable, TwilioNotConfigured
from .models import Notification
from .twilio_client import get_client

logger = logging.getLogger(__name__)

# Failures raised before the request left: nothing can have happened upstream.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# A claim still marked "sending" after this long lost its sender (worker died,
# request killed); it is then settled by a lookup, never by a fresh send.
SEND_WINDOW = timedelta(minutes=2)

# How far back the lookup-by-reference searches a destination's messages.
FIND_PAGES = 3
FIND_PAGE_SIZE = 100

# Reconciliation paging.
RECONCILE_PAGE_SIZE = 1000
RECONCILE_MAX_PAGES = 500

OUTBOUND = (MessageEnumDirection.OUTBOUND_API, MessageEnumDirection.OUTBOUND_CALL,
            MessageEnumDirection.OUTBOUND_REPLY)
NOT_SENT_STATUSES = (MessageEnumStatus.SCHEDULED, MessageEnumStatus.CANCELED,
                     MessageEnumStatus.ACCEPTED, MessageEnumStatus.QUEUED)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def ref_token_for(reference: str) -> str:
    """Short token derived from the reference; it travels in the message text."""
    return hashlib.sha256(reference.encode()).hexdigest()[:10]


def message_text(base: str, reference: str) -> str:
    return '%s (ref %s)' % (base, ref_token_for(reference))


def base_text(body: str) -> str:
    """The message text without its reference suffix."""
    return body.rsplit(' (ref ', 1)[0]


def mask_number(number: str | None) -> str:
    if not number:
        return ''
    return number[:2] + '*' * max(len(number) - 4, 0) + number[-2:]


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_provider_time(value: object) -> datetime | None:
    text = _opt_str(value)
    if text is None:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def provider_time_of(message: ApiV2010AccountMessage) -> datetime | None:
    """The provider's clock for a message: when sent, else when created."""
    return _parse_provider_time(message.date_sent) or _parse_provider_time(message.date_created)


def _error_code(e: ApiError[Any]) -> int | None:
    try:
        body = e.error.json()
    except ValueError:
        return None
    code = body.get('code') if isinstance(body, dict) else None
    return code if isinstance(code, int) else None


def _messages(page: Any) -> list[ApiV2010AccountMessage]:
    messages = page.messages
    return messages if isinstance(messages, list) else []


def _next_page(page: Any) -> tuple[int | None, str | None] | None:
    """(Page, PageToken) of the next page, or None when this is the last."""
    uri = _opt_str(page.next_page_uri)
    if uri is None:
        return None
    query = parse_qs(urlsplit(uri).query)
    tokens = query.get('PageToken', [])
    numbers = query.get('Page', [])
    if not tokens:
        return None
    number = numbers[0] if numbers else ''
    return (int(number) if number.isdigit() else None), tokens[0]


def provider_failure(e: BaseException, action: str) -> ProviderError:
    """Translate a failed *read* into the status this app answers with."""
    if isinstance(e, ApiError):
        if e.status_code in (401, 403):
            logger.error('twilio refused our credentials during %s (HTTP %s)', action, e.status_code)
            return ProviderUnavailable(502, 'The messaging provider refused the request.')
        if e.status_code == 429:
            return ProviderUnavailable(503, 'The messaging provider is rate limiting us; try again later.')
        logger.warning('twilio error during %s (HTTP %s, code %s)', action, e.status_code, _error_code(e))
        return ProviderUnavailable(502, 'The messaging provider returned an error.')
    if isinstance(e, NEVER_SENT):
        return ProviderUnavailable(502, 'The messaging provider could not be reached.')
    if isinstance(e, httpx.RequestError):
        return ProviderUnavailable(504, 'The messaging provider did not answer in time.')
    if isinstance(e, TwilioNotConfigured):
        return ProviderUnavailable(503, 'Messaging is not configured.', code='not_configured')
    # ValueError / pydantic.ValidationError: an answer we could not read.
    logger.error('unreadable twilio response during %s: %s', action, type(e).__name__)
    return ProviderUnavailable(502, 'The messaging provider returned an unreadable answer.')


# ---------------------------------------------------------------------------
# The claim store
# ---------------------------------------------------------------------------

def try_claim(fields: dict[str, Any]) -> tuple[Notification, bool]:
    """
    Insert-or-fail on the unique ``reference``; committed immediately.

    Returns the row and whether this caller owns the claim. A released claim
    (failed, and the provider never created anything) may be taken again - by
    exactly one caller, via a conditional update.
    """
    reference = fields['reference']
    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                outcome=Notification.SENDING, claimed_at=timezone.now(),
                ref_token=ref_token_for(reference), **fields)
        return notification, True
    except IntegrityError:
        existing = Notification.objects.get(reference=reference)
    if existing.outcome == Notification.FAILED and not existing.provider_sid:
        with transaction.atomic():
            won = Notification.objects.filter(
                pk=existing.pk, outcome=Notification.FAILED, provider_sid='',
            ).update(outcome=Notification.SENDING, claimed_at=timezone.now(),
                     error_code=None, error_message='')
        existing.refresh_from_db()
        return existing, bool(won)
    return existing, False


def _complete(notification: Notification, outcome: str,
              message: ApiV2010AccountMessage | None = None, *,
              error_code: int | None = None, error_message: str = '') -> Notification:
    notification.outcome = outcome
    notification.last_checked_at = timezone.now()
    if message is not None:
        sid = _opt_str(message.sid)
        if sid:
            notification.provider_sid = sid
        notification.provider_status = outcomes.status_text(message.status)
        code = message.error_code
        notification.error_code = code if isinstance(code, int) else None
        notification.error_message = (_opt_str(message.error_message) or '')[:255]
        notification.provider_time = provider_time_of(message) or notification.provider_time
    else:
        notification.error_code = error_code
        notification.error_message = error_message[:255]
    notification.save()
    logger.info('notification %s -> %s (provider status %r)', notification.pk,
                notification.outcome, notification.provider_status)
    return notification


# ---------------------------------------------------------------------------
# Provider calls
# ---------------------------------------------------------------------------

def _create_message(client: TwilioSdkClient, notification: Notification) -> ApiV2010AccountMessage:
    headers: RequestOptionsDict = {'extra_headers': {'Idempotency-Key': notification.reference}}
    to = notification.contact.phone_number
    if notification.send_at is not None:
        # Queued with the provider for later; scheduling needs the Messaging
        # Service, and From pins the sender to this app's own number.
        return client.api20100401_message.create_message(
            settings.TWILIO_ACCOUNT_SID, to,
            schedule_type=MessageEnumScheduleType.FIXED,
            send_at=notification.send_at,
            messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
            from_=settings.TWILIO_FROM_NUMBER,
            body=notification.body,
            request_options=headers,
        )
    return client.api20100401_message.create_message(
        settings.TWILIO_ACCOUNT_SID, to,
        from_=settings.TWILIO_FROM_NUMBER,
        body=notification.body,
        request_options=headers,
    )


def find_by_ref(client: TwilioSdkClient, notification: Notification) -> ApiV2010AccountMessage | None:
    """
    The provider's message carrying this notification's reference token, or
    None. Searches this app's sender to this destination, newest first.
    None proves nothing: the caller keeps the outcome unknown.
    """
    page: int | None = None
    page_token: str | None = None
    for _ in range(FIND_PAGES):
        result = client.api20100401_message.list_message(
            settings.TWILIO_ACCOUNT_SID,
            to=notification.contact.phone_number,
            from_=settings.TWILIO_FROM_NUMBER,
            page_size=FIND_PAGE_SIZE,
            page=page,
            page_token=page_token,
        )
        for message in _messages(result):
            body = _opt_str(message.body) or ''
            if notification.ref_token in body:
                return message
        following = _next_page(result)
        if following is None:
            return None
        page, page_token = following
    return None


def _settle_by_lookup(client: TwilioSdkClient, notification: Notification) -> Notification:
    try:
        found = find_by_ref(client, notification)
    except (ApiError, httpx.RequestError, ValueError) as e:
        logger.warning('lookup for notification %s failed (%s); outcome stays unknown',
                       notification.pk, type(e).__name__)
        return _complete(notification, Notification.UNKNOWN,
                         error_message='Provider outcome unknown; lookup failed.')
    if found is None or not _opt_str(found.sid):
        # An empty lookup cannot prove the message was not created.
        return _complete(notification, Notification.UNKNOWN,
                         error_message='Provider outcome unknown; not found by reference yet.')
    return _complete(notification, outcomes.status_from_provider(found.status), found)


def safe_send(fields: dict[str, Any]) -> tuple[Notification, bool]:
    """
    Claim, call, check, complete. Never raises for a provider failure: the
    outcome is recorded on the returned notification instead.

    Returns (notification, claimed) - ``claimed`` is False when an earlier
    request already holds this reference and the answer comes from its record.
    """
    notification, claimed = try_claim(fields)
    if not claimed:
        if (notification.outcome == Notification.SENDING
                and notification.claimed_at > timezone.now() - SEND_WINDOW):
            return notification, False          # in flight elsewhere
        if notification.outcome not in (Notification.SENDING, Notification.UNKNOWN):
            return notification, False          # settled: answer from the record
        # Stale sender or unknown outcome: look it up, never send it again.
        try:
            return _settle_by_lookup(get_client(), notification), False
        except TwilioNotConfigured:
            return notification, False

    try:
        client = get_client()
    except TwilioNotConfigured:
        logger.error('twilio is not configured; notification %s not sent', notification.pk)
        return _complete(notification, Notification.FAILED,
                         error_message='Messaging is not configured.'), claimed
    return _first_send(client, notification), claimed


def _first_send(client: TwilioSdkClient, notification: Notification) -> Notification:
    """The one provider create for a claim this caller owns."""
    try:
        message = _create_message(client, notification)
    except NEVER_SENT:
        # Never left: nothing happened, the claim is released for a later attempt.
        return _complete(notification, Notification.FAILED,
                         error_message='Provider unreachable; message not sent.')
    except ApiError as e:
        if e.status_code < 500:
            # Refused - nothing was created (401/403/429 are ours, not the caller's).
            level = logging.ERROR if e.status_code in (401, 403) else logging.WARNING
            code = _error_code(e)
            logger.log(level, 'twilio refused notification %s (HTTP %s, code %s)',
                       notification.pk, e.status_code, code)
            return _complete(notification, Notification.FAILED, error_code=code,
                             error_message='Provider refused the message (HTTP %s).' % e.status_code)
        logger.warning('twilio HTTP %s for notification %s; checking by reference',
                       e.status_code, notification.pk)
        return _settle_by_lookup(client, notification)
    except (httpx.RequestError, ValueError) as e:
        # Sent, with no readable answer: it may have landed.
        logger.warning('no readable answer for notification %s (%s); checking by reference',
                       notification.pk, type(e).__name__)
        return _settle_by_lookup(client, notification)

    if not _opt_str(message.sid):
        # Accepted, but we cannot name what was created.
        return _settle_by_lookup(client, notification)
    return _complete(notification, outcomes.status_from_provider(message.status), message)


def refresh_notification(notification: Notification) -> bool:
    """
    Ask the provider what became of a message that is not settled yet.
    Returns False when the provider could not be asked (state kept as it was).
    """
    stale_sending = (notification.outcome == Notification.SENDING
                     and notification.claimed_at <= timezone.now() - SEND_WINDOW)
    needs_fetch = bool(notification.provider_sid) and notification.outcome in (
        Notification.PENDING, Notification.UNKNOWN)
    needs_lookup = not notification.provider_sid and (
        notification.outcome == Notification.UNKNOWN or stale_sending)
    if not (needs_fetch or needs_lookup):
        return True
    try:
        client = get_client()
    except TwilioNotConfigured:
        return False
    if needs_lookup:
        _settle_by_lookup(client, notification)
        return notification.outcome != Notification.UNKNOWN or bool(notification.provider_sid)
    try:
        message = client.api20100401_message.fetch_message(
            settings.TWILIO_ACCOUNT_SID, notification.provider_sid)
    except (ApiError, httpx.RequestError, ValueError) as e:
        logger.warning('refresh of notification %s failed (%s)', notification.pk, type(e).__name__)
        return False
    _complete(notification, outcomes.status_from_provider(message.status), message)
    return True


def cancel_scheduled(notification: Notification) -> str:
    """
    Call off a scheduled message so it never goes out. Safe to repeat.
    Returns and stores the cancel state (done / pending / failed / unknown).
    """
    def record(state: str) -> str:
        notification.cancel_state = state
        notification.save(update_fields=['cancel_state', 'updated_at'])
        logger.info('notification %s cancel -> %s', notification.pk, state)
        return state

    if notification.provider_status == MessageEnumStatus.CANCELED.value:
        return record(Notification.CANCEL_DONE)
    try:
        client = get_client()
    except TwilioNotConfigured:
        return record(Notification.CANCEL_UNKNOWN)

    if not notification.provider_sid:
        settled = _cancel_state_without_sid(client, notification)
        if settled is not None:
            return record(settled)

    message = _request_cancel(client, notification)
    if isinstance(message, str):
        return record(message)
    _complete(notification, outcomes.status_from_provider(message.status), message)
    return record(outcomes.cancel_outcome(message.status))


def _request_cancel(client: TwilioSdkClient, notification: Notification) -> ApiV2010AccountMessage | str:
    """The message after the cancel request, or the cancel state if it cannot be read."""
    try:
        return client.api20100401_message.update_message(
            settings.TWILIO_ACCOUNT_SID, notification.provider_sid,
            status=MessageEnumUpdateStatus.CANCELED)
    except NEVER_SENT:
        return Notification.CANCEL_PENDING                  # not attempted: try again
    except (ApiError, httpx.RequestError, ValueError) as e:
        # Refused (typically: already sent) or no readable answer - ask what
        # the message's state is now.
        logger.warning('cancel of notification %s: %s; re-reading the message',
                       notification.pk, type(e).__name__)
    try:
        return client.api20100401_message.fetch_message(
            settings.TWILIO_ACCOUNT_SID, notification.provider_sid)
    except (ApiError, httpx.RequestError, ValueError):
        return Notification.CANCEL_UNKNOWN


def _cancel_state_without_sid(client: TwilioSdkClient, notification: Notification) -> str | None:
    """
    For a message whose provider sid we do not know yet: the cancel state if
    that settles it, or None once the lookup has found the sid to cancel.
    """
    if notification.outcome == Notification.FAILED:
        return Notification.CANCEL_DONE                    # nothing was ever created
    if (notification.outcome == Notification.SENDING
            and notification.claimed_at > timezone.now() - SEND_WINDOW):
        # Its sender re-checks the order and the number once it has an answer.
        return Notification.CANCEL_PENDING
    _settle_by_lookup(client, notification)
    if not notification.provider_sid:
        return Notification.CANCEL_UNKNOWN
    if notification.provider_status == MessageEnumStatus.CANCELED.value:
        return Notification.CANCEL_DONE
    return None


def redact_content(notification: Notification) -> Notification:
    """
    Have the provider dispose of the message text, keeping the record that it
    was sent and what became of it. Raises ProviderError when not confirmed.
    """
    if not notification.provider_sid:
        raise ProviderRejected(409, 'The provider has no record of this message yet.',
                               code='not_sent')
    if notification.provider_status == MessageEnumStatus.SCHEDULED.value:
        raise ProviderRejected(409, 'The message is still scheduled; call it off first.',
                               code='still_scheduled')
    try:
        client = get_client()
    except TwilioNotConfigured as e:
        raise provider_failure(e, 'redaction')
    message = _request_redaction(client, notification)
    if message.body != '':
        raise ProviderUnavailable(504, 'The provider did not confirm the redaction; retry.',
                                  outcome_unknown=True, code='outcome_unknown')
    notification.body = ''
    notification.content_disposed_at = timezone.now()
    _complete(notification, outcomes.status_from_provider(message.status), message)
    return notification


def _request_redaction(client: TwilioSdkClient, notification: Notification) -> ApiV2010AccountMessage:
    """The message as the provider holds it after the redaction request."""
    try:
        return client.api20100401_message.update_message(
            settings.TWILIO_ACCOUNT_SID, notification.provider_sid, body='')
    except NEVER_SENT as e:
        raise provider_failure(e, 'redaction')
    except ApiError as e:
        if e.status_code in (400, 404, 409, 422):
            raise ProviderRejected(409, 'The provider refused to redact this message (code %s).'
                                   % _error_code(e), code='redaction_refused')
        if e.status_code < 500:
            raise provider_failure(e, 'redaction')
        return _reread_after_redaction(client, notification)     # may have landed
    except (httpx.RequestError, ValueError):
        return _reread_after_redaction(client, notification)     # may have landed


def _reread_after_redaction(client: TwilioSdkClient, notification: Notification) -> ApiV2010AccountMessage:
    try:
        return client.api20100401_message.fetch_message(
            settings.TWILIO_ACCOUNT_SID, notification.provider_sid)
    except (ApiError, httpx.RequestError, ValueError):
        raise ProviderUnavailable(504, 'The provider did not confirm the redaction; retry.',
                                  outcome_unknown=True, code='outcome_unknown')


# ---------------------------------------------------------------------------
# Phone number lookup
# ---------------------------------------------------------------------------

def lookup_number(raw: str) -> tuple[str, str]:
    """(canonical E.164 number, country code) as the provider sees it."""
    try:
        result = get_client().lookups_v1_phone_number_api.fetch_phone_number2(raw)
    except ApiError as e:
        if e.status_code in (400, 404):
            raise ProviderRejected(400, 'The provider does not recognise this as a usable phone number.',
                                   code='invalid_phone_number')
        raise provider_failure(e, 'number lookup')
    except (httpx.RequestError, ValueError, TwilioNotConfigured) as e:
        raise provider_failure(e, 'number lookup')
    canonical = _opt_str(result.phone_number)
    if canonical is None or not canonical.startswith('+'):
        raise ProviderUnavailable(502, 'The provider returned no canonical number.')
    return canonical, _opt_str(result.country_code) or ''


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

@dataclass
class ProviderRecord:
    sid: str
    status: str
    provider_time: datetime | None
    to: str
    error_code: int | None


@dataclass
class ReconciliationFetch:
    records: list[ProviderRecord] = field(default_factory=list)
    pages: int = 0


def fetch_provider_messages(start: datetime, end: datetime) -> ReconciliationFetch:
    """
    Every outbound message the provider holds from this app's sending number
    whose provider time falls in [start, end). The provider filters by sender
    and by whole days; the day widening is narrowed back here.
    """
    try:
        client = get_client()
    except TwilioNotConfigured as e:
        raise provider_failure(e, 'reconciliation')
    utc_start = start.astimezone(dt_timezone.utc)
    utc_end = end.astimezone(dt_timezone.utc)
    day_start = utc_start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = utc_end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    fetched = ReconciliationFetch()
    page: int | None = None
    page_token: str | None = None
    while True:
        if fetched.pages >= RECONCILE_MAX_PAGES:
            raise ProviderRejected(422, 'The range holds too many messages; narrow it.',
                                   code='range_too_large')
        try:
            result = client.api20100401_message.list_message(
                settings.TWILIO_ACCOUNT_SID,
                from_=settings.TWILIO_FROM_NUMBER,
                date_sent_query_query=day_start,     # DateSent>
                date_sent_query=day_end,             # DateSent<
                page_size=RECONCILE_PAGE_SIZE,
                page=page,
                page_token=page_token,
            )
        except (ApiError, httpx.RequestError, ValueError) as e:
            raise provider_failure(e, 'reconciliation')
        fetched.pages += 1
        fetched.records.extend(_records_in_window(_messages(result), start, end))
        following = _next_page(result)
        if following is None:
            return fetched
        page, page_token = following


def _records_in_window(messages: list[ApiV2010AccountMessage], start: datetime,
                       end: datetime) -> list[ProviderRecord]:
    records = []
    for message in messages:
        if message.direction not in OUTBOUND:
            continue        # the inbound leg of a message to one of our own numbers
        when = provider_time_of(message)
        sid = _opt_str(message.sid)
        if when is None or sid is None or not (start <= when < end):
            continue
        code = message.error_code
        records.append(ProviderRecord(
            sid=sid, status=outcomes.status_text(message.status), provider_time=when,
            to=_opt_str(message.to) or '', error_code=code if isinstance(code, int) else None))
    return records
