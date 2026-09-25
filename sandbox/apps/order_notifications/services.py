"""
Sending, tracking, calling off and disposing of SMS notifications.

Every message goes out through ``safe_send``: a ``Notification`` row keyed by a
unique reference is committed *before* Twilio is called (the claim), the call is
made once, and a call whose outcome is unknown is settled only by looking the
message up at Twilio by that reference — never by sending again.
"""
from __future__ import annotations

import hashlib
import logging
import re
from functools import partial
from datetime import timedelta
from typing import Callable, TypeVar

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import provider
from .models import ContactNumber, Notification

logger = logging.getLogger(__name__)

T = TypeVar('T')

# Longer than one provider call can take (timeout) plus our own bookkeeping.
SEND_WINDOW = timedelta(minutes=2)
# Don't ask Twilio about the same message more often than this on reads.
RECHECK_INTERVAL = timedelta(seconds=10)
# Recent pages scanned when looking a message up by its reference.
LOOKUP_PAGES = 3

_TAG = re.compile(r' \[ref [0-9a-f]{10}\]$')

ORDER_CANCELLED_STATUS = 'Cancelled'


class Conflict(Exception):
    """The request cannot be carried out in the notification's current state."""


# --------------------------------------------------------------------------
# References and message text
# --------------------------------------------------------------------------

def order_reference(order_id: int, kind: str) -> str:
    return f'{settings.SMS_REFERENCE_PREFIX}:order:{order_id}:{kind}'


