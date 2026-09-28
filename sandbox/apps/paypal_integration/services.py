"""
Order placement, payment and saved-card flows on top of Oscar's models.

Every PayPal write goes through ``safe_write``; this module decides the
reference for each step, checks the order's state under a row lock, and
applies each outcome to Oscar's ``Order`` / ``payment.Source`` /
``payment.Transaction`` / ``payment.Bankcard``.
"""
import calendar
import datetime
import hashlib
import logging
import re
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from oscar.core import prices
from oscar.core.loading import get_class, get_model

from . import gateway
from .gateway import DONE, FAILED, PENDING, CardInput
from .models import InstallSetting, PayPalCustomer, PayPalPayment, ProviderWrite
from .safe_write import NOT_CLAIMED, safe_write, try_claim

log = logging.getLogger(__name__)

Order = get_model('order', 'Order')
Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Transaction = get_model('payment', 'Transaction')
Bankcard = get_model('payment', 'Bankcard')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Selector = get_class('partner.strategy', 'Selector')
Applicator = get_class('offer.applicator', 'Applicator')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')

# The sandbox's order status pipeline (settings.OSCAR_ORDER_STATUS_PIPELINE).
STATUS_AWAITING_PAYMENT = 'Pending'
STATUS_AUTHORIZED = 'Being processed'
STATUS_FULFILLED = 'Complete'
STATUS_CANCELLED = 'Cancelled'

SOURCE_TYPE_NAME = 'PayPal'
TXN_REAUTHORISE, TXN_VOID = 'Reauthorise', 'Void'

# PayPal's documented PayPal-Request-Id retention per operation.
CREATE_ORDER_KEY_WINDOW = datetime.timedelta(hours=6)
PAYMENTS_KEY_WINDOW = datetime.timedelta(days=45)
VAULT_KEY_WINDOW = datetime.timedelta(hours=3)
# Retention for reauthorize is not documented; assume the shortest one we know.
REAUTHORIZE_KEY_WINDOW = datetime.timedelta(hours=3)

# Authorization lifetime (reauthorize_payment docstring): funds are honoured
# for 3 days; renewals are possible up to day 29; after that, never.
HONOR_PERIOD = datetime.timedelta(days=3)
AUTHORIZATION_PERIOD = datetime.timedelta(days=29)

MAX_LINES = 50
MAX_QUANTITY = 1000
MAX_RECONCILIATION_SPAN = datetime.timedelta(days=3 * 365)
SEARCH_CHUNK = datetime.timedelta(days=31)
MAX_SEARCH_PAGES = 500

# Outcomes that hold part of the captured amount against further refunds.
RESERVING = (ProviderWrite.SENDING, ProviderWrite.PENDING, ProviderWrite.UNKNOWN,
             ProviderWrite.DONE, ProviderWrite.NEEDS_REVIEW)
UNSETTLED = (ProviderWrite.SENDING, ProviderWrite.UNKNOWN)


class DomainError(Exception):
    def __init__(self, http_status, code, message):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message


def _prefix():
    """
    Namespaces every reference sent to PayPal (PayPal-Request-Id, invoice_id),
    so installs sharing one PayPal account can never collide. Unless configured,
    a random prefix is generated once per database.
    """
    if settings.PAYPAL_REFERENCE_PREFIX:
        return settings.PAYPAL_REFERENCE_PREFIX
    setting, _ = InstallSetting.objects.get_or_create(
        key='paypal_reference_prefix', defaults={'value': 'osc-%s' % uuid.uuid4().hex[:10]})
    return setting.value


def _ref(*parts):
    return ':'.join([_prefix(), *[str(p) for p in parts]])


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]


def currency():
    return settings.PAYPAL_CURRENCY


def _charge_amount(order):
    return gateway.quantize(order.total_incl_tax, order.currency)


# --------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------

def parse_amount(value, currency_code):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DomainError(400, 'invalid_amount', 'amount must be a decimal number')
    if not amount.is_finite() or amount <= 0:
        raise DomainError(400, 'invalid_amount', 'amount must be greater than zero')
    if gateway.quantize(amount, currency_code) != amount:
        raise DomainError(400, 'invalid_amount', 'amount has more decimal places than %s allows' % currency_code)
    return amount


