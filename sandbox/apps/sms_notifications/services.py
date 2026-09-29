"""
Order notifications by SMS: registering numbers, placing and moving orders,
and every message that goes out as they move.

A message that cannot be sent never fails the order operation that caused
it - its outcome is recorded on the notification instead.
"""
import hashlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from twilio_sdk.core import ApiError
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import MessageEnumStatus

from . import gateway
from .gateway import ProviderError, unset_to_none
from .models import ContactNumber, Notification, Outcome, ProviderWrite, mask_number
from .safe_write import (
    GONE, LOOKUP_ON_REFUSAL, Answer, NeverSent, OutcomeUnknown, WriteRefused, body_token,
    deterministic_ref, load_existing, safe_write)

logger = logging.getLogger(__name__)

Basket = get_model('basket', 'Basket')
Country = get_model('address', 'Country')
Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
ShippingAddress = get_model('order', 'ShippingAddress')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
SurchargeApplicator = get_class('checkout.applicator', 'SurchargeApplicator')
Selector = get_class('partner.strategy', 'Selector')
Free = get_class('shipping.methods', 'Free')
InvalidOrderStatus = get_class('order.exceptions', 'InvalidOrderStatus')

# How many unsettled notifications one read refreshes from the provider.
REFRESH_LIMIT = 20


class ServiceError(Exception):
    """A request this service will not carry out, with the status to answer."""

    def __init__(self, status_code: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


def dispatched_status() -> str:
    return settings.SMS_ORDER_DISPATCHED_STATUS


def cancelled_status() -> str:
    return settings.SMS_ORDER_CANCELLED_STATUS


# Contact numbers
# ===============

def register_contact_number(user: Any, raw_number: str, country_code: str = '') -> tuple[ContactNumber, bool]:
    """
    Validate ``raw_number`` with the provider and store its canonical form.
    Registering a number the caller already has returns the existing record.
    """
    raw_number = (raw_number or '').strip()
    if not raw_number or len(raw_number) > 32:
        raise ServiceError(400, 'phoneNumber is required.')
    try:
        result = gateway.lookup_number(raw_number, country_code)
    except ProviderError as e:
        if e.status_code == 422:
            raise ServiceError(422, 'The messaging provider does not consider this number usable.')
        raise ServiceError(e.status_code, 'Could not validate the number with the messaging provider.')
    if not gateway.lookup_is_usable(result):
        errors = [str(getattr(err, 'value', err)) for err in (unset_to_none(result.validation_errors) or [])]
        raise ServiceError(422, 'The messaging provider does not consider this number usable.',
                           validationErrors=errors)
    canonical = unset_to_none(result.phone_number) or ''
    existing = ContactNumber.objects.active().filter(user=user, phone_number=canonical).first()
    if existing is not None:
        return existing, False
    contact = ContactNumber.objects.create(
        user=user, phone_number=canonical,
        country_code=(unset_to_none(result.country_code) or '')[:2])
    logger.info('Registered contact number #%s for user #%s', contact.pk, user.pk)
    return contact, True


def delete_contact_number(contact: ContactNumber) -> list[str]:
    """
    Remove a number: it is never selected for sending again, and any follow-up
    already queued with the provider for it is called off.
    """
    if contact.deleted_at is None:
        contact.deleted_at = timezone.now()
        contact.save(update_fields=['deleted_at'])
    call_offs = []
    followups = Notification.objects.filter(
        contact_number=contact, kind=Notification.FOLLOWUP,
        outcome__in=[Outcome.SENDING, Outcome.PENDING, Outcome.UNKNOWN])
    for followup in followups:
        call_offs.append(call_off_followup(followup))
    logger.info('Removed contact number #%s', contact.pk)
    return call_offs


def current_number(user: Any) -> ContactNumber | None:
    if user is None:
        return None
    return ContactNumber.objects.active().filter(user=user).first()


# Orders
# ======

def place_order(user: Any, lines: list[tuple[int, int]],
                shipping_address: dict[str, Any] | None = None) -> tuple[Any, list[Notification]]:
    """
    Place an Oscar order for ``user`` from ``lines`` - a list of
    ``(product_id, quantity)`` - through Oscar's own basket and order creator.
    """
    if not lines:
        raise ServiceError(400, 'At least one line is required.')
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(user=user)
        for product_id, quantity in lines:
            product = Product.objects.filter(pk=product_id, is_public=True).first()
            if product is None:
                raise ServiceError(422, 'Product %s does not exist.' % product_id)
            if product.is_parent:
                raise ServiceError(422, 'Product %s is a parent product; order one of its variants.' % product_id)
            info = basket.strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ServiceError(422, 'Product %s cannot be bought: %s' % (product_id, reason))
            basket.add_product(product, quantity)
        address = _shipping_address(shipping_address) if shipping_address else None
        shipping_method = Free()
        shipping_charge = shipping_method.calculate(basket)
        surcharges = SurchargeApplicator().get_applicable_surcharges(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge, surcharges)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user, shipping_address=address,
            surcharges=surcharges)
        basket.set_as_submitted()
    logger.info('Placed order %s for user #%s', order.number, user.pk)
    notification = notify_safely(order, Notification.PLACED)
    return order, [notification] if notification else []


