"""
Sending, tracking, calling off and disposing of order text messages.

Every provider write that *sends* goes through ``_safe_send``: the ``Notification`` row is claimed
(inserted under a unique, operation-derived ``reference``) and committed before the provider is
called, so a double request can never send twice, and a send whose answer was lost is settled by
looking the message up at the provider by the reference tag it carries - never by sending again.

None of the functions used by the order flow raise for a messaging problem: a message that cannot
be sent must never fail the order operation it belongs to. They record what happened instead.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from datetime import datetime, timedelta

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_model
from twilio_sdk.core import ApiError

from . import gateway
from .gateway import MessageState, ProviderError
from .models import ContactNumber, Notification

log = logging.getLogger('apps.order_sms')

Order = get_model('order', 'Order')

# A claim still "sending" after this long has lost its sender (crash, timeout); it is then settled
# by the lookup, never by a fresh send. Longer than one provider timeout plus the lookup.
SEND_WINDOW = timedelta(minutes=2)
# Minimum spacing between re-reads of one message's state on the read endpoints.
RECHECK_AFTER = timedelta(seconds=10)

_TAG_RE = re.compile(r' Ref [A-Z2-7]{8}$')


class NotificationConflict(Exception):
    """The requested action does not apply to this notification in its current state."""


# --------------------------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------------------------

def order_reference(order_pk: int, kind: str) -> str:
    return f'{settings.ORDER_SMS_INSTALL_ID}:order:{order_pk}:{kind}'


def resend_reference(source_pk: int, key_hash: str) -> str:
    return f'{settings.ORDER_SMS_INSTALL_ID}:resend:{source_pk}:{key_hash}'


def hash_key(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode()).hexdigest()


def reference_tag(reference: str) -> str:
    """Short, stable tag derived from the claim reference; travels in the message body."""
    digest = hashlib.sha256(reference.encode()).digest()
    return base64.b32encode(digest).decode()[:8]


def tagged(text: str, reference: str) -> str:
    return f'{text} Ref {reference_tag(reference)}'


def untagged(body: str) -> str:
    return _TAG_RE.sub('', body)


# --------------------------------------------------------------------------------------------
# The claim store
# --------------------------------------------------------------------------------------------

def _try_claim(reference: str, **fields: object) -> tuple[Notification, bool]:
    """Insert-or-fail. Returns (row, True) for the one caller that holds the claim."""
    now = timezone.now()
    try:
        with transaction.atomic():
            return Notification.objects.create(
                reference=reference, outcome=Notification.SENDING, claimed_at=now, **fields), True
    except IntegrityError:
        pass
    existing = Notification.objects.get(reference=reference)
    if existing.outcome == Notification.FAILED and existing.message_sid is None:
        # Nothing exists at the provider for this reference: the claim is free to take again,
        # just as atomically - the conditional UPDATE's row count decides the winner.
        taken = Notification.objects.filter(
            pk=existing.pk, outcome=Notification.FAILED, message_sid__isnull=True,
        ).update(outcome=Notification.SENDING, claimed_at=now, provider_status='',
                 error_code=None, error_detail='')
        if taken:
            existing.refresh_from_db()
            return existing, True
        existing.refresh_from_db()
    return existing, False


def _complete(n: Notification, state: MessageState | None = None, *, outcome: str | None = None,
              error_code: int | None = None, detail: str = '') -> Notification:
    """Record what the provider said (or, with no state, the known outcome) on the claim."""
    now = timezone.now()
    if state is not None:
        n.outcome = gateway.status_from_provider(state.status)
        n.provider_status = state.status
        n.message_sid = state.sid or n.message_sid
        n.provider_date_created = state.date_created or n.provider_date_created
        n.provider_date_sent = state.date_sent or n.provider_date_sent
        n.error_code = state.error_code
        n.error_detail = gateway.scrub(state.error_message)
    if outcome is not None:
        n.outcome = outcome
    if error_code is not None:
        n.error_code = error_code
    if detail:
        n.error_detail = detail
    n.last_checked_at = now
    n.save()
    return n


def _find(n: Notification) -> MessageState | None:
    """Look the message up at the provider by the reference tag it was sent with."""
    return gateway.find_message(n.contact.phone_number, reference_tag(n.reference))


def _settle_by_lookup(n: Notification) -> Notification:
    """Settle an unknown/stale claim by asking the provider - never by sending again."""
    try:
        state = _find(n)
    except ProviderError as exc:
        log.warning('notification %s: lookup failed (%s); outcome stays unknown', n.pk, exc.message)
        return _complete(n, outcome=Notification.UNKNOWN)
    if state is None:
        # An empty lookup cannot prove the send did not happen: it stays unknown.
        return _complete(n, outcome=Notification.UNKNOWN)
    return _complete(n, state)


def _send_claimed(n: Notification) -> Notification:
    """Make the provider call for a claim this request holds, then check and complete it."""
    if n.contact.deleted_at is not None or must_not_go_out(n):
        log.info('notification %s not sent: no longer applies', n.pk)
        return _complete(n, outcome=Notification.FAILED, detail='No longer applies; not sent.')
    n = _call_provider(n)
    if must_not_go_out(n):
        # The order was cancelled (or the number removed) while this was being queued.
        n = call_off(n)
    return n


def _call_provider(n: Notification) -> Notification:
    try:
        state = gateway.send_message(n.contact.phone_number, n.body, send_at=n.scheduled_for)
    except gateway.ProviderConfigError as exc:
        log.error('notification %s not sent: %s', n.pk, exc.message)
        return _complete(n, outcome=Notification.FAILED, detail=exc.message)
    except gateway.NEVER_SENT as exc:
        log.warning('notification %s not sent: %s', n.pk, type(exc).__name__)
        return _complete(n, outcome=Notification.FAILED, detail='Provider unreachable; not sent.')
    except ApiError as exc:
        if exc.status_code < 500:
            # A verdict on the request itself (invalid destination, refused, rate-limited): the
            # provider made nothing. Recorded as failed with no sid, which frees the claim.
            code = gateway.provider_error_code(exc)
            log.warning('notification %s refused by provider: HTTP %s code %s',
                        n.pk, exc.status_code, code)
            return _complete(n, outcome=Notification.FAILED, error_code=code,
                             detail=gateway.provider_error_detail(exc))
        log.warning('notification %s: provider HTTP %s; checking whether it landed',
                    n.pk, exc.status_code)
        return _settle_by_lookup(n)
    except (httpx.RequestError, ValueError) as exc:
        # Sent, and no readable answer: it may have landed. Check by reference.
        log.warning('notification %s: %s; checking whether it landed', n.pk, type(exc).__name__)
        return _settle_by_lookup(n)
    if state.sid is None:
        return _settle_by_lookup(n)
    n = _complete(n, state)
    log.info('notification %s sent: sid=%s status=%s', n.pk, n.message_sid, n.provider_status)
    return n


def _answer_existing(n: Notification) -> Notification:
    """A request that lost the claim: answer from the record, or settle it by lookup."""
    if n.outcome == Notification.SENDING and n.claimed_at > timezone.now() - SEND_WINDOW:
        return n                                     # in flight: no provider call from here
    if n.outcome in (Notification.SENDING, Notification.UNKNOWN) and n.message_sid is None:
        return _settle_by_lookup(n)
    return n


def _safe_send(reference: str, **fields: object) -> tuple[Notification, bool]:
    """The one path for every provider write that sends. Returns (notification, newly_sent)."""
    n, claimed = _try_claim(reference, **fields)
    if not claimed:
        return _answer_existing(n), False
    return _send_claimed(n), True


# --------------------------------------------------------------------------------------------
# Order flow
# --------------------------------------------------------------------------------------------

def active_contact(user_id: int) -> ContactNumber | None:
    return (ContactNumber.objects.filter(user_id=user_id, deleted_at__isnull=True)
            .order_by('-created_at', '-pk').first())


def notify(order: object, kind: str, text: str, *, send_at: datetime | None = None
           ) -> Notification | None:
    """Tell the order's shopper something. Never raises: the order operation must succeed."""
    order_pk: int = order.pk  # type: ignore[attr-defined]
    user_id: int | None = order.user_id  # type: ignore[attr-defined]
    try:
        contact = active_contact(user_id) if user_id else None
        if contact is None:
            log.info('order %s: no mobile number on file; %s not sent', order_pk, kind)
            return None
        reference = order_reference(order_pk, kind)
        n, _ = _safe_send(reference, order_id=order_pk, contact=contact, kind=kind,
                          body=tagged(text, reference), scheduled_for=send_at)
        return n
    except Exception as exc:   # noqa: BLE001 - messaging must never fail the order operation
        log.error('order %s: %s notification could not be processed (%s)',
                  order_pk, kind, type(exc).__name__)
        return None


