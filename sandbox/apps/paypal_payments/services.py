"""
Order, payment and saved-card workflows.

Every PayPal call here runs outside a database transaction, bracketed by two
short ones: a *claim* that moves the payment into an in-flight state with a
conditional UPDATE (so a double click finds nothing to claim and cannot
authorize or capture twice), and a *record* that stores what PayPal answered.
A claim whose outcome is unknown (PayPal never answered) is kept, and may be
resumed after ``CLAIM_LEASE`` with the same PayPal-Request-Id, which makes
PayPal replay its original answer instead of acting again.
"""
import hashlib
import logging
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import money
from .gateway import (
    PaymentGatewayError,
    PayPalGateway,
    amount_of,
    timestamp,
    value,
)
from .models import PayPalCustomer, PayPalPayment, PayPalRefund

logger = logging.getLogger(__name__)

Bankcard = get_model('payment', 'Bankcard')
Basket = get_model('basket', 'Basket')
Country = get_model('address', 'Country')
Order = get_model('order', 'Order')
PaymentEventType = get_model('order', 'PaymentEventType')
Product = get_model('catalogue', 'Product')
ShippingAddress = get_model('order', 'ShippingAddress')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Transaction = get_model('payment', 'Transaction')

Applicator = get_class('offer.applicator', 'Applicator')
EventHandler = get_class('order.processing', 'EventHandler')
Free = get_class('shipping.methods', 'Free')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Selector = get_class('partner.strategy', 'Selector')

# The PayPal gateway every workflow uses; tests swap in one with a stub transport.
paypal = PayPalGateway()

SOURCE_TYPE_NAME = 'PayPal'
# Oscar order statuses (sandbox OSCAR_ORDER_STATUS_PIPELINE).
STATUS_PAID = 'Being processed'
STATUS_FULFILLED = 'Complete'
STATUS_CANCELLED = 'Cancelled'

# PayPal honours an authorization for three days, after which it should be
# renewed; it can be renewed until PayPal's expiration_time (29 days).
HONOR_PERIOD = timedelta(days=3)
# Longer than any single PayPal call can take (the client timeout).
CLAIM_LEASE = timedelta(minutes=2)
# PayPal's transaction search can lag live activity by up to three hours.
REPORTING_LAG = timedelta(hours=3)
MAX_LINE_QUANTITY = 100


class ServiceError(Exception):
    """A request this app refuses, with the HTTP status the API answers with."""

    def __init__(self, message, *, http_status, code, **details):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code
        self.details = details


def _not_found(what='order'):
    return ServiceError('%s not found.' % what.capitalize(), http_status=404, code='%s_not_found' % what)


def configured_currency():
    currency = (getattr(settings, 'PAYPAL_CURRENCY', '') or '').strip().upper()
    if len(currency) != 3:
        raise ImproperlyConfigured('PAYPAL_CURRENCY must be a three-letter ISO 4217 code.')
    return currency


def _hash(*parts):
    return hashlib.sha256(':'.join(str(p) for p in parts).encode()).hexdigest()


# --------------------------------------------------------------------------
# Lookups (ownership is enforced here: another shopper's data is a 404)
# --------------------------------------------------------------------------

def _order_queryset():
    return Order.objects.select_related('paypal_payment', 'user').prefetch_related(
        'lines', 'paypal_payment__refunds')


def get_own_order(user, order_id):
    try:
        return _order_queryset().get(pk=order_id, user=user)
    except Order.DoesNotExist:
        raise _not_found()


def get_any_order(order_id):
    try:
        return _order_queryset().get(pk=order_id)
    except Order.DoesNotExist:
        raise _not_found()


def _payment_of(order):
    try:
        return order.paypal_payment
    except PayPalPayment.DoesNotExist:
        raise ServiceError(
            'Order %s was not placed through this API and has no PayPal payment.' % order.number,
            http_status=409, code='no_paypal_payment')


def saved_cards(user):
    """The caller's PayPal-vaulted cards (Oscar bankcards carrying a vault token)."""
    return Bankcard.objects.filter(user=user).exclude(partner_reference='').order_by('pk')