def resend_reference(notification_id: int, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    return f'{settings.SMS_REFERENCE_PREFIX}:resend:{notification_id}:{digest}'


def reference_tag(reference: str) -> str:
    return hashlib.sha256(reference.encode()).hexdigest()[:10]


def tagged(text: str, reference: str) -> str:
    """Message text carrying the reference, so the message can be found at Twilio later."""
    return f'{text} [ref {reference_tag(reference)}]'


def untagged(body: str) -> str:
    return _TAG.sub('', body)


def order_message(kind: str, order_number: str) -> str:
    shop = getattr(settings, 'OSCAR_SHOP_NAME', 'Oscar')
    return {
        Notification.KIND_ORDER_PLACED:
            f'{shop}: thanks for your order {order_number}. We will text you when it ships.',
        Notification.KIND_ORDER_DISPATCHED:
            f'{shop}: good news, order {order_number} is on its way.',
        Notification.KIND_DELIVERY_FOLLOWUP:
            f'{shop}: how did the delivery of order {order_number} go? Reply and let us know.',
        Notification.KIND_ORDER_CANCELLED:
            f'{shop}: your order {order_number} has been cancelled.',
    }[kind]


def primary_number(user) -> ContactNumber | None:
    """The number a shopper is messaged at: their most recently registered one."""
    return ContactNumber.objects.active().filter(user=user).first()


def contained(what: str, fn: Callable[[], T]) -> T | None:
    """Run SMS work so that no failure of it can fail the operation around it."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - messaging must never fail the order operation
        logger.error('sms %s failed: %s', what, type(e).__name__, exc_info=settings.DEBUG)
        return None


# --------------------------------------------------------------------------
# Recording what the provider said
# --------------------------------------------------------------------------

def _record(notification: Notification, state: provider.MessageState) -> Notification:
    notification.provider_sid = state.sid
    notification.provider_status = state.status
    notification.outcome = state.outcome
    notification.provider_error_code = state.error_code
    notification.provider_error_message = state.error_message
    notification.provider_time = state.provider_time
    notification.last_checked_at = timezone.now()
    fields = ['provider_sid', 'provider_status', 'outcome', 'provider_error_code',
              'provider_error_message', 'provider_time', 'last_checked_at', 'updated_at']
    if notification.cancel_state in (Notification.CANCEL_PENDING, Notification.CANCEL_UNKNOWN):
        # An unsettled call-off: the message's status now says how it ended.
        notification.cancel_state = _cancel_state(state.cancel_outcome)
        fields.append('cancel_state')
    notification.save(update_fields=fields)
    return notification


def _set(notification: Notification, **values) -> Notification:
    for name, value in values.items():
        setattr(notification, name, value)
    notification.save(update_fields=[*values, 'updated_at'])
    return notification


# --------------------------------------------------------------------------
# The safe write
# --------------------------------------------------------------------------

def find_by_reference(notification: Notification) -> provider.MessageState | None:
    """Look a message up at Twilio by the reference tag carried in its text."""
    tag = f'[ref {reference_tag(notification.reference)}]'
    for state in provider.list_messages(to=notification.contact_number.phone_number,
                                        page_size=50, max_pages=LOOKUP_PAGES):
        if state.body and state.body.endswith(tag):
            return state
    return None


def _settle_by_lookup(notification: Notification) -> Notification:
    """The outcome of a send is unknown: only the provider's answer can settle it."""
    try:
        found = find_by_reference(notification)
    except provider.ProviderError:
        found = None
    if found is None:
        # Not found (yet) proves nothing: a timed-out send can still land.
        return _set(notification, outcome=Notification.OUTCOME_UNKNOWN,
                    last_checked_at=timezone.now())
    return _record(notification, found)


def _send_claimed(notification: Notification) -> Notification:
    try:
        state = provider.send_message(
            to=notification.contact_number.phone_number,
            body=notification.body,
            reference=notification.reference,
            send_at=notification.scheduled_for,
        )
    except provider.ProviderError as e:
        if e.outcome_unknown:
            return _settle_by_lookup(notification)
        # Never sent, or refused as invalid: nothing exists at the provider.
        return _set(notification, outcome=Notification.OUTCOME_FAILED,
                    provider_error_code=e.provider_code,
                    provider_error_message=e.message[:255],
                    last_checked_at=timezone.now())
    return _record(notification, state)


def safe_send(reference: str, *, order, contact: ContactNumber, kind: str, text: str,
              send_at=None, resend_of: Notification | None = None,
              idempotency_key: str = '') -> tuple[Notification, bool]:
    """Send one message at most once per reference.

    Returns the notification and whether this call made (or re-made) the send.
    """
    now = timezone.now()
    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                reference=reference, order=order, user=order.user, contact_number=contact,
                kind=kind, body=tagged(text, reference), resend_of=resend_of,
                idempotency_key=idempotency_key, outcome=Notification.OUTCOME_SENDING,
                claimed_at=now, scheduled_for=send_at)
    except IntegrityError:
        existing = Notification.objects.select_related('contact_number').get(reference=reference)
        if existing.outcome == Notification.OUTCOME_FAILED and not existing.provider_sid:
            # Nothing ever reached the provider: the claim may be taken again, atomically.
            retaken = Notification.objects.filter(
                pk=existing.pk, outcome=Notification.OUTCOME_FAILED, provider_sid__isnull=True,
            ).update(outcome=Notification.OUTCOME_SENDING, claimed_at=now, updated_at=now)
            if retaken:
                existing.refresh_from_db()
                return _send_claimed(existing), True
            existing.refresh_from_db()
            return existing, False
        stale = existing.outcome == Notification.OUTCOME_SENDING and existing.claimed_at <= now - SEND_WINDOW
        if existing.outcome == Notification.OUTCOME_UNKNOWN or stale:
            # Unresolved: look, never create.
            return _settle_by_lookup(existing), False
        return existing, False
    return _send_claimed(notification), True


def notify(order, kind: str, *, send_at=None) -> Notification | None:
    """Tell the order's shopper about an order event; no number on file, no message."""
    if order.user is None:
        return None
    contact = primary_number(order.user)
    if contact is None:
        return None
    notification, _ = safe_send(
        order_reference(order.pk, kind), order=order, contact=contact, kind=kind,
        text=order_message(kind, order.number), send_at=send_at)
    return notification


# --------------------------------------------------------------------------
# Keeping the provider's state current (there are no callbacks: we ask)
# --------------------------------------------------------------------------