_EXPIRY = re.compile(r'^(\d{4})-(\d{2})$')


def parse_card(data):
    """Validates shape only; PayPal decides whether the card is good."""
    if not isinstance(data, dict):
        raise DomainError(400, 'invalid_card', 'card must be an object')
    number = re.sub(r'[\s-]', '', str(data.get('number') or ''))
    expiry = str(data.get('expiry') or '')
    cvc = str(data.get('securityCode') or data.get('cvc') or '')
    if not number.isdigit() or not 12 <= len(number) <= 19:
        raise DomainError(400, 'invalid_card', 'card.number must be 12-19 digits')
    match = _EXPIRY.match(expiry)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise DomainError(400, 'invalid_card', 'card.expiry must be YYYY-MM')
    if not cvc.isdigit() or len(cvc) not in (3, 4):
        raise DomainError(400, 'invalid_card', 'card.securityCode must be 3 or 4 digits')
    address = data.get('billingAddress')
    if address is not None and not isinstance(address, dict):
        raise DomainError(400, 'invalid_card', 'card.billingAddress must be an object')
    return CardInput(
        number=number, expiry=expiry, security_code=cvc, name=str(data.get('name') or '')[:300],
        billing_address={k: str(v) for k, v in (address or {}).items()} or None,
    )


