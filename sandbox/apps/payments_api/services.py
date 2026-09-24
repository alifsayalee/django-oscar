"""
Order, payment, refund, saved-card and reconciliation logic.

Every PayPal write follows the same discipline:

* the state change that says "we are about to ask PayPal" is committed before
  the call, through a conditional UPDATE, so a double-click finds it;
* the PayPal-Request-Id is derived from what identifies the operation (the
  payment reference plus the attempt, authorization or refund it concerns), so
  repeating the same request re-sends under the same id and PayPal answers with
  the original result instead of acting twice;
* a call whose outcome is unknown (no answer, a PayPal 5xx, an unreadable
  body) is recorded as ``unknown``, never as failed, and the next identical
  request resolves it by re-sending under the same id.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price

from . import gateway, money
from .models import PaypalCustomer, PaypalPayment, PaypalRefund, SavedCard

logger = logging.getLogger(__name__)

Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
Basket = get_model('basket', 'Basket')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
OrderCreator = get_class('order.utils', 'OrderCreator')
EventHandler = get_class('order.processing', 'EventHandler')
Selector = get_class('partner.strategy', 'Selector')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')

SOURCE_TYPE_NAME = 'PayPal'

# The honor period after which PayPal recommends reauthorizing before capture
# (reauthorize_payment docstring: "after its initial three-day honor period").
HONOR_PERIOD = timedelta(days=3)

# Order statuses from the sandbox's OSCAR_ORDER_STATUS_PIPELINE.
ORDER_STATUS_PAID = 'Being processed'
ORDER_STATUS_COMPLETE = 'Complete'
ORDER_STATUS_CANCELLED = 'Cancelled'

MAX_ORDER_LINES = 50
MAX_LINE_QUANTITY = 100
RECONCILIATION_WINDOW = timedelta(days=30)   # PayPal allows at most 31 days per search
RECONCILIATION_PAGE_SIZE = 500               # PayPal's maximum page size
RECONCILIATION_MAX_RANGE = timedelta(days=366)


class ApiProblem(Exception):
    """A request this app refuses or could not complete, with the status to answer."""

    def __init__(self, status_code: int, code: str, message: str, *, payment=None, refund=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.payment = payment
        self.refund = refund


@dataclass
class Result:
    """What a payment action produced: the object to show and the status to answer."""

    status_code: int
    payment: PaypalPayment
    refund: PaypalRefund | None = None


def _stale_after() -> timedelta:
    """How long an in-flight call can take before its record is treated as unknown."""
    return timedelta(seconds=float(settings.PAYPAL_TIMEOUT_SECONDS) * 2 + 30)


def _is_stale(payment: PaypalPayment) -> bool:
    return timezone.now() - payment.date_updated > _stale_after()


def _fmt(value: Decimal | None, currency: str) -> str | None:
    if value is None:
        return None
    return str(value.quantize(money.quantum(currency)))


def _claim(payment: PaypalPayment, from_statuses: tuple[str, ...], to_status: str, **fields) -> bool:
    """Atomically move the payment into an in-flight status. False if someone else did."""
    updated = PaypalPayment.objects.filter(pk=payment.pk, status__in=from_statuses).update(
        status=to_status, date_updated=timezone.now(), **fields)
    if updated:
        payment.refresh_from_db()
    return bool(updated)


def _set_error(payment: PaypalPayment, code: str, message: str) -> None:
    payment.last_error_code = code[:64]
    payment.last_error = message


def _clear_error(payment: PaypalPayment) -> None:
    payment.last_error_code = ''
    payment.last_error = ''


def _oscar_source(payment: PaypalPayment):
    source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
    source, _ = Source.objects.get_or_create(
        order=payment.order, source_type=source_type,
        defaults={'currency': payment.currency, 'reference': payment.reference})
    return source


def _set_order_status(order, status: str) -> None:
    if order.status != status:
        order.set_status(status)


# ---------------------------------------------------------------------------
# Lookups (ownership enforced here: a foreign object is simply not found)
# ---------------------------------------------------------------------------

def payment_for_owner(user, order_number: str) -> PaypalPayment:
    try:
        return PaypalPayment.objects.select_related('order').get(
            order__number=order_number, order__user=user)
    except PaypalPayment.DoesNotExist:
        raise ApiProblem(404, 'order_not_found', 'No such order.') from None


def payment_for_operator(order_number: str) -> PaypalPayment:
    try:
        return PaypalPayment.objects.select_related('order').get(order__number=order_number)
    except PaypalPayment.DoesNotExist:
        raise ApiProblem(404, 'order_not_found', 'No such order with a PayPal payment.') from None


def active_card(user, payment_method_id: str) -> SavedCard:
    try:
        card_uuid = uuid.UUID(str(payment_method_id))
    except ValueError:
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.') from None
    card = SavedCard.objects.filter(public_id=card_uuid, user=user, date_removed__isnull=True).first()
    if card is None:
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')
    return card


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def place_order(user, items: list[dict], request=None):
    """Place an Oscar order for catalogue items, awaiting payment."""
    currency = gateway.currency()
    quantities: dict[int, int] = {}
    for item in items:
        product_id, quantity = item['productId'], item['quantity']
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    for product_id, quantity in quantities.items():
        if quantity > MAX_LINE_QUANTITY:
            raise ApiProblem(422, 'invalid_quantity',
                             f'Quantity for product {product_id} exceeds {MAX_LINE_QUANTITY}.')

    products = {p.pk: p for p in Product.objects.filter(pk__in=quantities)}
    missing = sorted(set(quantities) - set(products))
    if missing:
        raise ApiProblem(422, 'unknown_product', f'Unknown product id(s): {missing}.')

    strategy = Selector().strategy(request=request, user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in quantities.items():
            product = products[product_id]
            if product.is_parent or not product.is_public:
                raise ApiProblem(422, 'not_purchasable',
                                 f'Product {product_id} cannot be bought directly.')
            info = strategy.fetch_for_product(product)
            if info.price is None or not info.price.exists or info.stockrecord is None:
                raise ApiProblem(422, 'not_purchasable', f'Product {product_id} has no price.')
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ApiProblem(422, 'not_available', f'Product {product_id}: {reason}')
            basket.add_product(product, quantity)
        basket.reset_offer_applications()

        amount = basket.total_incl_tax
        try:
            money.to_paypal(amount, currency)
        except money.AmountError as e:
            raise ApiProblem(422, 'unpayable_total', str(e)) from None
        total = Price(currency=currency, excl_tax=basket.total_excl_tax, incl_tax=amount)
        shipping_method = NoShippingRequired()
        shipping_charge = shipping_method.calculate(basket)
        try:
            order = OrderCreator().place_order(
                basket=basket, total=total, shipping_method=shipping_method,
                shipping_charge=shipping_charge, user=user, request=request)
        except ValueError as e:
            raise ApiProblem(422, 'order_rejected', str(e)) from None
        basket.submit()
        payment = PaypalPayment.objects.create(
            order=order, reference=f'oscar-{uuid.uuid4().hex}', amount=amount, currency=currency)
    logger.info('Order %s placed for user %s: %s %s', order.number, user.pk, amount, currency)
    return order, payment


# ---------------------------------------------------------------------------
# Pay (authorize)
# ---------------------------------------------------------------------------

PAID_STATUSES = (
    PaypalPayment.AUTHORIZED, PaypalPayment.CAPTURING, PaypalPayment.CAPTURE_PENDING,
    PaypalPayment.CAPTURED, PaypalPayment.PARTIALLY_REFUNDED, PaypalPayment.REFUNDED)


def pay(user, order_number: str, *, card: gateway.CardInput | None = None,
        payment_method_id: str | None = None) -> Result:
    payment = payment_for_owner(user, order_number)
    saved = active_card(user, payment_method_id) if payment_method_id else None

    if payment.status in PAID_STATUSES:
        return Result(200, payment)          # already paid: a repeat never authorizes twice
    if payment.status == PaypalPayment.AUTHORIZATION_PENDING:
        return _refresh_pending_authorization(payment)
    if payment.status in (PaypalPayment.VOIDING, PaypalPayment.VOIDED, PaypalPayment.CANCELLED):
        raise ApiProblem(409, 'order_cancelled', 'This order has been cancelled.', payment=payment)

    resend = (
        (payment.status == PaypalPayment.UNKNOWN and payment.unknown_operation == PaypalPayment.OP_AUTHORIZE)
        or (payment.status == PaypalPayment.AUTHORIZING and _is_stale(payment))
    )
    if payment.status == PaypalPayment.AUTHORIZING and not resend:
        raise ApiProblem(409, 'payment_in_progress',
                         'A payment for this order is already being processed.', payment=payment)
    if resend:
        # Same attempt, same PayPal-Request-Id: PayPal returns the original
        # result if the earlier call landed, or processes it now if it did not.
        claimed = _claim(payment, (payment.status,), PaypalPayment.AUTHORIZING)
    elif payment.status in (PaypalPayment.AWAITING_PAYMENT, PaypalPayment.FAILED):
        claimed = _claim(payment, (payment.status,), PaypalPayment.AUTHORIZING,
                         attempt=payment.attempt + 1, saved_card=saved)
    else:
        raise ApiProblem(409, 'not_payable', f'Order cannot be paid in state {payment.status}.',
                         payment=payment)
    if not claimed:
        raise ApiProblem(409, 'payment_in_progress',
                         'A payment for this order is already being processed.')

    request_id = f'{payment.reference}-authorize-{payment.attempt}'
    try:
        paypal_order = gateway.authorize_order(
            request_id=request_id,
            reference=payment.reference,
            invoice_id=f'{payment.reference}-{payment.attempt}',
            order_number=payment.order.number,
            amount=money.to_paypal(payment.amount, payment.currency),
            currency_code=payment.currency,
            card=None if saved else card,
            vault_id=saved.paypal_token_id if saved else None,
        )
    except gateway.CardDataError as e:
        payment.status = PaypalPayment.FAILED
        _set_error(payment, 'invalid_card', str(e))
        payment.save()
        raise ApiProblem(400, 'invalid_card', str(e), payment=payment) from None
    except gateway.ProviderError as e:
        return _authorization_failed(payment, e)
    return _record_authorization(payment, paypal_order)


def _authorization_failed(payment: PaypalPayment, e: gateway.ProviderError) -> Result:
    if e.outcome_unknown:
        payment.status = PaypalPayment.UNKNOWN
        payment.unknown_operation = PaypalPayment.OP_AUTHORIZE
        message = (f'{e.message} The payment may or may not have been authorized; '
                   'repeat the same pay request to find out. It will not be authorized twice.')
        _set_error(payment, 'outcome_unknown', message)
        payment.save()
        raise ApiProblem(e.status_code, 'outcome_unknown', message, payment=payment)
    payment.status = PaypalPayment.FAILED
    _set_error(payment, e.code or 'payment_failed', e.message)
    payment.save()
    raise ApiProblem(e.status_code, e.code or 'payment_failed', e.message, payment=payment)


def _record_authorization(payment: PaypalPayment, paypal_order) -> Result:
    payment.paypal_order_id = gateway.text(paypal_order.id)
    payment.paypal_order_status = gateway.text(paypal_order.status)
    payment.unknown_operation = ''
    card = None
    source = paypal_order.payment_source
    if not isinstance(source, gateway.UnsetType) and not isinstance(source.card, gateway.UnsetType):
        card = source.card
        payment.card_brand = gateway.text(card.brand)
        payment.card_last_digits = gateway.text(card.last_digits)[-4:]
    elif payment.saved_card_id:
        payment.card_brand = payment.saved_card.brand
        payment.card_last_digits = payment.saved_card.last_digits

    order_state = gateway.order_outcome(paypal_order.status)
    if order_state == 'payer_action':
        payment.status = PaypalPayment.FAILED
        message = ('PayPal requires the shopper to complete a card authentication challenge '
                   '(3-D Secure) in a browser, which this API does not support. '
                   'No money was held.')
        _set_error(payment, 'PAYER_ACTION_REQUIRED', message)
        payment.save()
        raise ApiProblem(422, 'PAYER_ACTION_REQUIRED', message, payment=payment)

    authorization = _first_authorization(paypal_order)
    if order_state == 'unknown' or (order_state == 'completed' and authorization is None):
        return _unknown(payment, PaypalPayment.OP_AUTHORIZE,
                        f'PayPal answered with order status {payment.paypal_order_status or "(none)"} '
                        'and no readable authorization.')
    if authorization is None:
        payment.status = PaypalPayment.FAILED
        message = f'PayPal did not authorize the payment (order status {payment.paypal_order_status}).'
        _set_error(payment, 'not_authorized', message)
        payment.save()
        raise ApiProblem(402, 'not_authorized', message, payment=payment)

    _apply_authorization(payment, authorization)
    outcome = gateway.authorization_outcome(authorization.status)
    if outcome == 'done':
        return _authorized(payment, authorization)
    if outcome == 'pending':
        payment.status = PaypalPayment.AUTHORIZATION_PENDING
        _set_error(payment, 'authorization_pending',
                   'PayPal is still reviewing this authorization; repeat the pay request later.')
        payment.save()
        return Result(202, payment)
    if outcome in ('failed', 'voided'):
        payment.status = PaypalPayment.FAILED
        message = 'The card was declined. Try another card.'
        _set_error(payment, 'card_declined', message)
        payment.save()
        raise ApiProblem(402, 'card_declined', message, payment=payment)
    return _unknown(payment, PaypalPayment.OP_AUTHORIZE,
                    f'PayPal returned authorization status {payment.authorization_status}.')


def _first_authorization(paypal_order):
    units = paypal_order.purchase_units
    if isinstance(units, gateway.UnsetType) or not units:
        return None
    payments = units[0].payments
    if isinstance(payments, gateway.UnsetType):
        return None
    authorizations = payments.authorizations
    if isinstance(authorizations, gateway.UnsetType) or not authorizations:
        return None
    if isinstance(authorizations[0].id, gateway.UnsetType):
        return None
    return authorizations[0]


def _apply_authorization(payment: PaypalPayment, authorization) -> None:
    payment.authorization_id = gateway.text(authorization.id)
    payment.authorization_status = gateway.text(authorization.status)
    created = gateway.parse_time(authorization.create_time)
    payment.authorization_created_at = created or payment.authorization_created_at or timezone.now()
    payment.authorization_expires_at = gateway.parse_time(authorization.expiration_time)
    amount = authorization.amount
    if not isinstance(amount, gateway.UnsetType):
        payment.authorized_amount = money.from_paypal(amount.value)


def _authorized(payment: PaypalPayment, authorization) -> Result:
    amount = authorization.amount
    if (isinstance(amount, gateway.UnsetType) or amount.currency_code != payment.currency
            or money.from_paypal(amount.value) != payment.amount):
        # Never keep a hold that differs from the order total.
        logger.error('Authorization %s amount does not match order %s total; voiding',
                     payment.authorization_id, payment.order.number)
        try:
            gateway.void_authorization(
                authorization_id=payment.authorization_id,
                request_id=f'{payment.reference}-void-{payment.authorization_id}')
        except gateway.ProviderError:
            logger.exception('Could not void mismatched authorization %s', payment.authorization_id)
        payment.status = PaypalPayment.FAILED
        message = 'PayPal authorized an amount different from the order total; the hold was released.'
        _set_error(payment, 'amount_mismatch', message)
        payment.save()
        raise ApiProblem(502, 'amount_mismatch', message, payment=payment)

    with transaction.atomic():
        payment.status = PaypalPayment.AUTHORIZED
        _clear_error(payment)
        payment.save()
        source = _oscar_source(payment)
        source.label = f'{payment.card_brand} ****{payment.card_last_digits}'.strip()
        source.allocate(payment.amount, reference=payment.authorization_id,
                        status=payment.authorization_status)
        _set_order_status(payment.order, ORDER_STATUS_PAID)
    logger.info('Order %s authorized: %s', payment.order.number, payment.authorization_id)
    return Result(201, payment)


def _refresh_pending_authorization(payment: PaypalPayment) -> Result:
    try:
        authorization = gateway.get_authorization(payment.authorization_id)
    except gateway.ProviderError as e:
        raise ApiProblem(e.status_code, e.code or 'paypal_unavailable', e.message, payment=payment) from e
    _apply_authorization(payment, authorization)
    outcome = gateway.authorization_outcome(authorization.status)
    if outcome == 'done':
        return _authorized(payment, authorization)
    if outcome == 'pending':
        payment.save()
        return Result(202, payment)
    if outcome in ('failed', 'voided'):
        payment.status = PaypalPayment.FAILED
        _set_error(payment, 'card_declined', 'The authorization was declined. Try another card.')
        payment.save()
        raise ApiProblem(402, 'card_declined', payment.last_error, payment=payment)
    payment.save()
    return Result(202, payment)


def _unknown(payment: PaypalPayment, operation: str, detail: str) -> Result:
    payment.status = PaypalPayment.UNKNOWN
    payment.unknown_operation = operation
    message = f'{detail} Repeat the same request to settle it; it will not be applied twice.'
    _set_error(payment, 'outcome_unknown', message)
    payment.save()
    raise ApiProblem(502, 'outcome_unknown', message, payment=payment)


# ---------------------------------------------------------------------------
# Fulfil (capture)
# ---------------------------------------------------------------------------

def fulfil(order_number: str) -> Result:
    payment = payment_for_operator(order_number)
    if payment.status in (PaypalPayment.CAPTURED, PaypalPayment.PARTIALLY_REFUNDED, PaypalPayment.REFUNDED):
        return Result(200, payment)
    if payment.status == PaypalPayment.CAPTURE_PENDING:
        return _refresh_capture(payment)

    resend = (
        (payment.status == PaypalPayment.UNKNOWN and payment.unknown_operation == PaypalPayment.OP_CAPTURE)
        or (payment.status == PaypalPayment.CAPTURING and _is_stale(payment))
    )
    if payment.status == PaypalPayment.CAPTURING and not resend:
        raise ApiProblem(409, 'fulfilment_in_progress', 'This order is already being fulfilled.',
                         payment=payment)
    if not resend and payment.status != PaypalPayment.AUTHORIZED:
        raise ApiProblem(409, 'not_authorized',
                         f'Only an authorized order can be fulfilled (payment status: {payment.status}).',
                         payment=payment)
    if not _claim(payment, (payment.status,), PaypalPayment.CAPTURING):
        raise ApiProblem(409, 'fulfilment_in_progress', 'This order is already being fulfilled.')

    if not resend:
        _renew_if_stale(payment)

    try:
        captured = gateway.capture_authorization(
            authorization_id=payment.authorization_id,
            request_id=f'{payment.reference}-capture-{payment.authorization_id}',
            amount=money.to_paypal(payment.amount, payment.currency),
            currency_code=payment.currency,
            invoice_id=f'{payment.reference}-{payment.attempt}',
        )
    except gateway.ProviderError as e:
        if e.outcome_unknown:
            payment.status = PaypalPayment.UNKNOWN
            payment.unknown_operation = PaypalPayment.OP_CAPTURE
            message = (f'{e.message} The capture may or may not have happened; repeat the fulfil '
                       'request to settle it. The money will not be taken twice.')
            _set_error(payment, 'outcome_unknown', message)
            payment.save()
            raise ApiProblem(e.status_code, 'outcome_unknown', message, payment=payment) from e
        payment.status = PaypalPayment.AUTHORIZED
        message = _capture_refused_message(payment, e)
        _set_error(payment, e.code or 'capture_refused', message)
        payment.save()
        status = e.status_code if isinstance(e, (gateway.ProviderConfigError, gateway.ProviderUnavailable)) else 409
        raise ApiProblem(status, e.code or 'capture_refused', message, payment=payment) from e
    return _record_capture(payment, captured)


def _renew_if_stale(payment: PaypalPayment) -> None:
    """Reauthorize an authorization past its honor period; refuse one that has expired."""
    now = timezone.now()
    if payment.authorization_expires_at and now >= payment.authorization_expires_at:
        payment.status = PaypalPayment.AUTHORIZED
        message = (f'The payment authorization expired on {payment.authorization_expires_at:%Y-%m-%d %H:%M} UTC '
                   'and can no longer be captured or renewed. Cancel this order to release it, '
                   'and ask the shopper to place and pay for a new order.')
        _set_error(payment, 'AUTHORIZATION_EXPIRED', message)
        payment.save()
        raise ApiProblem(409, 'AUTHORIZATION_EXPIRED', message, payment=payment)

    created = payment.authorization_created_at
    if created is None or now - created <= HONOR_PERIOD or payment.reauthorized_at:
        return
    try:
        renewed = gateway.reauthorize(
            authorization_id=payment.authorization_id,
            request_id=f'{payment.reference}-reauthorize-{payment.authorization_id}',
            amount=money.to_paypal(payment.amount, payment.currency),
            currency_code=payment.currency,
        )
    except gateway.ProviderError as e:
        if e.outcome_unknown:
            payment.status = PaypalPayment.AUTHORIZED
            message = (f'{e.message} Renewing the stale authorization may or may not have succeeded; '
                       'repeat the fulfil request to settle it.')
            _set_error(payment, 'outcome_unknown', message)
            payment.save()
            raise ApiProblem(e.status_code, 'outcome_unknown', message, payment=payment) from e
        # PayPal would not renew it (e.g. not eligible for this payment type).
        # The original authorization may still be capturable, so try that; if
        # the capture is refused too, the operator gets both reasons.
        logger.warning('Reauthorization of %s refused: %s', payment.authorization_id, e.code)
        _set_error(payment, e.code or 'reauthorization_refused',
                   f'PayPal would not renew the stale authorization: {e.message}')
        return
    new_id = gateway.text(renewed.id)
    if not new_id or gateway.authorization_outcome(renewed.status) != 'done':
        payment.status = PaypalPayment.AUTHORIZED
        message = (f'PayPal answered the renewal with status {gateway.text(renewed.status) or "(none)"}; '
                   'the authorization was not renewed. Cancel this order and ask the shopper to pay again.')
        _set_error(payment, 'reauthorization_failed', message)
        payment.save()
        raise ApiProblem(409, 'reauthorization_failed', message, payment=payment)
    logger.info('Authorization %s renewed as %s', payment.authorization_id, new_id)
    payment.original_authorization_id = payment.original_authorization_id or payment.authorization_id
    payment.reauthorized_at = now
    payment.authorization_id = new_id
    payment.authorization_status = gateway.text(renewed.status)
    payment.authorization_created_at = gateway.parse_time(renewed.create_time) or now
    payment.authorization_expires_at = (gateway.parse_time(renewed.expiration_time)
                                        or payment.authorization_expires_at)
    payment.save()


def _capture_refused_message(payment: PaypalPayment, e: gateway.ProviderError) -> str:
    prefix = ''
    if payment.last_error_code and payment.last_error and payment.last_error_code != 'outcome_unknown':
        prefix = payment.last_error + ' '
    reason = f'{e.code}: {e.message}' if e.code else e.message
    return (f'{prefix}PayPal refused to take the payment ({reason}). The money has not been taken. '
            'If the authorization is no longer valid, cancel this order and ask the shopper to pay again.')


def _apply_capture(payment: PaypalPayment, captured) -> None:
    payment.capture_id = gateway.text(captured.id)
    payment.capture_status = gateway.text(captured.status)
    details = captured.status_details
    payment.capture_status_reason = '' if isinstance(details, gateway.UnsetType) else gateway.text(details.reason)
    amount = captured.amount
    if not isinstance(amount, gateway.UnsetType):
        payment.captured_amount = money.from_paypal(amount.value)
    breakdown = captured.seller_receivable_breakdown
    if not isinstance(breakdown, gateway.UnsetType):
        payment.captured_amount = money.from_paypal(breakdown.gross_amount.value)
        if not isinstance(breakdown.paypal_fee, gateway.UnsetType):
            payment.paypal_fee = money.from_paypal(breakdown.paypal_fee.value)
        if not isinstance(breakdown.net_amount, gateway.UnsetType):
            payment.net_amount = money.from_paypal(breakdown.net_amount.value)
    payment.captured_at = gateway.parse_time(captured.create_time) or payment.captured_at or timezone.now()


def _record_capture(payment: PaypalPayment, captured) -> Result:
    if not gateway.text(captured.id):
        return _unknown(payment, PaypalPayment.OP_CAPTURE, 'PayPal answered the capture without a capture id.')
    _apply_capture(payment, captured)
    payment.unknown_operation = ''
    outcome = gateway.capture_outcome(captured.status)
    if outcome == 'done':
        return _captured(payment)
    if outcome == 'pending':
        payment.status = PaypalPayment.CAPTURE_PENDING
        _set_error(payment, 'capture_pending',
                   f'PayPal accepted the capture but it is pending ({payment.capture_status_reason or "no reason given"}). '
                   'Repeat the fulfil request later to refresh it.')
        payment.save()
        return Result(202, payment)
    if outcome == 'failed':
        payment.status = PaypalPayment.AUTHORIZED
        message = f'PayPal declined the capture (status {payment.capture_status}). The money has not been taken.'
        _set_error(payment, 'capture_declined', message)
        payment.save()
        raise ApiProblem(402, 'capture_declined', message, payment=payment)
    return _unknown(payment, PaypalPayment.OP_CAPTURE, f'PayPal returned capture status {payment.capture_status}.')


def _captured(payment: PaypalPayment) -> Result:
    with transaction.atomic():
        payment.status = PaypalPayment.CAPTURED
        _clear_error(payment)
        payment.save()
        source = _oscar_source(payment)
        source.debit(payment.captured_amount, reference=payment.capture_id, status=payment.capture_status)
        order = payment.order
        EventHandler().consume_stock_allocations(order)
        _set_order_status(order, ORDER_STATUS_COMPLETE)
    logger.info('Order %s captured: %s', payment.order.number, payment.capture_id)
    return Result(201, payment)


def _refresh_capture(payment: PaypalPayment) -> Result:
    try:
        captured = gateway.get_capture(payment.capture_id)
    except gateway.ProviderError as e:
        raise ApiProblem(e.status_code, e.code or 'paypal_unavailable', e.message, payment=payment) from e
    return _record_capture(payment, captured)


# ---------------------------------------------------------------------------
# Cancel (void)
# ---------------------------------------------------------------------------

def cancel(order_number: str) -> Result:
    payment = payment_for_operator(order_number)
    if payment.status in (PaypalPayment.VOIDED, PaypalPayment.CANCELLED):
        return Result(200, payment)
    if payment.status in (PaypalPayment.CAPTURED, PaypalPayment.PARTIALLY_REFUNDED, PaypalPayment.REFUNDED,
                          PaypalPayment.CAPTURING, PaypalPayment.CAPTURE_PENDING):
        raise ApiProblem(409, 'already_fulfilled',
                         'This order has been fulfilled and paid; issue a refund instead.', payment=payment)
    if payment.status in (PaypalPayment.AWAITING_PAYMENT, PaypalPayment.FAILED):
        if not _claim(payment, (payment.status,), PaypalPayment.CANCELLED):
            raise ApiProblem(409, 'state_changed', 'The order changed while cancelling; try again.')
        _cancel_oscar_order(payment, voided=False)
        return Result(200, payment)

    resend = (
        (payment.status == PaypalPayment.UNKNOWN and payment.unknown_operation == PaypalPayment.OP_VOID)
        or (payment.status == PaypalPayment.VOIDING and _is_stale(payment))
    )
    if payment.status == PaypalPayment.UNKNOWN and not resend:
        raise ApiProblem(409, 'outcome_unknown',
                         'The last payment operation on this order has an unknown outcome; '
                         'repeat it to settle it before cancelling.', payment=payment)
    if payment.status in (PaypalPayment.AUTHORIZING, PaypalPayment.VOIDING) and not resend:
        raise ApiProblem(409, 'payment_in_progress',
                         'A payment operation on this order is in progress; try again shortly.', payment=payment)
    if not resend and payment.status not in (PaypalPayment.AUTHORIZED, PaypalPayment.AUTHORIZATION_PENDING):
        raise ApiProblem(409, 'not_cancellable', f'Order cannot be cancelled in state {payment.status}.',
                         payment=payment)
    previous = PaypalPayment.AUTHORIZED if resend else payment.status
    if not _claim(payment, (payment.status,), PaypalPayment.VOIDING):
        raise ApiProblem(409, 'payment_in_progress', 'A payment operation on this order is in progress.')

    try:
        voided = gateway.void_authorization(
            authorization_id=payment.authorization_id,
            request_id=f'{payment.reference}-void-{payment.authorization_id}')
    except gateway.ProviderError as e:
        if e.outcome_unknown:
            payment.status = PaypalPayment.UNKNOWN
            payment.unknown_operation = PaypalPayment.OP_VOID
            message = (f'{e.message} The hold may or may not have been released; '
                       'repeat the cancel request to settle it.')
            _set_error(payment, 'outcome_unknown', message)
            payment.save()
            raise ApiProblem(e.status_code, 'outcome_unknown', message, payment=payment) from e
        payment.status = previous
        reason = f'{e.code}: {e.message}' if e.code else e.message
        message = f'PayPal refused to release the held funds ({reason}).'
        _set_error(payment, e.code or 'void_refused', message)
        payment.save()
        raise ApiProblem(409 if isinstance(e, gateway.ProviderRejected) else e.status_code,
                         e.code or 'void_refused', message, payment=payment) from e

    payment.authorization_status = gateway.text(voided.status)
    if gateway.authorization_outcome(voided.status) != 'voided':
        return _unknown(payment, PaypalPayment.OP_VOID,
                        f'PayPal answered the release with status {payment.authorization_status or "(none)"}.')
    payment.unknown_operation = ''
    payment.voided_at = timezone.now()
    with transaction.atomic():
        payment.status = PaypalPayment.VOIDED
        _clear_error(payment)
        payment.save()
        source = _oscar_source(payment)
        source.transactions.create(txn_type='Void', amount=payment.amount,
                                   reference=payment.authorization_id, status=payment.authorization_status)
        _cancel_oscar_order(payment, voided=True)
    logger.info('Order %s cancelled; authorization %s voided', payment.order.number, payment.authorization_id)
    return Result(200, payment)


def _cancel_oscar_order(payment: PaypalPayment, *, voided: bool) -> None:
    order = payment.order
    with transaction.atomic():
        if order.status != ORDER_STATUS_CANCELLED:
            EventHandler().cancel_stock_allocations(order)
            _set_order_status(order, ORDER_STATUS_CANCELLED)
        if not voided:
            _clear_error(payment)
            payment.save()


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

REFUNDABLE_STATUSES = (PaypalPayment.CAPTURED, PaypalPayment.PARTIALLY_REFUNDED)


def refund(user, order_number: str, *, idempotency_key: str, amount: Decimal | None) -> Result:
    payment = payment_for_owner(user, order_number)
    existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
    if existing is None:
        existing, created = _reserve_refund(payment, idempotency_key, amount)
        if created:
            return _send_refund(payment, existing, first=True)

    if amount is not None and amount != existing.amount:
        raise ApiProblem(422, 'idempotency_key_reused',
                         'This idempotency key was already used for a refund of a different amount.',
                         payment=payment, refund=existing)
    if existing.status in (PaypalRefund.COMPLETED, PaypalRefund.FAILED):
        return Result(200, payment, existing)
    if existing.status == PaypalRefund.PENDING and existing.paypal_refund_id:
        return _refresh_refund(payment, existing)
    stale = timezone.now() - existing.date_updated > _stale_after()
    if existing.status == PaypalRefund.SENDING and not stale:
        raise ApiProblem(409, 'refund_in_progress', 'This refund is already being processed.',
                         payment=payment, refund=existing)
    # Unknown (or a sender that never came back): re-send under the same
    # PayPal-Request-Id, so PayPal returns the refund if it landed.
    PaypalRefund.objects.filter(pk=existing.pk).update(status=PaypalRefund.SENDING, date_updated=timezone.now())
    existing.refresh_from_db()
    return _send_refund(payment, existing, first=False)


def _reserve_refund(payment: PaypalPayment, idempotency_key: str,
                    amount: Decimal | None) -> tuple[PaypalRefund, bool]:
    """Create the refund record, checking the amount against what remains refundable."""
    try:
        with transaction.atomic():
            locked = PaypalPayment.objects.select_for_update().get(pk=payment.pk)
            if locked.status not in REFUNDABLE_STATUSES:
                if locked.status == PaypalPayment.REFUNDED:
                    raise ApiProblem(409, 'fully_refunded', 'This order has already been fully refunded.',
                                     payment=locked)
                raise ApiProblem(409, 'not_refundable',
                                 'Only a fulfilled (captured) order can be refunded; '
                                 'an unfulfilled order is cancelled instead.', payment=locked)
            reserved = sum((r.amount for r in locked.refunds.filter(status__in=PaypalRefund.RESERVING_STATUSES)),
                           Decimal('0'))
            remaining = (locked.captured_amount or Decimal('0')) - reserved
            if remaining <= 0:
                raise ApiProblem(409, 'fully_refunded', 'Nothing remains to be refunded on this order.',
                                 payment=locked)
            value = remaining if amount is None else amount
            if value > remaining:
                raise ApiProblem(
                    422, 'refund_exceeds_captured',
                    f'The refund of {_fmt(value, locked.currency)} exceeds the '
                    f'{_fmt(remaining, locked.currency)} {locked.currency} that remains refundable.',
                    payment=locked)
            return PaypalRefund.objects.create(
                payment=locked, idempotency_key=idempotency_key, amount=value,
                currency=locked.currency), True
    except IntegrityError:
        # A concurrent request with the same key won the race.
        return payment.refunds.get(idempotency_key=idempotency_key), False


def _send_refund(payment: PaypalPayment, record: PaypalRefund, *, first: bool) -> Result:
    try:
        response = gateway.refund_capture(
            capture_id=payment.capture_id,
            request_id=f'{payment.reference}-refund-{record.public_id}',
            amount=money.to_paypal(record.amount, record.currency),
            currency_code=record.currency,
        )
    except gateway.ProviderError as e:
        payment.refresh_from_db()
        if e.outcome_unknown:
            record.status = PaypalRefund.UNKNOWN
            record.last_error_code = 'outcome_unknown'
            record.last_error = (f'{e.message} The refund may or may not have been made; repeat the request '
                                 'with the same Idempotency-Key to settle it. It will not be refunded twice.')
            record.save()
            raise ApiProblem(e.status_code, 'outcome_unknown', record.last_error,
                             payment=payment, refund=record) from e
        record.status = PaypalRefund.FAILED
        record.last_error_code = (e.code or 'refund_refused')[:64]
        record.last_error = f'PayPal refused the refund: {e.message}'
        record.save()
        raise ApiProblem(e.status_code, e.code or 'refund_refused', record.last_error,
                         payment=payment, refund=record) from e
    return _record_refund(payment, record, response, first=first)


def _record_refund(payment: PaypalPayment, record: PaypalRefund, response, *, first: bool) -> Result:
    record.paypal_refund_id = gateway.text(response.id)
    record.paypal_status = gateway.text(response.status)
    if not record.paypal_refund_id:
        record.status = PaypalRefund.UNKNOWN
        record.last_error_code = 'outcome_unknown'
        record.last_error = 'PayPal answered without a refund id; repeat the request with the same Idempotency-Key.'
        record.save()
        payment.refresh_from_db()
        raise ApiProblem(502, 'outcome_unknown', record.last_error, payment=payment, refund=record)

    outcome = gateway.refund_outcome(response.status)
    record.last_error_code = ''
    record.last_error = ''
    if outcome == 'done':
        with transaction.atomic():
            was_completed = PaypalRefund.objects.filter(pk=record.pk, status=PaypalRefund.COMPLETED).exists()
            record.status = PaypalRefund.COMPLETED
            record.save()
            locked = PaypalPayment.objects.select_for_update().get(pk=payment.pk)
            if not was_completed:
                _oscar_source(locked).refund(record.amount, reference=record.paypal_refund_id,
                                             status=record.paypal_status)
            refunded = sum((r.amount for r in locked.refunds.filter(status=PaypalRefund.COMPLETED)), Decimal('0'))
            locked.status = (PaypalPayment.REFUNDED if refunded >= (locked.captured_amount or Decimal('0'))
                             else PaypalPayment.PARTIALLY_REFUNDED)
            locked.save()
        payment.refresh_from_db()
        logger.info('Order %s refunded %s: %s', payment.order.number, record.amount, record.paypal_refund_id)
        return Result(201 if first else 200, payment, record)
    if outcome == 'pending':
        record.status = PaypalRefund.PENDING
        record.save()
        payment.refresh_from_db()
        return Result(202, payment, record)
    if outcome == 'failed':
        record.status = PaypalRefund.FAILED
        record.last_error_code = 'refund_failed'
        record.last_error = f'PayPal reports the refund as {record.paypal_status}.'
        record.save()
        payment.refresh_from_db()
        raise ApiProblem(402, 'refund_failed', record.last_error, payment=payment, refund=record)
    record.status = PaypalRefund.UNKNOWN
    record.last_error_code = 'outcome_unknown'
    record.last_error = (f'PayPal returned refund status {record.paypal_status}; '
                         'repeat the request with the same Idempotency-Key to settle it.')
    record.save()
    payment.refresh_from_db()
    raise ApiProblem(502, 'outcome_unknown', record.last_error, payment=payment, refund=record)


def _refresh_refund(payment: PaypalPayment, record: PaypalRefund) -> Result:
    try:
        response = gateway.get_refund(record.paypal_refund_id)
    except gateway.ProviderError as e:
        raise ApiProblem(e.status_code, e.code or 'paypal_unavailable', e.message,
                         payment=payment, refund=record) from e
    return _record_refund(payment, record, response, first=False)


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

def save_card(user, card: gateway.CardInput) -> SavedCard:
    customer = PaypalCustomer.objects.filter(user=user).first()
    try:
        token = gateway.vault_card(
            request_id=f'vault-{uuid.uuid4().hex}',
            card=card,
            customer_id=customer.paypal_customer_id if customer else None,
        )
    except gateway.CardDataError as e:
        raise ApiProblem(400, 'invalid_card', str(e)) from None
    except gateway.ProviderError as e:
        code = 'outcome_unknown' if e.outcome_unknown else (e.code or 'card_not_saved')
        message = e.message + (' The card may or may not have been saved; list your saved cards before '
                               'trying again.' if e.outcome_unknown else '')
        raise ApiProblem(e.status_code, code, message) from e

    token_id = gateway.text(token.id)
    customer_id = gateway.text(token.customer.id) if not isinstance(token.customer, gateway.UnsetType) else ''
    if not token_id or not customer_id:
        raise ApiProblem(502, 'outcome_unknown', 'PayPal saved the card but did not return its identifiers.')
    brand = last_digits = expiry = ''
    source = token.payment_source
    if not isinstance(source, gateway.UnsetType) and not isinstance(source.card, gateway.UnsetType):
        brand = gateway.text(source.card.brand)
        last_digits = gateway.text(source.card.last_digits)[-4:]
        expiry = gateway.text(source.card.expiry)
    with transaction.atomic():
        if customer is None:
            PaypalCustomer.objects.get_or_create(user=user, defaults={'paypal_customer_id': customer_id})
        saved = SavedCard.objects.create(
            user=user, paypal_token_id=token_id, paypal_customer_id=customer_id,
            brand=brand, last_digits=last_digits or card.number[-4:], expiry=expiry or card.expiry)
    logger.info('User %s saved card %s (%s ****%s)', user.pk, saved.public_id, saved.brand, saved.last_digits)
    return saved


def list_cards(user):
    return SavedCard.objects.filter(user=user, date_removed__isnull=True)


def remove_card(user, payment_method_id: str) -> SavedCard:
    """Remove a saved card. It stops being listed or usable immediately."""
    try:
        card_uuid = uuid.UUID(str(payment_method_id))
    except ValueError:
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.') from None
    card = SavedCard.objects.filter(public_id=card_uuid, user=user).filter(
        Q(date_removed__isnull=True) | Q(paypal_delete_pending=True)).first()
    if card is None:
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')
    if card.date_removed is None:
        card.date_removed = timezone.now()
        card.paypal_delete_pending = True
        card.save()
    try:
        gateway.delete_vaulted_card(card.paypal_token_id)
    except gateway.ProviderError as e:
        logger.warning('PayPal deletion of saved card %s pending: %s', card.public_id, e.message)
        return card
    card.paypal_delete_pending = False
    card.save()
    logger.info('User %s removed saved card %s', user.pk, card.public_id)
    return card


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile(start: datetime, end: datetime) -> dict:
    """Line up PayPal's transactions for [start, end] against this app's payments."""
    now = timezone.now()
    end = min(end, now)
    if start >= end:
        raise ApiProblem(400, 'invalid_range', '"from" must be earlier than "to" (and not in the future).')
    if end - start > RECONCILIATION_MAX_RANGE:
        raise ApiProblem(400, 'range_too_large', 'The range may cover at most 366 days.')

    transactions: dict[tuple[str, str], object] = {}
    last_refreshed: datetime | None = None
    pages_fetched = 0
    window_start = start
    while window_start < end:
        window_end = min(window_start + RECONCILIATION_WINDOW, end)
        page, total_pages = 1, 1
        while page <= total_pages:
            try:
                response = gateway.search_transactions(start=window_start, end=window_end, page=page,
                                                       page_size=RECONCILIATION_PAGE_SIZE)
            except gateway.ProviderError as e:
                raise ApiProblem(e.status_code, e.code or 'paypal_unavailable',
                                 f'Could not read PayPal transactions: {e.message}') from e
            pages_fetched += 1
            total_pages = response.total_pages if isinstance(response.total_pages, int) else page
            refreshed = gateway.parse_time(response.last_refreshed_datetime)
            if refreshed and (last_refreshed is None or refreshed < last_refreshed):
                last_refreshed = refreshed
            details = response.transaction_details
            for detail in ([] if isinstance(details, gateway.UnsetType) else details):
                info = detail.transaction_info
                if isinstance(info, gateway.UnsetType):
                    continue
                key = (gateway.text(info.transaction_id), gateway.text(info.transaction_event_code))
                transactions[key] = info
            page += 1
        window_start = window_end

    # This app's side: every payment with PayPal activity in the range.
    payments = list(PaypalPayment.objects.select_related('order').prefetch_related('refunds').filter(
        Q(authorization_created_at__range=(start, end)) | Q(captured_at__range=(start, end))
        | Q(voided_at__range=(start, end)) | Q(refunds__date_created__range=(start, end))
        | Q(date_created__range=(start, end))
    ).distinct())
    by_reference = {p.reference: p for p in payments}
    by_paypal_id: dict[str, tuple[PaypalPayment, str, object]] = {}
    for p in payments:
        for auth_id in (p.authorization_id, p.original_authorization_id):
            if auth_id:
                by_paypal_id[auth_id] = (p, 'authorization', None)
        if p.capture_id:
            by_paypal_id[p.capture_id] = (p, 'capture', None)
        for r in p.refunds.all():
            if r.paypal_refund_id:
                by_paypal_id[r.paypal_refund_id] = (p, 'refund', r)
    # Payments outside the range can still own a PayPal transaction inside it.
    unresolved_ids = {tid for tid, _ in transactions if tid and tid not in by_paypal_id}
    if unresolved_ids:
        for p in PaypalPayment.objects.select_related('order').filter(
                Q(authorization_id__in=unresolved_ids) | Q(original_authorization_id__in=unresolved_ids)
                | Q(capture_id__in=unresolved_ids)):
            for auth_id in (p.authorization_id, p.original_authorization_id):
                if auth_id:
                    by_paypal_id.setdefault(auth_id, (p, 'authorization', None))
            if p.capture_id:
                by_paypal_id.setdefault(p.capture_id, (p, 'capture', None))
        for r in PaypalRefund.objects.select_related('payment__order').filter(paypal_refund_id__in=unresolved_ids):
            by_paypal_id.setdefault(r.paypal_refund_id, (r.payment, 'refund', r))

    matched, paypal_only, mismatches = [], [], []
    seen_ids = set()
    for (tid, event_code), info in sorted(transactions.items(), key=lambda kv: gateway.text(
            kv[1].transaction_initiation_date)):
        amount_money = info.transaction_amount
        amount = None if isinstance(amount_money, gateway.UnsetType) else money.from_paypal(amount_money.value)
        currency = '' if isinstance(amount_money, gateway.UnsetType) else amount_money.currency_code
        fee_money = info.fee_amount
        row = {
            'transactionId': tid,
            'eventCode': event_code,
            'status': gateway.text(info.transaction_status),
            'amount': None if amount is None else str(amount),
            'currency': currency,
            'fee': None if isinstance(fee_money, gateway.UnsetType) else fee_money.value,
            'date': gateway.text(info.transaction_initiation_date),
            'invoiceId': gateway.text(info.invoice_id),
            'customField': gateway.text(info.custom_field),
        }
        hit = by_paypal_id.get(tid)
        if hit is None:
            ref = row['customField'] or row['invoiceId'].rsplit('-', 1)[0]
            owner = by_reference.get(ref)
            if owner is None and ref:
                owner = PaypalPayment.objects.select_related('order').filter(reference=ref).first()
            hit = (owner, 'reference', None) if owner else None
        if hit is None:
            paypal_only.append(row)
            continue
        p, kind, refund_record = hit
        seen_ids.add(tid)
        row.update({'orderId': str(p.order.number), 'matchedAs': kind, 'paymentStatus': p.status})
        matched.append(row)
        expected = None
        if kind == 'capture':
            expected = p.captured_amount
        elif kind == 'refund' and refund_record is not None:
            expected = refund_record.amount
        if expected is not None and amount is not None and abs(amount) != expected:
            mismatches.append({**row, 'expectedAmount': str(expected)})

    # References PayPal reported, via custom_id/invoice_id, on any transaction.
    reported_references = set()
    for info in transactions.values():
        reported_references.add(gateway.text(info.custom_field))
        reported_references.add(gateway.text(info.invoice_id).rsplit('-', 1)[0])

    app_only = []
    for p in payments:
        expected_records = []
        if p.authorization_id and p.authorization_created_at and start <= p.authorization_created_at <= end:
            expected_records.append(('authorization', p.authorization_id, p.authorized_amount,
                                     p.authorization_created_at))
        if p.capture_id and p.captured_at and start <= p.captured_at <= end and p.status in (
                PaypalPayment.CAPTURED, PaypalPayment.PARTIALLY_REFUNDED, PaypalPayment.REFUNDED):
            expected_records.append(('capture', p.capture_id, p.captured_amount, p.captured_at))
        for r in p.refunds.all():
            if r.paypal_refund_id and r.status == PaypalRefund.COMPLETED and start <= r.date_created <= end:
                expected_records.append(('refund', r.paypal_refund_id, r.amount, r.date_created))
        for kind, paypal_id, amount, when in expected_records:
            if paypal_id in seen_ids:
                continue
            if kind == 'authorization' and (p.capture_id in seen_ids or p.reference in reported_references):
                # PayPal reports the hold under the capture or under our reference.
                continue
            app_only.append({
                'orderId': str(p.order.number),
                'kind': kind,
                'paypalId': paypal_id,
                'amount': _fmt(amount, p.currency),
                'currency': p.currency,
                'date': when.isoformat(),
                'paymentStatus': p.status,
                'notYetReported': bool(last_refreshed is None or when > last_refreshed),
            })

    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'paypalDataRefreshedAt': last_refreshed.isoformat() if last_refreshed else None,
        'pagesFetched': pages_fetched,
        'summary': {
            'paypalTransactions': len(transactions),
            'matched': len(matched),
            'paypalOnly': len(paypal_only),
            'appOnly': len(app_only),
            'amountMismatches': len(mismatches),
        },
        'matched': matched,
        'paypalOnly': paypal_only,
        'appOnly': app_only,
        'amountMismatches': mismatches,
        'note': ('PayPal transaction reporting lags live activity by up to about three hours; '
                 'app records newer than paypalDataRefreshedAt are flagged notYetReported.'),
    }
