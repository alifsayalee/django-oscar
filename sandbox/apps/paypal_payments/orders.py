"""
Placing an Oscar order from catalogue ids and quantities, through Oscar's own
basket, pricing strategy, offers and OrderCreator - the same machinery the
storefront checkout uses - so the order and its lines are ordinary Oscar
records.
"""
from decimal import Decimal

from django.db import transaction
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price

from . import money
from .errors import PaymentAPIError
from .models import PayPalPayment

Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Selector = get_class('partner.strategy', 'Selector')
Applicator = get_class('offer.applicator', 'Applicator')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')

MAX_LINES = 50
MAX_QUANTITY = 100


def parse_lines(payload: object) -> list[tuple[int, int]]:
    items = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise PaymentAPIError(400, 'invalid_request', 'items must be a non-empty list.')
    if len(items) > MAX_LINES:
        raise PaymentAPIError(400, 'invalid_request', 'At most %d items per order.' % MAX_LINES)
    lines: dict[int, int] = {}
    for item in items:
        product_id = item.get('productId') if isinstance(item, dict) else None
        quantity = item.get('quantity', 1) if isinstance(item, dict) else None
        if (not isinstance(product_id, int) or isinstance(product_id, bool)
                or not isinstance(quantity, int) or isinstance(quantity, bool)
                or not 1 <= quantity <= MAX_QUANTITY):
            raise PaymentAPIError(
                400, 'invalid_request',
                'Each item needs an integer productId and a quantity from 1 to %d.' % MAX_QUANTITY)
        lines[product_id] = lines.get(product_id, 0) + quantity
    return list(lines.items())


def place_order(user, payload, request=None):
    code = money.currency()
    lines = parse_lines(payload)
    products = Product.objects.in_bulk([pid for pid, _ in lines])
    strategy = Selector().strategy(request=request, user=user)

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in lines:
            product = products.get(product_id)
            if product is None or not product.is_public:
                raise PaymentAPIError(400, 'unknown_product', 'No catalogue item %s.' % product_id)
            info = strategy.fetch_for_product(product)
            allowed, reason = info.availability.is_purchase_permitted(quantity)
            if not allowed or not info.price.exists:
                raise PaymentAPIError(
                    409, 'not_purchasable',
                    'Catalogue item %s cannot be bought: %s' % (product_id, reason or 'no price'))
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, request)

        shipping_method = NoShippingRequired()
        shipping_charge = shipping_method.calculate(basket)
        basket_total = OrderTotalCalculator().calculate(basket, shipping_charge)
        # Amounts come from catalogue prices; the charging currency comes from
        # configuration (PAYPAL_CURRENCY), so the order is recorded in it.
        amount = Decimal(basket_total.incl_tax)
        if amount <= 0 or not money.representable(amount, code):
            raise PaymentAPIError(
                409, 'unpayable_total', 'The order total %s cannot be charged in %s.' % (amount, code))
        total = Price(currency=code, excl_tax=basket_total.excl_tax, incl_tax=basket_total.incl_tax)

        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user, request=request)
        basket.submit()

        source_type, _ = SourceType.objects.get_or_create(name='PayPal')
        source = Source.objects.create(
            order=order, source_type=source_type, currency=code, label='PayPal card')
        PayPalPayment.objects.create(order=order, source=source, currency=code, amount=amount)
    return order
