"""
Order, payment, saved-card and reconciliation flows. Views stay thin; every PayPal write here
goes through :func:`safe_write.safe_write`.
"""
import hashlib
import hmac
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import gateway, provider
from .gateway import PayPalConfig, ProviderError, quantize
from .models import (
    OrderRequestKey, PayPalCustomer, PayPalPayment, PayPalRefund, ProviderWrite, SavedCard)
from .safe_write import install_prefix, load_existing, make_ref, safe_write

logger = logging.getLogger('apps.paypal_payments')

Order = get_model('order', 'Order')
Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Selector = get_class('partner.strategy', 'Selector')
Applicator = get_class('offer.applicator', 'Applicator')
FreeShipping = get_class('shipping.methods', 'Free')

# Oscar order statuses (see OSCAR_ORDER_STATUS_PIPELINE in sandbox/settings.py)
STATUS_AWAITING_PAYMENT = 'Awaiting payment'
STATUS_PAYMENT_AUTHORIZED = 'Payment authorized'
STATUS_COMPLETE = 'Complete'
STATUS_CANCELLED = 'Cancelled'

# From the SDK docstrings (payments.reauthorize_payment / ReauthorizeRequest): a 3-day honor
# period; reauthorization possible up to day 29; after 30 days a new authorization is needed.
HONOR_PERIOD = timedelta(days=3)
REAUTHORIZATION_LIMIT = timedelta(days=29)

MAX_LINES = 50
MAX_QUANTITY = 99

CAPTURED_STATES = (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED)


class ServiceError(Exception):
    def __init__(self, status_code, code, message, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self):
        body = {'code': self.code, 'message': self.message}
        body.update(self.extra)
        return body

    @classmethod
    def from_provider(cls, err, code=None, message=None):
        extra = {k: v for k, v in err.as_dict().items() if k not in ('code', 'message')}
        return cls(err.status_code, code or err.code, message or err.message, **extra)


# --------------------------------------------------------------------------------------------
# configuration / client
# --------------------------------------------------------------------------------------------

def paypal_config():
    return PayPalConfig(
        client_id=settings.PAYPAL_CLIENT_ID,
        client_secret=settings.PAYPAL_CLIENT_SECRET,
        environment=settings.PAYPAL_ENVIRONMENT,
        currency=(settings.PAYPAL_CURRENCY or '').upper(),
        base_url=settings.PAYPAL_BASE_URL or None,
        timeout=float(getattr(settings, 'PAYPAL_TIMEOUT', 20.0)),
    )


def paypal_client():
    try:
        return gateway.get_client(paypal_config())
    except gateway.ConfigurationError as exc:
        raise ServiceError(503, 'paypal_not_configured', str(exc))


def shop_currency():
    currency = (settings.PAYPAL_CURRENCY or '').upper()
    if not currency:
        raise ServiceError(503, 'paypal_not_configured', 'PAYPAL_CURRENCY is not set.')
    return currency


def order_custom_id(number):
    return '%s:%s' % (install_prefix(), number)


# --------------------------------------------------------------------------------------------
# lookups with ownership
# --------------------------------------------------------------------------------------------

def get_order_for_shopper(user, number):
    order = Order.objects.filter(number=str(number), user=user).first()
    if order is None:
        # Another shopper's order and a missing one look the same.
        raise ServiceError(404, 'order_not_found', 'Order not found.')
    return order


def get_order_for_operator(number):
    order = Order.objects.filter(number=str(number)).first()
    if order is None:
        raise ServiceError(404, 'order_not_found', 'Order not found.')
    return order


def payment_for(order):
    payment = PayPalPayment.objects.filter(order=order).first()
    if payment is None:
        raise ServiceError(409, 'not_a_paypal_order',
                           'This order was not placed through the payments API and has no PayPal payment.')
    return payment


def _move(payment, from_states, to_state):
    """Compare-and-set of the payment state; True when this request made the move."""
    if isinstance(from_states, str):
        from_states = (from_states,)
    return PayPalPayment.objects.filter(pk=payment.pk, state__in=from_states).update(
        state=to_state, updated=timezone.now()) == 1


def _set_order_status(order, status):
    order.refresh_from_db()
    if order.status != status:
        order.set_status(status)


def _source(order, currency):
    source_type, _ = SourceType.objects.get_or_create(name='PayPal')
    source, _ = Source.objects.get_or_create(order=order, source_type=source_type,
                                             defaults={'currency': currency})
    return source


def _stock(order, action):
    for line in order.lines.select_related('stockrecord', 'product'):
        if line.stockrecord and line.product and line.product.get_product_class().track_stock:
            if action == 'consume':
                line.stockrecord.consume_allocation(line.quantity)
            else:
                line.stockrecord.cancel_allocation(line.quantity)


# --------------------------------------------------------------------------------------------
# POST /api/orders
# --------------------------------------------------------------------------------------------

