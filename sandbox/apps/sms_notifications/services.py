"""
SMS notifications for orders: contact numbers, the messages sent as an order
moves, and the operator actions on them.

A message that cannot be sent never fails the order operation that caused it:
every notification step records its outcome and swallows the failure. Nothing
here logs a phone number or a message body.
"""
import hashlib
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from oscar.apps.order.exceptions import InvalidOrderStatus
from oscar.core.loading import get_class, get_model
from twilio_sdk.models import ApiV2010AccountMessage

from . import provider
from .models import ContactNumber, Notification, ProviderWrite
from .provider import ProviderError
from .safe_write import (
    DONE, FAILED, NEEDS_REVIEW, PENDING, SEND_WINDOW, SENDING, UNKNOWN, OutcomeUnknown, cancel_outcome, complete,
    deterministic_ref, read_message, read_redaction, redact_outcome, ref_token, safe_write,
    status_from_provider)

logger = logging.getLogger(__name__)

Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
Basket = get_model('basket', 'Basket')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Selector = get_class('partner.strategy', 'Selector')
Free = get_class('shipping.methods', 'Free')

DISPATCHED = 'Dispatched'
CANCELLED = 'Cancelled'

# How many not-yet-final notifications one read request re-checks with the provider.
REFRESH_LIMIT = 20


class ServiceError(Exception):
    def __init__(self, status_code, message, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------- contact numbers

def active_numbers(user):
    return ContactNumber.objects.filter(user=user, deleted_at__isnull=True)


def register_contact_number(user, raw_number, country_code=None):
    """Validate with the provider, store its canonical form. Returns (contact, created)."""
    raw_number = (raw_number or '').strip()
    if not raw_number or len(raw_number) > 32:
        raise ServiceError(400, 'phoneNumber is required.')
    if country_code is not None and (not isinstance(country_code, str) or len(country_code) != 2):
        raise ServiceError(400, 'countryCode must be a two-letter ISO country code.')
    try:
        result = provider.lookup_number(raw_number, country_code)
    except ProviderError as exc:
        raise ServiceError(exc.status_code, exc.message) from exc
    if not result.valid or not result.phone_number:
        raise ServiceError(
            400, 'The messaging provider does not consider this a usable destination.',
            validationErrors=result.validation_errors)
    with transaction.atomic():
        existing = active_numbers(user).filter(phone_number=result.phone_number).first()
        if existing:
            return existing, False
        contact = ContactNumber.objects.create(
            user=user, phone_number=result.phone_number,
            country_code=(result.country_code or '')[:2])
    logger.info('contact number %s registered for user %s', contact.pk, user.pk)
    return contact, True


def remove_contact_number(user, contact_id):
    """Soft-delete one of the caller's numbers and call off anything still scheduled to it."""
    with transaction.atomic():
        contact = active_numbers(user).select_for_update().filter(pk=contact_id).first()
        if contact is None:
            raise ServiceError(404, 'Contact number not found.')
        contact.deleted_at = timezone.now()
        contact.save(update_fields=['deleted_at'])
    scheduled = Notification.objects.filter(
        contact_number=contact, kind=Notification.FOLLOW_UP,
        send_write__outcome__in=[PENDING, SENDING, UNKNOWN])
    for notification in scheduled:
        cancel_follow_up(notification)
    logger.info('contact number %s removed for user %s', contact.pk, user.pk)


def serialize_contact(contact):
    return {
        'contactNumberId': contact.pk,
        'phoneNumber': contact.phone_number,
        'countryCode': contact.country_code or None,
        'createdAt': contact.created_at.isoformat(),
    }


# ---------------------------------------------------------------- orders

def place_order(user, request, items):
    """Place an order from (product id, quantity) pairs through Oscar's own models."""
    if not isinstance(items, list) or not items:
        raise ServiceError(400, 'items must be a non-empty list.')
    wanted = []
    for item in items:
        if not isinstance(item, dict):
            raise ServiceError(400, 'Each item needs productId and quantity.')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if not isinstance(product_id, int) or not isinstance(quantity, int) or not 0 < quantity <= 100:
            raise ServiceError(400, 'Each item needs an integer productId and a quantity of 1-100.')
        wanted.append((product_id, quantity))

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for product_id, quantity in wanted:
            product = Product.objects.filter(pk=product_id).first()
            if product is None:
                raise ServiceError(400, 'Catalogue item %s does not exist.' % product_id)
            info = basket.strategy.fetch_for_product(product)
            if not product.is_public or not info.availability.is_available_to_buy:
                raise ServiceError(400, 'Catalogue item %s is not available to buy.' % product_id)
            allowed, reason = info.availability.is_purchase_permitted(quantity)
            if not allowed:
                raise ServiceError(400, 'Catalogue item %s: %s' % (product_id, reason))
            basket.add_product(product, quantity)
        shipping_method = Free()
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user,
            order_number=OrderNumberGenerator().order_number(basket), request=request)
        basket.submit()
    notification = notify(order, Notification.PLACED)
    return order, notification


