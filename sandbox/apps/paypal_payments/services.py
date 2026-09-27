"""
Order, payment, saved-card and reconciliation flows.

Every PayPal write goes through ``gateway.safe_write``; its outcome is applied
to Oscar's models (order status, ``payment.Source`` ledger, ``Bankcard``) and
to ``OrderPayment`` exactly once, guarded by ``ProviderWrite.applied``.
Service functions return ``(http_status, body)`` or raise ``PaymentError``.
"""
from __future__ import annotations

import calendar
import hmac
import logging
import re
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_class, get_model
from paypal import PaypalClient
from paypal.core import ApiError, Failure, OAuthProviderError, Success, UnsetType
from paypal.models import (
    Address, AmountWithBreakdown, CapturedPayment, CaptureRequest, CardRequest, Money, Order,
    OrderAuthorizeResponse, OrderRequest, PaymentAuthorization, PaymentSource,
    PaymentTokenRequest, PaymentTokenRequestCard, PaymentTokenRequestPaymentSource,
    PaymentTokenResponse, PurchaseUnitRequest, ReauthorizeRequest, Refund, RefundRequest,
    SearchResponse, TransactionDetails)
from paypal.models.enums import (
    AuthorizationStatus, CaptureStatus, CheckoutPaymentIntent, OrderStatus, RefundStatus)

from . import gateway
from .gateway import (
    NEVER_SENT, Answer, PaymentError, WriteStep, call_read, current_write, error_issues,
    format_amount, given, provider_error, provider_time, quantize, reference, safe_write,
    to_decimal)
from .models import OrderPayment, ProviderWrite

logger = logging.getLogger(__name__)

Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
OscarOrder = get_model('order', 'Order')
ShippingAddress = get_model('order', 'ShippingAddress')
Country = get_model('address', 'Country')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Bankcard = get_model('payment', 'Bankcard')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Repository = get_class('shipping.repository', 'Repository')
Selector = get_class('partner.strategy', 'Selector')

# PayPal-Request-Id retention, from the SDK docstrings of each operation.
ORDERS_REQUEST_ID_RETENTION = timedelta(hours=6)
PAYMENTS_REQUEST_ID_RETENTION = timedelta(days=45)
VAULT_REQUEST_ID_RETENTION = timedelta(hours=3)
# "reauthorize a payment after its initial three-day honor period expires"
# (reauthorize_payment docstring).
HONOR_PERIOD = timedelta(days=3)
# "It takes a maximum of three hours for executed transactions to appear"
# (search_transactions docstring).
REPORTING_LAG = timedelta(hours=3)
# "The maximum supported range is 31 days" (search_transactions docstring).
SEARCH_CHUNK = timedelta(days=31)
SEARCH_HISTORY = timedelta(days=3 * 365)

REPRESENTATION = 'return=representation'

# Oscar order statuses (the sandbox's OSCAR_ORDER_STATUS_PIPELINE).
STATUS_AUTHORIZED = 'Being processed'
STATUS_FULFILLED = 'Complete'
STATUS_CANCELLED = 'Cancelled'

MAX_LINES = 50
MAX_QUANTITY = 100

DONE, PENDING, FAILED, UNKNOWN = (
    ProviderWrite.DONE, ProviderWrite.PENDING, ProviderWrite.FAILED, ProviderWrite.UNKNOWN)


def client() -> PaypalClient:
    return gateway.get_client()


def currency() -> str:
    return str(settings.PAYPAL_CURRENCY).upper()


def wire(value: Any) -> str | None:
    """An SDK enum/str member as its wire string; ``None`` when unset."""
    if value is None or isinstance(value, UnsetType):
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def money(amount: Decimal, code: str) -> Money:
    return Money(currency_code=code, value=format_amount(amount, code))


def bad_request(message: str, code: str = 'invalid_request') -> PaymentError:
    return PaymentError(400, code, message)


def not_found(what: str = 'Order') -> PaymentError:
    return PaymentError(404, 'not_found', '%s not found.' % what)


# ---------------------------------------------------------------------------
# Status -> outcome, one mapper per write step
# ---------------------------------------------------------------------------

def hold_outcome(status: str | None) -> str:
    """An authorization, for a step whose job is to put a hold in place."""
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return FAILED
        case _:
            # CAPTURED / PARTIALLY_CAPTURED are not a hold; unlisted values
            # and a missing status are not-yet-known.
            return UNKNOWN


def create_order_outcome(answer: Answer) -> str:
    order_status = (answer.details or {}).get('order_status')
    match order_status:
        case OrderStatus.COMPLETED:
            return hold_outcome(answer.status)
        case OrderStatus.APPROVED:
            # Created and approved: the authorize step (its own claim) follows.
            return DONE
        case OrderStatus.CREATED | OrderStatus.SAVED:
            return PENDING
        case OrderStatus.PAYER_ACTION_REQUIRED | OrderStatus.VOIDED:
            return FAILED
        case _:
            return UNKNOWN


def authorize_order_outcome(answer: Answer) -> str:
    order_status = (answer.details or {}).get('order_status')
    match order_status:
        case OrderStatus.COMPLETED:
            return hold_outcome(answer.status)
        case OrderStatus.PAYER_ACTION_REQUIRED | OrderStatus.VOIDED:
            return FAILED
        case OrderStatus.APPROVED | OrderStatus.CREATED | OrderStatus.SAVED:
            return PENDING
        case _:
            return UNKNOWN


def reauthorize_outcome(answer: Answer) -> str:
    return hold_outcome(answer.status)


def capture_outcome(answer: Answer) -> str:
    match answer.status:
        case CaptureStatus.COMPLETED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return FAILED
        case CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED:
            # Taken, then given back: not in effect any more.
            return FAILED
        case _:
            return UNKNOWN


def void_outcome(answer: Answer) -> str:
    """For the step that releases the hold, VOIDED is its done."""
    match answer.status:
        case AuthorizationStatus.VOIDED:
            return DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED  # too late: the money was taken
        case AuthorizationStatus.DENIED:
            return FAILED
        case _:
            return UNKNOWN


def refund_outcome(answer: Answer) -> str:
    """For the refund step, a completed refund is its done."""
    match answer.status:
        case RefundStatus.COMPLETED:
            return DONE
        case RefundStatus.PENDING:
            return PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return FAILED
        case _:
            return UNKNOWN


def vault_outcome(answer: Answer) -> str:
    # A payment token has no status member: it is in effect when PayPal
    # returns its id together with the card it vaulted.
    if answer.provider_id and (answer.details or {}).get('last_digits'):
        return DONE
    return UNKNOWN


# ---------------------------------------------------------------------------
# Reading PayPal responses into Answers
# ---------------------------------------------------------------------------