def parse_datetime(value, name):
    if not value:
        raise DomainError(400, 'invalid_range', '%s is required (ISO-8601 date-time)' % name)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00').replace(' ', '+'))
    except ValueError:
        raise DomainError(400, 'invalid_range', '%s must be an ISO-8601 date-time' % name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

def _parse_lines(raw):
    if not isinstance(raw, list) or not raw:
        raise DomainError(400, 'invalid_lines', 'lines must be a non-empty list')
    if len(raw) > MAX_LINES:
        raise DomainError(400, 'invalid_lines', 'at most %d lines per order' % MAX_LINES)
    quantities = {}
    for line in raw:
        if not isinstance(line, dict):
            raise DomainError(400, 'invalid_lines', 'each line must be an object')
        product_id, quantity = line.get('productId'), line.get('quantity', 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise DomainError(400, 'invalid_lines', 'productId must be an integer')
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise DomainError(400, 'invalid_lines', 'quantity must be an integer between 1 and %d' % MAX_QUANTITY)
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    return quantities


def place_order(user, raw_lines, request):
    """Place an Oscar order (awaiting payment) from catalogue product ids."""
    quantities = _parse_lines(raw_lines)
    strategy = Selector().strategy(request=request, user=user)
    with transaction.atomic():
        # Owner-less, so it can never be mistaken for (or merged into) the
        # shopper's storefront basket.
        basket = Basket.objects.create()
        basket.strategy = strategy
        for product_id, quantity in quantities.items():
            product = Product.objects.filter(pk=product_id, is_public=True).first()
            if product is None:
                raise DomainError(422, 'unknown_product', 'product %s does not exist' % product_id)
            if product.is_parent:
                raise DomainError(422, 'not_purchasable', 'product %s is a parent; order one of its variants' % product_id)
            info = strategy.fetch_for_product(product)
            if not info.price.exists or not info.availability.is_available_to_buy:
                raise DomainError(422, 'not_purchasable', 'product %s is not available' % product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise DomainError(422, 'not_purchasable', 'product %s: %s' % (product_id, reason))
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, request)

        shipping_method = NoShippingRequired()
        shipping_charge = shipping_method.calculate(basket)
        basket_total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        # Amounts from the catalogue, currency from configuration.
        total = prices.Price(currency=currency(), excl_tax=basket_total.excl_tax, incl_tax=basket_total.incl_tax)
        if total.incl_tax <= 0:
            raise DomainError(422, 'nothing_to_pay', 'the order total must be greater than zero')
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method, shipping_charge=shipping_charge,
            user=user, request=request, status=STATUS_AWAITING_PAYMENT)
        basket.submit()
        source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
        source = Source.objects.create(order=order, source_type=source_type, currency=order.currency)
        PayPalPayment.objects.create(source=source)
    log.info('Order %s placed by user %s for %s %s', order.number, user.pk, order.total_incl_tax, order.currency)
    return order


def order_for_shopper(user, order_number):
    order = Order.objects.filter(number=order_number, user=user).first()
    if order is None:
        # Another shopper's order is indistinguishable from a missing one.
        raise DomainError(404, 'not_found', 'order not found')
    return order


def order_for_operator(order_number):
    order = Order.objects.filter(number=order_number).first()
    if order is None:
        raise DomainError(404, 'not_found', 'order not found')
    return order


def _payment(order):
    payment = PayPalPayment.objects.select_related('source').filter(source__order=order).first()
    if payment is None:
        raise DomainError(409, 'not_payable', 'this order was not placed through the payments API')
    return payment


def _lock_payment(payment):
    """Row lock: select_for_update where supported; the UPDATE takes SQLite's write lock."""
    PayPalPayment.objects.filter(pk=payment.pk).update(date_updated=timezone.now())
    return PayPalPayment.objects.select_for_update().select_related('source', 'source__order').get(pk=payment.pk)


def _writes(order, kind, outcomes=None):
    qs = ProviderWrite.objects.filter(order=order, kind=kind)
    return qs.filter(outcome__in=outcomes) if outcomes else qs


# --------------------------------------------------------------------------
# Pay (authorize)
# --------------------------------------------------------------------------

def _instrument(user, payload):
    card_data, method_id = payload.get('card'), payload.get('paymentMethodId')
    if (card_data is None) == (method_id is None):
        raise DomainError(400, 'invalid_payment', 'send either card or paymentMethodId')
    if card_data is not None:
        return parse_card(card_data), None, None
    bankcard = _usable_card(user, method_id)
    return None, bankcard.partner_reference, bankcard.pk


def pay(user, order_number, payload):
    order = order_for_shopper(user, order_number)
    card, vault_id, bankcard_id = _instrument(user, payload)
    amount, currency_code = _charge_amount(order), order.currency

    with transaction.atomic():
        payment = _lock_payment(_payment(order))
        order.refresh_from_db()
        ref = _ref('ord', order.number, 'auth', payment.authorize_attempt)
        existing = ProviderWrite.objects.filter(ref=ref).first()
        if existing is not None and existing.outcome == ProviderWrite.FAILED and order.status == STATUS_AWAITING_PAYMENT:
            # The last attempt was declined: a new attempt, a new reference.
            payment.authorize_attempt += 1
            payment.save(update_fields=['authorize_attempt'])
            ref = _ref('ord', order.number, 'auth', payment.authorize_attempt)
            existing = None
        if existing is None and order.status != STATUS_AWAITING_PAYMENT:
            raise DomainError(409, 'not_awaiting_payment', 'order %s is %s, not awaiting payment' % (order.number, order.status))
        claimed = try_claim(ref, kind=ProviderWrite.AUTHORIZE, order=order, user=user,
                            amount=amount, currency=currency_code, bankcard_id=bankcard_id)
        claimed = claimed or NOT_CLAIMED
        attempt = payment.authorize_attempt

    def send(request_id):
        return gateway.create_authorized_order(
            request_id, amount=amount, currency=currency_code,
            invoice_id='%s-%s-%s' % (_prefix(), order.number, attempt), custom_id=order.number,
            description='Order %s' % order.number, card=card, vault_id=vault_id)

    return safe_write(
        ref, kind=ProviderWrite.AUTHORIZE, send=send, find=send, repeat_is_safe=True,
        resend_window=CREATE_ORDER_KEY_WINDOW, sent=(amount, currency_code), claimed=claimed,
        on_complete=_apply_authorization,
        refresh=lambda record: gateway.get_authorization(record.provider_id),
    )


def _apply_authorization(record, answer):
    payment = PayPalPayment.objects.select_for_update().select_related('source').get(source__order=record.order)
    order = payment.source.order
    payment.paypal_order_id = answer.extra.get('paypal_order_id') or payment.paypal_order_id
    payment.card_brand = answer.extra.get('card_brand') or payment.card_brand
    payment.card_last_digits = answer.extra.get('card_last_digits') or payment.card_last_digits
    if answer.provider_id and answer.outcome in (DONE, PENDING):
        payment.authorization_id = answer.provider_id
        payment.authorization_status = answer.status
        payment.authorization_created_at = answer.extra.get('created_at')
        payment.authorization_expires_at = answer.extra.get('expires_at')
    if answer.outcome == DONE and payment.state == PayPalPayment.AWAITING_PAYMENT:
        payment.state = PayPalPayment.AUTHORIZED
        payment.source.allocate(record.amount, reference=answer.provider_id, status=answer.status)
        order.set_status(STATUS_AUTHORIZED)
    payment.save()


# --------------------------------------------------------------------------
# Fulfil (capture, renewing a stale authorization first)
# --------------------------------------------------------------------------

def fulfil(order_number):
    order = order_for_operator(order_number)
    payment = _payment(order)
    amount, currency_code = _charge_amount(order), order.currency

    done_capture = _writes(order, ProviderWrite.CAPTURE, [ProviderWrite.DONE]).first()
    if done_capture is not None:
        return done_capture                         # fulfilled already: same answer
    open_capture = _writes(order, ProviderWrite.CAPTURE, [ProviderWrite.SENDING, ProviderWrite.UNKNOWN,
                                                          ProviderWrite.PENDING]).first()
    if open_capture is None:
        if order.status != STATUS_AUTHORIZED or payment.state != PayPalPayment.AUTHORIZED:
            raise DomainError(409, 'not_fulfillable', {
                STATUS_AWAITING_PAYMENT: 'order %s has not been paid yet' % order.number,
                STATUS_CANCELLED: 'order %s was cancelled' % order.number,
            }.get(order.status, 'order %s is %s and has no authorized payment to capture' % (order.number, order.status)))
        authorization_id, renewal_problem = _fresh_authorization(order, payment, amount, currency_code)
    else:
        authorization_id, renewal_problem = open_capture.ref.rsplit(':', 1)[1], None

    try:
        return _capture(order, authorization_id, amount, currency_code)
    except gateway.ProviderError as exc:
        if exc.http_status in (409, 422):
            hint = (' Renewing the authorization also failed: %s' % renewal_problem) if renewal_problem else ''
            raise DomainError(409, 'capture_refused', (
                'PayPal refused to capture authorization %s: %s%s Nothing was charged. Cancel the order '
                '(POST /api/orders/%s/cancel) and ask the shopper to pay again.'
            ) % (authorization_id, exc.message, hint, order.number))
        raise


def _capture(order, authorization_id, amount, currency_code):
    ref = _ref('ord', order.number, 'capture', authorization_id)

    def send(request_id):
        return gateway.capture(request_id, authorization_id, amount=amount, currency=currency_code)

    return safe_write(
        ref, kind=ProviderWrite.CAPTURE, send=send, find=send, repeat_is_safe=True,
        resend_window=PAYMENTS_KEY_WINDOW, sent=(amount, currency_code), on_complete=_apply_capture,
        refresh=lambda record: gateway.get_capture(record.provider_id),
        order=order, amount=amount, currency=currency_code,
    )


def _fresh_authorization(order, payment, amount, currency_code):
    """
    The authorization to capture: the current one while it is inside PayPal's
    3-day honor period, a renewed one after it. Raises an operator-actionable
    error when the hold is gone and can no longer be renewed.
    """
    current = gateway.get_authorization(payment.authorization_id)
    now = timezone.now()
    created = current.extra.get('created_at') or payment.authorization_created_at
    expires = current.extra.get('expires_at') or payment.authorization_expires_at
    if current.outcome == FAILED:
        raise DomainError(409, 'authorization_unusable', (
            'PayPal reports authorization %s as %s, so there is no hold left to capture. Cancel the order '
            '(POST /api/orders/%s/cancel) and ask the shopper to pay again.'
        ) % (payment.authorization_id, current.status, order.number))
    if (expires and expires <= now) or (created and now - created >= AUTHORIZATION_PERIOD):
        raise DomainError(409, 'authorization_expired', (
            'Authorization %s expired%s and can no longer be renewed (PayPal allows renewal only within 29 days). '
            'Nothing was charged. Cancel the order (POST /api/orders/%s/cancel) and ask the shopper to place '
            'and pay a new order.'
        ) % (payment.authorization_id, ' on %s' % expires.isoformat() if expires else '', order.number))
    if created and now - created > HONOR_PERIOD:
        try:
            record = _reauthorize(order, payment.authorization_id, amount, currency_code)
        except gateway.ProviderError as exc:
            if exc.outcome_unknown:
                raise
            log.warning('Reauthorizing %s for order %s refused: %s', payment.authorization_id, order.number, exc.message)
            return payment.authorization_id, exc.message    # the original may still be capturable
        if record.outcome == DONE:
            return record.provider_id, None
        if record.outcome == FAILED:
            return payment.authorization_id, record.message or 'reauthorization %s' % record.provider_status
        raise DomainError(409, 'authorization_renewal_pending', (
            'Renewing stale authorization %s is %s at PayPal; retry the fulfilment shortly.'
        ) % (payment.authorization_id, record.outcome))
    return payment.authorization_id, None


def _reauthorize(order, authorization_id, amount, currency_code):
    ref = _ref('ord', order.number, 'reauth', authorization_id)

    def send(request_id):
        return gateway.reauthorize(request_id, authorization_id, amount=amount, currency=currency_code)

    return safe_write(
        ref, kind=ProviderWrite.REAUTHORIZE, send=send, find=send, repeat_is_safe=True,
        resend_window=REAUTHORIZE_KEY_WINDOW, sent=(amount, currency_code), on_complete=_apply_reauthorization,
        order=order, amount=amount, currency=currency_code,
    )


def _apply_reauthorization(record, answer):
    if answer.outcome != DONE:
        return
    payment = PayPalPayment.objects.select_for_update().select_related('source').get(source__order=record.order)
    payment.authorization_id = answer.provider_id
    payment.authorization_status = answer.status
    payment.authorization_created_at = answer.extra.get('created_at')
    payment.authorization_expires_at = answer.extra.get('expires_at')
    payment.save()
    Transaction.objects.create(source=payment.source, txn_type=TXN_REAUTHORISE, amount=record.amount,
                               reference=answer.provider_id, status=answer.status)


def _apply_capture(record, answer):
    payment = PayPalPayment.objects.select_for_update().select_related('source').get(source__order=record.order)
    order = payment.source.order
    payment.capture_id = answer.provider_id or payment.capture_id
    payment.capture_status = answer.status
    payment.captured_amount = answer.amount
    payment.paypal_fee = answer.extra.get('paypal_fee', payment.paypal_fee)
    payment.net_amount = answer.extra.get('net_amount', payment.net_amount)
    if answer.outcome == DONE and payment.state in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURE_PENDING):
        payment.state = PayPalPayment.CAPTURED
        payment.source.debit(answer.amount, reference=answer.provider_id, status=answer.status)
        order.set_status(STATUS_FULFILLED)
    elif answer.outcome == PENDING:
        payment.state = PayPalPayment.CAPTURE_PENDING
    payment.save()