def _parse_items(items):
    if not isinstance(items, list) or not items:
        raise ServiceError(400, 'invalid_items', '"items" must be a non-empty list of '
                                                '{"productId": <id>, "quantity": <n>}.')
    if len(items) > MAX_LINES:
        raise ServiceError(400, 'invalid_items', 'Too many lines (max %d).' % MAX_LINES)
    parsed = {}
    for item in items:
        if not isinstance(item, dict):
            raise ServiceError(400, 'invalid_items', 'Each item must be an object.')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if isinstance(product_id, bool) or not isinstance(quantity, int) or isinstance(quantity, bool):
            raise ServiceError(400, 'invalid_items', 'productId and quantity must be integers.')
        try:
            product_id = int(product_id)
        except (TypeError, ValueError):
            raise ServiceError(400, 'invalid_items', 'productId must be an integer.')
        if not 1 <= quantity <= MAX_QUANTITY:
            raise ServiceError(400, 'invalid_items', 'quantity must be between 1 and %d.' % MAX_QUANTITY)
        parsed[product_id] = parsed.get(product_id, 0) + quantity
    return parsed


def place_order(user, items, request=None, idempotency_key=None):
    """Place an Oscar order (Order + Lines via Oscar's OrderCreator) awaiting payment.
    Returns (order, created)."""
    parsed = _parse_items(items)
    currency = shop_currency()
    request_hash = hashlib.sha256(json.dumps(sorted(parsed.items())).encode()).hexdigest()
    if idempotency_key:
        existing = OrderRequestKey.objects.filter(user=user, key=idempotency_key).first()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ServiceError(422, 'idempotency_key_reused',
                                   'This Idempotency-Key was already used for a different order.')
            if existing.order_id:
                return existing.order, False

    strategy = Selector().strategy(request=request, user=user)
    with transaction.atomic():
        if idempotency_key:
            try:
                with transaction.atomic():
                    key_row = OrderRequestKey.objects.create(user=user, key=idempotency_key,
                                                             request_hash=request_hash)
            except IntegrityError:
                raise ServiceError(409, 'request_in_progress',
                                   'An order with this Idempotency-Key is being placed; retry shortly.')
        # A dedicated, owner-less basket: the shopper's own storefront basket is left untouched.
        basket = Basket.objects.create()
        basket.strategy = strategy
        products = {p.pk: p for p in Product.objects.filter(pk__in=parsed.keys())}
        for product_id, quantity in parsed.items():
            product = products.get(product_id)
            if product is None:
                raise ServiceError(400, 'unknown_product', 'Catalogue item %s does not exist.' % product_id)
            if product.is_parent:
                raise ServiceError(400, 'not_purchasable',
                                   'Catalogue item %s is a parent product; order one of its variants.'
                                   % product_id)
            info = strategy.fetch_for_product(product)
            if not info.availability.is_available_to_buy:
                raise ServiceError(409, 'unavailable', '"%s" is not available.' % product.get_title())
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ServiceError(409, 'unavailable', '"%s": %s' % (product.get_title(), reason))
            basket.add_product(product, quantity)
        basket.reset_offer_applications()
        Applicator().apply(basket, user, request)
        if not basket.is_tax_known:
            raise ServiceError(409, 'tax_unknown', 'Tax could not be calculated for this order.')
        shipping_method = FreeShipping()
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        amount = quantize(total.incl_tax, currency)
        if amount <= 0:
            raise ServiceError(409, 'nothing_to_pay', 'The order total is zero.')
        number = OrderNumberGenerator().order_number(basket)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method, shipping_charge=shipping_charge,
            user=user, order_number=number, status=STATUS_AWAITING_PAYMENT, request=request,
            currency=currency)
        basket.submit()
        PayPalPayment.objects.create(order=order, currency=currency, amount=amount)
        if idempotency_key:
            key_row.order = order
            key_row.save(update_fields=['order'])
    return order, True


# --------------------------------------------------------------------------------------------
# POST /api/orders/{id}/pay — authorize (hold) the order total
# --------------------------------------------------------------------------------------------