def get_saved_card(user, card_id):
    try:
        return saved_cards(user).get(pk=card_id)
    except (Bankcard.DoesNotExist, ValueError):
        raise _not_found('payment_method')


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------

def _claim(payment, to_state, from_states, resumable_state=None):
    """
    Atomically move ``payment`` into ``to_state`` if it is in one of
    ``from_states`` (or in ``resumable_state`` with an expired lease).
    Returns True if this request won the claim.
    """
    now = timezone.now()
    condition = Q(state__in=from_states)
    if resumable_state:
        condition |= Q(state=resumable_state, claimed_at__lt=now - CLAIM_LEASE)
    won = PayPalPayment.objects.filter(condition, pk=payment.pk).update(
        state=to_state, claimed_at=now, updated_at=now)
    payment.refresh_from_db()
    return bool(won)


def _in_progress(action):
    return ServiceError(
        'A %s for this order is already in progress; repeat the request in a moment.' % action,
        http_status=409, code='%s_in_progress' % action.replace(' ', '_'))


def _source(order):
    source_type, __ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
    source = order.sources.filter(source_type=source_type).first()
    if source is None:
        source = Source.objects.create(order=order, source_type=source_type, currency=order.currency)
    return source


def _payment_event(order, name, amount, reference):
    event_type, __ = PaymentEventType.objects.get_or_create(name=name)
    EventHandler().create_payment_event(order, event_type, amount, reference=reference)


def _move_order(order, status):
    order.refresh_from_db()
    if order.status != status and status in order.available_statuses():
        order.set_status(status)


# --------------------------------------------------------------------------
# Placing orders
# --------------------------------------------------------------------------

def _shipping_address(data):
    if not data:
        return None
    if not isinstance(data, dict):
        raise ServiceError('shippingAddress must be an object.', http_status=400, code='invalid_request')
    code = str(data.get('country', '')).upper()
    try:
        country = Country.objects.get(iso_3166_1_a2=code)
    except Country.DoesNotExist:
        raise ServiceError('shippingAddress.country must be an ISO 3166-1 alpha-2 code.',
                           http_status=400, code='invalid_request')
    fields = {
        'first_name': 'firstName', 'last_name': 'lastName', 'line1': 'line1', 'line2': 'line2',
        'line4': 'city', 'state': 'state', 'postcode': 'postcode',
    }
    values = {model: str(data.get(key, ''))[:255] for model, key in fields.items()}
    if not values['line1'] or not values['last_name']:
        raise ServiceError('shippingAddress needs at least lastName and line1.',
                           http_status=400, code='invalid_request')
    address = ShippingAddress(country=country, **values)
    address.save()
    return address


def _requested_lines(items):
    if not isinstance(items, list) or not items:
        raise ServiceError('items must be a non-empty list of {productId, quantity}.',
                           http_status=400, code='invalid_request')
    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise ServiceError('Each item must be an object.', http_status=400, code='invalid_request')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            raise ServiceError('productId must be an integer catalogue item id.',
                               http_status=400, code='invalid_request')
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_LINE_QUANTITY:
            raise ServiceError('quantity must be an integer from 1 to %d.' % MAX_LINE_QUANTITY,
                               http_status=400, code='invalid_request')
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    return quantities


def place_order(user, items, shipping_address=None, request=None):
    """Place an Oscar order for catalogue items; it starts awaiting payment."""
    currency = configured_currency()
    quantities = _requested_lines(items)
    products = {p.pk: p for p in Product.objects.filter(pk__in=quantities, is_public=True)}
    missing = sorted(set(quantities) - set(products))
    if missing:
        raise ServiceError('Unknown catalogue items: %s.' % ', '.join(map(str, missing)),
                           http_status=400, code='unknown_product', productIds=missing)

    strategy = Selector().strategy(request=request, user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in quantities.items():
            product = products[product_id]
            if product.is_parent:
                raise ServiceError(
                    '"%s" has variants; order one of its child products instead.' % product.get_title(),
                    http_status=400, code='product_has_variants', productId=product_id)
            info = strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not info.price.exists or not permitted:
                raise ServiceError(
                    '"%s" cannot be bought: %s' % (product.get_title(), reason or 'no price'),
                    http_status=409, code='product_unavailable', productId=product_id)
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, request)

        shipping_method = Free()
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        if total.incl_tax is None:
            raise ImproperlyConfigured('The pricing strategy does not know the tax on this order.')
        try:
            amount = money.exact(total.incl_tax, currency)
        except money.AmountError as exc:
            raise ServiceError(str(exc), http_status=422, code='amount_not_representable')

        number = OrderNumberGenerator().order_number(basket)
        if Order.objects.filter(number=number).exists():
            number = '%s-%s' % (number, _hash(basket.pk, timezone.now())[:6])
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=_shipping_address(shipping_address),
            order_number=number,
            request=request,
            currency=currency,
        )
        basket.submit()
        PayPalPayment.objects.create(order=order, currency=currency, amount=amount)
    return get_own_order(user, order.pk)