def read_order(order: Order | OrderAuthorizeResponse) -> Answer:
    order_id = given(order.id)
    details: dict[str, Any] = {'order_status': wire(order.status), 'paypal_order_id': order_id}
    units = given(order.purchase_units) or []
    unit = units[0] if units else None
    authorization = None
    if unit is not None:
        payments = given(unit.payments)
        authorizations = (given(payments.authorizations) if payments is not None else None) or []
        authorization = authorizations[0] if authorizations else None
    if authorization is not None:
        amount = given(authorization.amount)
        details['expiration_time'] = given(authorization.expiration_time)
        reason = given(authorization.status_details)
        if reason is not None:
            details['status_reason'] = wire(reason.reason)
        return Answer(
            provider_id=given(authorization.id),
            status=wire(authorization.status),
            provider_time=provider_time(given(authorization.create_time)),
            amount=to_decimal(amount.value) if amount is not None else None,
            currency=amount.currency_code if amount is not None else None,
            details=details)
    unit_amount = given(unit.amount) if unit is not None else None
    return Answer(
        provider_id=order_id,
        status=None,
        provider_time=provider_time(given(order.create_time)),
        amount=to_decimal(unit_amount.value) if unit_amount is not None else None,
        currency=unit_amount.currency_code if unit_amount is not None else None,
        details=details)


def read_hold(result: Order | OrderAuthorizeResponse | PaymentAuthorization) -> Answer:
    """A pay step's answer: the order it created, or (on refresh) its authorization."""
    if isinstance(result, PaymentAuthorization):
        answer = read_authorization(result)
        return answer._replace(
            provider_time=provider_time((answer.details or {}).get('create_time')) or answer.provider_time,
            details={**(answer.details or {}), 'order_status': OrderStatus.COMPLETED.value})
    return read_order(result)


def refresh_hold(write: ProviderWrite) -> PaymentAuthorization | None:
    if not write.provider_id or not write.provider_status:
        return None  # no authorization yet: nothing to re-read
    return client().payments.get_authorized_payment(write.provider_id)


def read_authorization(authorization: PaymentAuthorization) -> Answer:
    amount = given(authorization.amount)
    return Answer(
        provider_id=given(authorization.id),
        status=wire(authorization.status),
        provider_time=provider_time(given(authorization.update_time) or given(authorization.create_time)),
        amount=to_decimal(amount.value) if amount is not None else None,
        currency=amount.currency_code if amount is not None else None,
        details={
            'create_time': given(authorization.create_time),
            'expiration_time': given(authorization.expiration_time),
        })


def read_capture(capture: CapturedPayment) -> Answer:
    amount = given(capture.amount)
    details: dict[str, Any] = {}
    breakdown = given(capture.seller_receivable_breakdown)
    if breakdown is not None:
        fee = given(breakdown.paypal_fee)
        net = given(breakdown.net_amount)
        details['gross'] = breakdown.gross_amount.value
        details['paypal_fee'] = fee.value if fee is not None else None
        details['net'] = net.value if net is not None else None
    reason = given(capture.status_details)
    if reason is not None:
        details['status_reason'] = wire(reason.reason)
    return Answer(
        provider_id=given(capture.id),
        status=wire(capture.status),
        provider_time=provider_time(given(capture.create_time)),
        amount=to_decimal(amount.value) if amount is not None else None,
        currency=amount.currency_code if amount is not None else None,
        details=details)


def read_refund(refund: Refund) -> Answer:
    amount = given(refund.amount)
    reason = given(refund.status_details)
    return Answer(
        provider_id=given(refund.id),
        status=wire(refund.status),
        provider_time=provider_time(given(refund.create_time)),
        amount=to_decimal(amount.value) if amount is not None else None,
        currency=amount.currency_code if amount is not None else None,
        details={'status_reason': wire(reason.reason)} if reason is not None else {})


def read_token(token: PaymentTokenResponse) -> Answer:
    details: dict[str, Any] = {}
    source = given(token.payment_source)
    card = given(source.card) if source is not None else None
    if card is not None:
        details['last_digits'] = given(card.last_digits)
        details['brand'] = wire(card.brand)
        details['expiry'] = given(card.expiry)
    customer = given(token.customer)
    if customer is not None:
        details['customer_id'] = given(customer.id)
    created = (token.model_extra or {}).get('create_time')
    return Answer(
        provider_id=given(token.id),
        status='VAULTED' if card is not None else None,
        provider_time=provider_time(created) or timezone.now(),
        details=details)


# ---------------------------------------------------------------------------
# Input validation (card data is never logged, stored or echoed)
# ---------------------------------------------------------------------------

def _luhn_ok(number: str) -> bool:
    total = 0
    for i, digit in enumerate(reversed(number)):
        n = int(digit)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _text(data: dict[str, Any], key: str, max_length: int, required: bool = False) -> str:
    value = data.get(key)
    if value is None or value == '':
        if required:
            raise bad_request('"%s" is required.' % key)
        return ''
    if not isinstance(value, str) or len(value) > max_length:
        raise bad_request('"%s" must be a string of at most %d characters.' % (key, max_length))
    return value.strip()


def _address(data: Any) -> Address | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise bad_request('"billingAddress" must be an object.')
    country = _text(data, 'countryCode', 2, required=True).upper()
    if not re.fullmatch(r'[A-Z]{2}', country):
        raise bad_request('"countryCode" must be a two-letter ISO country code.')
    fields: dict[str, str] = {}
    for key, member, limit in (('line1', 'address_line_1', 300), ('line2', 'address_line_2', 300),
                               ('city', 'admin_area_2', 120), ('state', 'admin_area_1', 300),
                               ('postalCode', 'postal_code', 60)):
        value = _text(data, key, limit)
        if value:
            fields[member] = value
    return Address(country_code=country, **fields)


@sensitive_variables('data', 'number', 'security_code')
def parse_card(data: Any) -> dict[str, Any]:
    """Validates card input; the result goes to PayPal and nowhere else."""
    if not isinstance(data, dict):
        raise bad_request('"card" must be an object.', 'invalid_card')
    number = re.sub(r'[\s-]', '', str(data.get('number') or ''))
    if not re.fullmatch(r'\d{12,19}', number) or not _luhn_ok(number):
        raise bad_request('The card number is not valid.', 'invalid_card')
    expiry = str(data.get('expiry') or '')
    match = re.fullmatch(r'(\d{4})-(\d{2})', expiry)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise bad_request('"expiry" must be YYYY-MM.', 'invalid_card')
    year, month = int(match.group(1)), int(match.group(2))
    today = timezone.now().date()
    if (year, month) < (today.year, today.month):
        raise bad_request('The card has expired.', 'invalid_card')
    security_code = str(data.get('securityCode') or '')
    if security_code and not re.fullmatch(r'\d{3,4}', security_code):
        raise bad_request('"securityCode" must be 3 or 4 digits.', 'invalid_card')
    name = _text(data, 'name', 300)
    return {
        'number': number, 'expiry': expiry, 'security_code': security_code, 'name': name,
        'billing_address': _address(data.get('billingAddress')),
    }