def pay_order(user, number, card=None, saved_card_id=None):
    order = get_order_for_shopper(user, number)
    payment = payment_for(order)
    saved = None
    if saved_card_id is not None:
        saved = SavedCard.objects.filter(public_id=saved_card_id, user=user, state=SavedCard.ACTIVE).first()
        if saved is None:
            raise ServiceError(404, 'payment_method_not_found',
                               'Saved card not found, or it has been removed.')
    currency = payment.currency

    while True:
        payment.refresh_from_db()
        if payment.state in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING) + CAPTURED_STATES:
            # Already held: a repeat answers from the stored outcome, no second authorization.
            return load_existing(make_ref('order', order.number, 'authorize', payment.pay_attempt)), payment
        if payment.state in (PayPalPayment.VOIDING, PayPalPayment.VOIDED, PayPalPayment.CANCELLED):
            raise ServiceError(409, 'order_cancelled', 'This order has been cancelled.')
        if payment.state == PayPalPayment.NEEDS_REVIEW:
            raise ServiceError(409, 'needs_review', 'This payment needs review by an operator.')
        attempt = payment.pay_attempt
        ref = make_ref('order', order.number, 'authorize', attempt)
        record = load_existing(ref)
        if record is not None and record.outcome == ProviderWrite.FAILED:
            # The previous attempt was declined: a new attempt gets a new reference.
            PayPalPayment.objects.filter(pk=payment.pk, pay_attempt=attempt).update(pay_attempt=attempt + 1)
            continue
        break

    client = paypal_client()
    _move(payment, PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZING)
    vault_id = saved.token_id if saved else None

    def send(key):
        return provider.create_authorization(
            client, ref=key, amount=payment.amount, currency=currency,
            custom_id=order_custom_id(order.number), invoice_id='%s-%s-%s' % (
                install_prefix(), order.number, attempt),
            description='Order %s' % order.number, card=card, vault_id=vault_id)

    def find(rec):
        paypal_order_id = (rec.data or {}).get('paypal_order_id')
        return provider.get_order(client, paypal_order_id) if paypal_order_id else None

    try:
        record = safe_write(ref, kind='authorize', send=send, read=provider.read_order_authorization,
                            find=find, sent=(payment.amount, currency), owner=user, order=order)
    except ProviderError as err:
        _move(payment, PayPalPayment.AUTHORIZING, PayPalPayment.AWAITING_PAYMENT)
        PayPalPayment.objects.filter(pk=payment.pk).update(last_error=err.as_dict())
        raise ServiceError.from_provider(err, code='payment_refused' if err.code == 'paypal_rejected' else None)
    if record.outcome != ProviderWrite.SENDING:
        apply_authorization(payment.pk, record, saved)
    payment.refresh_from_db()
    return record, payment


def apply_authorization(payment_pk, record, saved=None):
    """Apply an authorize-step record to the payment (idempotent: safe to run again)."""
    data = record.data or {}
    with transaction.atomic():
        payment = _lock_payment(payment_pk)
        payment.paypal_order_id = data.get('paypal_order_id') or payment.paypal_order_id
        payment.paypal_order_status = data.get('paypal_order_status') or ''
        card = data.get('card') or {}
        payment.card_brand = card.get('brand') or payment.card_brand
        payment.card_last_digits = card.get('last_digits') or payment.card_last_digits
        if saved is not None:
            payment.saved_card = saved
        if record.outcome == ProviderWrite.DONE:
            if payment.state in (PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZING):
                payment.state = PayPalPayment.AUTHORIZED
                payment.authorization_id = record.provider_id
                payment.authorization_status = data.get('authorization_status') or ''
                payment.authorized_at = record.provider_time or timezone.now()
                payment.original_authorized_at = payment.authorized_at
                payment.authorization_expires_at = provider.parse_time(data.get('expiration_time'))
                payment.last_error = {}
                payment.save()
                _set_order_status(payment.order, STATUS_PAYMENT_AUTHORIZED)
                _source(payment.order, payment.currency).allocate(
                    payment.amount, reference=payment.authorization_id,
                    status=payment.authorization_status)
                return
        elif record.outcome == ProviderWrite.FAILED:
            if payment.state == PayPalPayment.AUTHORIZING:
                payment.state = PayPalPayment.AWAITING_PAYMENT
            if data.get('payer_action_required'):
                payment.last_error = {
                    'code': 'payer_action_required',
                    'message': 'PayPal requires the card holder to complete a 3-D Secure challenge in a '
                               'browser. This integration does not support that; use another card.'}
            else:
                payment.last_error = {
                    'code': 'payment_declined',
                    'message': 'The card was declined (PayPal status %s%s).' % (
                        record.provider_status or data.get('paypal_order_status'),
                        ', reason %s' % data['status_reason'] if data.get('status_reason') else '')}
        elif record.outcome == ProviderWrite.NEEDS_REVIEW:
            payment.state = PayPalPayment.NEEDS_REVIEW
            payment.last_error = record.error
        else:
            if payment.state == PayPalPayment.AWAITING_PAYMENT:
                payment.state = PayPalPayment.AUTHORIZING
            payment.last_error = record.error or {}
        payment.save()


# --------------------------------------------------------------------------------------------
# POST /api/orders/{id}/fulfil — capture (renewing a stale authorization first)
# --------------------------------------------------------------------------------------------