def _shipping_address(data: dict[str, Any]) -> Any:
    country = Country.objects.filter(iso_3166_1_a2=str(data.get('country', '')).upper()).first()
    if country is None:
        raise ServiceError(422, 'shippingAddress.country must be an ISO 3166-1 alpha-2 code of a known country.')
    required = {'firstName': 'first_name', 'lastName': 'last_name', 'line1': 'line1', 'city': 'line4'}
    values = {}
    for key, field in required.items():
        value = str(data.get(key) or '').strip()
        if not value:
            raise ServiceError(400, 'shippingAddress.%s is required.' % key)
        values[field] = value[:255]
    address = ShippingAddress(
        country=country, postcode=str(data.get('postcode') or '').strip()[:64],
        line2=str(data.get('line2') or '').strip()[:255], **values)
    address.save()
    return address


def _move_order(order: Any, new_status: str) -> Any:
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status != new_status:
            try:
                order.set_status(new_status)
            except InvalidOrderStatus:
                raise ServiceError(409, "Order %s cannot move from '%s' to '%s'." % (
                    order.number, order.status, new_status))
    return order


def dispatch_order(order: Any) -> tuple[Any, list[Notification]]:
    """Mark the order dispatched, tell the shopper, and queue the follow-up with the provider."""
    order = _move_order(order, dispatched_status())
    notifications = []
    dispatched = notify_safely(order, Notification.DISPATCHED)
    if dispatched:
        notifications.append(dispatched)
    send_at = timezone.now() + timedelta(hours=settings.SMS_FOLLOWUP_DELAY_HOURS)
    followup = notify_safely(order, Notification.FOLLOWUP, send_at=send_at)
    if followup:
        order.refresh_from_db()
        if order.status == cancelled_status():
            # Cancelled while the follow-up was being queued: call it off now.
            call_off_followup(followup)
            followup.refresh_from_db()
        notifications.append(followup)
    return order, notifications


def cancel_order(order: Any) -> tuple[Any, list[Notification]]:
    """Cancel the order, tell the shopper, and call off a follow-up that has not gone out."""
    order = _move_order(order, cancelled_status())
    notifications = []
    cancelled = notify_safely(order, Notification.CANCELLED)
    if cancelled:
        notifications.append(cancelled)
    followup = order.sms_notifications.filter(kind=Notification.FOLLOWUP, resend_of=None).first()
    if followup is not None:
        call_off_followup(followup)
        followup.refresh_from_db()
        notifications.append(followup)
    return order, notifications


# Messages
# ========

TEMPLATES = {
    Notification.PLACED: '%(shop)s: thanks - your order %(number)s has been placed.',
    Notification.DISPATCHED: '%(shop)s: your order %(number)s is on its way.',
    Notification.FOLLOWUP: '%(shop)s: how did the delivery of order %(number)s go? Reply and let us know.',
    Notification.CANCELLED: '%(shop)s: your order %(number)s has been cancelled.',
}