def refresh(notification: Notification, *, force: bool = False) -> Notification:
    now = timezone.now()
    if not force and notification.last_checked_at and \
            notification.last_checked_at > now - RECHECK_INTERVAL:
        return notification
    if notification.provider_sid:
        if notification.outcome in (Notification.OUTCOME_PENDING, Notification.OUTCOME_UNKNOWN) \
                or notification.cancel_state in (Notification.CANCEL_PENDING,
                                                 Notification.CANCEL_UNKNOWN):
            try:
                return _record(notification, provider.fetch_message(notification.provider_sid))
            except provider.ProviderError:
                return notification
        return notification
    if notification.outcome == Notification.OUTCOME_UNKNOWN or (
            notification.outcome == Notification.OUTCOME_SENDING
            and notification.claimed_at <= now - SEND_WINDOW):
        return _settle_by_lookup(notification)
    return notification


def sweep(notifications) -> None:
    """Bring notifications up to date and call off follow-ups that must not go out."""
    for notification in notifications:
        contained(f'refresh #{notification.pk}', partial(refresh, notification))
        if notification.kind == Notification.KIND_DELIVERY_FOLLOWUP and \
                notification.order.status == ORDER_CANCELLED_STATUS:
            contained(f'call off #{notification.pk}', partial(call_off, notification))


# --------------------------------------------------------------------------
# Calling off a scheduled message
# --------------------------------------------------------------------------

def _cancel_state(outcome: str) -> str:
    return {
        provider.DONE: Notification.CANCEL_DONE,
        provider.PENDING: Notification.CANCEL_PENDING,
        provider.FAILED: Notification.CANCEL_TOO_LATE,
    }.get(outcome, Notification.CANCEL_UNKNOWN)


def call_off(notification: Notification) -> Notification:
    """Make sure a scheduled message never goes out."""
    if notification.cancel_state in (Notification.CANCEL_DONE, Notification.CANCEL_TOO_LATE):
        return notification
    if not notification.provider_sid:
        notification = refresh(notification, force=True)
        if not notification.provider_sid:
            if notification.outcome == Notification.OUTCOME_FAILED:
                # Never created at the provider: there is nothing that could go out.
                return _set(notification, cancel_state=Notification.CANCEL_DONE)
            # Still being sent, or unresolved: the sender / next sweep calls it off.
            return _set(notification, cancel_state=Notification.CANCEL_UNKNOWN)
    if notification.outcome in (Notification.OUTCOME_DONE, Notification.OUTCOME_FAILED) \
            and notification.provider_status != 'scheduled':
        # Already delivered / failed / canceled at the provider.
        settled = Notification.CANCEL_DONE if notification.provider_status == 'canceled' \
            else Notification.CANCEL_TOO_LATE
        return _set(notification, cancel_state=settled)

    now = timezone.now()
    claimed = Notification.objects.filter(pk=notification.pk).filter(
        Q(cancel_state__in=[Notification.CANCEL_NONE, Notification.CANCEL_PENDING,
                            Notification.CANCEL_UNKNOWN])
        | Q(cancel_state=Notification.CANCEL_REQUESTED, cancel_requested_at__lte=now - SEND_WINDOW)
    ).update(cancel_state=Notification.CANCEL_REQUESTED, cancel_requested_at=now, updated_at=now)
    notification.refresh_from_db()
    if not claimed:
        return notification

    sid = notification.provider_sid
    assert sid is not None
    try:
        state = provider.cancel_message(sid)
    except provider.ProviderError as e:
        # Refused (often: it already left the schedule) or no readable answer:
        # ask the provider what state the message is in now.
        try:
            state = provider.fetch_message(sid)
        except provider.ProviderError:
            return _set(notification, cancel_state=Notification.CANCEL_UNKNOWN
                        if e.outcome_unknown else Notification.CANCEL_PENDING)
    _record(notification, state)
    return _set(notification, cancel_state=_cancel_state(state.cancel_outcome))


def call_off_followups(order) -> None:
    for notification in order.sms_notifications.filter(
            kind=Notification.KIND_DELIVERY_FOLLOWUP).select_related('contact_number'):
        contained(f'call off #{notification.pk}', partial(call_off, notification))