def fulfil_order(number):
    order = get_order_for_operator(number)
    payment = payment_for(order)
    if payment.state in CAPTURED_STATES:
        return load_existing(make_ref('order', order.number, 'capture', payment.authorization_id)), payment
    client = paypal_client()
    if payment.state == PayPalPayment.AUTHORIZED:
        if not _move(payment, PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING):
            payment.refresh_from_db()
            if payment.state != PayPalPayment.CAPTURING:
                raise ServiceError(409, 'order_changed', 'The order changed state (%s); retry.' % payment.state)
    elif payment.state != PayPalPayment.CAPTURING:
        raise ServiceError(409, 'not_authorized',
                           'Order %s has no authorized payment to capture (payment state: %s).'
                           % (order.number, payment.state))
    payment.refresh_from_db()
    currency = payment.currency

    capture_ref = make_ref('order', order.number, 'capture', payment.authorization_id)
    if load_existing(capture_ref) is None and _honor_period_over(payment):
        renewed = _renew_authorization(client, order, payment)
        if renewed is not None:
            return renewed, payment
        payment.refresh_from_db()
        capture_ref = make_ref('order', order.number, 'capture', payment.authorization_id)

    authorization_id = payment.authorization_id

    def send(key):
        return provider.capture(client, authorization_id, ref=key, amount=payment.amount, currency=currency)

    def find(rec):
        return provider.get_capture(client, rec.provider_id) if rec.provider_id else None

    try:
        record = safe_write(capture_ref, kind='capture', send=send, read=provider.read_capture, find=find,
                            sent=(payment.amount, currency), order=order)
    except ProviderError as err:
        _move(payment, PayPalPayment.CAPTURING, PayPalPayment.AUTHORIZED)
        PayPalPayment.objects.filter(pk=payment.pk).update(last_error=err.as_dict())
        raise ServiceError.from_provider(
            err, code='capture_refused',
            message='PayPal refused the capture: %s. No money was taken; the authorization is unchanged.'
                    % err.message)
    if record.outcome != ProviderWrite.SENDING:
        apply_capture(payment.pk, record)
    payment.refresh_from_db()
    return record, payment


def _honor_period_over(payment):
    return payment.authorized_at is not None and timezone.now() > payment.authorized_at + HONOR_PERIOD


def _renew_authorization(client, order, payment):
    """Reauthorize a stale authorization. Returns None when the payment now holds a fresh one
    (continue to capture), or the reauthorize record when its outcome is not settled."""
    now = timezone.now()
    original = payment.original_authorized_at or payment.authorized_at
    expired_at = payment.authorization_expires_at
    if now >= original + REAUTHORIZATION_LIMIT or (expired_at and now >= expired_at):
        _move(payment, PayPalPayment.CAPTURING, PayPalPayment.AUTHORIZED)
        raise ServiceError(
            409, 'authorization_expired',
            'The card authorization for order %s was made on %s and can no longer be renewed '
            '(PayPal allows renewing it only up to day 29). No money was taken. Cancel this order '
            'to release the hold and ask the customer to place and pay a new order.'
            % (order.number, original.date().isoformat()),
            authorizedAt=original.isoformat())
    authorization_id = payment.authorization_id
    ref = make_ref('order', order.number, 'reauthorize', authorization_id)

    def send(key):
        return provider.reauthorize(client, authorization_id, ref=key, amount=payment.amount,
                                    currency=payment.currency)

    try:
        record = safe_write(ref, kind='reauthorize', send=send, read=provider.read_authorization,
                            sent=(payment.amount, payment.currency), order=order)
    except ProviderError as err:
        _move(payment, PayPalPayment.CAPTURING, PayPalPayment.AUTHORIZED)
        raise ServiceError.from_provider(
            err, code='authorization_renewal_failed',
            message='The authorization for order %s is past its 3-day honor period and PayPal refused to '
                    'renew it: %s. No money was taken. Cancel this order and ask the customer to pay '
                    'again.' % (order.number, err.message))
    if record.outcome == ProviderWrite.DONE:
        data = record.data or {}
        with transaction.atomic():
            fresh = _lock_payment(payment.pk)
            if fresh.authorization_id == authorization_id:
                fresh.authorization_id = record.provider_id or authorization_id
                fresh.authorization_status = data.get('authorization_status') or ''
                fresh.authorized_at = provider.parse_time(data.get('create_time')) or record.provider_time or now
                fresh.authorization_expires_at = (provider.parse_time(data.get('expiration_time'))
                                                  or fresh.authorization_expires_at)
                fresh.reauthorization_count += 1
                fresh.save()
                _source(order, fresh.currency).transactions.create(
                    txn_type='Reauthorise', amount=fresh.amount, reference=fresh.authorization_id,
                    status=fresh.authorization_status)
        return None
    if record.outcome == ProviderWrite.FAILED:
        _move(payment, PayPalPayment.CAPTURING, PayPalPayment.AUTHORIZED)
        raise ServiceError(
            409, 'authorization_renewal_failed',
            'The authorization for order %s is past its 3-day honor period and PayPal declined to renew '
            'it (status %s). No money was taken. Cancel this order and ask the customer to pay again.'
            % (order.number, record.provider_status))
    return record