# --------------------------------------------------------------------------
# Authorizing (Flow 1: pay)
# --------------------------------------------------------------------------

def pay(user, order_id, card=None, saved_card_id=None):
    """
    Put a hold on the order total, paying with a one-off card or a saved one.
    Repeating the request after it succeeded returns the existing hold.
    """
    order = get_own_order(user, order_id)
    payment = _payment_of(order)
    if (card is None) == (saved_card_id is None):
        raise ServiceError('Send either "card" or "paymentMethodId", not both.',
                           http_status=400, code='invalid_request')
    saved = get_saved_card(user, saved_card_id) if saved_card_id is not None else None

    if payment.state == PayPalPayment.CANCELLED:
        raise ServiceError('This order is cancelled.', http_status=409, code='order_cancelled')
    if payment.state not in (PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZING):
        return get_own_order(user, order_id)  # already paid: a double click changes nothing
    if not _claim(payment, PayPalPayment.AUTHORIZING, [PayPalPayment.AWAITING_PAYMENT],
                  resumable_state=PayPalPayment.AUTHORIZING):
        if payment.state == PayPalPayment.AUTHORIZING:
            raise _in_progress('payment')
        return pay(user, order_id, card, saved_card_id)  # state moved under us; re-evaluate

    request_id = payment.request_id('authorize', payment.authorization_attempt)
    try:
        auth = paypal.authorize(
            request_id=request_id,
            amount=payment.amount,
            currency=payment.currency,
            custom_id=order.number,
            description='Order %s' % order.number,
            card=card,
            vault_id=saved.partner_reference if saved else None,
        )
    except PaymentGatewayError as exc:
        _release_authorization(payment, exc.message, keep_claim=exc.outcome_unknown)
        raise

    if auth.status not in ('CREATED', 'PENDING'):
        _release_authorization(payment, 'Authorization %s' % auth.status)
        raise ServiceError('The card was declined (authorization %s).' % auth.status,
                           http_status=402, code='payment_declined', authorizationStatus=auth.status)
    if auth.amount != payment.amount:
        # Never keep a hold for a different amount than the order total.
        logger.error('PayPal held %s for order %s totalling %s; releasing it',
                     auth.amount, order.number, payment.amount)
        try:
            paypal.void(auth.authorization_id,
                        request_id=payment.request_id('void', auth.authorization_id))
        finally:
            _release_authorization(payment, 'Held amount did not match the order total')
        raise ServiceError('PayPal held a different amount than the order total; the hold was released.',
                           http_status=502, code='amount_mismatch')

    if saved:
        label = '%s ending %s' % (saved.card_type, saved.number[-4:])
    elif auth.card_last_digits:
        label = '%s ending %s' % (auth.card_brand or 'Card', auth.card_last_digits)
    else:
        label = 'Card'
    with transaction.atomic():
        payment.paypal_order_id = auth.paypal_order_id
        payment.paypal_order_status = auth.paypal_order_status
        payment.authorization_id = auth.authorization_id
        payment.authorization_status = auth.status
        payment.authorized_amount = auth.amount
        payment.authorized_at = auth.created_at or timezone.now()
        payment.authorization_expires_at = auth.expires_at
        payment.saved_card = saved
        payment.card_label = label[:64]
        payment.state = PayPalPayment.AUTHORIZED
        payment.claimed_at = None
        payment.last_error = ''
        payment.save()
        source = _source(order)
        source.label = payment.card_label
        source.reference = auth.authorization_id
        source.allocate(payment.amount, reference=auth.authorization_id, status=auth.status)
        _payment_event(order, 'Authorised', payment.amount, auth.authorization_id)
        _move_order(order, STATUS_PAID)
    return get_own_order(user, order_id)