# --------------------------------------------------------------------------
# Cancel (void before fulfilment)
# --------------------------------------------------------------------------

def cancel(order_number):
    order = order_for_operator(order_number)
    with transaction.atomic():
        payment = _lock_payment(_payment(order))
        order.refresh_from_db()
        if order.status == STATUS_CANCELLED:
            return _writes(order, ProviderWrite.VOID).first(), order
        if order.status == STATUS_FULFILLED or payment.state not in (PayPalPayment.AWAITING_PAYMENT,
                                                                     PayPalPayment.AUTHORIZED):
            raise DomainError(409, 'already_fulfilled',
                              'order %s was fulfilled and the money taken; refund it instead' % order.number)
        if _writes(order, ProviderWrite.CAPTURE, RESERVING).exists():
            raise DomainError(409, 'capture_in_progress', 'a capture for order %s is in progress' % order.number)
        if _writes(order, ProviderWrite.AUTHORIZE, [ProviderWrite.SENDING, ProviderWrite.UNKNOWN,
                                                    ProviderWrite.PENDING]).exists():
            raise DomainError(409, 'payment_unresolved', (
                'a payment attempt for order %s has not settled at PayPal yet; retry the cancellation shortly'
            ) % order.number)
        if payment.state == PayPalPayment.AWAITING_PAYMENT:
            order.set_status(STATUS_CANCELLED)     # nothing held: nothing to release
            return None, order
        authorization_id = payment.authorization_id
        ref = _ref('ord', order.number, 'void', authorization_id)
        claimed = try_claim(ref, kind=ProviderWrite.VOID, order=order,
                            amount=payment.source.amount_allocated, currency=order.currency) or NOT_CLAIMED

    record = safe_write(
        ref, kind=ProviderWrite.VOID,
        send=lambda request_id: gateway.void(request_id, authorization_id),
        # the authorization itself says whether it was voided
        find=lambda _ref: gateway.get_authorization_for_void(authorization_id),
        repeat_is_safe=True, resend_window=PAYMENTS_KEY_WINDOW, claimed=claimed, on_complete=_apply_void,
    )
    order.refresh_from_db()
    return record, order