def parse_amount(value: Any, code: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise bad_request('"amount" must be a decimal string such as "5.00".') from None
    if not amount.is_finite() or amount <= 0:
        raise bad_request('"amount" must be greater than zero.')
    if quantize(amount, code) != amount:
        raise bad_request('"amount" has more decimal places than %s allows.' % code)
    return amount


def card_fingerprint(card: dict[str, Any]) -> str:
    """A keyed digest of the card: stable for de-duplication, not reversible."""
    return hmac.new(settings.SECRET_KEY.encode(), ('%s|%s' % (card['number'], card['expiry'])).encode(),
                    'sha256').hexdigest()


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _money_str(value: Decimal | None, code: str) -> str | None:
    return None if value is None else format_amount(value, code)


def serialize_refund(write: ProviderWrite) -> dict[str, Any]:
    code = write.currency or currency()
    return {
        'refundId': str(write.pk),
        'paypalRefundId': write.provider_id or None,
        'amount': _money_str(write.amount, code),
        'currency': code,
        'status': write.outcome,
        'paypalStatus': write.provider_status or None,
        'message': write.error_message or None,
        'createdAt': write.claimed_at.isoformat(),
    }


def serialize_order(op: OrderPayment) -> dict[str, Any]:
    order = op.order
    code = op.currency
    refunds = [serialize_refund(w) for w in ProviderWrite.objects.filter(order_payment=op, kind=ProviderWrite.REFUND)
               if not (w.released and w.outcome == FAILED and not w.provider_id)]
    last_attempt = ProviderWrite.objects.filter(order_payment=op).filter(
        kind__in=[ProviderWrite.CREATE_ORDER, ProviderWrite.AUTHORIZE]).order_by('-pk').first()
    return {
        'orderId': order.number,
        'status': order.status,
        'placedAt': order.date_placed.isoformat() if order.date_placed else None,
        'total': format_amount(op.amount, code),
        'currency': code,
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'unitPrice': format_amount(line.unit_price_incl_tax, code) if line.unit_price_incl_tax is not None else None,
            'linePrice': format_amount(line.line_price_incl_tax, code),
        } for line in order.lines.all()],
        'payment': {
            'state': op.state,
            'paypalOrderId': op.paypal_order_id or None,
            'lastAttempt': {
                'status': last_attempt.outcome,
                'message': last_attempt.error_message or None,
            } if last_attempt else None,
            'authorization': {
                'id': op.authorization_id,
                'status': op.authorization_status,
                'createdAt': op.authorization_created_at.isoformat() if op.authorization_created_at else None,
                'expiresAt': op.authorization_expires_at.isoformat() if op.authorization_expires_at else None,
            } if op.authorization_id else None,
            'capture': {
                'id': op.capture_id,
                'status': op.capture_status,
                'amount': format_amount(op.captured_amount, code),
                'paypalFee': _money_str(op.paypal_fee, code),
                'netAmount': _money_str(op.net_amount, code),
            } if op.capture_id else None,
            'refundedAmount': format_amount(op.refunded_amount, code),
            'refundableAmount': format_amount(op.captured_amount - op.refund_reserved, code),
            'refunds': refunds,
        },
    }


def order_response(status: int, op: OrderPayment, **extra: Any) -> tuple[int, dict[str, Any]]:
    op.refresh_from_db()
    body = serialize_order(op)
    body.update(extra)
    return status, body


# ---------------------------------------------------------------------------
# Lookups (ownership is enforced here)
# ---------------------------------------------------------------------------

def shopper_order(user: Any, order_id: str) -> OrderPayment:
    op = OrderPayment.objects.select_related('order', 'source').filter(
        order__number=order_id, user=user).first()
    if op is None:
        raise not_found()
    return op


def operator_order(order_id: str) -> OrderPayment:
    op = OrderPayment.objects.select_related('order', 'source').filter(order__number=order_id).first()
    if op is None:
        raise not_found()
    return op


def _set_order_status(order: Any, status: str) -> None:
    try:
        order.set_status(status)
    except Exception:  # InvalidOrderStatus: keep the payment state authoritative
        logger.warning('Order %s could not move from %r to %r', order.number, order.status, status)


# ---------------------------------------------------------------------------
# Flow 1: orders
# ---------------------------------------------------------------------------

def _parse_lines(data: Any) -> list[tuple[int, int]]:
    lines = data.get('lines') if isinstance(data, dict) else None
    if not isinstance(lines, list) or not lines:
        raise bad_request('"lines" must be a non-empty list of {"productId", "quantity"}.')
    if len(lines) > MAX_LINES:
        raise bad_request('At most %d lines per order.' % MAX_LINES)
    merged: dict[int, int] = {}
    for line in lines:
        if not isinstance(line, dict):
            raise bad_request('Each line must be an object.')
        product_id, quantity = line.get('productId'), line.get('quantity', 1)
        if isinstance(product_id, str) and product_id.isdigit():
            product_id = int(product_id)
        if not isinstance(product_id, int) or isinstance(product_id, bool) or \
                not isinstance(quantity, int) or isinstance(quantity, bool) or \
                not 1 <= quantity <= MAX_QUANTITY:
            raise bad_request('Each line needs an integer "productId" and a "quantity" of 1-%d.' % MAX_QUANTITY)
        merged[product_id] = merged.get(product_id, 0) + quantity
    return list(merged.items())


def _shipping_address(data: Any) -> Any:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise bad_request('"shippingAddress" must be an object.')
    code = _text(data, 'countryCode', 2, required=True).upper()
    country = Country.objects.filter(iso_3166_1_a2=code).first()
    if country is None:
        raise bad_request('Unknown country "%s".' % code)
    return ShippingAddress(
        first_name=_text(data, 'firstName', 255), last_name=_text(data, 'lastName', 255),
        line1=_text(data, 'line1', 255, required=True), line2=_text(data, 'line2', 255),
        line4=_text(data, 'city', 255), state=_text(data, 'state', 255),
        postcode=_text(data, 'postcode', 64), country=country)


def place_order(request: Any, data: Any, client_key: str | None) -> tuple[int, dict[str, Any]]:
    user = request.user
    if client_key is not None and len(client_key) > 64:
        raise bad_request('Idempotency-Key must be at most 64 characters.')
    if client_key:
        existing = OrderPayment.objects.filter(user=user, client_key=client_key).first()
        if existing is not None:
            return order_response(200, existing)
    lines = _parse_lines(data)
    shipping_address = _shipping_address(data.get('shippingAddress'))
    code = currency()
    try:
        with transaction.atomic():
            op = _create_order(request, user, lines, shipping_address, code, client_key)
    except IntegrityError:
        existing = OrderPayment.objects.filter(user=user, client_key=client_key).first() if client_key else None
        if existing is None:
            raise
        return order_response(200, existing)
    return order_response(201, op)


