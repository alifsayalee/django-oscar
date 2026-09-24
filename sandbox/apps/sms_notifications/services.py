"""
Order, contact-number and notification operations behind the /api/ views.

A message that cannot be sent never fails the operation that triggered it:
the order change is committed first, and each message's outcome is recorded
on its ``Notification`` instead of being raised.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.apps.order.exceptions import InvalidOrderStatus
from oscar.core.loading import get_class, get_model

from . import sending
from .models import ContactNumber, Notification

logger = logging.getLogger(__name__)

Basket = get_model('basket', 'Basket')
Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
SurchargeApplicator = get_class('checkout.applicator', 'SurchargeApplicator')
Selector = get_class('partner.strategy', 'Selector')
FreeShipping = get_class('shipping.methods', 'Free')

STATUS_DISPATCHED = 'Dispatched'
STATUS_CANCELLED = 'Cancelled'
STATUS_COMPLETE = 'Complete'

MAX_LINES = 50
MAX_QUANTITY = 99
MAX_IDEMPOTENCY_KEY = 100
REFRESH_LIMIT = 25


class ApiProblem(Exception):
    """A request this API refuses, with the status and a message we wrote."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Contact numbers
# ---------------------------------------------------------------------------

def active_contacts(user):
    if user is None:
        return ContactNumber.objects.none()
    return ContactNumber.objects.filter(user=user, deleted_at__isnull=True).order_by('pk')


def register_contact(user, raw_number) -> tuple[ContactNumber, bool]:
    if not isinstance(raw_number, str) or not raw_number.strip() or len(raw_number) > 40:
        raise ApiProblem(400, 'invalid_request', 'Provide "phoneNumber" as a string.')
    # Raises ProviderRejected (400) when the provider does not recognise it.
    canonical, country = sending.lookup_number(raw_number.strip())
    try:
        with transaction.atomic():
            contact = ContactNumber.objects.create(
                user=user, phone_number=canonical, country_code=country)
        return contact, True
    except IntegrityError:
        return active_contacts(user).get(phone_number=canonical), False


def delete_contact(user, contact_id) -> dict:
    """
    Remove a number: it is no longer listed or messaged, and any follow-up
    still queued with the provider for it is called off.
    """
    try:
        contact = ContactNumber.objects.get(pk=contact_id, user=user)
    except ContactNumber.DoesNotExist:
        raise ApiProblem(404, 'not_found', 'No such contact number.')
    ContactNumber.objects.filter(pk=contact.pk, deleted_at__isnull=True).update(
        deleted_at=timezone.now())
    results = [
        {'notificationId': n.pk, 'cancelState': sending.cancel_scheduled(n)}
        for n in _followups_to_call_off(Notification.objects.filter(contact=contact))
    ]
    return {
        'contactNumberId': contact.pk,
        'deleted': True,
        'followupsCalledOff': results,
        'allFollowupsCalledOff': all(r['cancelState'] == Notification.CANCEL_DONE for r in results),
    }


def _followups_to_call_off(queryset):
    return list(
        queryset.filter(kind=Notification.KIND_DELIVERY_FOLLOWUP)
        .exclude(cancel_state=Notification.CANCEL_DONE)
        .exclude(outcome=Notification.DONE)
        .exclude(outcome=Notification.FAILED, provider_sid='')
        .select_related('contact')
    )


# ---------------------------------------------------------------------------
# Messages about an order
# ---------------------------------------------------------------------------

def _order_texts(order) -> dict[str, str]:
    return {
        Notification.KIND_ORDER_PLACED:
            'Oscar Sandbox: thanks! Your order %s has been placed.' % order.number,
        Notification.KIND_DISPATCHED:
            'Oscar Sandbox: good news - your order %s is on its way.' % order.number,
        Notification.KIND_DELIVERY_FOLLOWUP:
            'Oscar Sandbox: how did the delivery of order %s go? Reply and let us know.' % order.number,
        Notification.KIND_CANCELLED:
            'Oscar Sandbox: your order %s has been cancelled.' % order.number,
    }


def _order_reference(order, kind, contact) -> str:
    return '%s:order:%s:%s:%s' % (settings.SMS_NOTIFICATIONS_INSTALL_PREFIX, order.pk, kind, contact.pk)