def _apply_void(record, answer):
    if answer.outcome != DONE:
        return
    payment = PayPalPayment.objects.select_for_update().select_related('source').get(source__order=record.order)
    payment.state = PayPalPayment.VOIDED
    payment.authorization_status = answer.status
    payment.save()
    Transaction.objects.create(source=payment.source, txn_type=TXN_VOID, amount=payment.source.amount_allocated,
                               reference=answer.provider_id, status=answer.status)
    payment.source.order.set_status(STATUS_CANCELLED)


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------

def refund(user, order_number, payload, idempotency_key):
    if not idempotency_key or len(idempotency_key) > 200:
        raise DomainError(400, 'idempotency_key_required',
                          'send an Idempotency-Key header (or idempotencyKey), at most 200 characters')
    order = order_for_shopper(user, order_number)
    requested = payload.get('amount')
    requested = None if requested in (None, '') else parse_amount(requested, order.currency)
    fingerprint = 'full' if requested is None else str(requested)
    ref = _ref('ord', order.number, 'refund', _digest(idempotency_key))

    with transaction.atomic():
        payment = _lock_payment(_payment(order))
        existing = ProviderWrite.objects.filter(ref=ref).first()
        if existing is not None and existing.fingerprint != fingerprint:
            raise DomainError(422, 'idempotency_key_reused',
                              'this Idempotency-Key was already used for a different refund request')
        if existing is not None and not (existing.outcome == ProviderWrite.FAILED and not existing.provider_id):
            claimed, amount = NOT_CLAIMED, existing.amount     # a repeat: answered from the record
        else:
            if payment.state not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED):
                raise DomainError(409, 'not_refundable', {
                    PayPalPayment.REFUNDED: 'order %s is already fully refunded' % order.number,
                    PayPalPayment.VOIDED: 'order %s was cancelled before fulfilment; nothing was captured, '
                                          'so there is nothing to refund' % order.number,
                }.get(payment.state, 'order %s has not been fulfilled, so nothing was captured to refund; '
                                     'cancel it instead' % order.number))
            reserved = sum((w.amount for w in _writes(order, ProviderWrite.REFUND, RESERVING)), Decimal(0))
            refundable = payment.captured_amount - reserved
            amount = refundable if requested is None else requested
            if amount <= 0 or amount > refundable:
                raise DomainError(422, 'exceeds_refundable',
                                  'at most %s %s can still be refunded' % (
                                      gateway.format_amount(max(refundable, Decimal(0)), order.currency),
                                      order.currency))
            claimed = try_claim(ref, kind=ProviderWrite.REFUND, order=order, user=user, amount=amount,
                                currency=order.currency, fingerprint=fingerprint) or NOT_CLAIMED
        capture_id = payment.capture_id

    def send(request_id):
        return gateway.refund(request_id, capture_id, amount=amount, currency=order.currency)

    return safe_write(
        ref, kind=ProviderWrite.REFUND, send=send, find=send, repeat_is_safe=True,
        resend_window=PAYMENTS_KEY_WINDOW, sent=(amount, order.currency), claimed=claimed,
        on_complete=_apply_refund, refresh=lambda record: gateway.get_refund(record.provider_id),
    )