def _release_authorization(payment, error, keep_claim=False):
    """Undo an authorization claim; an unknown outcome keeps it for resumption."""
    payment.last_error = error[:255]
    if keep_claim:
        payment.save(update_fields=['last_error', 'updated_at'])
        return
    payment.state = PayPalPayment.AWAITING_PAYMENT
    payment.claimed_at = None
    payment.authorization_attempt += 1  # the next attempt is a new request, not a replay
    payment.save(update_fields=['state', 'claimed_at', 'authorization_attempt', 'last_error', 'updated_at'])


# --------------------------------------------------------------------------
# Fulfilment (capture)
# --------------------------------------------------------------------------

def fulfil(order_id):
    """Operator: mark the order fulfilled, which is when the held money is taken."""
    order = get_any_order(order_id)
    payment = _payment_of(order)
    done = (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED)
    if payment.state in done:
        return get_any_order(order_id)
    if payment.state in (PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZING):
        raise ServiceError('Order %s has not been paid yet.' % order.number,
                           http_status=409, code='order_not_paid')
    if payment.state in (PayPalPayment.CANCELLED, PayPalPayment.VOIDING):
        raise ServiceError('Order %s is cancelled.' % order.number, http_status=409, code='order_cancelled')
    if not _claim(payment, PayPalPayment.CAPTURING, [PayPalPayment.AUTHORIZED],
                  resumable_state=PayPalPayment.CAPTURING):
        if payment.state == PayPalPayment.CAPTURING:
            raise _in_progress('fulfilment')
        return fulfil(order_id)

    try:
        capture = _capture_with_renewal(order, payment)
    except (PaymentGatewayError, ServiceError) as exc:
        unknown = isinstance(exc, PaymentGatewayError) and exc.outcome_unknown
        payment.last_error = exc.message[:255]
        if not unknown:
            payment.state = PayPalPayment.AUTHORIZED
            payment.claimed_at = None
        payment.save()
        raise

    status = str(value(capture.status))
    captured = amount_of(capture.amount) or payment.amount
    breakdown = value(capture.seller_receivable_breakdown)
    if breakdown is None or value(breakdown.paypal_fee) is None:
        # A pending capture may not carry the fee yet; ask once more.
        try:
            breakdown = value(paypal.get_capture(capture.id).seller_receivable_breakdown) or breakdown
        except PaymentGatewayError:
            logger.warning('Could not re-read capture %s for its fee breakdown', capture.id)

    if status in ('DECLINED', 'FAILED'):
        payment.state = PayPalPayment.AUTHORIZED
        payment.claimed_at = None
        payment.capture_id, payment.capture_status = capture.id, status
        payment.last_error = 'Capture %s' % status
        payment.save()
        raise ServiceError(
            'PayPal %s the capture of order %s; no money was taken. Cancel the order and ask the '
            'shopper to pay again.' % (status.lower(), order.number),
            http_status=402, code='capture_declined', captureStatus=status)

    with transaction.atomic():
        payment.capture_id = capture.id
        payment.capture_status = status
        payment.captured_amount = captured
        payment.paypal_fee = amount_of(breakdown.paypal_fee) if breakdown else None
        payment.net_amount = amount_of(breakdown.net_amount) if breakdown else None
        payment.captured_at = timestamp(capture.create_time) or timezone.now()
        payment.authorization_status = 'CAPTURED'
        payment.state = PayPalPayment.CAPTURED
        payment.claimed_at = None
        payment.last_error = ''
        payment.save()
        _source(order).debit(captured, reference=capture.id, status=status)
        _payment_event(order, 'Settled', captured, capture.id)
        EventHandler().consume_stock_allocations(order)
        _move_order(order, STATUS_FULFILLED)
    return get_any_order(order_id)