def message_body(kind: str, order: Any, reference: str) -> str:
    text = TEMPLATES[kind] % {'shop': getattr(settings, 'OSCAR_SHOP_NAME', 'Oscar'), 'number': order.number}
    return '%s Ref %s' % (text, body_token(reference))


def send_ref(notification: Notification) -> str:
    return notification.reference + ':send'


def _create_notification(reference: str, **fields: Any) -> tuple[Notification, bool]:
    """Create the notification for ``reference``, or return the one that already has it."""
    try:
        with transaction.atomic():
            return Notification.objects.create(reference=reference, **fields), True
    except IntegrityError:
        return Notification.objects.get(reference=reference), False


def notify(order: Any, kind: str, send_at: datetime | None = None) -> Notification:
    reference = deterministic_ref('order', order.pk, kind)
    contact = current_number(order.user)
    notification, _ = _create_notification(
        reference, order=order, user=order.user, contact_number=contact, kind=kind,
        send_at=send_at, body=message_body(kind, order, reference),
        outcome=Outcome.SENDING if contact else Outcome.SKIPPED)
    return send_notification(notification)


def notify_safely(order: Any, kind: str, send_at: datetime | None = None) -> Notification | None:
    """``notify``, but a message that cannot be sent never fails the order operation."""
    try:
        return notify(order, kind, send_at=send_at)
    except Exception:
        logger.exception('Could not send the %s notification for order %s', kind, order.number)
        return Notification.objects.filter(reference=deterministic_ref('order', order.pk, kind)).first()


def message_answer(message: ApiV2010AccountMessage) -> Answer:
    return Answer(
        provider_id=unset_to_none(message.sid) or '',
        status=unset_to_none(message.status),
        provider_time=gateway.parse_provider_time(message.date_sent),
        provider_status=gateway.status_value(message.status),
        error_code=unset_to_none(message.error_code),
        error_message=unset_to_none(message.error_message) or '',
    )


def _fetch_or_none(sid: str) -> ApiV2010AccountMessage | None:
    try:
        return gateway.fetch_message(sid)
    except ApiError as e:
        if e.status_code == 404:
            return None
        raise


def _find_sent(notification: Notification) -> Callable[[str], ApiV2010AccountMessage | None]:
    """The provider's record of this notification's message, by sid when known, else by reference."""
    def find(ref: str) -> ApiV2010AccountMessage | None:
        write = load_existing(ref)
        if write is not None and write.provider_id:
            return _fetch_or_none(write.provider_id)
        if notification.provider_sid:
            return _fetch_or_none(notification.provider_sid)
        contact = notification.contact_number
        if contact is None:
            return None
        return gateway.find_message_by_token(contact.phone_number, body_token(notification.reference))
    return find


def _send_steps(notification: Notification) -> dict[str, Any]:
    contact = notification.contact_number
    if contact is None:
        raise ValueError('Notification #%s has no number to send to.' % notification.pk)
    return dict(
        notification=notification, step=ProviderWrite.SEND,
        send=lambda key: gateway.create_message(
            contact.phone_number, notification.body, key, send_at=notification.send_at),
        find=_find_sent(notification),
        read=message_answer,
        outcome_of=gateway.status_from_provider,
    )


def apply_write(notification: Notification, write: ProviderWrite | None) -> Notification:
    """Carry what the provider said about the send onto the notification."""
    if write is None:
        return notification
    notification.outcome = write.outcome
    if write.provider_id:
        notification.provider_sid = write.provider_id
    if write.provider_status:
        notification.provider_status = write.provider_status
    notification.error_code = write.error_code
    notification.error_message = write.error_message
    if write.provider_time:
        notification.provider_sent_at = write.provider_time
    notification.last_checked_at = timezone.now()
    notification.save()
    return notification