def _apply_refund(record, answer):
    if answer.outcome != DONE:
        return
    payment = PayPalPayment.objects.select_for_update().select_related('source').get(source__order=record.order)
    source = payment.source
    if source.transactions.filter(txn_type=Transaction.REFUND, reference=answer.provider_id).exists():
        return
    source.refund(answer.amount, reference=answer.provider_id, status=answer.status)
    payment.state = (PayPalPayment.REFUNDED if source.amount_refunded >= payment.captured_amount
                     else PayPalPayment.PARTIALLY_REFUNDED)
    payment.save()


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------

def _deleting_card_ids(user):
    return set(ProviderWrite.objects.filter(kind=ProviderWrite.VAULT_DELETE, user=user)
               .exclude(outcome=ProviderWrite.FAILED).values_list('bankcard_id', flat=True))


def saved_cards(user):
    excluded = _deleting_card_ids(user)
    return [card for card in Bankcard.objects.filter(user=user).exclude(partner_reference='').order_by('pk')
            if card.pk not in excluded]


def _usable_card(user, method_id):
    if not isinstance(method_id, int) or isinstance(method_id, bool):
        raise DomainError(400, 'invalid_payment', 'paymentMethodId must be an integer')
    card = Bankcard.objects.filter(pk=method_id, user=user).exclude(partner_reference='').first()
    if card is None or card.pk in _deleting_card_ids(user):
        raise DomainError(404, 'not_found', 'saved card not found')
    return card