def apply_capture(payment_pk, record):
    data = record.data or {}
    with transaction.atomic():
        payment = _lock_payment(payment_pk)
        payment.capture_id = record.provider_id or payment.capture_id
        payment.capture_status = data.get('capture_status') or payment.capture_status
        if record.outcome == ProviderWrite.DONE:
            if payment.state not in (PayPalPayment.CAPTURING, PayPalPayment.AUTHORIZED):
                payment.save()
                return
            payment.state = PayPalPayment.CAPTURED
            payment.captured_amount = record.amount
            payment.paypal_fee = _dec(data.get('paypal_fee'))
            payment.net_amount = _dec(data.get('net'))
            payment.captured_at = record.provider_time or timezone.now()
            payment.last_error = {}
            payment.save()
            _set_order_status(payment.order, STATUS_COMPLETE)
            _source(payment.order, payment.currency).debit(
                payment.captured_amount, reference=payment.capture_id, status=payment.capture_status)
            _stock(payment.order, 'consume')
            return
        if record.outcome == ProviderWrite.FAILED:
            if payment.state == PayPalPayment.CAPTURING:
                payment.state = PayPalPayment.AUTHORIZED
            payment.last_error = {'code': 'capture_declined',
                                  'message': 'PayPal did not capture the payment (status %s). Cancel the '
                                             'order to release the hold.' % record.provider_status}
        elif record.outcome == ProviderWrite.NEEDS_REVIEW:
            payment.state = PayPalPayment.NEEDS_REVIEW
            payment.last_error = record.error
        else:
            payment.last_error = record.error or {}
        payment.save()