def _create_order(request: Any, user: Any, lines: list[tuple[int, int]], shipping_address: Any,
                  code: str, client_key: str | None) -> OrderPayment:
    strategy = Selector().strategy(request=request, user=user)
    # A basket of its own (not the shopper's open storefront basket).
    basket = Basket(owner=user, status=Basket.SAVED)
    basket.strategy = strategy
    basket.save()
    for product_id, quantity in lines:
        product = Product.objects.filter(pk=product_id).first()
        if product is None:
            raise bad_request('Unknown product %s.' % product_id, 'unknown_product')
        if product.is_parent:
            raise bad_request('Product %s is a parent product; order one of its variants.' % product_id,
                              'unknown_product')
        info = strategy.fetch_for_product(product)
        if not info.price.exists:
            raise PaymentError(409, 'product_unavailable', 'Product %s has no price.' % product_id)
        permitted, reason = info.availability.is_purchase_permitted(quantity)
        if not permitted:
            raise PaymentError(409, 'product_unavailable', 'Product %s: %s' % (product_id, reason))
        basket.add_product(product, quantity)

    shipping_method = Repository().get_default_shipping_method(
        basket=basket, shipping_addr=shipping_address, request=request)
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
    amount = quantize(total.incl_tax, code)
    if amount <= 0:
        raise PaymentError(409, 'nothing_to_pay', 'The order total is zero.')
    if shipping_address is not None:
        shipping_address.save()
    order = OrderCreator().place_order(
        basket=basket, total=total, shipping_method=shipping_method,
        shipping_charge=shipping_charge, user=user, shipping_address=shipping_address,
        request=request)
    basket.submit()
    # Catalogue amounts are charged in the configured PayPal currency.
    OscarOrder.objects.filter(pk=order.pk).update(currency=code)
    source_type, _ = SourceType.objects.get_or_create(name='PayPal')
    source = Source.objects.create(order=order, source_type=source_type, currency=code, label='PayPal')
    return OrderPayment.objects.create(
        order=order, user=user, source=source, client_key=client_key or None,
        amount=amount, currency=code)


def my_orders(user: Any) -> tuple[int, dict[str, Any]]:
    ops = OrderPayment.objects.select_related('order').filter(user=user).order_by('-date_created')
    return 200, {'orders': [serialize_order(op) for op in ops]}


# ---------------------------------------------------------------------------
# Pay: authorize the order total
# ---------------------------------------------------------------------------

@sensitive_variables('data', 'card', 'payment_source')
def pay(user: Any, order_id: str, data: Any) -> tuple[int, dict[str, Any]]:
    op = shopper_order(user, order_id)
    if op.state in (OrderPayment.AUTHORIZED, OrderPayment.CAPTURED,
                    OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED):
        return order_response(200, op)  # already paid: a repeat changes nothing
    if op.state != OrderPayment.AWAITING_PAYMENT:
        raise PaymentError(409, 'order_not_payable', 'The order is %s and cannot be paid.' % op.state)

    base = reference('o%d' % op.pk, 'pay%d' % op.pay_attempt)
    write = current_write(base)
    if write is not None and write.outcome == FAILED:
        # The last attempt was declined: the next one goes out under a new
        # reference. The conditional update lets one request advance it.
        OrderPayment.objects.filter(pk=op.pk, pay_attempt=op.pay_attempt).update(
            pay_attempt=F('pay_attempt') + 1)
        op.refresh_from_db()
        base = reference('o%d' % op.pk, 'pay%d' % op.pay_attempt)
        write = current_write(base)

    # A repeat of an attempt whose outcome is not known is checked by resending
    # the identical request under the same PayPal-Request-Id, which needs the
    # payment details again; a first attempt needs them anyway.
    has_source = isinstance(data, dict) and (
        data.get('card') is not None or data.get('paymentMethodId') is not None)
    payment_source = _payment_source(user, data) if write is None or has_source else None
    if write is not None and payment_source is None and (
            write.outcome == UNKNOWN or (write.outcome == ProviderWrite.SENDING
                                         and write.claimed_at <= timezone.now() - gateway.SEND_WINDOW)):
        raise PaymentError(504, 'outcome_unknown',
                           'The outcome of the last payment attempt is not known yet; repeat the request '
                           'with the same payment details to check it.',
                           outcome_unknown=True, reference=write.ref)

    request_body = None
    if payment_source is not None:
        request_body = OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[PurchaseUnitRequest(
                reference_id=op.order.number,
                custom_id=gateway.custom_id_prefix() + op.order.number,
                description='Order %s' % op.order.number,
                amount=AmountWithBreakdown(currency_code=op.currency,
                                           value=format_amount(op.amount, op.currency)))],
            payment_source=payment_source)

    def send(key: str) -> Order:
        assert request_body is not None, 'a send always has the payment details (checked above)'
        return client().orders.create_order(request_body, pay_pal_request_id=key, prefer=REPRESENTATION)

    def still_payable(claim: ProviderWrite) -> None:
        # Written claim first, then read the state: a concurrent cancel does
        # the reverse, so one of the two always sees the other.
        if not OrderPayment.objects.filter(pk=op.pk, state=OrderPayment.AWAITING_PAYMENT).exists():
            raise PaymentError(409, 'order_not_payable', 'The order is no longer awaiting payment.')

    try:
        write = safe_write(WriteStep(
            kind=ProviderWrite.CREATE_ORDER, base_ref=base, send=send, read=read_hold,
            outcome_of=create_order_outcome, resend_window=ORDERS_REQUEST_ID_RETENTION,
            sent=(op.amount, op.currency), order_payment=op, user=user,
            on_claimed=still_payable, refresh=refresh_hold))
    except PaymentError as exc:
        if exc.status_code == 422:
            raise PaymentError(402, 'payment_declined', exc.message) from exc
        raise
    return _finish_pay(op, write)


@sensitive_variables('data', 'card', 'parsed')
def _payment_source(user: Any, data: Any) -> PaymentSource:
    if not isinstance(data, dict):
        raise bad_request('Send either "card" or "paymentMethodId".')
    card, method_id = data.get('card'), data.get('paymentMethodId')
    if (card is None) == (method_id is None):
        raise bad_request('Send exactly one of "card" or "paymentMethodId".')
    if method_id is not None:
        saved = Bankcard.objects.filter(user=user, pk=str(method_id)).first() \
            if str(method_id).isdigit() else None
        if saved is None or not saved.partner_reference:
            raise not_found('Payment method')
        return PaymentSource(card=CardRequest(vault_id=saved.partner_reference))
    parsed = parse_card(card)
    fields: dict[str, Any] = {'number': parsed['number'], 'expiry': parsed['expiry']}
    if parsed['security_code']:
        fields['security_code'] = parsed['security_code']
    if parsed['name']:
        fields['name'] = parsed['name']
    if parsed['billing_address'] is not None:
        fields['billing_address'] = parsed['billing_address']
    return PaymentSource(card=CardRequest(**fields))


def _finish_pay(op: OrderPayment, write: ProviderWrite) -> tuple[int, dict[str, Any]]:
    order_status = write.details.get('order_status')
    if write.outcome == DONE and order_status == OrderStatus.APPROVED:
        write = _authorize(op, write)
    if write.outcome == DONE:
        _apply_authorization(op, write)
        return order_response(200, op)
    if write.outcome in (ProviderWrite.SENDING, PENDING):
        _record_pending_hold(op, write)
        return order_response(202, op, message='The payment is being processed by PayPal.')
    if write.outcome == FAILED:
        if order_status == OrderStatus.PAYER_ACTION_REQUIRED:
            raise PaymentError(402, 'payer_action_required',
                               'The card issuer requires the shopper to approve this payment in a '
                               'browser (3-D Secure), which this API does not support.')
        raise PaymentError(402, 'payment_declined',
                           'PayPal declined the payment (%s).' % (
                               write.details.get('status_reason') or write.provider_status or
                               write.error_message or 'no reason given'))
    if write.outcome == ProviderWrite.NEEDS_REVIEW:
        raise PaymentError(409, 'needs_review', write.error_message)
    raise PaymentError(504, 'outcome_unknown', 'PayPal did not confirm the payment.',
                       outcome_unknown=True, reference=write.ref)