def _capture_with_renewal(order, payment):
    renewal_note = None
    authorization_id = payment.authorization_id
    if timezone.now() >= (payment.authorized_at or payment.created_at) + HONOR_PERIOD:
        authorization_id, renewal_note = _renew_authorization(order, payment)
    try:
        return paypal.capture(
            authorization_id,
            request_id=payment.request_id('capture', authorization_id),
            amount=payment.amount,
            currency=payment.currency,
        )
    except PaymentGatewayError as exc:
        if exc.outcome_unknown or exc.http_status not in (409, 422):
            raise
        parts = ['PayPal would not take the held payment for order %s (%s).'
                 % (order.number, exc.issue or exc.message)]
        if renewal_note:
            parts.append(renewal_note)
        parts.append('No money was taken. Cancel the order and ask the shopper to pay again'
                     + (', quoting PayPal debug id %s to PayPal support if needed.' % exc.debug_id
                        if exc.debug_id else '.'))
        raise ServiceError(' '.join(parts), http_status=409, code='capture_refused',
                           paypalIssue=exc.issue, paypalDebugId=exc.debug_id)


def _renew_authorization(order, payment):
    """
    Renew a hold whose honor period has passed. Returns the authorization id
    to capture and, if PayPal refused the renewal, a note saying so.
    """
    current = paypal.get_authorization(payment.authorization_id)
    status = str(value(current.status))
    expires = timestamp(current.expiration_time) or payment.authorization_expires_at
    payment.authorization_status = status
    payment.authorization_expires_at = expires
    payment.save(update_fields=['authorization_status', 'authorization_expires_at', 'updated_at'])

    if status == 'CAPTURED':
        return payment.authorization_id, None  # our earlier capture landed; replay it
    if status in ('VOIDED', 'DENIED'):
        raise ServiceError(
            'The payment hold on order %s is %s at PayPal and cannot be renewed. No money was taken. '
            'Cancel the order and ask the shopper to pay again.' % (order.number, status.lower()),
            http_status=409, code='authorization_unusable', authorizationStatus=status)
    if expires and timezone.now() >= expires:
        raise ServiceError(
            'The payment hold on order %s expired on %s and PayPal no longer allows it to be renewed. '
            'No money was taken. Cancel the order and ask the shopper to pay again.'
            % (order.number, expires.strftime('%Y-%m-%d %H:%M UTC')),
            http_status=409, code='authorization_expired', expiredAt=expires.isoformat())

    try:
        renewed = paypal.reauthorize(
            payment.authorization_id,
            request_id=payment.request_id('reauthorize', payment.authorization_id),
            amount=payment.amount,
            currency=payment.currency,
        )
    except PaymentGatewayError as exc:
        if exc.outcome_unknown:
            raise
        # The original hold stays capturable until it expires, so try it anyway.
        logger.warning('PayPal refused to renew authorization for order %s: %s', order.number, exc.issue)
        return payment.authorization_id, 'PayPal also refused to renew the hold (%s).' % (
            exc.issue or exc.message)

    with transaction.atomic():
        payment.authorization_id = renewed.id
        payment.authorization_status = str(value(renewed.status))
        payment.authorized_at = timestamp(renewed.create_time) or timezone.now()
        payment.authorization_expires_at = timestamp(renewed.expiration_time) or expires
        payment.reauthorization_count += 1
        payment.save()
        source = _source(order)
        source.reference = renewed.id
        source.save()
        source.transactions.create(txn_type='Reauthorise', amount=payment.amount,
                                   reference=renewed.id, status=payment.authorization_status)
    return renewed.id, None


# --------------------------------------------------------------------------
# Cancelling (void)
# --------------------------------------------------------------------------