def get_order_for(user, order_id, *, staff_ok=False):
    orders = Order.objects.all() if (staff_ok and user.is_staff) else Order.objects.filter(user=user)
    order = orders.filter(pk=order_id).first()
    if order is None:
        raise ServiceError(404, 'Order not found.')
    return order


def _transition(order_id, new_status):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None:
            raise ServiceError(404, 'Order not found.')
        try:
            order.set_status(new_status)
        except InvalidOrderStatus as exc:
            raise ServiceError(409, 'Order %s cannot move from %s to %s.' % (
                order.number, order.status, new_status)) from exc
    return order


def dispatch_order(order_id):
    order = _transition(order_id, DISPATCHED)
    dispatched = notify(order, Notification.DISPATCHED)
    send_at = timezone.now() + timedelta(days=settings.SMS_FOLLOW_UP_DELAY_DAYS)
    follow_up = notify(order, Notification.FOLLOW_UP, send_at=send_at)
    # A cancel that ran while the follow-up was being scheduled must still win.
    order.refresh_from_db(fields=['status'])
    if follow_up is not None and order.status == CANCELLED:
        cancel_follow_up(follow_up)
    return order, [n for n in (dispatched, follow_up) if n is not None]


def cancel_order(order_id):
    order = _transition(order_id, CANCELLED)
    follow_ups = []
    for notification in order.sms_notifications.filter(kind=Notification.FOLLOW_UP):
        follow_ups.append((notification, cancel_follow_up(notification)))
    cancelled = notify(order, Notification.CANCELLED)
    return order, cancelled, follow_ups


# ---------------------------------------------------------------- sending

def message_text(kind, order):
    shop = getattr(settings, 'OSCAR_SHOP_NAME', 'Oscar')
    return {
        Notification.PLACED: '%s: thanks, your order %s has been placed.' % (shop, order.number),
        Notification.DISPATCHED: '%s: good news, your order %s is on its way.' % (shop, order.number),
        Notification.FOLLOW_UP: '%s: how did the delivery of order %s go? Reply and let us know.' % (
            shop, order.number),
        Notification.CANCELLED: '%s: your order %s has been cancelled.' % (shop, order.number),
    }[kind]


def notify(order, kind, *, send_at=None):
    """Tell the order's shopper about ``kind``. Never raises; None if they have no number."""
    if order.user is None:
        return None
    contact = active_numbers(order.user).first()
    if contact is None:
        return None
    reference = deterministic_ref('order', order.pk, kind)
    return _send(order, kind, reference, contact, message_text(kind, order), send_at=send_at)