def _authorize(op: OrderPayment, created: ProviderWrite) -> ProviderWrite:
    paypal_order_id = created.details.get('paypal_order_id') or created.provider_id
    return safe_write(WriteStep(
        kind=ProviderWrite.AUTHORIZE, base_ref=created.base_ref + ':authorize',
        send=lambda key: client().orders.authorize_order(
            paypal_order_id, pay_pal_request_id=key, prefer=REPRESENTATION),
        read=read_hold, outcome_of=authorize_order_outcome,
        resend_window=ORDERS_REQUEST_ID_RETENTION, sent=(op.amount, op.currency),
        order_payment=op, user=op.user, refresh=refresh_hold))


def _record_pending_hold(op: OrderPayment, write: ProviderWrite) -> None:
    OrderPayment.objects.filter(pk=op.pk, state=OrderPayment.AWAITING_PAYMENT).update(
        paypal_order_id=write.details.get('paypal_order_id') or '',
        authorization_id=write.provider_id if write.provider_status else '',
        authorization_status=write.provider_status)


def _apply_authorization(op: OrderPayment, write: ProviderWrite) -> None:
    with transaction.atomic():
        if not ProviderWrite.objects.filter(pk=write.pk, applied=False).update(applied=True):
            return
        op = OrderPayment.objects.select_for_update().select_related('order', 'source').get(pk=op.pk)
        op.paypal_order_id = write.details.get('paypal_order_id') or op.paypal_order_id
        op.authorization_id = write.provider_id
        op.authorization_status = write.provider_status
        op.authorization_created_at = write.provider_time
        op.authorization_expires_at = provider_time(write.details.get('expiration_time'))
        if op.state != OrderPayment.AWAITING_PAYMENT:
            logger.error('Authorization %s arrived for order %s in state %s; needs an operator.',
                         write.provider_id, op.order.number, op.state)
            op.save()
            return
        op.state = OrderPayment.AUTHORIZED
        op.save()
        op.source.allocate(write.echoed_amount or op.amount, reference=write.provider_id,
                           status=write.provider_status)
        _set_order_status(op.order, STATUS_AUTHORIZED)


# ---------------------------------------------------------------------------
# Fulfil: capture (renewing a stale authorization first)
# ---------------------------------------------------------------------------

def fulfil(order_id: str) -> tuple[int, dict[str, Any]]:
    op = operator_order(order_id)
    if op.state in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED):
        return order_response(200, op)
    if op.state != OrderPayment.AUTHORIZED:
        raise PaymentError(409, 'order_not_authorized',
                           'Only an order whose payment is authorized can be fulfilled (it is %s).' % op.state)
    authorization_id = op.authorization_id
    capture_base = reference('o%d' % op.pk, 'capture', authorization_id)
    if current_write(capture_base) is None:
        authorization_id = _renew_if_stale(op)
        capture_base = reference('o%d' % op.pk, 'capture', authorization_id)
    try:
        write = _capture(op, authorization_id, capture_base)
    except PaymentError as exc:
        if not any('EXPIRED' in issue for issue in exc.issues):
            raise
        # PayPal says the hold went stale: renew it once and capture that.
        authorization_id = _renew(op, authorization_id)
        write = _capture(op, authorization_id, reference('o%d' % op.pk, 'capture', authorization_id))
    return _finish_capture(op, write)


def _renew_if_stale(op: OrderPayment) -> str:
    live = call_read(lambda: client().payments.get_authorized_payment(op.authorization_id))
    status = wire(live.status)
    created = provider_time(given(live.create_time)) or op.authorization_created_at
    expires = provider_time(given(live.expiration_time)) or op.authorization_expires_at
    OrderPayment.objects.filter(pk=op.pk).update(
        authorization_status=status or op.authorization_status,
        authorization_created_at=created, authorization_expires_at=expires)
    if status in (AuthorizationStatus.VOIDED, AuthorizationStatus.DENIED):
        raise PaymentError(
            409, 'authorization_not_capturable',
            'PayPal reports the authorization %s as %s, so there are no held funds to take. '
            'Cancel the order and ask the shopper to pay again.' % (op.authorization_id, status))
    now = timezone.now()
    if expires is not None and now >= expires:
        raise PaymentError(
            409, 'authorization_expired',
            'The authorization expired on %s and can no longer be captured or renewed. '
            'Cancel the order and ask the shopper to pay again.' % expires.isoformat())
    if status == AuthorizationStatus.CREATED and created is not None and now >= created + HONOR_PERIOD:
        return _renew(op, op.authorization_id)
    return op.authorization_id


def _renew(op: OrderPayment, authorization_id: str) -> str:
    try:
        write = safe_write(WriteStep(
            kind=ProviderWrite.REAUTHORIZE,
            base_ref=reference('o%d' % op.pk, 'reauth', authorization_id),
            send=lambda key: client().payments.reauthorize_payment(
                authorization_id, pay_pal_request_id=key, prefer=REPRESENTATION,
                body=ReauthorizeRequest(amount=money(op.amount, op.currency))),
            read=read_authorization, outcome_of=lambda a: reauthorize_outcome(a),
            resend_window=PAYMENTS_REQUEST_ID_RETENTION, sent=(op.amount, op.currency),
            order_payment=op, user=op.user))
    except PaymentError as exc:
        if exc.status_code in (400, 409, 422):
            raise PaymentError(
                409, 'authorization_renewal_failed',
                'The authorization is past its honor period and PayPal refused to renew it: %s '
                'Cancel the order and ask the shopper to pay again.' % exc.message) from exc
        raise
    if write.outcome == DONE:
        _apply_reauthorization(op, write)
        return write.provider_id
    if write.outcome in (ProviderWrite.SENDING, PENDING):
        raise PaymentError(409, 'authorization_renewal_pending',
                           'PayPal is still renewing the authorization; retry fulfilment shortly.')
    if write.outcome == FAILED:
        raise PaymentError(
            409, 'authorization_renewal_failed',
            'PayPal did not renew the authorization (%s). Cancel the order and ask the shopper '
            'to pay again.' % (write.provider_status or write.error_message))
    if write.outcome == ProviderWrite.NEEDS_REVIEW:
        raise PaymentError(409, 'needs_review', write.error_message or 'Renewal needs review.')
    raise PaymentError(504, 'outcome_unknown', 'PayPal did not confirm the renewal (status %s).'
                       % (write.provider_status or 'missing'), outcome_unknown=True, reference=write.ref)


def _apply_reauthorization(op: OrderPayment, write: ProviderWrite) -> None:
    with transaction.atomic():
        if not ProviderWrite.objects.filter(pk=write.pk, applied=False).update(applied=True):
            return
        op = OrderPayment.objects.select_for_update().select_related('source').get(pk=op.pk)
        op.authorization_id = write.provider_id
        op.authorization_status = write.provider_status
        op.authorization_created_at = provider_time(write.details.get('create_time')) or write.provider_time
        op.authorization_expires_at = provider_time(write.details.get('expiration_time'))
        op.save()
        op.source.transactions.create(txn_type='Reauthorise', amount=op.amount,
                                      reference=write.provider_id, status=write.provider_status)