def save_card(user, payload, idempotency_key):
    card = parse_card(payload.get('card', payload))
    key = idempotency_key or uuid.uuid4().hex
    ref = _ref('user', user.pk, 'card', _digest(key))
    fingerprint = '%s:%s' % (card.number[-4:], card.expiry)
    existing = ProviderWrite.objects.filter(ref=ref).first()
    if existing is not None and existing.fingerprint != fingerprint:
        raise DomainError(422, 'idempotency_key_reused', 'this Idempotency-Key was already used for a different card')
    customer = PayPalCustomer.objects.filter(user=user).first()

    def send(request_id):
        return gateway.vault_card(request_id, card, customer_id=customer.customer_id if customer else '')

    return safe_write(
        ref, kind=ProviderWrite.VAULT_CREATE, send=send, find=send, repeat_is_safe=True,
        resend_window=VAULT_KEY_WINDOW, on_complete=_apply_vaulted_card, user=user, fingerprint=fingerprint,
    )


def _expiry_date(expiry):
    year, month = (int(p) for p in expiry.split('-'))
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def _apply_vaulted_card(record, answer):
    if answer.outcome != DONE or record.bankcard_id:
        return
    user = record.user
    if answer.extra.get('customer_id'):
        PayPalCustomer.objects.get_or_create(user=user, defaults={'customer_id': answer.extra['customer_id']})
    expiry = answer.extra.get('expiry') or ''
    bankcard = Bankcard(user=user, number='XXXX-XXXX-XXXX-%s' % answer.extra['last_digits'],
                        expiry_date=_expiry_date(expiry) if _EXPIRY.match(expiry) else datetime.date.max,
                        partner_reference=answer.provider_id)
    bankcard.card_type = answer.extra.get('brand') or 'CARD'
    bankcard.save()
    record.bankcard_id = bankcard.pk
    record.save(update_fields=['bankcard_id'])


def delete_card(user, method_id):
    ref = _ref('card', method_id, 'delete')
    card = Bankcard.objects.filter(pk=method_id, user=user).exclude(partner_reference='').first()
    if card is None:
        done = ProviderWrite.objects.filter(ref=ref, user=user, outcome=ProviderWrite.DONE).first()
        if done is not None:
            return done                              # deleted already: same answer
        raise DomainError(404, 'not_found', 'saved card not found')
    token_id = card.partner_reference

    def send(_request_id):
        return gateway.delete_vaulted_card(token_id)

    return safe_write(
        ref, kind=ProviderWrite.VAULT_DELETE, send=send, find=send, repeat_is_safe=True,
        on_complete=_apply_card_deleted, user=user, bankcard_id=card.pk,
    )