def _send(order, kind, reference, contact, text, *, send_at=None, resend_of=None):
    token = ref_token(reference)

    def on_claim(write):
        Notification.objects.create(
            order=order, user=order.user, contact_number=contact, kind=kind,
            body='%s Ref %s' % (text, token), ref_token=token, scheduled_for=send_at,
            resend_of=resend_of, send_write=write)

    def send(write):
        notification = write.notification
        if notification.contact_number.deleted_at is not None:
            raise ServiceError(409, 'The destination number was removed.')
        return provider.create_sms(
            notification.contact_number.phone_number, notification.body,
            notification.scheduled_for)

    def find(write):
        notification = write.notification
        return provider.find_sms_by_token(
            notification.contact_number.phone_number, notification.ref_token)

    write = _guarded_send(reference, send, find, on_claim)
    if write is None:
        return None
    return Notification.objects.select_related('send_write').get(send_write=write)


def _guarded_send(reference, send, find, on_claim):
    """Run a send through the safe write; a failure is recorded, never raised."""
    try:
        return safe_write(reference, ProviderWrite.SEND, send=send, find=find, on_claim=on_claim)
    except OutcomeUnknown as exc:
        logger.warning('notification send %s outcome unknown', exc.write.pk)
        return exc.write
    except Exception as exc:  # noqa: BLE001 - a message must never fail the order operation
        logger.warning('notification send failed: %s', type(exc).__name__)
        write = ProviderWrite.objects.filter(reference=reference).first()
        if write is not None and write.outcome == SENDING:
            # Raised before the provider was called (e.g. the number was removed).
            complete(write, FAILED if isinstance(exc, ServiceError) else UNKNOWN)
        return write


# ---------------------------------------------------------------- reading state back

def refresh(notification):
    """Ask the provider where a not-yet-final message got to. Returns False on a read failure."""
    write = notification.send_write
    stale_sending = write.outcome == SENDING and write.claimed_at <= timezone.now() - SEND_WINDOW
    if write.outcome not in (PENDING, UNKNOWN) and not stale_sending:
        return True
    message: ApiV2010AccountMessage | None
    try:
        if write.provider_id:
            message = provider.fetch_sms(write.provider_id)
        else:
            if notification.contact_number is None:
                return True
            message = provider.find_sms_by_token(
                notification.contact_number.phone_number, notification.ref_token)
            if message is None:
                return True     # still not found: stays unknown, under its reference
        answer = read_message(message)
        if answer.provider_id is None:
            return False
        complete(write, status_from_provider(answer.status), answer)
        return True
    except Exception as exc:  # noqa: BLE001 - a read failure leaves the record as it was
        logger.info('refresh of notification %s failed: %s', notification.pk, type(exc).__name__)
        return False


def refresh_many(notifications):
    budget = REFRESH_LIMIT
    for notification in notifications:
        if budget <= 0:
            break
        if notification.send_write.outcome in (PENDING, UNKNOWN, SENDING):
            refresh(notification)
            notification.send_write.refresh_from_db()
            budget -= 1


def serialize_write(write):
    if write is None:
        return None
    return {
        'status': write.outcome,
        'providerStatus': write.provider_status or None,
        'updatedAt': write.updated_at.isoformat(),
    }


def serialize_notification(notification):
    write = notification.send_write
    return {
        'notificationId': notification.pk,
        'orderId': notification.order_id,
        'kind': notification.kind,
        'status': write.outcome,
        'providerStatus': write.provider_status or None,
        'messageSid': write.provider_id,
        'errorCode': write.error_code,
        'body': None if notification.content_redacted_at else notification.body,
        'scheduledFor': notification.scheduled_for.isoformat() if notification.scheduled_for else None,
        'providerTime': write.provider_time.isoformat() if write.provider_time else None,
        'createdAt': notification.created_at.isoformat(),
        'resendOf': notification.resend_of_id,
        'contentRedacted': notification.content_redacted_at is not None,
        'cancellation': serialize_write(notification.cancel_write),
        'redaction': serialize_write(notification.redact_write),
    }