def _notify(order, kind, send_at: datetime | None = None) -> list[Notification]:
    """Message every number the order's shopper has on file. Never raises."""
    text = _order_texts(order)[kind]
    sent = []
    for contact in active_contacts(order.user):
        reference = _order_reference(order, kind, contact)
        try:
            notification, _ = sending.safe_send({
                'user': order.user, 'order': order, 'contact': contact, 'kind': kind,
                'reference': reference, 'body': sending.message_text(text, reference),
                'send_at': send_at,
            })
        except Exception:
            # The order operation has already succeeded; the claim row (if any)
            # records where this message got to.
            logger.exception('could not send %s message for order %s', kind, order.pk)
            continue
        sent.append(notification)
    return sent


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _parse_lines(payload) -> list[tuple[int, int]]:
    lines = payload.get('lines') if isinstance(payload, dict) else None
    if not isinstance(lines, list) or not lines or len(lines) > MAX_LINES:
        raise ApiProblem(400, 'invalid_request',
                         'Provide "lines": a list of {"productId": int, "quantity": int}.')
    parsed: dict[int, int] = defaultdict(int)
    for line in lines:
        if not isinstance(line, dict):
            raise ApiProblem(400, 'invalid_request', 'Each line must be an object.')
        product_id, quantity = line.get('productId'), line.get('quantity', 1)
        if (not isinstance(product_id, int) or isinstance(product_id, bool)
                or not isinstance(quantity, int) or isinstance(quantity, bool)
                or not 1 <= quantity <= MAX_QUANTITY):
            raise ApiProblem(400, 'invalid_request',
                             'productId must be an integer and quantity between 1 and %s.' % MAX_QUANTITY)
        parsed[product_id] += quantity
    return list(parsed.items())


def place_order(request, payload):
    user = request.user
    wanted = _parse_lines(payload)
    strategy = Selector().strategy(request=request, user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in wanted:
            try:
                product = Product.objects.get(pk=product_id)
            except Product.DoesNotExist:
                raise ApiProblem(400, 'unknown_product', 'Product %s does not exist.' % product_id)
            if product.is_parent:
                raise ApiProblem(400, 'not_purchasable',
                                 'Product %s is a parent product; order one of its variants.' % product_id)
            info = strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ApiProblem(400, 'not_purchasable', 'Product %s: %s' % (product_id, reason))
            basket.add_product(product, quantity)
        shipping_method = FreeShipping()
        shipping_charge = shipping_method.calculate(basket)
        surcharges = SurchargeApplicator(request).get_applicable_surcharges(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge, surcharges=surcharges)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user, request=request, surcharges=surcharges)
        basket.set_as_submitted()
    notifications = _notify(order, Notification.KIND_ORDER_PLACED)
    return order, notifications


def _staff_order(order_id):
    try:
        return Order.objects.select_for_update().get(pk=order_id)
    except Order.DoesNotExist:
        raise ApiProblem(404, 'not_found', 'No such order.')


def dispatch_order(order_id):
    """
    Mark the order dispatched, tell the shopper, and queue the delivery
    follow-up with the provider. Repeating it is safe: messages already sent
    are answered from their records.
    """
    with transaction.atomic():
        order = _staff_order(order_id)
        if order.status == STATUS_CANCELLED:
            raise ApiProblem(409, 'order_cancelled', 'A cancelled order cannot be dispatched.')
        if order.status != STATUS_DISPATCHED:
            try:
                order.set_status(STATUS_DISPATCHED)
            except InvalidOrderStatus:
                raise ApiProblem(409, 'invalid_status',
                                 'An order with status %r cannot be dispatched.' % order.status)
    dispatched = _notify(order, Notification.KIND_DISPATCHED)
    send_at = timezone.now() + timedelta(days=settings.SMS_NOTIFICATIONS_FOLLOWUP_DELAY_DAYS)
    followups = _notify(order, Notification.KIND_DELIVERY_FOLLOWUP, send_at=send_at)

    # The order may have been cancelled (or a number removed) while the
    # follow-up was being queued: call it off rather than let it go out.
    order.refresh_from_db()
    for followup in followups:
        followup.contact.refresh_from_db()
        if order.status == STATUS_CANCELLED or followup.contact.deleted_at is not None:
            sending.cancel_scheduled(followup)
    return order, dispatched + followups