def _apply_card_deleted(record, answer):
    if answer.outcome == DONE:
        Bankcard.objects.filter(pk=record.bankcard_id, user=record.user).delete()


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def _provider_transactions(start, end):
    rows = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + SEARCH_CHUNK, end)
        page = 1
        while True:
            result = gateway.search_transactions(chunk_start, chunk_end, page=page)
            rows.extend(result.transactions)
            if page >= result.total_pages or page >= MAX_SEARCH_PAGES:
                break
            page += 1
        chunk_start = chunk_end
    # PayPal's bounds are inclusive; keep the half-open window [start, end).
    seen, unique = set(), []
    for row in rows:
        key = (row.transaction_id, row.event_code, row.initiated_at)
        if row.initiated_at is not None and start <= row.initiated_at < end and key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def reconcile(start, end):
    if end <= start:
        raise DomainError(400, 'invalid_range', 'to must be after from')
    if end - start > MAX_RECONCILIATION_SPAN:
        raise DomainError(400, 'invalid_range', 'PayPal reports cover at most the last three years')
    provider = _provider_transactions(start, end)

    # Our side, on PayPal's clock: money movements whose PayPal time is in the window.
    local = list(ProviderWrite.objects.filter(
        kind__in=[ProviderWrite.CAPTURE, ProviderWrite.REFUND],
        outcome__in=[ProviderWrite.DONE, ProviderWrite.PENDING, ProviderWrite.NEEDS_REVIEW],
        provider_time__gte=start, provider_time__lt=end,
    ).select_related('order'))
    unsettled = list(ProviderWrite.objects.filter(
        kind__in=[ProviderWrite.AUTHORIZE, ProviderWrite.REAUTHORIZE, ProviderWrite.CAPTURE,
                  ProviderWrite.VOID, ProviderWrite.REFUND],
        outcome__in=UNSETTLED, claimed_at__gte=start, claimed_at__lt=end,
    ).select_related('order'))

    by_id = defaultdict(list)
    for row in provider:
        by_id[row.transaction_id].append(row)
    matched, app_only = [], []
    for record in local:
        rows = by_id.pop(record.provider_id, [])
        if rows:
            amounts = {abs(r.amount) for r in rows if r.amount is not None}
            matched.append({'local': _write_json(record), 'paypal': [_tx_json(r) for r in rows],
                            'amountsAgree': amounts == {record.amount} if record.amount is not None else None})
        else:
            app_only.append(_write_json(record))
    paypal_only = [_tx_json(r) for rows in by_id.values() for r in rows]
    return {
        'from': start.isoformat(), 'to': end.isoformat(),
        'summary': {'paypalTransactions': len(provider), 'matched': len(matched), 'appOnly': len(app_only),
                    'paypalOnly': len(paypal_only), 'unsettled': len(unsettled)},
        'matched': matched, 'appOnly': app_only, 'paypalOnly': paypal_only,
        'unsettled': [_write_json(r) for r in unsettled],
        'note': 'PayPal lists transactions up to 3 hours after they happen; recent activity can be app-only.',
    }


def _order_number_from_invoice(invoice_id):
    prefix = _prefix() + '-'
    if invoice_id.startswith(prefix):
        return invoice_id[len(prefix):].rsplit('-', 1)[0]
    return None


def _tx_json(row):
    return {
        'transactionId': row.transaction_id, 'eventCode': row.event_code, 'status': row.status,
        'initiatedAt': row.initiated_at.isoformat() if row.initiated_at else None,
        'amount': str(row.amount) if row.amount is not None else None, 'currency': row.currency,
        'fee': str(row.fee) if row.fee is not None else None, 'invoiceId': row.invoice_id,
        'orderId': _order_number_from_invoice(row.invoice_id),
    }


def _write_json(record):
    return {
        'kind': record.kind, 'outcome': record.outcome, 'orderId': record.order.number if record.order else None,
        'paypalId': record.provider_id or None, 'paypalStatus': record.provider_status or None,
        'amount': gateway.format_amount(record.amount, record.currency) if record.amount is not None else None,
        'currency': record.currency or None,
        'paypalTime': record.provider_time.isoformat() if record.provider_time else None,
        'claimedAt': record.claimed_at.isoformat(),
    }