def cancel(order_id):
    """Operator: cancel before fulfilment, releasing any held funds."""
    order = get_any_order(order_id)
    payment = _payment_of(order)
    if payment.state == PayPalPayment.CANCELLED:
        return get_any_order(order_id)
    if payment.state in (PayPalPayment.CAPTURING, PayPalPayment.CAPTURED,
                         PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        raise ServiceError('Order %s has been fulfilled; refund it instead.' % order.number,
                           http_status=409, code='order_fulfilled')
    if payment.state == PayPalPayment.AUTHORIZING:
        raise _in_progress('payment')

    if _claim(payment, PayPalPayment.CANCELLED, [PayPalPayment.AWAITING_PAYMENT]):
        _finish_cancel(order, payment, voided=False)  # nothing was ever held
        return get_any_order(order_id)
    if not _claim(payment, PayPalPayment.VOIDING, [PayPalPayment.AUTHORIZED],
                  resumable_state=PayPalPayment.VOIDING):
        if payment.state == PayPalPayment.VOIDING:
            raise _in_progress('cancellation')
        return cancel(order_id)

    try:
        paypal.void(payment.authorization_id,
                    request_id=payment.request_id('void', payment.authorization_id))
    except PaymentGatewayError as exc:
        already_voided = False
        if not exc.outcome_unknown and exc.http_status in (409, 422):
            try:
                already_voided = str(value(paypal.get_authorization(
                    payment.authorization_id).status)) == 'VOIDED'
            except PaymentGatewayError:
                already_voided = False
        if not already_voided:
            payment.last_error = exc.message[:255]
            if not exc.outcome_unknown:
                payment.state = PayPalPayment.AUTHORIZED
                payment.claimed_at = None
            payment.save()
            raise
    _finish_cancel(order, payment, voided=True)
    return get_any_order(order_id)


def _finish_cancel(order, payment, voided):
    with transaction.atomic():
        payment.state = PayPalPayment.CANCELLED
        payment.claimed_at = None
        payment.last_error = ''
        if voided:
            payment.authorization_status = 'VOIDED'
            payment.voided_at = timezone.now()
            source = _source(order)
            source.amount_allocated = Decimal('0.00')
            source.save()
            source.transactions.create(txn_type='Void', amount=payment.amount,
                                       reference=payment.authorization_id, status='VOIDED')
            _payment_event(order, 'Released', payment.amount, payment.authorization_id)
        payment.save()
        EventHandler().cancel_stock_allocations(order)
        _move_order(order, STATUS_CANCELLED)


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------

def refundable_amount(payment):
    if payment.captured_amount is None:
        return Decimal('0.00')
    return payment.captured_amount - payment.refunded_amount


def refund(user, order_id, idempotency_key, amount=None):
    """
    Refund all or part of the captured payment. The idempotency key makes a
    repeated request return the first refund instead of refunding again.
    Returns ``(refund, created)``.
    """
    order = get_own_order(user, order_id)
    payment = _payment_of(order)
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255:
        raise ServiceError('An idempotency key (1-255 characters) is required, as the '
                           '"Idempotency-Key" header or "idempotencyKey" field.',
                           http_status=400, code='idempotency_key_required')
    if amount is not None:
        try:
            amount = money.parse(amount, payment.currency)
        except money.AmountError as exc:
            raise ServiceError(str(exc), http_status=400, code='invalid_amount')

    existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return _replay_refund(order, payment, existing, amount), False

    if payment.state not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED,
                             PayPalPayment.REFUNDED):
        raise ServiceError('Only fulfilled orders can be refunded; order %s is %s.'
                           % (order.number, payment.get_state_display().lower()),
                           http_status=409, code='order_not_fulfilled')
    try:
        with transaction.atomic():
            # Take the payment's row lock first so concurrent refunds are
            # checked against each other, not against a stale total.
            PayPalPayment.objects.filter(pk=payment.pk).update(updated_at=timezone.now())
            payment.refresh_from_db()
            remaining = refundable_amount(payment)
            amount = remaining if amount is None else amount
            if amount <= 0 or amount > remaining:
                raise ServiceError(
                    'At most %s %s of this payment can still be refunded.' % (remaining, payment.currency),
                    http_status=422, code='refund_exceeds_captured', refundable=str(remaining))
            pending = PayPalRefund.objects.create(
                payment=payment, idempotency_key=idempotency_key, amount=amount,
                requested_by=user, claimed_at=timezone.now())
    except IntegrityError:
        # The same key arrived twice at once; the other request owns it.
        existing = payment.refunds.get(idempotency_key=idempotency_key)
        return _replay_refund(order, payment, existing, amount), False
    return _submit_refund(order, payment, pending), True