def _capture(op: OrderPayment, authorization_id: str, base: str) -> ProviderWrite:
    body = CaptureRequest(amount=money(op.amount, op.currency), final_capture=True)
    return safe_write(WriteStep(
        kind=ProviderWrite.CAPTURE, base_ref=base,
        send=lambda key: client().payments.capture_authorized_payment(
            authorization_id, pay_pal_request_id=key, prefer=REPRESENTATION, body=body),
        read=read_capture, outcome_of=capture_outcome,
        resend_window=PAYMENTS_REQUEST_ID_RETENTION, sent=(op.amount, op.currency),
        order_payment=op, user=op.user,
        refresh=lambda w: client().payments.get_captured_payment(w.provider_id) if w.provider_id else None))


def _finish_capture(op: OrderPayment, write: ProviderWrite) -> tuple[int, dict[str, Any]]:
    if write.outcome == DONE:
        _apply_capture(op, write)
        return order_response(200, op)
    if write.outcome in (ProviderWrite.SENDING, PENDING):
        OrderPayment.objects.filter(pk=op.pk).update(
            capture_id=write.provider_id, capture_status=write.provider_status)
        return order_response(202, op, message='PayPal has not completed the capture yet; '
                                               'repeat the request to check.')
    if write.outcome == FAILED:
        raise PaymentError(409, 'capture_failed', 'PayPal did not complete the capture (%s).' % (
            write.details.get('status_reason') or write.provider_status or write.error_message))
    if write.outcome == ProviderWrite.NEEDS_REVIEW:
        raise PaymentError(409, 'needs_review', write.error_message or 'The capture needs review.')
    raise PaymentError(504, 'outcome_unknown', 'PayPal did not confirm the capture (status %s).'
                       % (write.provider_status or 'missing'), outcome_unknown=True, reference=write.ref)


def _apply_capture(op: OrderPayment, write: ProviderWrite) -> None:
    with transaction.atomic():
        if not ProviderWrite.objects.filter(pk=write.pk, applied=False).update(applied=True):
            return
        op = OrderPayment.objects.select_for_update().select_related('order', 'source').get(pk=op.pk)
        amount = write.echoed_amount or op.amount
        op.capture_id = write.provider_id
        op.capture_status = write.provider_status
        op.captured_amount = amount
        op.paypal_fee = to_decimal(write.details.get('paypal_fee'))
        op.net_amount = to_decimal(write.details.get('net'))
        op.authorization_status = AuthorizationStatus.CAPTURED.value
        op.state = OrderPayment.CAPTURED
        op.save()
        op.source.debit(amount, reference=write.provider_id, status=write.provider_status)
        _set_order_status(op.order, STATUS_FULFILLED)


# ---------------------------------------------------------------------------
# Cancel: release the hold before fulfilment
# ---------------------------------------------------------------------------

def cancel(order_id: str) -> tuple[int, dict[str, Any]]:
    op = operator_order(order_id)
    if op.state in (OrderPayment.VOIDED, OrderPayment.CANCELLED):
        return order_response(200, op)
    if op.state in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED):
        raise PaymentError(409, 'already_fulfilled',
                           'The payment was already captured; refund it instead of cancelling.')
    if op.state == OrderPayment.AWAITING_PAYMENT:
        return _cancel_unpaid(op)
    return _void(op)


def _cancel_unpaid(op: OrderPayment) -> tuple[int, dict[str, Any]]:
    if not OrderPayment.objects.filter(pk=op.pk, state=OrderPayment.AWAITING_PAYMENT).update(
            state=OrderPayment.CANCELLED):
        return cancel(op.order.number)  # the state moved under us: decide again
    attempts = ProviderWrite.objects.filter(
        order_payment=op, kind__in=[ProviderWrite.CREATE_ORDER, ProviderWrite.AUTHORIZE],
        released=False).exclude(outcome=FAILED)
    if attempts.exists():
        OrderPayment.objects.filter(pk=op.pk, state=OrderPayment.CANCELLED).update(
            state=OrderPayment.AWAITING_PAYMENT)
        raise PaymentError(409, 'payment_in_progress',
                           'A payment attempt for this order is in progress or unsettled; retry shortly.')
    _set_order_status(op.order, STATUS_CANCELLED)
    return order_response(200, op, message='Cancelled; no payment had been taken.')


def _void(op: OrderPayment) -> tuple[int, dict[str, Any]]:
    authorization_id = op.authorization_id
    captures = ProviderWrite.objects.filter(
        order_payment=op, kind=ProviderWrite.CAPTURE, released=False).exclude(outcome=FAILED)
    if captures.exists():
        raise PaymentError(409, 'capture_in_progress',
                           'A capture for this order is in progress; it cannot be cancelled now.')
    write = safe_write(WriteStep(
        kind=ProviderWrite.VOID, base_ref=reference('o%d' % op.pk, 'void', authorization_id),
        send=lambda key: client().payments.void_payment(
            authorization_id, pay_pal_request_id=key, prefer=REPRESENTATION),
        read=read_authorization, outcome_of=void_outcome,
        resend_window=PAYMENTS_REQUEST_ID_RETENTION, order_payment=op, user=op.user,
        find=lambda ref: client().payments.get_authorized_payment(authorization_id),
        is_landing=lambda exc: 'PREVIOUSLY_VOIDED' in error_issues(exc.error)))
    if write.outcome == DONE:
        _apply_void(op, write)
        return order_response(200, op, message='Cancelled; the held funds were released.')
    if write.outcome in (ProviderWrite.SENDING, PENDING):
        return order_response(202, op, message='The release of held funds is in progress.')
    if write.outcome == FAILED:
        raise PaymentError(409, 'void_failed', 'PayPal did not release the hold: the authorization is %s.'
                           % (write.provider_status or 'not voidable'))
    raise PaymentError(504, 'outcome_unknown', 'PayPal did not confirm the release.',
                       outcome_unknown=True, reference=write.ref)