def call_off(n: Notification) -> Notification:
    """Make sure a scheduled follow-up never reaches the shopper. Never raises."""
    if n.cancel_outcome == Notification.DONE:
        return n
    try:
        if n.message_sid is None:
            if n.outcome == Notification.FAILED:
                n.cancel_outcome = Notification.DONE      # nothing was ever queued
                n.save(update_fields=['cancel_outcome', 'updated_at'])
                return n
            n = _answer_existing(n) if n.outcome == Notification.SENDING else _settle_by_lookup(n)
            if n.message_sid is None:
                n.cancel_outcome = (Notification.DONE if n.outcome == Notification.FAILED
                                    else Notification.UNKNOWN)
                n.save(update_fields=['cancel_outcome', 'updated_at'])
                return n
        sid = n.message_sid
        n.cancel_outcome = Notification.ACTION_REQUESTED
        n.save(update_fields=['cancel_outcome', 'updated_at'])
        try:
            state = gateway.cancel_message(sid)
        except (ApiError, httpx.RequestError, ValueError) as exc:
            # Refused (e.g. it already went out) or no readable answer: ask for its real state.
            log.warning('notification %s: cancel answered %s; re-reading', n.pk,
                        getattr(exc, 'status_code', type(exc).__name__))
            try:
                state = gateway.fetch_message(sid)
            except ProviderError:
                n.cancel_outcome = Notification.UNKNOWN
                n.save(update_fields=['cancel_outcome', 'updated_at'])
                return n
        n.cancel_outcome = gateway.call_off_outcome(state.status)
        n = _complete(n, state)
        log.info('notification %s call-off: %s (provider status %s)',
                 n.pk, n.cancel_outcome, n.provider_status)
        return n
    except Exception as exc:  # noqa: BLE001
        log.error('notification %s: call-off could not be processed (%s)', n.pk, type(exc).__name__)
        Notification.objects.filter(pk=n.pk).update(cancel_outcome=Notification.UNKNOWN)
        n.cancel_outcome = Notification.UNKNOWN
        return n