def cancel_order(order_id):
    """
    Cancel the order, call off any follow-up not yet sent, then tell the
    shopper. Repeating it re-attempts any follow-up not confirmed called off.
    """
    with transaction.atomic():
        order = _staff_order(order_id)
        if order.status != STATUS_CANCELLED:
            try:
                order.set_status(STATUS_CANCELLED)
            except InvalidOrderStatus:
                raise ApiProblem(409, 'invalid_status',
                                 'An order with status %r cannot be cancelled.' % order.status)
    called_off = []
    for followup in _followups_to_call_off(order.sms_notifications.all()):
        called_off.append({'notificationId': followup.pk,
                           'cancelState': sending.cancel_scheduled(followup)})
    cancelled = _notify(order, Notification.KIND_CANCELLED)
    return order, called_off, cancelled


# ---------------------------------------------------------------------------
# Reading notifications
# ---------------------------------------------------------------------------

def refresh_unsettled(notifications) -> bool:
    """Ask the provider about messages not settled yet. False if any refresh failed."""
    ok = True
    for notification in [n for n in notifications if n.outcome in (
            Notification.SENDING, Notification.PENDING, Notification.UNKNOWN)][:REFRESH_LIMIT]:
        ok = sending.refresh_notification(notification) and ok
    return ok


def notification_json(n: Notification) -> dict:
    return {
        'notificationId': n.pk,
        'orderId': n.order_id,
        'kind': n.kind,
        'outcome': n.outcome,
        'providerStatus': n.provider_status or None,
        'providerSid': n.provider_sid or None,
        'errorCode': n.error_code,
        'errorMessage': n.error_message or None,
        'to': sending.mask_number(n.contact.phone_number),
        'contactNumberId': n.contact_id,
        'scheduledFor': n.send_at.isoformat() if n.send_at else None,
        'providerTime': n.provider_time.isoformat() if n.provider_time else None,
        'cancelState': n.cancel_state or None,
        'text': None if n.content_disposed_at else sending.base_text(n.body),
        'contentDisposedAt': n.content_disposed_at.isoformat() if n.content_disposed_at else None,
        'resendOf': n.resend_of_id,
        'createdAt': n.created_at.isoformat(),
    }


def order_json(order, notifications) -> dict:
    return {
        'orderId': order.pk,
        'number': order.number,
        'status': order.status,
        'total': str(order.total_incl_tax.quantize(Decimal('0.01'))),
        'currency': order.currency,
        'placedAt': order.date_placed.isoformat() if order.date_placed else None,
        'lines': [{'productId': line.product_id, 'title': line.title, 'quantity': line.quantity}
                  for line in order.lines.all()],
        'notifications': [notification_json(n) for n in notifications],
    }


# ---------------------------------------------------------------------------
# Operator actions on a notification
# ---------------------------------------------------------------------------

def _notification(notification_id) -> Notification:
    try:
        return Notification.objects.select_related('order', 'contact').get(pk=notification_id)
    except Notification.DoesNotExist:
        raise ApiProblem(404, 'not_found', 'No such notification.')