def _apply_void(op: OrderPayment, write: ProviderWrite) -> None:
    with transaction.atomic():
        if not ProviderWrite.objects.filter(pk=write.pk, applied=False).update(applied=True):
            return
        op = OrderPayment.objects.select_for_update().select_related('order', 'source').get(pk=op.pk)
        op.authorization_status = write.provider_status
        op.state = OrderPayment.VOIDED
        op.save()
        source = op.source
        source.amount_allocated -= min(source.amount_allocated, op.amount)
        source.save()
        source.transactions.create(txn_type='Void', amount=op.amount, reference=write.provider_id,
                                   status=write.provider_status)
        _set_order_status(op.order, STATUS_CANCELLED)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def refund(user: Any, order_id: str, data: Any, idempotency_key: str | None) -> tuple[int, dict[str, Any]]:
    op = shopper_order(user, order_id)
    if not idempotency_key and isinstance(data, dict):
        idempotency_key = data.get('idempotencyKey')
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 255:
        raise bad_request('An idempotency key (Idempotency-Key header or "idempotencyKey") of at most '
                          '255 characters is required.', 'idempotency_key_required')
    requested = None
    if isinstance(data, dict) and data.get('amount') is not None:
        requested = parse_amount(data['amount'], op.currency)

    base = reference('o%d' % op.pk, 'refund', gateway.digest(idempotency_key))
    existing = current_write(base)
    if existing is not None:
        if requested is not None and existing.amount != requested:
            raise PaymentError(422, 'idempotency_key_reused',
                               'This idempotency key was already used for a refund of %s.' % existing.amount)
        if existing.amount is None:
            raise PaymentError(409, 'needs_review', 'The refund record has no amount.')
        amount = existing.amount
    else:
        if op.state not in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED):
            if op.state == OrderPayment.REFUNDED:
                raise PaymentError(409, 'nothing_to_refund', 'The payment is already fully refunded.')
            raise PaymentError(409, 'not_captured',
                               'Only a captured (fulfilled) payment can be refunded; the order is %s.' % op.state)
        amount = requested if requested is not None else op.captured_amount - op.refund_reserved
        if amount <= 0:
            raise PaymentError(409, 'nothing_to_refund', 'Nothing is left to refund.')

    def reserve(claim: ProviderWrite) -> None:
        # Atomic: never lets reserved refunds exceed what was captured.
        reserved = OrderPayment.objects.filter(
            pk=op.pk, refund_reserved__lte=F('captured_amount') - amount).update(
            refund_reserved=F('refund_reserved') + amount)
        if not reserved:
            op.refresh_from_db()
            raise PaymentError(409, 'refund_exceeds_captured',
                               'At most %s can still be refunded.' % format_amount(
                                   op.captured_amount - op.refund_reserved, op.currency))
        claim.details = {**claim.details, 'reserved': True}
        claim.save(update_fields=['details'])

    body = RefundRequest(amount=money(amount, op.currency),
                         custom_id=gateway.custom_id_prefix() + op.order.number)
    capture_id = op.capture_id
    try:
        write = safe_write(WriteStep(
            kind=ProviderWrite.REFUND, base_ref=base,
            send=lambda key: client().payments.refund_captured_payment(
                capture_id, pay_pal_request_id=key, prefer=REPRESENTATION, body=body),
            read=read_refund, outcome_of=refund_outcome,
            resend_window=PAYMENTS_REQUEST_ID_RETENTION, sent=(amount, op.currency),
            order_payment=op, user=user, on_claimed=reserve,
            refresh=lambda w: client().payments.get_refund(w.provider_id) if w.provider_id else None))
    except PaymentError:
        last = ProviderWrite.objects.filter(base_ref=base).order_by('-pk').first()
        if last is not None and last.outcome == FAILED:
            _apply_refund(op, last)
        raise
    _apply_refund(op, write)
    op.refresh_from_db()
    payload = serialize_refund(write)
    payload['order'] = serialize_order(op)
    match write.outcome:
        case ProviderWrite.DONE:
            return 201, payload
        case ProviderWrite.SENDING | ProviderWrite.PENDING:
            return 202, payload
        case ProviderWrite.FAILED | ProviderWrite.NEEDS_REVIEW:
            payload['error'] = {'code': 'refund_failed' if write.outcome == FAILED else 'needs_review',
                                'message': write.error_message or 'PayPal did not complete the refund (%s).'
                                % (write.details.get('status_reason') or write.provider_status)}
            return 409, payload
        case _:
            return 504, payload


def _apply_refund(op: OrderPayment, write: ProviderWrite) -> None:
    if write.outcome not in (DONE, FAILED):
        return
    with transaction.atomic():
        if not ProviderWrite.objects.filter(pk=write.pk, applied=False).update(applied=True):
            return
        op = OrderPayment.objects.select_for_update().select_related('source').get(pk=op.pk)
        amount = write.amount or Decimal('0')
        if write.outcome == FAILED:
            if write.details.get('reserved'):
                op.refund_reserved -= amount
                op.save()
            return
        refunded = write.echoed_amount or amount
        op.refunded_amount += refunded
        op.state = OrderPayment.REFUNDED if op.refunded_amount >= op.captured_amount \
            else OrderPayment.PARTIALLY_REFUNDED
        op.save()
        op.source.refund(refunded, reference=write.provider_id, status=write.provider_status)


# ---------------------------------------------------------------------------
# Flow 2: saved cards
# ---------------------------------------------------------------------------

def serialize_card(card: Any) -> dict[str, Any]:
    last4 = card.number[-4:]
    return {
        'paymentMethodId': str(card.pk),
        'brand': card.card_type,
        'last4': last4,
        'expiry': card.expiry_date.strftime('%Y-%m'),
        'label': '%s ending %s, expires %s' % (card.card_type, last4, card.expiry_date.strftime('%m/%Y')),
    }


def list_cards(user: Any) -> tuple[int, dict[str, Any]]:
    cards = Bankcard.objects.filter(user=user).exclude(partner_reference='').order_by('pk')
    return 200, {'paymentMethods': [serialize_card(c) for c in cards]}


@sensitive_variables('data', 'card', 'request_body')
def save_card(user: Any, data: Any, idempotency_key: str | None) -> tuple[int, dict[str, Any]]:
    if not isinstance(data, dict):
        raise bad_request('Send {"card": {...}}.', 'invalid_card')
    card = parse_card(data.get('card', data))
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 255):
        raise bad_request('Idempotency-Key must be 1-255 characters.')
    key = 'key:' + idempotency_key if idempotency_key else 'card:' + card_fingerprint(card)
    base = reference('u%d' % user.pk, 'card', gateway.digest(key))

    fields: dict[str, Any] = {'number': card['number'], 'expiry': card['expiry']}
    if card['security_code']:
        fields['security_code'] = card['security_code']
    if card['name']:
        fields['name'] = card['name']
    if card['billing_address'] is not None:
        fields['billing_address'] = card['billing_address']
    request_body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(**fields)))

    write = safe_write(WriteStep(
        kind=ProviderWrite.VAULT_CREATE, base_ref=base,
        send=lambda ref: client().vault.create_payment_token(request_body, pay_pal_request_id=ref),
        read=read_token, outcome_of=vault_outcome,
        resend_window=VAULT_REQUEST_ID_RETENTION, user=user))
    if write.outcome == ProviderWrite.SENDING:
        return 202, {'status': 'in_progress', 'message': 'The card is being saved; repeat the request.'}
    if write.outcome != DONE:
        raise PaymentError(504, 'outcome_unknown', 'PayPal did not confirm the saved card.',
                           outcome_unknown=True, reference=write.ref)
    saved = _apply_vaulted_card(user, write)
    return 201, serialize_card(saved)