def must_not_go_out(n: Notification) -> bool:
    """A follow-up whose order was cancelled or whose number was removed must never be sent."""
    if n.kind != Notification.KIND_FOLLOWUP:
        return False
    status = Order.objects.filter(pk=n.order_id).values_list('status', flat=True).first()
    removed = ContactNumber.objects.filter(pk=n.contact_id, deleted_at__isnull=False).exists()
    return status == 'Cancelled' or removed


def refresh(n: Notification, *, force: bool = False) -> Notification:
    """Bring a notification up to date with the provider (no callbacks reach this app)."""
    now = timezone.now()
    if not force and n.last_checked_at and n.last_checked_at > now - RECHECK_AFTER:
        return n
    try:
        if must_not_go_out(n) and n.cancel_outcome != Notification.DONE:
            return call_off(n)
        if n.outcome in (Notification.PENDING, Notification.UNKNOWN, Notification.SENDING):
            if n.message_sid:
                return _complete(n, gateway.fetch_message(n.message_sid))
            return _answer_existing(n)
    except ProviderError as exc:
        log.warning('notification %s: refresh failed (%s)', n.pk, exc.message)
        Notification.objects.filter(pk=n.pk).update(last_checked_at=now)
    return n


# --------------------------------------------------------------------------------------------
# Operator actions
# --------------------------------------------------------------------------------------------