def order_notifications(order):
    notifications = list(order.sms_notifications.select_related(
        'send_write', 'cancel_write', 'redact_write'))
    refresh_many(notifications)
    return notifications


def serialize_order(order, notifications):
    return {
        'orderId': order.pk,
        'number': order.number,
        'status': order.status,
        'total': str(order.total_incl_tax.quantize(Decimal('0.01'))),
        'currency': order.currency,
        'placedAt': order.date_placed.isoformat(),
        'notifications': [serialize_notification(n) for n in notifications],
    }


# ---------------------------------------------------------------- operator actions

def cancel_follow_up(notification):
    """
    Call off a scheduled follow-up so it never reaches the shopper. Returns the
    cancel outcome: done (it will never go out), pending, failed (it already
    went out), or unknown.
    """
    write = notification.send_write
    if not write.provider_id:
        refresh(notification)
        write.refresh_from_db()
    if not write.provider_id:
        if write.outcome == FAILED:
            return DONE                 # never left: nothing to call off
        logger.warning('follow-up %s has no message SID to cancel; needs review', notification.pk)
        return UNKNOWN
    if cancel_outcome(write.provider_status) == DONE:
        return DONE                     # already canceled / never delivered

    sid = write.provider_id
    reference = deterministic_ref('notification', notification.pk, 'cancel')

    def on_claim(cancel_write):
        Notification.objects.filter(pk=notification.pk).update(cancel_write=cancel_write)

    try:
        cancel = safe_write(
            reference, ProviderWrite.CANCEL,
            send=lambda _w: provider.cancel_sms(sid),
            find=lambda _w: provider.fetch_sms(sid),
            outcome_of=cancel_outcome, repeat_is_safe=True, on_claim=on_claim)
    except OutcomeUnknown:
        return UNKNOWN
    except Exception as exc:  # noqa: BLE001 - a refused cancel must not fail the order cancel
        logger.warning('cancel of follow-up %s refused: %s', notification.pk, type(exc).__name__)
        refresh(notification)
        write.refresh_from_db()
        return cancel_outcome(write.provider_status) if write.provider_status else FAILED
    # The cancel's answer is the message's current state: keep the send record in step.
    if cancel.provider_status:
        write.provider_status = cancel.provider_status
        write.outcome = status_from_provider(cancel.provider_status)
        write.save(update_fields=['provider_status', 'outcome', 'updated_at'])
    return cancel.outcome


def resend(notification, idempotency_key):
    """Send a message again that did not reach the shopper. Same key -> same resend."""
    refresh(notification)
    notification.send_write.refresh_from_db()
    order = notification.order
    if notification.content_redacted_at:
        raise ServiceError(409, 'The content of this message was disposed of; it cannot be re-sent.')
    if notification.kind == Notification.FOLLOW_UP and order.status == CANCELLED:
        raise ServiceError(409, 'The order was cancelled; its delivery follow-up must not be sent.')
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    reference = deterministic_ref('notification', notification.pk, 'resend', key_hash)
    existing = ProviderWrite.objects.filter(reference=reference).first()
    if existing is None and notification.send_write.outcome != FAILED:
        raise ServiceError(
            409, 'Only a message that did not reach the shopper can be re-sent '
                 '(this one is %s).' % notification.send_write.outcome)
    contact = notification.contact_number
    if contact is None or contact.deleted_at is not None:
        contact = active_numbers(notification.user).first()
    if contact is None and existing is None:
        raise ServiceError(409, 'The shopper has no contact number on file.')
    return _send(order, notification.kind, reference, contact,
                 message_text(notification.kind, order), resend_of=notification)