def _dec(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


# --------------------------------------------------------------------------------------------
# POST /api/orders/{id}/cancel — release the hold before fulfilment
# --------------------------------------------------------------------------------------------

def cancel_order(number):
    """Returns (outcome, record-or-None, payment)."""
    order = get_order_for_operator(number)
    payment = payment_for(order)
    if payment.state in (PayPalPayment.CANCELLED, PayPalPayment.VOIDED):
        return ProviderWrite.DONE, None, payment
    if payment.state == PayPalPayment.AWAITING_PAYMENT:
        if _move(payment, PayPalPayment.AWAITING_PAYMENT, PayPalPayment.CANCELLED):
            with transaction.atomic():
                _lock_payment(payment.pk)
                _set_order_status(order, STATUS_CANCELLED)
                _stock(order, 'cancel')
            payment.refresh_from_db()
            return ProviderWrite.DONE, None, payment
        payment.refresh_from_db()
    if payment.state == PayPalPayment.AUTHORIZING:
        raise ServiceError(409, 'payment_in_progress',
                           'A payment for this order is in progress; retry the cancellation shortly.')
    if payment.state == PayPalPayment.CAPTURING:
        raise ServiceError(409, 'capture_in_progress', 'The order is being fulfilled; it cannot be cancelled.')
    if payment.state in CAPTURED_STATES:
        raise ServiceError(409, 'already_captured',
                           'The payment was already captured at fulfilment; issue a refund instead.')
    client = paypal_client()
    if payment.state == PayPalPayment.AUTHORIZED:
        if not _move(payment, PayPalPayment.AUTHORIZED, PayPalPayment.VOIDING):
            payment.refresh_from_db()
            if payment.state != PayPalPayment.VOIDING:
                raise ServiceError(409, 'order_changed', 'The order changed state (%s); retry.' % payment.state)
    elif payment.state != PayPalPayment.VOIDING:
        raise ServiceError(409, 'not_cancellable', 'Payment state %s cannot be cancelled.' % payment.state)

    authorization_id = payment.authorization_id
    ref = make_ref('order', order.number, 'void', authorization_id)

    def send(key):
        return provider.void(client, authorization_id, ref=key)

    def find(rec):
        return provider.get_authorization(client, authorization_id)

    try:
        record = safe_write(ref, kind='void', send=send,
                            read=lambda result: provider.read_authorization(result, for_void=True),
                            find=find, order=order)
    except ProviderError as err:
        _move(payment, PayPalPayment.VOIDING, PayPalPayment.AUTHORIZED)
        raise ServiceError.from_provider(
            err, message='PayPal refused to release the hold: %s. The order is unchanged.' % err.message)
    if record.outcome != ProviderWrite.SENDING:
        apply_void(payment.pk, record)
    payment.refresh_from_db()
    return record.outcome, record, payment


def apply_void(payment_pk, record):
    with transaction.atomic():
        payment = _lock_payment(payment_pk)
        payment.authorization_status = record.provider_status or payment.authorization_status
        if record.outcome == ProviderWrite.DONE:
            if payment.state == PayPalPayment.VOIDING:
                payment.state = PayPalPayment.VOIDED
                payment.voided_at = record.provider_time or timezone.now()
                payment.last_error = {}
                payment.save()
                _set_order_status(payment.order, STATUS_CANCELLED)
                _stock(payment.order, 'cancel')
                source = _source(payment.order, payment.currency)
                source.amount_allocated -= payment.amount
                source.save()
                source.transactions.create(txn_type='Void', amount=payment.amount,
                                           reference=payment.authorization_id,
                                           status=payment.authorization_status)
                return
        elif record.outcome == ProviderWrite.FAILED:
            # PayPal says the money was already taken: this app and PayPal disagree.
            payment.state = PayPalPayment.NEEDS_REVIEW
            payment.last_error = {'code': 'already_captured_at_paypal',
                                  'message': 'PayPal reports this authorization as %s; it could not be '
                                             'released. Review the order.' % record.provider_status}
        else:
            payment.last_error = record.error or {}
        payment.save()


# --------------------------------------------------------------------------------------------
# POST /api/orders/{id}/refunds
# --------------------------------------------------------------------------------------------

def refund_order(number, idempotency_key, amount=None, reason='', requested_by=None):
    order = get_order_for_operator(number)
    payment = payment_for(order)
    if payment.state not in CAPTURED_STATES:
        raise ServiceError(409, 'not_captured',
                           'Only a fulfilled (captured) order can be refunded; cancel it instead.')
    currency = payment.currency
    if amount is not None:
        amount = quantize(amount, currency)
        if amount <= 0:
            raise ServiceError(400, 'invalid_amount', 'amount must be greater than zero.')

    refund = PayPalRefund.objects.filter(payment=payment, idempotency_key=idempotency_key).first()
    if refund is not None:
        if amount is not None and amount != refund.amount:
            raise ServiceError(422, 'idempotency_key_reused',
                               'This Idempotency-Key was already used for a refund of %s %s.'
                               % (refund.amount, currency))
        if refund.reservation_released and refund.outcome == ProviderWrite.FAILED \
                and load_existing(refund.ref) is None:
            # Nothing happened last time (never sent / refused): reserve again and resend.
            _reserve(payment, refund.amount)
            PayPalRefund.objects.filter(pk=refund.pk).update(reservation_released=False,
                                                             outcome=ProviderWrite.SENDING)
            refund.refresh_from_db()
    else:
        refund = _create_refund(payment, order, idempotency_key, amount, reason, requested_by)

    client = paypal_client()
    capture_id = payment.capture_id

    def send(key):
        return provider.refund(client, capture_id, ref=key, amount=refund.amount, currency=currency,
                               custom_id=order_custom_id(order.number))

    def find(rec):
        return provider.get_refund(client, rec.provider_id) if rec.provider_id else None

    try:
        record = safe_write(refund.ref, kind='refund', send=send, read=provider.read_refund, find=find,
                            sent=(refund.amount, currency), order=order, owner=requested_by)
    except ProviderError as err:
        _release(refund.pk)
        raise ServiceError.from_provider(err, message='PayPal refused the refund: %s' % err.message,
                                         refundId=str(refund.public_id))
    if record.outcome != ProviderWrite.SENDING:
        apply_refund(refund.pk, record)
    refund.refresh_from_db()
    payment.refresh_from_db()
    return record, refund, payment


def _lock_payment(pk):
    """Take the payment row's write lock before reading it: a no-op UPDATE first (SQLite takes its
    database write lock; Postgres the row lock), then SELECT ... FOR UPDATE. Must run inside
    transaction.atomic(). Arithmetic then happens in Python Decimal — SQLite compares decimals
    as floats."""
    PayPalPayment.objects.filter(pk=pk).update(updated=timezone.now())
    return PayPalPayment.objects.select_for_update().get(pk=pk)


def _reserve(payment, amount):
    """Promise ``amount`` of the captured money to a refund; never beyond what was captured."""
    with transaction.atomic():
        locked = _lock_payment(payment.pk)
        refundable = (locked.captured_amount or Decimal('0')) - locked.refund_reserved
        if amount > refundable:
            raise ServiceError(409, 'exceeds_refundable',
                               'Refund of %s %s exceeds what remains refundable (%s %s).' % (
                                   amount, locked.currency, refundable, locked.currency),
                               refundable=str(refundable))
        locked.refund_reserved += amount
        locked.save(update_fields=['refund_reserved', 'updated'])


def _create_refund(payment, order, idempotency_key, amount, reason, requested_by):
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    try:
        with transaction.atomic():
            locked = _lock_payment(payment.pk)
            if amount is None:   # a full refund of whatever remains
                amount = (locked.captured_amount or Decimal('0')) - locked.refund_reserved
                if amount <= 0:
                    raise ServiceError(409, 'nothing_refundable', 'Nothing remains to be refunded.')
            refund = PayPalRefund.objects.create(
                payment=payment, idempotency_key=idempotency_key, amount=amount, reason=reason[:255],
                ref=make_ref('order', order.number, 'refund', key_hash), requested_by=requested_by)
            _reserve(payment, amount)
            return refund
    except IntegrityError:
        # The same key arrived concurrently: answer from the winner's refund.
        refund = PayPalRefund.objects.get(payment=payment, idempotency_key=idempotency_key)
        if amount is not None and amount != refund.amount:
            raise ServiceError(422, 'idempotency_key_reused', 'This Idempotency-Key was used for another amount.')
        return refund


def _release(refund_pk):
    payment_id = PayPalRefund.objects.values_list('payment_id', flat=True).get(pk=refund_pk)
    with transaction.atomic():
        locked = _lock_payment(payment_id)
        refund = PayPalRefund.objects.select_for_update().get(pk=refund_pk)
        refund.outcome = ProviderWrite.FAILED
        if not refund.reservation_released:
            locked.refund_reserved -= refund.amount
            locked.save(update_fields=['refund_reserved', 'updated'])
            refund.reservation_released = True
        refund.save()


def apply_refund(refund_pk, record):
    data = record.data or {}
    payment_id = PayPalRefund.objects.values_list('payment_id', flat=True).get(pk=refund_pk)
    with transaction.atomic():
        payment = _lock_payment(payment_id)
        refund = PayPalRefund.objects.select_for_update().get(pk=refund_pk)
        refund.outcome = record.outcome
        refund.paypal_refund_id = record.provider_id or refund.paypal_refund_id
        refund.paypal_status = data.get('refund_status') or refund.paypal_status
        refund.save()
        if record.outcome == ProviderWrite.DONE and not refund.recorded:
            payment.refunded_amount += refund.amount
            payment.state = (PayPalPayment.REFUNDED if payment.refunded_amount >= payment.captured_amount
                             else PayPalPayment.PARTIALLY_REFUNDED)
            payment.save()
            _source(payment.order, payment.currency).refund(
                refund.amount, reference=refund.paypal_refund_id, status=refund.paypal_status)
            refund.recorded = True
            refund.save(update_fields=['recorded'])
    if record.outcome == ProviderWrite.FAILED:
        _release(refund_pk)


# --------------------------------------------------------------------------------------------
# saved cards
# --------------------------------------------------------------------------------------------

def save_card(user, card, idempotency_key=None):
    customer, _ = PayPalCustomer.objects.get_or_create(user=user)
    if idempotency_key:
        basis = 'k' + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    else:
        # Same shopper + same card + same number of deletions = the same save (a double submit);
        # keyed with SECRET_KEY so the reference reveals nothing about the card.
        basis = 'c' + hmac.new(settings.SECRET_KEY.encode(), ('%s|%s|%s|%s' % (
            user.pk, card.number, card.expiry, customer.deletions)).encode(), hashlib.sha256).hexdigest()[:32]
    ref = make_ref('user', user.pk, 'vault', basis)
    client = paypal_client()

    def send(key):
        return provider.vault_card(client, ref=key, card=card, customer_id=customer.customer_id or None)

    try:
        record = safe_write(ref, kind='vault', send=send, read=provider.read_vault, owner=user)
    except ProviderError as err:
        raise ServiceError.from_provider(err, message='PayPal did not save the card: %s' % err.message)
    saved = None
    if record.outcome == ProviderWrite.DONE:
        saved = _record_saved_card(user, customer, record)
    return record, saved


def _record_saved_card(user, customer, record):
    data = record.data or {}
    card = data.get('card') or {}
    with transaction.atomic():
        saved, _ = SavedCard.objects.get_or_create(token_id=record.provider_id, defaults={
            'user': user, 'customer_id': data.get('customer_id') or '',
            'brand': card.get('brand') or '', 'last_digits': card.get('last_digits') or '',
            'expiry': card.get('expiry') or '', 'name': card.get('name') or ''})
        if data.get('customer_id') and not customer.customer_id:
            PayPalCustomer.objects.filter(pk=customer.pk, customer_id='').update(
                customer_id=data['customer_id'])
    if saved.user_id != user.pk:
        raise ServiceError(409, 'conflict', 'This payment method belongs to another account.')
    return saved


def list_cards(user):
    return SavedCard.objects.filter(user=user, state=SavedCard.ACTIVE)


def delete_card(user, public_id):
    card = SavedCard.objects.filter(public_id=public_id, user=user).first()
    if card is None:
        raise ServiceError(404, 'payment_method_not_found', 'Saved card not found.')
    if card.state == SavedCard.DELETED:
        return ProviderWrite.DONE, card
    SavedCard.objects.filter(pk=card.pk, state=SavedCard.ACTIVE).update(state=SavedCard.DELETING)
    client = paypal_client()
    token_id = card.token_id

    try:
        record = safe_write(make_ref('vault-delete', token_id), kind='vault_delete',
                            send=lambda key: provider.delete_token(client, token_id),
                            read=lambda answer: answer, owner=user)
    except ProviderError as err:
        SavedCard.objects.filter(pk=card.pk, state=SavedCard.DELETING).update(state=SavedCard.ACTIVE)
        raise ServiceError.from_provider(err, message='PayPal did not remove the card: %s' % err.message)
    if record.outcome == ProviderWrite.DONE:
        if SavedCard.objects.filter(pk=card.pk).exclude(state=SavedCard.DELETED).update(
                state=SavedCard.DELETED, deleted_at=timezone.now()):
            PayPalCustomer.objects.filter(user=user).update(deletions=F('deletions') + 1)
    elif record.outcome == ProviderWrite.FAILED:
        SavedCard.objects.filter(pk=card.pk, state=SavedCard.DELETING).update(state=SavedCard.ACTIVE)
    card.refresh_from_db()
    return record.outcome, card


# --------------------------------------------------------------------------------------------
# GET /api/reconciliation
# --------------------------------------------------------------------------------------------

RECONCILED_KINDS = ('authorize', 'reauthorize', 'capture', 'refund')


def reconcile(start, end):
    client = paypal_client()
    try:
        remote = provider.search_transactions(client, start, end)
    except Exception as exc:
        raise ServiceError.from_provider(gateway.translate(exc),
                                         message='Could not read PayPal\'s transaction report.')
    prefix = install_prefix()

    local = list(ProviderWrite.objects.select_related('order').filter(
        kind__in=RECONCILED_KINDS, outcome__in=(ProviderWrite.DONE, ProviderWrite.PENDING),
        provider_time__gte=start, provider_time__lt=end).exclude(provider_id=''))
    unsettled = ProviderWrite.objects.select_related('order').filter(
        kind__in=RECONCILED_KINDS,
        outcome__in=(ProviderWrite.SENDING, ProviderWrite.UNKNOWN, ProviderWrite.NEEDS_REVIEW),
        claimed_at__gte=start, claimed_at__lt=end)

    by_id = {}
    for record in remote:
        by_id.setdefault(record.transaction_id, []).append(record)

    matched, local_only = [], []
    for write in local:
        records = by_id.pop(write.provider_id, [])
        entry = _local_entry(write)
        if not records:
            local_only.append(entry)
            continue
        for record in records:
            remote_amount = abs(record.amount) if record.amount is not None else None
            matched.append({
                **entry,
                'paypal': _remote_entry(record),
                'amountMatches': remote_amount is not None and write.amount is not None
                and Decimal(remote_amount) == Decimal(write.amount),
            })
    provider_only = []
    for records in by_id.values():
        for record in records:
            ours = any(value and value.startswith(prefix) for value in (record.custom_field, record.invoice_id))
            provider_only.append({**_remote_entry(record), 'source': 'this_app' if ours else 'other'})
    provider_only.sort(key=lambda r: r['initiatedAt'] or '')

    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'summary': {
            'paypalTransactions': len(remote),
            'matched': len(matched),
            'paypalOnly': len(provider_only),
            'paypalOnlyFromThisApp': sum(1 for r in provider_only if r['source'] == 'this_app'),
            'appOnly': len(local_only),
            'unsettled': unsettled.count(),
            'amountMismatches': sum(1 for m in matched if not m['amountMatches']),
        },
        'matched': matched,
        'paypalOnly': provider_only,
        'appOnly': local_only,
        'unsettled': [_local_entry(w) for w in unsettled],
        'note': 'PayPal reports transactions with up to 3 hours of delay; very recent app payments may '
                'appear under appOnly until PayPal reports them.',
    }


def _local_entry(write):
    return {
        'orderId': write.order.number if write.order_id else None,
        'kind': write.kind,
        'paypalId': write.provider_id or None,
        'outcome': write.outcome,
        'amount': (str(quantize(write.amount, write.currency)) if write.amount is not None and write.currency
                   else None),
        'currency': write.currency or None,
        'paypalTime': write.provider_time.isoformat() if write.provider_time else None,
    }


def _remote_entry(record):
    return {
        'transactionId': record.transaction_id,
        'referenceId': record.reference_id,
        'eventCode': record.event_code,
        'status': record.status,
        'initiatedAt': record.initiated_at.isoformat() if record.initiated_at else None,
        'amount': str(record.amount) if record.amount is not None else None,
        'fee': str(record.fee) if record.fee is not None else None,
        'currency': record.currency,
        'invoiceId': record.invoice_id,
        'customField': record.custom_field,
    }