def send_notification(notification: Notification) -> Notification:
    """Send the notification's message once, whatever happens and however often this is called."""
    if notification.outcome == Outcome.SKIPPED:
        return notification
    contact = notification.contact_number
    if contact is None or (contact.deleted_at is not None and load_existing(send_ref(notification)) is None):
        # No number, or it was removed before this message went out: nothing may be sent to it.
        notification.outcome = Outcome.SKIPPED
        notification.save(update_fields=['outcome', 'updated'])
        return notification
    try:
        gateway.get_client()
    except ImproperlyConfigured as e:
        notification.outcome = Outcome.FAILED
        notification.error_message = str(e)[:255]
        notification.save(update_fields=['outcome', 'error_message', 'updated'])
        return notification
    try:
        write = safe_write(send_ref(notification), **_send_steps(notification))
    except (OutcomeUnknown, WriteRefused, NeverSent) as e:
        write = e.write
    apply_write(notification, write)
    logger.info('Notification #%s (%s, order #%s): %s %s', notification.pk, notification.kind,
                notification.order_id, notification.outcome, notification.provider_status)
    return notification


def refresh_notification(notification: Notification) -> Notification:
    """Ask the provider where an unsettled message has got to. Never sends."""
    if notification.outcome not in (Outcome.SENDING, Outcome.PENDING, Outcome.UNKNOWN):
        return notification
    if notification.contact_number is None:
        return notification
    try:
        write = safe_write(send_ref(notification), lookup_only=True, **_send_steps(notification))
    except OutcomeUnknown as e:
        write = e.write
    except (ImproperlyConfigured, ProviderError, ApiError) as e:
        logger.warning('Could not refresh notification #%s: %s', notification.pk, type(e).__name__)
        return notification
    return apply_write(notification, write)


def refresh_many(notifications: list[Notification]) -> list[Notification]:
    refreshed = 0
    for notification in notifications:
        if notification.outcome in (Outcome.SENDING, Outcome.PENDING, Outcome.UNKNOWN):
            if refreshed >= REFRESH_LIMIT:
                break
            refresh_notification(notification)
            refreshed += 1
    return notifications


# Calling off a follow-up
# =======================

def _gone_or(get: Callable[[], ApiV2010AccountMessage]) -> object:
    try:
        return get()
    except ApiError as e:
        if e.status_code == 404:
            return GONE
        raise


def call_off_followup(followup: Notification) -> str:
    """
    Make sure a queued follow-up never reaches the shopper. Returns the
    call-off outcome ('' when there was nothing to call off).
    """
    write = load_existing(send_ref(followup))
    if followup.outcome == Outcome.SKIPPED or write is None:
        return _set_call_off(followup, '')
    if write.outcome == Outcome.SENDING and not write.provider_id:
        # Being queued by another request right now: that request calls it off once it lands.
        return _set_call_off(followup, Outcome.SENDING)
    if not write.provider_id:
        refresh_notification(followup)
        write = load_existing(send_ref(followup))
        if write is None or not write.provider_id:
            if write is not None and write.outcome == Outcome.FAILED:
                return _set_call_off(followup, '')     # it never landed: nothing to call off
            return _set_call_off(followup, Outcome.UNKNOWN)
    sid = write.provider_id
    try:
        call_off = safe_write(
            followup.reference + ':cancel', notification=followup, step=ProviderWrite.CANCEL,
            send=lambda key: _gone_or(lambda: gateway.cancel_message(sid, key)),
            find=lambda ref: _gone_or(lambda: gateway.fetch_message(sid)),
            read=lambda r: (Answer(sid, MessageEnumStatus.CANCELED, provider_status='gone') if r is GONE
                            else message_answer(r)),
            outcome_of=gateway.cancel_outcome,
            repeat_is_safe=True,
            on_refusal=LOOKUP_ON_REFUSAL,
        )
    except (OutcomeUnknown, WriteRefused, NeverSent) as e:
        call_off = e.write
    except ImproperlyConfigured:
        return _set_call_off(followup, Outcome.FAILED)
    if call_off is None:
        return _set_call_off(followup, Outcome.UNKNOWN)
    if call_off.provider_status and call_off.provider_status != 'gone':
        followup.provider_status = call_off.provider_status
        followup.outcome = gateway.status_from_provider(_status_member(call_off.provider_status))
        followup.last_checked_at = timezone.now()
    logger.info('Follow-up #%s call-off: %s', followup.pk, call_off.outcome)
    return _set_call_off(followup, call_off.outcome)