def call_off_scheduled_to(contact: ContactNumber) -> None:
    """A removed number must not be sent anything again."""
    for notification in contact.notifications.filter(scheduled_for__isnull=False).exclude(
            cancel_state__in=[Notification.CANCEL_DONE, Notification.CANCEL_TOO_LATE]):
        contained(f'call off #{notification.pk}', partial(call_off, notification))


# --------------------------------------------------------------------------
# Operator actions
# --------------------------------------------------------------------------

def dispose_content(notification: Notification) -> Notification:
    """Remove the message text at the provider and here; keep the fact and fate of the message."""
    if notification.content_state == Notification.CONTENT_DISPOSED:
        return notification
    if not notification.provider_sid:
        notification = refresh(notification, force=True)
    if not notification.provider_sid:
        if notification.outcome != Notification.OUTCOME_FAILED:
            raise Conflict('The message has not been settled with the provider yet; try again later.')
        # Never created at the provider: only our own copy exists.
        return _set(notification, body='', content_state=Notification.CONTENT_DISPOSED,
                    content_disposed_at=timezone.now())

    now = timezone.now()
    claimed = Notification.objects.filter(pk=notification.pk).filter(
        Q(content_state__in=[Notification.CONTENT_RETAINED, Notification.CONTENT_UNKNOWN])
        | Q(content_state=Notification.CONTENT_DISPOSING, updated_at__lte=now - SEND_WINDOW)
    ).update(content_state=Notification.CONTENT_DISPOSING, updated_at=now)
    notification.refresh_from_db()
    if not claimed:
        if notification.content_state == Notification.CONTENT_DISPOSED:
            return notification
        raise Conflict('Disposal of this message is already in progress.')

    sid = notification.provider_sid
    assert sid is not None
    try:
        provider.redact_message(sid)
    except provider.ProviderRejected as e:
        _set(notification, content_state=Notification.CONTENT_RETAINED)
        raise Conflict(f'The provider refused to remove the text: {e.message}') from e
    except provider.ProviderError as e:
        if not e.outcome_unknown:
            _set(notification, content_state=Notification.CONTENT_RETAINED)
            raise
        # May have landed: the verification read below decides.
    # Verify with a fresh read: the provider must no longer return the text.
    try:
        state = provider.fetch_message(sid)
    except provider.ProviderError:
        _set(notification, content_state=Notification.CONTENT_UNKNOWN)
        raise
    if state.body != '':
        _set(notification, content_state=Notification.CONTENT_UNKNOWN)
        raise provider.ProviderUnavailable(
            502, 'The provider still returns the message text.', outcome_unknown=True)
    _record(notification, state)
    return _set(notification, body='', content_state=Notification.CONTENT_DISPOSED,
                content_disposed_at=timezone.now())


def resend(original: Notification, idempotency_key: str) -> tuple[Notification, bool]:
    """Send a message that did not reach the shopper again, once per idempotency key."""
    reference = resend_reference(original.pk, idempotency_key)
    existing = Notification.objects.filter(reference=reference).first()
    if existing is not None:
        # The same key again: answer from what that request did (the claim below decides races).
        return safe_send(reference, order=existing.order, contact=existing.contact_number,
                         kind=existing.kind, text=untagged(existing.body),
                         resend_of=original, idempotency_key=idempotency_key)
    original = refresh(original, force=True)
    if original.outcome != Notification.OUTCOME_FAILED:
        raise Conflict(f'Only a message that did not reach the shopper can be re-sent '
                       f'(this one is {original.outcome}).')
    if original.content_state != Notification.CONTENT_RETAINED or not original.body:
        raise Conflict('The content of this message has been disposed of.')
    if original.kind == Notification.KIND_DELIVERY_FOLLOWUP and \
            original.order.status == ORDER_CANCELLED_STATUS:
        raise Conflict('The order was cancelled; its delivery follow-up must not be sent.')
    contact = primary_number(original.user)
    if contact is None:
        raise Conflict('The shopper has no number on file.')
    return safe_send(reference, order=original.order, contact=contact, kind=original.kind,
                     text=untagged(original.body), resend_of=original,
                     idempotency_key=idempotency_key)