def resend_notification(notification_id, idempotency_key) -> tuple[Notification, bool]:
    if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
            or len(idempotency_key) > MAX_IDEMPOTENCY_KEY):
        raise ApiProblem(400, 'idempotency_key_required',
                         'Send an Idempotency-Key header (1-%s characters).' % MAX_IDEMPOTENCY_KEY)
    original = _notification(notification_id)
    reference = '%s:resend:%s:%s' % (settings.SMS_NOTIFICATIONS_INSTALL_PREFIX, original.pk,
                                     idempotency_key.strip())
    fields = {
        'user': original.user, 'order': original.order, 'contact': original.contact,
        'kind': original.kind, 'reference': reference,
        'body': sending.message_text(sending.base_text(original.body), reference),
        'resend_of': original, 'idempotency_key': idempotency_key.strip(),
    }
    if Notification.objects.filter(reference=reference).exists():
        # A repeat under the same key: answered from (or settling) that attempt.
        return sending.safe_send(fields)

    sending.refresh_notification(original)
    if original.outcome != Notification.FAILED:
        raise ApiProblem(409, 'not_resendable',
                         'Only a message that did not reach the shopper can be re-sent '
                         '(this one is %r).' % original.outcome)
    if original.content_disposed_at or not original.body:
        raise ApiProblem(409, 'content_disposed', 'The content of this message was disposed of.')
    if original.contact.deleted_at is not None:
        raise ApiProblem(409, 'number_removed', 'The shopper removed this number.')
    if original.order.status == STATUS_CANCELLED and original.kind in (
            Notification.KIND_DELIVERY_FOLLOWUP, Notification.KIND_DISPATCHED,
            Notification.KIND_ORDER_PLACED):
        raise ApiProblem(409, 'order_cancelled', 'The order was cancelled; this message no longer applies.')
    return sending.safe_send(fields)


def dispose_content(notification_id) -> Notification:
    notification = _notification(notification_id)
    if notification.content_disposed_at is not None:
        return notification
    return sending.redact_content(notification)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile(start: datetime, end: datetime) -> dict:
    fetched = sending.fetch_provider_messages(start, end)
    provider_by_sid = {record.sid: record for record in fetched.records}

    # This app's side, on the same clock: the provider's time we stored.
    local = list(Notification.objects.filter(
        provider_time__gte=start, provider_time__lt=end).exclude(provider_sid='')
        .select_related('contact'))
    unsettled = list(Notification.objects.filter(
        provider_sid='', claimed_at__gte=start, claimed_at__lt=end,
        outcome__in=[Notification.SENDING, Notification.UNKNOWN]).select_related('contact'))
    local_sids = {n.provider_sid for n in local}
    # Messages in the provider list that we did record, but whose stored time
    # has moved out of the window (e.g. a follow-up created earlier, sent now).
    late = {n.provider_sid: n for n in Notification.objects.filter(
        provider_sid__in=[sid for sid in provider_by_sid if sid not in local_sids])}

    matched, local_only, not_sent = [], [], []
    for n in local + list(late.values()):
        record = provider_by_sid.pop(n.provider_sid, None)
        if record is None:
            entry = {'notificationId': n.pk, 'orderId': n.order_id, 'providerSid': n.provider_sid,
                     'outcome': n.outcome, 'providerStatus': n.provider_status or None,
                     'providerTime': n.provider_time.isoformat() if n.provider_time else None}
            if n.provider_status in [s.value for s in sending.NOT_SENT_STATUSES]:
                not_sent.append(entry)       # scheduled / called off: never sent, not a gap
            else:
                local_only.append(entry)
            continue
        matched.append({
            'notificationId': n.pk, 'orderId': n.order_id, 'providerSid': record.sid,
            'localOutcome': n.outcome, 'localProviderStatus': n.provider_status or None,
            'providerStatus': record.status,
            'statusAgrees': (n.provider_status or None) == record.status,
            'providerTime': record.provider_time.isoformat() if record.provider_time else None,
        })
    provider_only = [
        {'providerSid': r.sid, 'providerStatus': r.status, 'errorCode': r.error_code,
         'providerTime': r.provider_time.isoformat() if r.provider_time else None,
         'to': sending.mask_number(r.to)}
        for r in provider_by_sid.values()
    ]
    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'sender': sending.mask_number(settings.TWILIO_FROM_NUMBER),
        'providerPagesFetched': fetched.pages,
        'summary': {
            'providerMessages': len(fetched.records),
            'matched': len(matched),
            'statusDisagreements': sum(1 for m in matched if not m['statusAgrees']),
            'providerOnly': len(provider_only),
            'localOnly': len(local_only),
            'notSent': len(not_sent),
            'unsettled': len(unsettled),
        },
        'matched': matched,
        'providerOnly': provider_only,
        'localOnly': local_only,
        'notSent': not_sent,
        'unsettled': [{'notificationId': n.pk, 'orderId': n.order_id, 'outcome': n.outcome,
                       'claimedAt': n.claimed_at.isoformat()} for n in unsettled],
    }
