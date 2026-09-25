"""
What the order flows ask of the SMS provider, and what an operator can do about it.

Rules held here:

* A message that cannot be sent never fails the order operation: ``notify``
  and ``schedule_followup`` swallow provider failures after recording them.
* Every create goes through ``safe_write`` under a reference derived from the
  order and the step (or from the operator's idempotency key for a resend).
* A delivery follow-up must never reach the customer of a cancelled order:
  both the cancel path and the dispatch path check for the other after their
  own committed write, so whichever finishes second calls it off.
"""
import hashlib
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError
from twilio_sdk.core import ApiError
from twilio_sdk.models import ApiV2010AccountMessage

from . import outcomes
from .claims import ClaimStore, apply_snapshot
from .errors import Conflict, InvalidRequest, NotificationError, OutcomeUnknown
from .gateway import (
    MessageSnapshot, ProviderUnreadable, TwilioGateway, get_gateway, require_readable,
    translate_provider_failure, with_reference)
from .models import ContactNumber, SmsNotification
from .orders import STATUS_CANCELLED
from .safe_write import safe_write

logger = logging.getLogger('apps.sms_notifications')

Kind = SmsNotification
MESSAGES = {
    Kind.KIND_ORDER_PLACED: "Thanks! Your order {number} has been placed. We'll text you when it ships.",
    Kind.KIND_ORDER_DISPATCHED: 'Good news: your order {number} is on its way.',
    Kind.KIND_DELIVERY_FOLLOWUP: 'How did the delivery of your order {number} go? Reply and let us know.',
    Kind.KIND_ORDER_CANCELLED: 'Your order {number} has been cancelled.',
}
MAX_REFRESH_PER_REQUEST = 25
MAX_IDEMPOTENCY_KEY_LENGTH = 200


def install_prefix() -> str:
    return str(getattr(settings, 'SMS_NOTIFICATIONS_INSTALL_PREFIX', 'oscar-sandbox'))


def followup_delay() -> timedelta:
    return timedelta(hours=float(getattr(settings, 'SMS_FOLLOWUP_DELAY_HOURS', 72)))


def order_reference(order: Any, kind: str) -> str:
    return '%s:order:%s:%s' % (install_prefix(), order.number, kind)


def resend_reference(original: SmsNotification, idempotency_key: str) -> str:
    key_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    return '%s:resend:%s:%s' % (install_prefix(), original.pk, key_digest)


def current_contact(user: Any) -> ContactNumber | None:
    """The shopper's most recently registered number is the one we message."""
    return ContactNumber.objects.filter(user=user).order_by('-date_created', '-id').first()