def redact(notification):
    """Dispose of a message's text at the provider (and here); keep that it was sent."""
    write = notification.send_write
    if notification.content_redacted_at:
        return notification.redact_write
    if not write.provider_id:
        raise ServiceError(409, 'This message has no provider record to redact.')
    sid = write.provider_id
    reference = deterministic_ref('notification', notification.pk, 'redact')

    def on_claim(redact_write):
        Notification.objects.filter(pk=notification.pk).update(redact_write=redact_write)

    try:
        result = safe_write(
            reference, ProviderWrite.REDACT,
            send=lambda _w: provider.redact_sms(sid),
            find=lambda _w: provider.fetch_sms(sid),
            read=read_redaction, outcome_of=redact_outcome, repeat_is_safe=True,
            on_claim=on_claim, retry_outcomes=(NEEDS_REVIEW, FAILED))
    except OutcomeUnknown as exc:
        return exc.write
    except Exception as exc:
        raise provider.translate(exc) from exc
    if result.outcome == DONE:
        Notification.objects.filter(pk=notification.pk).update(
            body='', content_redacted_at=timezone.now())
    return result


# ---------------------------------------------------------------- reconciliation

def reconcile(start, end):
    """
    The provider's record of messages sent from our number in [start, end),
    lined up against ours on the provider's clock.
    """
    try:
        provider_messages = provider.list_sent_from_our_number(start, end)
    except Exception as exc:
        raise provider.translate(exc) from exc
    by_sid = {m.sid: m for m in provider_messages if isinstance(m.sid, str)}

    local_by_sid = {
        w.provider_id: w for w in ProviderWrite.objects.filter(
            operation=ProviderWrite.SEND, provider_id__in=list(by_sid)).select_related('notification')}
    matched, provider_only = [], []
    for sid, message in by_sid.items():
        answer = read_message(message)
        status = str(answer.status) if answer.status is not None else None
        local = local_by_sid.get(sid)
        if local is None:
            provider_only.append({
                'messageSid': sid,
                'providerStatus': status,
                'sentAt': answer.provider_time.isoformat() if answer.provider_time else None,
                'to': provider.mask_number(provider._set(message.to)),
            })
            continue
        matched.append({
            'notificationId': local.notification.pk,
            'orderId': local.notification.order_id,
            'messageSid': sid,
            'providerStatus': status,
            'localProviderStatus': local.provider_status or None,
            'localStatus': local.outcome,
            'statusDiffers': status != (local.provider_status or None),
            'sentAt': answer.provider_time.isoformat() if answer.provider_time else None,
        })

    # Our side on the same clock: records whose stored provider time is in the window.
    local_only, not_sent = [], []
    in_window = ProviderWrite.objects.filter(
        operation=ProviderWrite.SEND, provider_id__isnull=False,
        provider_time__gte=start, provider_time__lt=end).exclude(
        provider_id__in=list(by_sid)).select_related('notification')
    for write in in_window:
        entry = {
            'notificationId': write.notification.pk,
            'orderId': write.notification.order_id,
            'messageSid': write.provider_id,
            'localStatus': write.outcome,
            'localProviderStatus': write.provider_status or None,
            'providerTime': write.provider_time.isoformat() if write.provider_time else None,
        }
        if write.provider_status in ('scheduled', 'canceled', 'accepted', 'queued'):
            not_sent.append(entry)      # never sent, so legitimately absent from a sent-date list
        else:
            local_only.append(entry)

    # No provider time yet (claimed in the window): reported, never dropped.
    unsettled = [{
        'notificationId': w.notification.pk,
        'orderId': w.notification.order_id,
        'localStatus': w.outcome,
        'claimedAt': w.claimed_at.isoformat(),
    } for w in ProviderWrite.objects.filter(
        operation=ProviderWrite.SEND, provider_id__isnull=True,
        claimed_at__gte=start, claimed_at__lt=end).select_related('notification')]

    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'fromNumber': settings.TWILIO_FROM_NUMBER,
        'providerCount': len(by_sid),
        'matchedCount': len(matched),
        'matched': matched,
        'providerOnly': provider_only,
        'localOnly': local_only,
        'notSent': not_sent,
        'unsettled': unsettled,
    }