def _replay_refund(order, payment, existing, amount):
    if amount is not None and amount != existing.amount:
        raise ServiceError('This idempotency key was already used for a refund of %s %s.'
                           % (existing.amount, payment.currency),
                           http_status=422, code='idempotency_key_reused')
    if existing.status == PayPalRefund.SUBMITTING:
        if existing.claimed_at and existing.claimed_at > timezone.now() - CLAIM_LEASE:
            raise _in_progress('refund')
        existing.claimed_at = timezone.now()
        existing.save(update_fields=['claimed_at', 'updated_at'])
        return _submit_refund(order, payment, existing)  # resume: PayPal replays by request id
    return existing


def _submit_refund(order, payment, pending):
    try:
        result = paypal.refund(
            payment.capture_id,
            request_id=payment.request_id('refund', _hash(pending.idempotency_key)[:24]),
            amount=pending.amount,
            currency=payment.currency,
        )
    except PaymentGatewayError as exc:
        pending.last_error = exc.message[:255]
        if not exc.outcome_unknown:
            pending.status = PayPalRefund.FAILED  # releases the amount for another attempt
        pending.save()
        raise

    status = str(value(result.status))
    breakdown = value(result.seller_payable_breakdown)
    with transaction.atomic():
        pending.paypal_refund_id = result.id
        pending.status = status
        pending.claimed_at = None
        pending.last_error = ''
        if breakdown is not None:
            pending.paypal_fee_returned = amount_of(breakdown.paypal_fee)
            pending.net_amount = amount_of(breakdown.net_amount)
        pending.save()
        if pending.counts_against_capture:
            _source(order).refund(pending.amount, reference=result.id, status=status)
            _payment_event(order, 'Refunded', pending.amount, result.id)
        payment.refresh_from_db()
        payment.state = (PayPalPayment.REFUNDED if refundable_amount(payment) <= 0
                         else PayPalPayment.PARTIALLY_REFUNDED if payment.refunded_amount > 0
                         else PayPalPayment.CAPTURED)
        payment.save(update_fields=['state', 'updated_at'])
    if not pending.counts_against_capture:
        raise ServiceError('PayPal did not complete the refund (status %s).' % status,
                           http_status=422, code='refund_failed', refundId=pending.pk)
    return pending


# --------------------------------------------------------------------------
# Saved cards (Flow 2)
# --------------------------------------------------------------------------

def _expiry_date(expiry):
    year, month = (int(part) for part in expiry.split('-'))
    return date(year, month, monthrange(year, month)[1])


def save_card(user, card, idempotency_key=None):
    """Vault a card at PayPal and remember only its token and a safe description."""
    customer = PayPalCustomer.objects.filter(user=user).first()
    if idempotency_key:
        request_id = 'vault-' + _hash(settings.SECRET_KEY, user.pk, idempotency_key)[:40]
    else:
        request_id = 'vault-' + _hash(settings.SECRET_KEY, user.pk, timezone.now().isoformat(),
                                      id(card))[:40]
    token = paypal.vault_card(card, request_id=request_id,
                              customer_id=customer.customer_id if customer else None)

    vaulted_customer = value(token.customer)
    customer_id = value(vaulted_customer.id) if vaulted_customer is not None else None
    if customer is None and customer_id:
        PayPalCustomer.objects.get_or_create(user=user, defaults={'customer_id': customer_id})

    source = value(token.payment_source)
    details = value(source.card) if source is not None else None
    brand = value(details.brand) if details is not None else None
    last_digits = (value(details.last_digits) if details is not None else None) or card.number[-4:]
    expiry = (value(details.expiry) if details is not None else None) or card.expiry
    name = (value(details.name) if details is not None else None) or card.name or ''

    existing = Bankcard.objects.filter(user=user, partner_reference=token.id).first()
    if existing is not None:
        return existing  # PayPal replayed an earlier save
    bankcard = Bankcard(
        user=user,
        number='XXXX-XXXX-XXXX-%s' % last_digits,
        expiry_date=_expiry_date(expiry),
        name=name[:255],
        partner_reference=token.id,
    )
    bankcard.card_type = str(brand) if brand else 'Card'
    bankcard.save()
    return bankcard