def _status_member(value: str) -> MessageEnumStatus | str:
    try:
        return MessageEnumStatus(value)
    except ValueError:
        return value


def _set_call_off(followup: Notification, outcome: str) -> str:
    followup.call_off_outcome = outcome
    followup.save()
    return outcome


# Operator actions on a notification
# ==================================

RESENDABLE = (Outcome.FAILED, Outcome.SKIPPED)


def resend(source: Notification, idempotency_key: str) -> Notification:
    """
    Send ``source``'s message again to the shopper's current number. The same
    idempotency key always answers with the same new notification.
    """
    key = (idempotency_key or '').strip()
    if not key or len(key) > 255:
        raise ServiceError(400, 'An Idempotency-Key header (1-255 characters) is required.')
    reference = deterministic_ref('resend', source.pk, hashlib.sha256(key.encode()).hexdigest()[:32])
    existing = Notification.objects.filter(reference=reference).first()
    if existing is not None:
        return send_notification(existing)

    if source.content_redacted_at is not None:
        raise ServiceError(409, 'The content of this message was disposed of; it cannot be re-sent.')
    if source.outcome not in RESENDABLE:
        raise ServiceError(409, "Only a message that did not reach the shopper can be re-sent "
                                "(this one is '%s')." % source.outcome)
    if source.kind == Notification.FOLLOWUP and (
            source.order.status == cancelled_status() or source.call_off_outcome):
        raise ServiceError(409, 'The follow-up was called off; it must not be sent.')
    contact = current_number(source.user)
    if contact is None:
        raise ServiceError(409, 'The shopper has no number on file.')
    notification, _ = _create_notification(
        reference, order=source.order, user=source.user, contact_number=contact,
        kind=source.kind, resend_of=source, body=message_body(source.kind, source.order, reference),
        outcome=Outcome.SENDING)
    return send_notification(notification)


def dispose_content(notification: Notification) -> str:
    """
    Dispose of a message's text at the provider (redaction), keeping the fact
    that it was sent and what became of it. Returns the outcome.
    """
    if notification.content_redacted_at is not None:
        return Outcome.DONE
    if not notification.provider_sid:
        refresh_notification(notification)
    sid = notification.provider_sid
    if not sid:
        send_write = load_existing(send_ref(notification))
        if send_write is not None and send_write.outcome in (Outcome.SENDING, Outcome.UNKNOWN):
            # It may be at the provider and we cannot name it yet.
            return Outcome.UNKNOWN
        return _redacted_locally(notification)
    ref = notification.reference + ':redact'
    try:
        redaction = safe_write(
            ref, notification=notification, step=ProviderWrite.REDACT,
            send=lambda key: gateway.redact_message(sid, key),
            find=lambda r: gateway.fetch_message(sid),
            read=lambda m: Answer(sid, unset_to_none(m.body), provider_status=gateway.status_value(m.status)),
            outcome_of=gateway.redact_outcome,
            repeat_is_safe=True,
        )
    except OutcomeUnknown as e:
        redaction = e.write
    except NeverSent:
        raise ServiceError(502, 'Could not reach the messaging provider; nothing was changed.')
    except WriteRefused as e:
        raise ServiceError(409, e.message or 'The messaging provider refused to redact this message.',
                           providerCode=e.code)
    outcome = redaction.outcome if redaction is not None else Outcome.UNKNOWN
    if outcome == Outcome.DONE:
        _redacted_locally(notification)
    logger.info('Notification #%s content disposal: %s', notification.pk, outcome)
    return outcome


def _redacted_locally(notification: Notification) -> str:
    notification.body = ''
    notification.content_redacted_at = timezone.now()
    notification.save(update_fields=['body', 'content_redacted_at', 'updated'])
    return Outcome.DONE


