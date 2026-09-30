"""
Order SMS notifications: what gets sent when, and what became of it.

Every lifecycle message follows the same order: a claim row is written in the
same transaction as the order change it describes (a unique constraint rejects
a second claim), the transaction commits, and only then is the message handed to
Twilio and the provider's answer recorded. A failure to message never fails the
order operation itself.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import twilio_gateway
from .models import ContactNumber, Notification, mask_number
from .twilio_gateway import FINAL_STATUSES, IN_FLIGHT_STATUSES, GatewayNotConfigured, ProviderError

logger = logging.getLogger(__name__)

Order = get_model('order', 'Order')
ShippingEventType = get_model('order', 'ShippingEventType')
Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
OrderCreator = get_class('order.utils', 'OrderCreator')
EventHandler = get_class('order.processing', 'EventHandler')
Repository = get_class('shipping.repository', 'Repository')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Selector = get_class('partner.strategy', 'Selector')
InvalidOrderStatus = get_class('order.exceptions', 'InvalidOrderStatus')

STATUS_DISPATCHED = 'Dispatched'
STATUS_CANCELLED = 'Cancelled'

MAX_ORDER_LINES = 50
MAX_LINE_QUANTITY = 100
# Upper bound on provider status look-ups made while serving one request.
MAX_REFRESHES_PER_REQUEST = 25
RESENDABLE_PROVIDER_STATUSES = frozenset({'failed', 'undelivered'})


class ServiceError(Exception):
    def __init__(self, status_code, message, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


def _gateway():
    return twilio_gateway.get_gateway()


# ---------------------------------------------------------------------------
# Contact numbers
# ---------------------------------------------------------------------------

def primary_contact(user):
    """The number a shopper is messaged on: their most recently registered one."""
    return ContactNumber.objects.filter(user=user).order_by('-created_at', '-pk').first()


def _clean_number_input(raw_number, country_code):
    if not isinstance(raw_number, str) or not raw_number.strip():
        raise ServiceError(400, 'phoneNumber is required.')
    if country_code is not None and (not isinstance(country_code, str) or len(country_code) != 2):
        raise ServiceError(400, 'countryCode must be a two-letter ISO country code.')
    # Keep only what can be part of a phone number; Twilio decides the rest.
    cleaned = ''.join(ch for ch in raw_number.strip() if ch.isdigit() or ch == '+')
    if not cleaned or len(cleaned) > 20:
        raise ServiceError(400, 'That is not a usable mobile number.', reasons=['NOT_A_NUMBER'])
    return cleaned


def register_contact_number(user, raw_number, country_code=None):
    cleaned = _clean_number_input(raw_number, country_code)
    try:
        result = _gateway().lookup_number(cleaned, country_code.upper() if country_code else None)
    except GatewayNotConfigured:
        raise ServiceError(503, 'Messaging is not configured; numbers cannot be verified right now.')
    except ProviderError as e:
        if e.rejected and e.provider_status == 400:
            raise ServiceError(400, 'That is not a usable mobile number.', reasons=['INVALID'])
        raise ServiceError(e.status_code, 'Could not verify the number with the messaging provider.')
    if not result.valid or not result.e164:
        raise ServiceError(400, 'That is not a usable mobile number.', reasons=result.reasons)
    try:
        with transaction.atomic():
            contact, created = ContactNumber.objects.get_or_create(user=user, phone_number=result.e164)
    except IntegrityError:
        contact, created = ContactNumber.objects.get(user=user, phone_number=result.e164), False
    logger.info('Contact number #%s %s for user #%s', contact.pk, 'registered' if created else 'reused', user.pk)
    return contact, created


def remove_contact_number(user, contact_id):
    contact = ContactNumber.objects.filter(user=user, pk=contact_id).first()
    if contact is None:
        raise ServiceError(404, 'Contact number not found.')
    # Nothing may be sent to this number again: stop anything not yet handed to
    # Twilio, and call off anything Twilio is holding for later.
    Notification.objects.filter(
        contact_number=contact, send_state=Notification.STATE_PENDING,
    ).update(send_state=Notification.STATE_SUPPRESSED, updated_at=timezone.now())
    for notification in _outstanding_scheduled(contact.notifications.all()):
        if not _call_off(notification):
            raise ServiceError(
                502, 'A scheduled message to this number could not be called off; the number was not removed. '
                     'Try again shortly.')
    contact.delete()
    logger.info('Contact number #%s removed by user #%s', contact_id, user.pk)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _parse_lines(lines):
    if not isinstance(lines, list) or not lines:
        raise ServiceError(400, 'lines must be a non-empty list of {productId, quantity}.')
    if len(lines) > MAX_ORDER_LINES:
        raise ServiceError(400, 'Too many lines (max %d).' % MAX_ORDER_LINES)
    quantities = {}
    for line in lines:
        if not isinstance(line, dict):
            raise ServiceError(400, 'Each line must be an object with productId and quantity.')
        product_id, quantity = line.get('productId'), line.get('quantity', 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise ServiceError(400, 'productId must be a positive integer.')
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_LINE_QUANTITY:
            raise ServiceError(400, 'quantity must be an integer between 1 and %d.' % MAX_LINE_QUANTITY)
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    return quantities


def place_order(user, request, lines):
    quantities = _parse_lines(lines)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for product_id, quantity in quantities.items():
            product = Product.objects.filter(pk=product_id).first()
            if product is None or not product.is_public or product.is_parent:
                raise ServiceError(400, 'Product %s cannot be ordered.' % product_id)
            info = basket.strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ServiceError(400, 'Product %s: %s' % (product_id, reason))
            basket.add_product(product, quantity)
        shipping_method = Repository().get_default_shipping_method(
            basket=basket, user=user, request=request)
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user, request=request)
        basket.submit()
        claims = _claim(order, [
            (Notification.KIND_PLACED,
             'Thanks for your order %s. We have received it and will let you know when it ships.' % order.number,
             None),
        ])
    _deliver_all(claims)
    logger.info('Order #%s placed by user #%s', order.pk, user.pk)
    return order


def _locked_order(order_id):
    order = Order.objects.select_for_update().filter(pk=order_id).first()
    if order is None:
        raise ServiceError(404, 'Order not found.')
    return order


def dispatch_order(order_id):
    delay = timedelta(seconds=settings.ORDER_NOTIFICATIONS_FOLLOW_UP_DELAY)
    try:
        with transaction.atomic():
            order = _locked_order(order_id)
            if order.status == STATUS_DISPATCHED:
                raise ServiceError(409, 'Order %s has already been dispatched.' % order.number)
            try:
                order.set_status(STATUS_DISPATCHED)
            except InvalidOrderStatus:
                raise ServiceError(409, 'Order %s cannot be dispatched from status %s.' % (order.number, order.status))
            event_type, __ = ShippingEventType.objects.get_or_create(
                code='dispatched', defaults={'name': 'Dispatched'})
            lines = list(order.lines.all())
            EventHandler().create_shipping_event(order, event_type, lines, [line.quantity for line in lines])
            claims = _claim(order, [
                (Notification.KIND_DISPATCHED,
                 'Good news: your order %s is on its way.' % order.number, None),
                (Notification.KIND_FOLLOW_UP,
                 'How did the delivery of your order %s go? Reply to this message to let us know.' % order.number,
                 timezone.now() + delay),
            ])
    except IntegrityError:
        raise ServiceError(409, 'Order is already being dispatched.')
    _deliver_all(claims)
    logger.info('Order #%s dispatched', order.pk)
    return order


def cancel_order(order_id):
    """
    Cancel an order. Calling this again on a cancelled order re-attempts calling
    off any follow-up whose cancellation could not be confirmed, and sends nothing new.
    """
    claims = []
    try:
        with transaction.atomic():
            order = _locked_order(order_id)
            if order.status != STATUS_CANCELLED:
                try:
                    order.set_status(STATUS_CANCELLED)
                except InvalidOrderStatus:
                    raise ServiceError(
                        409, 'Order %s cannot be cancelled from status %s.' % (order.number, order.status))
                claims = _claim(order, [
                    (Notification.KIND_CANCELLED,
                     'Your order %s has been cancelled. Contact us if you have any questions.' % order.number,
                     None),
                ])
            # Follow-ups not yet handed to Twilio will now never be.
            Notification.objects.filter(
                order=order, kind=Notification.KIND_FOLLOW_UP, send_state=Notification.STATE_PENDING,
            ).update(send_state=Notification.STATE_SUPPRESSED, updated_at=timezone.now())
    except IntegrityError:
        raise ServiceError(409, 'Order is already being cancelled.')
    # Calling off the follow-up comes first: it is the message that must never arrive.
    for notification in _outstanding_scheduled(order.sms_notifications.filter(kind=Notification.KIND_FOLLOW_UP)):
        _call_off(notification)
    _deliver_all(claims)
    logger.info('Order #%s cancelled', order.pk)
    return order


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _claim(order, specs):
    """
    Write one claim row per message. Must run inside the transaction that
    changes the order; the unique (order, kind) constraint rejects a second claim.
    """
    contact = primary_contact(order.user) if order.user_id else None
    if contact is None:
        return []  # A shopper with no number on file is simply not messaged.
    return [
        Notification.objects.create(order=order, contact_number=contact, kind=kind, body=body, send_at=send_at)
        for kind, body, send_at in specs
    ]


def _deliver_all(notifications):
    for notification in notifications:
        _deliver(notification)


def _should_not_send(notification):
    if notification.contact_number_id is None:
        return True
    if notification.kind in (Notification.KIND_DISPATCHED, Notification.KIND_FOLLOW_UP):
        return Order.objects.filter(pk=notification.order_id, status=STATUS_CANCELLED).exists()
    return False


def _deliver(notification):
    """
    Hand one claimed notification to Twilio and record the outcome. Never raises:
    a message that cannot be sent must not fail the operation that triggered it.
    """
    claimed = Notification.objects.filter(
        pk=notification.pk, send_state=Notification.STATE_PENDING,
    ).update(send_state=Notification.STATE_SENDING, updated_at=timezone.now())
    notification.refresh_from_db()
    if not claimed:
        return notification  # somebody else has it, or it was suppressed
    if _should_not_send(notification):
        notification.send_state = Notification.STATE_SUPPRESSED
        notification.save(update_fields=['send_state', 'updated_at'])
        return notification
    try:
        snapshot = _gateway().send_sms(
            notification.contact_number.phone_number, notification.body, send_at=notification.send_at)
    except GatewayNotConfigured:
        logger.error('Notification #%s not sent: Twilio is not configured', notification.pk)
        notification.send_state = Notification.STATE_REJECTED
        notification.error_message = 'Messaging is not configured.'
    except ProviderError as e:
        notification.send_state = Notification.STATE_UNKNOWN if e.outcome_unknown else Notification.STATE_REJECTED
        notification.error_code = e.provider_code
        notification.error_message = e.provider_message or e.message
    except Exception:
        # A bug must not fail the order operation; we cannot tell whether Twilio got it.
        logger.exception('Notification #%s: unexpected error while sending', notification.pk)
        notification.send_state = Notification.STATE_UNKNOWN
        notification.error_message = 'Unexpected error while sending.'
    else:
        notification.send_state = Notification.STATE_SENT
        _apply_snapshot(notification, snapshot)
    notification.save()
    logger.info('Notification #%s (%s, order #%s): %s %s', notification.pk, notification.kind,
                notification.order_id, notification.send_state, notification.provider_status or '')
    # The order may have been cancelled (or the number removed) while this
    # follow-up was being scheduled: call it off straight away.
    if notification.kind == Notification.KIND_FOLLOW_UP and notification.send_state == Notification.STATE_SENT:
        notification.refresh_from_db()
        if _should_not_send(notification) and notification.provider_status not in FINAL_STATUSES:
            _call_off(notification)
    return notification


def _apply_snapshot(notification, snapshot):
    notification.provider_sid = snapshot.sid
    if snapshot.status:
        notification.provider_status = snapshot.status
    if snapshot.from_number:
        notification.from_number = snapshot.from_number
    notification.error_code = snapshot.error_code
    notification.error_message = snapshot.error_message or ''
    notification.provider_date_sent = snapshot.date_sent
    notification.last_checked_at = timezone.now()


def _outstanding_scheduled(queryset):
    """Messages Twilio may still be holding to send later."""
    return [
        n for n in queryset.filter(provider_sid__isnull=False).exclude(provider_status__in=FINAL_STATUSES)
        if n.send_at is not None or n.provider_status == 'scheduled' or n.cancel_failed
    ]


def _call_off(notification):
    """
    Make sure a scheduled message never goes out. Returns True once Twilio
    confirms it is no longer pending (cancelled, or already past the point of
    no return), False when that could not be confirmed.
    """
    gateway = None
    try:
        gateway = _gateway()
        snapshot = gateway.cancel_scheduled(notification.provider_sid)
    except (ProviderError, GatewayNotConfigured) as e:
        # Twilio refuses to cancel a message that is no longer scheduled; ask what it is now.
        logger.warning('Notification #%s: cancel not confirmed (%s)', notification.pk, type(e).__name__)
        snapshot = None
        if gateway is not None:
            try:
                snapshot = gateway.fetch(notification.provider_sid)
            except ProviderError:
                snapshot = None
    if snapshot is None:
        notification.cancel_failed = True
        notification.save(update_fields=['cancel_failed', 'updated_at'])
        return False
    _apply_snapshot(notification, snapshot)
    notification.cancel_failed = notification.provider_status in IN_FLIGHT_STATUSES
    notification.save()
    if notification.provider_status != 'canceled':
        logger.warning('Notification #%s could not be cancelled; provider status %s',
                       notification.pk, notification.provider_status)
    return not notification.cancel_failed


def refresh_statuses(notifications, limit=MAX_REFRESHES_PER_REQUEST):
    """Ask Twilio what became of messages whose outcome is not final yet."""
    remaining = limit
    for notification in notifications:
        if remaining <= 0:
            break
        if not notification.provider_sid or notification.provider_status in FINAL_STATUSES:
            continue
        remaining -= 1
        try:
            snapshot = _gateway().fetch(notification.provider_sid)
        except (ProviderError, GatewayNotConfigured):
            continue  # keep the last known state
        _apply_snapshot(notification, snapshot)
        notification.save()


# ---------------------------------------------------------------------------
# Operator actions on individual messages
# ---------------------------------------------------------------------------

def _get_notification(notification_id):
    notification = Notification.objects.select_related('order', 'contact_number').filter(pk=notification_id).first()
    if notification is None:
        raise ServiceError(404, 'Notification not found.')
    return notification


def resend_notification(notification_id, idempotency_key):
    """Returns (notification, created)."""
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 128:
        raise ServiceError(400, 'An idempotency key (1-128 characters) is required.')
    idempotency_key = idempotency_key.strip()
    original = _get_notification(notification_id)
    earlier = Notification.objects.filter(resend_of=original, idempotency_key=idempotency_key).first()
    if earlier is not None:
        return earlier, False

    refresh_statuses([original])
    if original.content_disposed_at:
        raise ServiceError(409, 'The content of this message was disposed of; it cannot be re-sent.')
    if original.kind in (Notification.KIND_FOLLOW_UP, Notification.KIND_DISPATCHED) \
            and original.order.status == STATUS_CANCELLED:
        raise ServiceError(409, 'The order was cancelled; this message must not be sent.')
    did_not_arrive = (
        original.send_state in (Notification.STATE_REJECTED, Notification.STATE_UNKNOWN)
        or original.provider_status in RESENDABLE_PROVIDER_STATUSES)
    if not did_not_arrive:
        raise ServiceError(
            409, 'Only a message that did not reach the shopper can be re-sent (state %s, provider status %s).'
                 % (original.send_state, original.provider_status or 'none'))
    contact = primary_contact(original.order.user) if original.order.user_id else None
    if contact is None:
        raise ServiceError(409, 'The shopper has no number on file.')

    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                order=original.order, contact_number=contact, kind=original.kind, body=original.body,
                resend_of=original, idempotency_key=idempotency_key)
    except IntegrityError:
        # The same key arrived twice: the store refused the second claim.
        return Notification.objects.get(resend_of=original, idempotency_key=idempotency_key), False

    _deliver(notification)
    if notification.send_state in (Notification.STATE_REJECTED, Notification.STATE_SUPPRESSED):
        # Definitely not sent: release the claim so the same key can be retried.
        message = notification.error_message or 'The message could not be sent.'
        code = notification.error_code
        notification.delete()
        raise ServiceError(502, 'Re-send failed: %s' % message, providerErrorCode=code)
    return notification, True


def dispose_content(notification_id):
    notification = _get_notification(notification_id)
    if notification.content_disposed_at:
        return notification
    if notification.send_state in (Notification.STATE_PENDING, Notification.STATE_SENDING):
        raise ServiceError(409, 'The message is being sent; try again shortly.')
    if notification.send_state == Notification.STATE_UNKNOWN and not notification.provider_sid:
        raise ServiceError(
            409, 'It is not known whether the provider holds this message; reconcile it before disposing.')
    if notification.provider_sid:
        refresh_statuses([notification])
        if notification.provider_status in IN_FLIGHT_STATUSES:
            raise ServiceError(
                409, 'The message has not been delivered yet (provider status %s); its content can be disposed '
                     'of once it has gone out or been cancelled.' % notification.provider_status)
        try:
            snapshot = _gateway().redact(notification.provider_sid)
        except GatewayNotConfigured:
            raise ServiceError(503, 'Messaging is not configured.')
        except ProviderError as e:
            raise ServiceError(e.status_code if e.status_code != 422 else 409,
                               'The provider did not erase the message: %s' % (e.provider_message or e.message))
        _apply_snapshot(notification, snapshot)
    notification.body = ''
    notification.content_disposed_at = timezone.now()
    notification.save()
    logger.info('Notification #%s content disposed', notification.pk)
    return notification


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile(start, end):
    try:
        gateway = _gateway()
        provider_messages = gateway.list_sent_from_our_number(start, end)
    except GatewayNotConfigured:
        raise ServiceError(503, 'Messaging is not configured.')
    except ProviderError as e:
        raise ServiceError(e.status_code, 'Could not read the provider\'s message log: %s' % e.message)

    by_sid = {m.sid: m for m in provider_messages}
    known = {n.provider_sid: n for n in Notification.objects.filter(provider_sid__in=list(by_sid))}

    # What this app believes it sent in the range: messages Twilio accepted
    # whose send time (or, until known, creation time) falls in the range.
    in_range = Notification.objects.filter(provider_sid__isnull=False).exclude(
        provider_status__in=['scheduled', 'canceled'])
    local_candidates = [
        n for n in in_range.filter(created_at__lte=end)
        if start <= (n.provider_date_sent or n.created_at) <= end
    ]

    matched, provider_only = [], []
    for sid, message in by_sid.items():
        notification = known.get(sid)
        if notification is None:
            provider_only.append({
                'providerSid': sid,
                'providerStatus': message.status,
                'to': mask_number(message.to_number),
                'dateSent': _iso(message.date_sent),
                'errorCode': message.error_code,
            })
            continue
        app_status_before = notification.provider_status
        _apply_snapshot(notification, message)
        notification.save()
        matched.append({
            'notificationId': notification.pk,
            'orderId': notification.order_id,
            'kind': notification.kind,
            'providerSid': sid,
            'providerStatus': message.status,
            'appStatusBeforeReconciliation': app_status_before or None,
            'statusAgreed': app_status_before == message.status,
            'dateSent': _iso(message.date_sent),
        })
    app_only = [
        {
            'notificationId': n.pk,
            'orderId': n.order_id,
            'kind': n.kind,
            'providerSid': n.provider_sid,
            'appStatus': n.provider_status or None,
            'appDate': _iso(n.provider_date_sent or n.created_at),
        }
        for n in local_candidates if n.provider_sid not in by_sid
    ]
    unknown_outcome = [
        {'notificationId': n.pk, 'orderId': n.order_id, 'kind': n.kind, 'createdAt': _iso(n.created_at)}
        for n in Notification.objects.filter(
            send_state=Notification.STATE_UNKNOWN, provider_sid__isnull=True,
            created_at__gte=start, created_at__lte=end)
    ]
    return {
        'from': _iso(start),
        'to': _iso(end),
        'fromNumber': gateway.from_number,
        'providerMessageCount': len(by_sid),
        'appMessageCount': len({n.provider_sid for n in local_candidates} | set(known)),
        'matchedCount': len(matched),
        'providerOnlyCount': len(provider_only),
        'appOnlyCount': len(app_only),
        'matched': matched,
        'providerOnly': provider_only,
        'appOnly': app_only,
        'appUnknownOutcome': unknown_outcome,
    }


def _iso(value):
    return value.isoformat() if value else None