class NotificationService:

    def __init__(self, gateway: TwilioGateway | None = None, store: ClaimStore | None = None) -> None:
        self._gateway = gateway
        self.store = store or ClaimStore()

    @property
    def gateway(self) -> TwilioGateway:
        if self._gateway is None:
            self._gateway = get_gateway()
        return self._gateway

    # -- Sending -------------------------------------------------------------------------

    def _send(self, *, reference: str, fields: dict[str, Any], to: str, body: str,
              send_at: datetime | None = None) -> SmsNotification:
        gateway = self.gateway

        def send(ref: str) -> ApiV2010AccountMessage:
            return gateway.send_message(to=to, body=with_reference(body, ref), reference=ref, send_at=send_at)

        def find(ref: str) -> ApiV2010AccountMessage | None:
            return gateway.find_message_by_token(to=to, reference=ref)

        fields = dict(fields, to_number=to, body=body, scheduled_for=send_at)
        return safe_write(self.store, reference, fields, send, find)

    def notify(self, order: Any, kind: str, *, send_at: datetime | None = None) -> SmsNotification | None:
        """Tell the order's shopper about ``kind``. Never raises for a provider failure."""
        reference = order_reference(order, kind)
        existing = SmsNotification.objects.filter(reference=reference).first()
        contact = existing.contact_number if existing else current_contact(order.user)
        to = existing.to_number if existing else (contact.phone_number if contact else None)
        if order.user is None or to is None:
            return None                      # no number on file: simply not messaged
        if existing is not None and existing.contact_number_id is None:
            return existing                  # its number was removed: nothing may be sent to it again
        try:
            return self._send(
                reference=reference,
                fields={'kind': kind, 'order': order, 'user': order.user, 'contact_number': contact},
                to=to, body=MESSAGES[kind].format(number=order.number), send_at=send_at)
        except NotificationError as e:
            logger.warning('SMS %s for order %s not confirmed: %s', kind, order.number, e.message)
            return SmsNotification.objects.filter(reference=reference).first()

    def schedule_followup(self, order: Any) -> SmsNotification | None:
        """Queue the 'how did delivery go' message with the provider for later."""
        order.refresh_from_db(fields=['status'])
        if order.status == STATUS_CANCELLED:
            return None
        send_at = (timezone.now() + followup_delay()).astimezone(dt_timezone.utc).replace(microsecond=0)
        record = self.notify(order, Kind.KIND_DELIVERY_FOLLOWUP, send_at=send_at)
        # The order may have been cancelled while we were scheduling: whoever is
        # second calls it off, and this path is second if the cancel won.
        order.refresh_from_db(fields=['status'])
        if order.status == STATUS_CANCELLED:
            self.cancel_followups(order)
        return record

    # -- Calling off a scheduled follow-up ------------------------------------------------

    def cancel_followups(self, order: Any) -> list[SmsNotification]:
        records = list(SmsNotification.objects.filter(order=order, kind=Kind.KIND_DELIVERY_FOLLOWUP))
        return [self._call_off(r) for r in records]

    def cancel_followups_to_contact(self, contact: ContactNumber) -> None:
        """A deleted number must never be messaged again: call off anything queued for it."""
        for record in SmsNotification.objects.filter(
                contact_number=contact, kind=Kind.KIND_DELIVERY_FOLLOWUP):
            self._call_off(record)

    def _call_off(self, record: SmsNotification) -> SmsNotification:
        if record.cancel_outcome == outcomes.DONE:
            return record
        if record.cancel_requested_at is None:
            record.cancel_requested_at = timezone.now()
            record.save(update_fields=['cancel_requested_at', 'date_updated'])
        if not record.provider_sid:
            # The create's outcome is not settled yet: settle it first, by reference.
            if self.store.is_in_flight(record):
                return record                # the sender will see the cancellation and call it off
            if record.outcome == outcomes.FAILED:
                return self._set_cancel(record, outcomes.DONE)   # never reached the provider
            record = self._resolve_unknown(record)
            if not record.provider_sid:
                return self._set_cancel(record, outcomes.UNKNOWN)
        try:
            snap = require_readable(self.gateway.cancel_message(record.provider_sid))
        except (ApiError, httpx.RequestError, ValidationError, ValueError, ProviderUnreadable) as e:
            # Refused (e.g. it already went out) or no answer: ask the provider what it holds now.
            logger.warning('Calling off follow-up #%s failed (%s); re-reading it', record.pk, type(e).__name__)
            try:
                snap = require_readable(self.gateway.fetch_message(record.provider_sid))
            except (ApiError, httpx.RequestError, ValidationError, ValueError, ProviderUnreadable):
                record.last_error = 'call-off outcome unknown'
                return self._set_cancel(record, outcomes.UNKNOWN)
        apply_snapshot(record, snap)
        record.outcome = outcomes.status_from_provider(snap.status)
        return self._set_cancel(record, outcomes.cancel_outcome_from_provider(snap.status))

    def _set_cancel(self, record: SmsNotification, cancel_outcome: str) -> SmsNotification:
        record.cancel_outcome = cancel_outcome
        record.last_checked_at = timezone.now()
        record.save()
        if cancel_outcome == outcomes.FAILED:
            logger.error('Follow-up #%s for order %s could not be called off: it already went out',
                         record.pk, record.order.number)
        return record

    # -- Reading back what became of each message -----------------------------------------

    def _resolve_unknown(self, record: SmsNotification) -> SmsNotification:
        try:
            found = self.gateway.find_message_by_token(to=record.to_number, reference=record.reference)
            snap = require_readable(found) if found is not None else None
        except (ApiError, httpx.RequestError, ValidationError, ValueError, ProviderUnreadable,
                NotificationError):
            return record
        if snap is None:
            return record                    # not found yet: still unknown, same reference
        return self.store.complete(record.reference, outcomes.status_from_provider(snap.status), snap)

    def refresh(self, records: Iterable[SmsNotification]) -> None:
        """Ask the provider what became of messages that have not settled. Failures leave stored state."""
        budget = MAX_REFRESH_PER_REQUEST
        for record in records:
            if budget <= 0:
                return
            try:
                if record.outcome == outcomes.PENDING and record.provider_sid:
                    budget -= 1
                    snap = require_readable(self.gateway.fetch_message(record.provider_sid))
                    self.store.complete(record.reference, outcomes.status_from_provider(snap.status), snap)
                elif record.outcome == outcomes.UNKNOWN or (
                        record.outcome == outcomes.SENDING and not self.store.is_in_flight(record)):
                    budget -= 1
                    self._resolve_unknown(record)
                record.refresh_from_db()
                if (record.kind == Kind.KIND_DELIVERY_FOLLOWUP and record.order.status == STATUS_CANCELLED
                        and record.cancel_outcome != outcomes.DONE):
                    budget -= 1
                    self._call_off(record)
            except (ApiError, httpx.RequestError, ValidationError, ValueError, ProviderUnreadable,
                    NotificationError) as e:
                logger.info('Refreshing notification #%s failed: %s', record.pk, type(e).__name__)

    # -- Operator: resend ------------------------------------------------------------------

    def resend(self, original: SmsNotification, idempotency_key: str) -> tuple[SmsNotification, bool]:
        """Re-send a message that did not reach the shopper. Returns (record, created_now)."""
        if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise InvalidRequest('An idempotency key (1-%d characters) is required.' % MAX_IDEMPOTENCY_KEY_LENGTH)
        reference = resend_reference(original, idempotency_key)
        existing = SmsNotification.objects.filter(reference=reference).first()
        if existing is None:
            self._check_resendable(original)
        elif existing.contact_number_id is None:
            return existing, False           # its number was removed: nothing may be sent to it again
        contact = original.contact_number
        to = existing.to_number if existing else (contact.phone_number if contact else '')
        record = self._send(
            reference=reference,
            fields={'kind': Kind.KIND_RESEND, 'order': original.order, 'user': original.user,
                    'contact_number': contact, 'resend_of': original},
            to=to, body=existing.body if existing else original.body)
        return record, existing is None

    def _check_resendable(self, original: SmsNotification) -> None:
        if original.content_redacted_at is not None or not original.body:
            raise Conflict('The content of this message has been disposed of; it cannot be re-sent.')
        if original.contact_number is None:
            raise Conflict('The destination number has been removed; nothing may be sent to it.')
        if original.kind == Kind.KIND_DELIVERY_FOLLOWUP and original.order.status == STATUS_CANCELLED:
            raise Conflict('The order was cancelled; its delivery follow-up must not be sent.')
        if original.outcome != outcomes.FAILED:
            raise Conflict('Only a message that did not reach the shopper can be re-sent '
                           '(current outcome: %s).' % original.outcome)

    # -- Operator: content disposal --------------------------------------------------------

    def dispose_content(self, record: SmsNotification) -> SmsNotification:
        if record.content_redacted_at is not None:
            return record
        if not record.provider_sid and record.outcome in (outcomes.UNKNOWN, outcomes.SENDING):
            record = self._resolve_unknown(record)
            if not record.provider_sid:
                raise Conflict('The provider outcome of this message is not settled yet; '
                               'its content cannot be disposed of until it is.')
        record.redaction_requested_at = timezone.now()
        record.save(update_fields=['redaction_requested_at', 'date_updated'])
        if record.provider_sid:
            if record.kind == Kind.KIND_DELIVERY_FOLLOWUP and outcomes.status_from_provider(
                    record.provider_status) == outcomes.PENDING and record.cancel_outcome != outcomes.DONE:
                # Content that is disposed of must not go out later.
                record = self._call_off(record)
            record.redaction_outcome = self._redact_at_provider(record)
            if record.redaction_outcome != outcomes.DONE:
                record.save()
                raise OutcomeUnknown(record.reference, 'The provider has not confirmed the content was removed.')
        record.body = ''
        record.content_redacted_at = timezone.now()
        record.redaction_outcome = outcomes.DONE
        record.save()
        return record

    def _redact_at_provider(self, record: SmsNotification) -> str:
        try:
            message = self.gateway.redact_message(record.provider_sid)
            outcome = outcomes.redaction_outcome(message.body)
            if outcome == outcomes.DONE:
                return outcome
        except ApiError as e:
            if 400 <= e.status_code < 500:
                raise translate_provider_failure(e, is_write=True) from e
        except (httpx.RequestError, ValidationError, ValueError):
            pass
        # No readable confirmation: ask the provider what it holds now.
        try:
            return outcomes.redaction_outcome(self.gateway.fetch_message(record.provider_sid).body)
        except (ApiError, httpx.RequestError, ValidationError, ValueError):
            return outcomes.UNKNOWN

    # -- Operator: reconciliation ----------------------------------------------------------

    def reconcile(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Line up the provider's record of messages from our number against ours, over [start, end)."""
        first_day = datetime.combine(start.astimezone(dt_timezone.utc).date(), datetime.min.time(),
                                     tzinfo=dt_timezone.utc)
        after_last_day = datetime.combine(end.astimezone(dt_timezone.utc).date() + timedelta(days=1),
                                          datetime.min.time(), tzinfo=dt_timezone.utc)
        try:
            fetched = list(self.gateway.messages_sent_from_us(first_day, after_last_day))
        except (ApiError, httpx.RequestError, ValidationError, ValueError) as e:
            raise translate_provider_failure(e, is_write=False) from e
        # The filter is whole GMT days: narrow back to the caller's instants.
        provider = [m for m in fetched if m.date_sent is not None and start <= m.date_sent < end]
        provider_sids = {m.sid for m in provider if m.sid}

        by_sid = {n.provider_sid: n for n in SmsNotification.objects.filter(
            provider_sid__in=provider_sids).select_related('order')}
        matched, provider_only = [], []
        with transaction.atomic():
            for message in provider:
                local = by_sid.get(message.sid or '')
                if local is None:
                    provider_only.append(_provider_item(message))
                    continue
                apply_snapshot(local, message)       # the provider is authoritative for what happened
                local.outcome = outcomes.status_from_provider(message.status)
                local.last_checked_at = timezone.now()
                local.save()
                matched.append({**notification_json(local, staff=True),
                                'providerStatus': message.status})

        local_in_window = SmsNotification.objects.filter(
            provider_date_sent__gte=start, provider_date_sent__lt=end).exclude(provider_sid__in=provider_sids)
        local_only = [notification_json(n, staff=True) for n in local_in_window.select_related('order')]
        unsettled = [notification_json(n, staff=True) for n in SmsNotification.objects.filter(
            provider_date_sent__isnull=True, claimed_at__gte=start, claimed_at__lt=end,
        ).select_related('order')]
        return {
            'from': start.isoformat(), 'to': end.isoformat(),
            'sender': self.gateway.from_number,
            'summary': {'provider': len(provider), 'matched': len(matched), 'providerOnly': len(provider_only),
                        'localOnly': len(local_only), 'unsettled': len(unsettled)},
            'matched': matched, 'providerOnly': provider_only, 'localOnly': local_only,
            'unsettled': unsettled,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _provider_item(message: MessageSnapshot) -> dict[str, Any]:
    from .models import mask_number
    return {'providerSid': message.sid, 'providerStatus': message.status, 'dateSent': _iso(message.date_sent),
            'to': mask_number(message.to or '')}


def notification_json(n: SmsNotification, *, staff: bool = False) -> dict[str, Any]:
    from .models import mask_number
    data: dict[str, Any] = {
        'notificationId': n.pk,
        'orderId': n.order_id,
        'orderNumber': n.order.number,
        'kind': n.kind,
        'outcome': n.outcome,
        'delivered': n.outcome == outcomes.DONE,
        'providerSid': n.provider_sid or None,
        'providerStatus': n.provider_status or None,
        'providerErrorCode': n.provider_error_code,
        'to': mask_number(n.to_number),
        'body': n.body if n.content_redacted_at is None else None,
        'contentDisposed': n.content_redacted_at is not None,
        'createdAt': _iso(n.claimed_at),
        'providerCreatedAt': _iso(n.provider_date_created),
        'sentAt': _iso(n.provider_date_sent),
        'scheduledFor': _iso(n.scheduled_for),
        'resendOf': n.resend_of_id,
    }
    if n.kind == Kind.KIND_DELIVERY_FOLLOWUP:
        data['callOff'] = {'requestedAt': _iso(n.cancel_requested_at), 'outcome': n.cancel_outcome or None}
    if n.redaction_requested_at:
        data['contentDisposal'] = {'requestedAt': _iso(n.redaction_requested_at),
                                   'outcome': n.redaction_outcome or None}
    if staff and n.last_error:
        data['lastError'] = n.last_error
    return data