# Reconciliation
# ==============

def reconcile(date_from: datetime, date_to: datetime) -> dict[str, Any]:
    """
    Line the provider's record of messages sent from our number between
    ``date_from`` and ``date_to`` up against the notifications we sent.
    Both sides are selected on the provider's clock (the message's send date).
    """
    if date_from >= date_to:
        raise ServiceError(400, '`from` must be before `to`.')
    # The provider filters by whole UTC days: widen to cover the range, then narrow back.
    day_from = date_from.astimezone(dt_timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day_to = date_to.astimezone(dt_timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        fetched = gateway.list_sent_messages(day_from, day_to + timedelta(days=1))
    except Exception as exc:
        raise gateway.translate(exc) from exc

    provider = {}
    for message in fetched:
        sent_at = gateway.parse_provider_time(message.date_sent)
        sid = unset_to_none(message.sid)
        if sid and sent_at is not None and date_from <= sent_at < date_to:
            provider[sid] = (message, sent_at)

    # Our records that the provider has now dated: store its clock on them.
    for notification in Notification.objects.filter(provider_sid__in=list(provider), provider_sent_at__isnull=True):
        notification.provider_sent_at = provider[notification.provider_sid][1]
        notification.save(update_fields=['provider_sent_at', 'updated'])

    local = list(Notification.objects.filter(
        provider_sent_at__gte=date_from, provider_sent_at__lt=date_to).exclude(provider_sid=''))
    # Ours with no send date from the provider yet: still in flight or unresolved (unsettled),
    # or settled without ever going out - refused, or a follow-up called off (never sent).
    undated = list(Notification.objects.filter(
        created__gte=date_from, created__lt=date_to, provider_sent_at__isnull=True,
    ).exclude(outcome=Outcome.SKIPPED))
    unsettled = [n for n in undated if n.outcome in (Outcome.SENDING, Outcome.PENDING, Outcome.UNKNOWN)]
    never_sent = [n for n in undated if n.outcome not in (Outcome.SENDING, Outcome.PENDING, Outcome.UNKNOWN)]

    matched, app_only = [], []
    for notification in local:
        entry = provider.pop(notification.provider_sid, None)
        if entry is None:
            app_only.append(_local_entry(notification))
            continue
        message, sent_at = entry
        provider_status = gateway.status_value(message.status)
        matched.append({
            **_local_entry(notification),
            'providerStatus': provider_status,
            'statusAgrees': provider_status == notification.provider_status,
        })
    provider_only = [{
        'providerSid': sid,
        'providerStatus': gateway.status_value(message.status),
        'sentAt': sent_at.isoformat(),
        'to': mask_number(unset_to_none(message.to) or ''),
        'errorCode': unset_to_none(message.error_code),
    } for sid, (message, sent_at) in provider.items()]

    return {
        'from': date_from.isoformat(),
        'to': date_to.isoformat(),
        'fromNumber': mask_number(settings.TWILIO_FROM_NUMBER),
        'summary': {
            'providerMessages': len(matched) + len(provider_only),
            'appMessages': len(local),
            'matched': len(matched),
            'providerOnly': len(provider_only),
            'appOnly': len(app_only),
            'unsettled': len(unsettled),
            'neverSent': len(never_sent),
            'statusMismatches': sum(1 for m in matched if not m['statusAgrees']),
        },
        'matched': matched,
        'providerOnly': provider_only,
        'appOnly': app_only,
        'unsettled': [_local_entry(n) for n in unsettled],
        'neverSent': [_local_entry(n) for n in never_sent],
    }


def _local_entry(notification: Notification) -> dict[str, Any]:
    return {
        'notificationId': notification.pk,
        'orderId': notification.order_id,
        'kind': notification.kind,
        'providerSid': notification.provider_sid or None,
        'appStatus': notification.provider_status or None,
        'outcome': notification.outcome,
        'sentAt': notification.provider_sent_at.isoformat() if notification.provider_sent_at else None,
    }