def _apply_vaulted_card(user: Any, write: ProviderWrite) -> Any:
    with transaction.atomic():
        write = ProviderWrite.objects.select_for_update().get(pk=write.pk)
        if write.applied:
            card = Bankcard.objects.filter(pk=write.details.get('bankcard_id'), user=user).first()
            if card is None:
                raise not_found('Payment method')
            return card
        expiry = str(write.details.get('expiry') or '')
        year, month = (int(part) for part in expiry.split('-')[:2])
        card = Bankcard(user=user, number='XXXX-XXXX-XXXX-%s' % write.details.get('last_digits'),
                        expiry_date=date(year, month, calendar.monthrange(year, month)[1]),
                        partner_reference=write.provider_id)
        card.card_type = str(write.details.get('brand') or 'CARD')
        card.save()
        write.applied = True
        write.details = {**write.details, 'bankcard_id': card.pk}
        write.save(update_fields=['applied', 'details'])
        return card


def delete_card(user: Any, payment_method_id: str) -> tuple[int, dict[str, Any] | None]:
    card = Bankcard.objects.filter(user=user, pk=payment_method_id).first() \
        if payment_method_id.isdigit() else None
    if card is None:
        raise not_found('Payment method')
    if card.partner_reference:
        token_id = card.partner_reference
        try:
            result = client().vault.with_raw_response.delete_payment_token(token_id)
        except ApiError as exc:  # the token fetch failed: nothing was sent
            raise provider_error(exc.status_code, exc.error) from exc
        except NEVER_SENT as exc:
            raise PaymentError(502, 'paypal_unreachable', 'PayPal could not be reached; the card was kept.') from exc
        except httpx.RequestError as exc:
            raise PaymentError(504, 'outcome_unknown', 'PayPal did not confirm the deletion; the card was '
                               'kept. Repeat the request.', outcome_unknown=True) from exc
        match result:
            case Success():
                pass
            case Failure(error=error, response=response) if response.status_code != 404:
                raise provider_error(response.status_code, error)
    with transaction.atomic():
        # Free the save-card claim so the same card can be saved again later.
        ProviderWrite.objects.filter(
            kind=ProviderWrite.VAULT_CREATE, user=user, details__bankcard_id=card.pk).update(released=True)
        card.delete()
    return 204, None


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _rfc3339(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _fetch_transactions(start: datetime, end: datetime) -> list[TransactionDetails]:
    rows: list[TransactionDetails] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + SEARCH_CHUNK, end)
        page = 1
        while True:
            response: SearchResponse = call_read(
                lambda: client().transaction_search.search_transactions(
                    _rfc3339(chunk_start), _rfc3339(chunk_end), fields='all', page_size=100, page=page))
            rows.extend(given(response.transaction_details) or [])
            total_pages = given(response.total_pages) or 0
            if page >= total_pages:
                break
            page += 1
        chunk_start = chunk_end
    return rows


def _provider_row(detail: TransactionDetails) -> dict[str, Any] | None:
    info = given(detail.transaction_info)
    if info is None:
        return None
    amount = given(info.transaction_amount)
    fee = given(info.fee_amount)
    return {
        'transactionId': given(info.transaction_id),
        'referenceId': given(info.paypal_reference_id),
        'eventCode': given(info.transaction_event_code),
        'initiatedAt': given(info.transaction_initiation_date),
        'amount': amount.value if amount is not None else None,
        'currency': amount.currency_code if amount is not None else None,
        'fee': fee.value if fee is not None else None,
        'status': given(info.transaction_status),
        'customField': given(info.custom_field),
        'invoiceId': given(info.invoice_id),
    }


def _local_row(write: ProviderWrite) -> dict[str, Any]:
    op = write.order_payment
    return {
        'orderId': op.order.number if op else None,
        'kind': write.kind,
        'transactionId': write.provider_id or None,
        'amount': _money_str(write.echoed_amount or write.amount, write.currency or currency()),
        'currency': write.currency or None,
        'outcome': write.outcome,
        'paypalStatus': write.provider_status or None,
        'paypalTime': write.provider_time.isoformat() if write.provider_time else None,
        'claimedAt': write.claimed_at.isoformat(),
    }


def reconciliation(start: datetime, end: datetime) -> tuple[int, dict[str, Any]]:
    if end <= start:
        raise bad_request('"to" must be after "from".')
    now = timezone.now()
    if start < now - SEARCH_HISTORY:
        raise bad_request('PayPal only lists transactions from the previous three years.')
    query_end = min(end, now)
    fetched = _fetch_transactions(start, query_end) if start < query_end else []

    # Provider side, on PayPal's clock, narrowed back to the caller's window.
    provider: list[dict[str, Any]] = []
    for detail in fetched:
        row = _provider_row(detail)
        when = provider_time(row['initiatedAt']) if row else None
        if row is not None and when is not None and start <= when < end:
            provider.append(row)

    # Local side on the same clock: money movements PayPal reported to us.
    money_kinds = [ProviderWrite.CAPTURE, ProviderWrite.REFUND]
    local = list(ProviderWrite.objects.select_related('order_payment__order').filter(
        kind__in=money_kinds, provider_time__gte=start, provider_time__lt=end,
        outcome__in=[DONE, PENDING, ProviderWrite.NEEDS_REVIEW]))
    unsettled = list(ProviderWrite.objects.select_related('order_payment__order').filter(
        kind__in=money_kinds, claimed_at__gte=start, claimed_at__lt=end,
        outcome__in=[ProviderWrite.SENDING, UNKNOWN]))

    # Match against the set: a local write owns every PayPal row with its id.
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in provider:
        by_id.setdefault(row['transactionId'] or '', []).append(row)
    matched, local_only, mismatches = [], [], []
    claimed_ids: set[str] = set()
    for write in local:
        rows = by_id.get(write.provider_id, []) if write.provider_id else []
        entry = _local_row(write)
        if not rows:
            entry['withinReportingLag'] = bool(write.provider_time and write.provider_time > now - REPORTING_LAG)
            local_only.append(entry)
            continue
        claimed_ids.add(write.provider_id)
        entry['paypal'] = rows
        matched.append(entry)
        local_amount = write.echoed_amount or write.amount
        for row in rows:
            paypal_amount = to_decimal(row['amount'])
            if paypal_amount is not None and local_amount is not None and abs(paypal_amount) != local_amount:
                mismatches.append({'transactionId': write.provider_id, 'local': str(local_amount),
                                   'paypal': row['amount']})
    ours_prefix = gateway.custom_id_prefix()
    provider_only = [row for tid, rows in by_id.items() if tid not in claimed_ids for row in rows]
    return 200, {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'paypalTransactionCount': len(provider),
        'summary': {
            'matched': len(matched),
            'localOnly': len(local_only),
            'paypalOnly': len(provider_only),
            'unsettled': len(unsettled),
            'amountMismatches': len(mismatches),
        },
        'matched': matched,
        'localOnly': local_only,
        'paypalOnly': {
            'thisSite': [r for r in provider_only if (r['customField'] or '').startswith(ours_prefix)],
            'other': [r for r in provider_only if not (r['customField'] or '').startswith(ours_prefix)],
        },
        'unsettled': [_local_row(w) for w in unsettled],
        'amountMismatches': mismatches,
        'note': 'PayPal transaction reporting can lag live activity by up to three hours; local-only '
                'entries flagged withinReportingLag may simply not be reported yet.',
    }