def delete_card(user, card_id):
    bankcard = get_saved_card(user, card_id)
    paypal.delete_vaulted_card(bankcard.partner_reference)
    bankcard.delete()


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def _local_ledger(start, end):
    """
    Every PayPal id this app knows, and the app-side events inside the range.
    """
    known = {}
    in_range = []
    payments = PayPalPayment.objects.select_related('order').prefetch_related('refunds')
    auth_refs = Transaction.objects.filter(
        source__source_type__name=SOURCE_TYPE_NAME).exclude(reference='').values_list(
        'reference', 'source__order_id')
    orders = {p.order_id: p for p in payments}
    for reference, order_id in auth_refs:
        payment = orders.get(order_id)
        if payment is not None:
            known.setdefault(reference, {'kind': 'authorization', 'payment': payment,
                                         'amount': payment.amount})

    def event(kind, paypal_id, payment, amount, when):
        entry = {'kind': kind, 'payment': payment, 'amount': amount, 'at': when}
        known[paypal_id] = entry
        if when and start <= when <= end:
            in_range.append((paypal_id, entry))

    for payment in payments:
        if payment.authorization_id:
            event('authorization', payment.authorization_id, payment, payment.amount, payment.authorized_at)
        if payment.capture_id and payment.captured_amount is not None:
            event('capture', payment.capture_id, payment, payment.captured_amount, payment.captured_at)
        for refund_ in payment.refunds.all():
            if refund_.paypal_refund_id:
                event('refund', refund_.paypal_refund_id, payment, refund_.amount, refund_.created_at)
    return known, in_range


def reconcile(start, end):
    """Line PayPal's transaction records for a range up against this app's orders."""
    if start >= end:
        raise ServiceError('"from" must be before "to".', http_status=400, code='invalid_range')
    known, in_range = _local_ledger(start, end)

    matched, paypal_only, mismatched = [], [], []
    seen = set()
    for info in paypal.search_transactions(start, end):
        paypal_id = value(info.transaction_id) or ''
        amount = amount_of(info.transaction_amount)
        record = {
            'paypalTransactionId': paypal_id,
            'eventCode': value(info.transaction_event_code),
            'status': value(info.transaction_status),
            'initiatedAt': value(info.transaction_initiation_date),
            'amount': str(amount) if amount is not None else None,
            'currency': value(info.transaction_amount).currency_code
            if value(info.transaction_amount) is not None else None,
            'fee': str(amount_of(info.fee_amount)) if amount_of(info.fee_amount) is not None else None,
            'customId': value(info.custom_field),
        }
        entry = known.get(paypal_id)
        if entry is None:
            paypal_only.append(record)
            continue
        seen.add(paypal_id)
        record.update({
            'kind': entry['kind'],
            'orderId': entry['payment'].order_id,
            'orderNumber': entry['payment'].order.number,
            'appAmount': str(entry['amount']),
        })
        if amount is not None and abs(amount) != entry['amount']:
            mismatched.append(record)
        else:
            matched.append(record)

    lag_cutoff = timezone.now() - REPORTING_LAG
    app_only = []
    for paypal_id, entry in in_range:
        if paypal_id in seen:
            continue
        app_only.append({
            'kind': entry['kind'],
            'paypalTransactionId': paypal_id,
            'orderId': entry['payment'].order_id,
            'orderNumber': entry['payment'].order.number,
            'amount': str(entry['amount']),
            'at': entry['at'].isoformat(),
            # PayPal's reporting lags by up to three hours; recent items may still appear.
            'possiblyReportingLag': entry['at'] >= lag_cutoff,
        })
    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'summary': {
            'paypalTransactions': len(matched) + len(mismatched) + len(paypal_only),
            'matched': len(matched),
            'amountMismatches': len(mismatched),
            'paypalOnly': len(paypal_only),
            'appOnly': len(app_only),
        },
        'matched': matched,
        'amountMismatches': mismatched,
        'paypalOnly': paypal_only,
        'appOnly': app_only,
    }
