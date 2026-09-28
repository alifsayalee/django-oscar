"""Placing Oscar orders from catalogue items, and describing them to callers."""
from decimal import Decimal
from typing import Any

from django.db import transaction
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price

from . import money
from .errors import ApiProblem
from .models import PayPalPayment, ProviderWrite
from .paypal_client import currency as configured_currency

Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
Order = get_model('order', 'Order')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
Selector = get_class('partner.strategy', 'Selector')

MAX_LINES = 50
MAX_QUANTITY = 100


def _parse_items(items: object) -> list[tuple[int, int]]:
    if not isinstance(items, list) or not items:
        raise ApiProblem(422, 'invalid_items', '"items" must be a non-empty list.')
    if len(items) > MAX_LINES:
        raise ApiProblem(422, 'invalid_items', 'At most %d items per order.' % MAX_LINES)
    parsed = []
    for item in items:
        if not isinstance(item, dict):
            raise ApiProblem(422, 'invalid_items', 'Each item needs "productId" and "quantity".')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise ApiProblem(422, 'invalid_items', '"productId" must be an integer catalogue id.')
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise ApiProblem(422, 'invalid_items',
                             '"quantity" must be an integer from 1 to %d.' % MAX_QUANTITY)
        parsed.append((product_id, quantity))
    return parsed


def place_order(user, items: object) -> Any:
    """Create an Oscar order awaiting payment, priced from the catalogue."""
    lines = _parse_items(items)
    currency = configured_currency()
    strategy = Selector().strategy(user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in lines:
            try:
                product = Product.objects.get(pk=product_id, is_public=True)
            except Product.DoesNotExist:
                raise ApiProblem(422, 'unknown_product', 'Product %d does not exist.' % product_id) from None
            if product.is_parent:
                raise ApiProblem(422, 'not_purchasable',
                                 'Product %d is a parent product; order one of its variants.' % product_id)
            info = strategy.fetch_for_product(product)
            already = basket.product_quantity(product)
            permitted, reason = info.availability.is_purchase_permitted(already + quantity)
            if not permitted or info.price.excl_tax is None:
                raise ApiProblem(422, 'not_purchasable', 'Product %d cannot be bought: %s' % (
                    product_id, reason or 'it has no price'))
            basket.add_product(product, quantity)

        shipping_method = NoShippingRequired()
        shipping_charge = shipping_method.calculate(basket)
        basket_total = OrderTotalCalculator().calculate(basket, shipping_charge)
        # Amounts are the catalogue prices; the currency is the configured one.
        amount = basket_total.incl_tax if basket_total.is_tax_known else basket_total.excl_tax
        if not money.is_representable(amount, currency):
            raise ApiProblem(422, 'amount_not_representable',
                             'The order total %s cannot be charged exactly in %s.' % (amount, currency))
        total = Price(currency=currency, excl_tax=basket_total.excl_tax,
                      incl_tax=basket_total.incl_tax if basket_total.is_tax_known else None)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user)
        basket.submit()
        PayPalPayment.objects.create(order=order, currency=currency, amount=amount)
    return order


def order_for(user, number: str, *, staff: bool = False) -> Any:
    """The caller's order (any order for staff); 404 otherwise, never 403,
    so one shopper cannot learn that another's order exists."""
    orders = Order.objects.select_related('paypal_payment')
    if not staff:
        orders = orders.filter(user=user)
    try:
        order = orders.get(number=number)
        order.paypal_payment  # noqa: B018 -- orders placed outside this API have none
    except (Order.DoesNotExist, PayPalPayment.DoesNotExist):
        raise ApiProblem(404, 'order_not_found', 'No such order.') from None
    return order


def _amount(value: Decimal | None, currency: str) -> str | None:
    if value is None:
        return None
    return str(value.quantize(money.quantum(currency)))


def refundable_amount(payment: PayPalPayment) -> Decimal:
    """Captured minus every refund that is done, pending or not yet settled."""
    if payment.captured_amount is None:
        return Decimal('0')
    reserved = sum(
        (w.amount or Decimal('0')) for w in ProviderWrite.objects.filter(
            order_id=payment.order_id, kind=ProviderWrite.REFUND,
        ).exclude(outcome=ProviderWrite.FAILED))
    return max(payment.captured_amount - reserved, Decimal('0'))


def refund_body(write: ProviderWrite) -> dict[str, Any]:
    return {
        'refundId': str(write.pk),
        'paypalRefundId': write.provider_id or None,
        'amount': _amount(write.amount, write.currency),
        'currency': write.currency,
        'outcome': write.outcome,
        'paypalStatus': write.provider_status or None,
        'createdAt': write.claimed_at.isoformat(),
    }


def payment_body(payment: PayPalPayment) -> dict[str, Any]:
    c = payment.currency
    refunds = ProviderWrite.objects.filter(order_id=payment.order_id, kind=ProviderWrite.REFUND)
    return {
        'state': payment.state,
        'amount': _amount(payment.amount, c),
        'currency': c,
        'card': payment.card_label or None,
        'paymentMethodId': payment.payment_method_id,
        'paypalOrderId': payment.paypal_order_id or None,
        'authorization': {
            'id': payment.authorization_id,
            'status': payment.authorization_status,
            'createdAt': payment.authorization_created_at.isoformat() if payment.authorization_created_at else None,
            'expiresAt': payment.authorization_expires_at.isoformat() if payment.authorization_expires_at else None,
            'reauthorized': payment.reauthorized,
        } if payment.authorization_id else None,
        'capture': {
            'id': payment.capture_id,
            'status': payment.capture_status,
            'amount': _amount(payment.captured_amount, c),
            'paypalFee': _amount(payment.paypal_fee, c),
            'netAmount': _amount(payment.net_amount, c),
            'capturedAt': payment.captured_at.isoformat() if payment.captured_at else None,
        } if payment.capture_id else None,
        'refunds': [refund_body(w) for w in refunds],
        'refundedAmount': _amount(payment.refunded_amount, c),
        'refundableAmount': _amount(refundable_amount(payment), c),
        'lastError': payment.last_error or None,
    }


def order_body(order) -> dict[str, Any]:
    payment = order.paypal_payment
    return {
        'orderId': order.number,
        'status': order.status,
        'placedAt': order.date_placed.isoformat(),
        'currency': order.currency,
        'total': _amount(payment.amount, payment.currency),
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'unitPrice': _amount(line.unit_price_incl_tax if line.unit_price_incl_tax is not None
                                 else line.unit_price_excl_tax, payment.currency),
            'lineTotal': _amount(line.line_price_incl_tax, payment.currency),
        } for line in order.lines.all()],
        'payment': payment_body(payment),
    }