def resend(source: Notification, idempotency_key: str) -> tuple[Notification, bool]:
    """Re-send a message that did not reach the shopper, at most once per idempotency key."""
    key_hash = hash_key(idempotency_key)
    reference = resend_reference(source.pk, key_hash)
    existing = Notification.objects.filter(reference=reference).first()
    if existing is None:
        # Preconditions only gate a *new* resend; a repeat is answered from its record below.
        source = refresh(source, force=True)
        if source.outcome != Notification.FAILED:
            raise NotificationConflict(
                f'Only a message that did not reach the shopper can be re-sent '
                f'(this one is {source.outcome}).')
        if source.order.status == 'Cancelled' and source.kind != Notification.KIND_CANCELLED:
            raise NotificationConflict('This message no longer applies to the order.')
        if source.contact.deleted_at is not None:
            raise NotificationConflict('The shopper removed this number; nothing is sent to it.')
        if not source.body:
            raise NotificationConflict('The content of this message was disposed of.')
    return _safe_send(
        reference, order_id=source.order_id, contact=source.contact, kind=Notification.KIND_RESEND,
        resend_of=source, idempotency_key_hash=key_hash,
        body=tagged(untagged(source.body), reference))


def dispose_content(n: Notification) -> Notification:
    """Erase a message's text here and at the provider; keep the fact and outcome of sending."""
    if n.content_disposal == Notification.DONE:
        return n
    n.body = ''
    n.content_disposal = Notification.ACTION_REQUESTED
    n.content_disposed_at = timezone.now()
    n.save(update_fields=['body', 'content_disposal', 'content_disposed_at', 'updated_at'])
    if n.message_sid is None and n.outcome in (Notification.SENDING, Notification.UNKNOWN):
        n = _settle_by_lookup(n)
    if n.message_sid is None:
        # Never created at the provider (failed with no sid): nothing is held there.
        n.content_disposal = (Notification.DONE if n.outcome == Notification.FAILED
                              else Notification.UNKNOWN)
        n.save(update_fields=['content_disposal', 'updated_at'])
        return n
    sid = n.message_sid
    try:
        state = gateway.redact_message(sid)
    except ApiError as exc:
        if exc.status_code < 500:
            n.content_disposal = Notification.FAILED
            n.error_detail = gateway.provider_error_detail(exc)
            n.save(update_fields=['content_disposal', 'error_detail', 'updated_at'])
            log.warning('notification %s: redaction refused (HTTP %s)', n.pk, exc.status_code)
            return n
        state = None
    except (httpx.RequestError, ValueError):
        state = None
    if state is None:
        try:
            state = gateway.fetch_message(sid)
        except ProviderError:
            n.content_disposal = Notification.UNKNOWN
            n.save(update_fields=['content_disposal', 'updated_at'])
            return n
    n.content_disposal = Notification.DONE if state.body == '' else Notification.FAILED
    n = _complete(n, state)
    log.info('notification %s content disposal: %s', n.pk, n.content_disposal)
    return n


def call_off_for_contact(contact: ContactNumber) -> None:
    """A removed number must receive nothing more: call off its scheduled follow-ups."""
    pending = Notification.objects.filter(
        contact=contact, kind=Notification.KIND_FOLLOWUP,
    ).exclude(cancel_outcome=Notification.DONE).exclude(outcome=Notification.DONE)
    for n in pending:
        call_off(n)
