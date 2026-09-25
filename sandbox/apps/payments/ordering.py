"""
Placing an order through Oscar's own checkout machinery: a basket priced by
the partner strategy, offers applied, then ``OrderCreator.place_order``.
"""
from django.conf import settings
from django.db import transaction
from oscar.core import prices
from oscar.core.loading import get_class, get_model

from .errors import ApiProblem
from .models import OrderPayment

Applicator = get_class('offer.applicator', 'Applicator')
Basket = get_model('basket', 'Basket')
Country = get_model('address', 'Country')
Free = get_class('shipping.methods', 'Free')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Product = get_model('catalogue', 'Product')
Selector = get_class('partner.strategy', 'Selector')
ShippingAddress = get_model('order', 'ShippingAddress')

AWAITING_PAYMENT = 'Awaiting payment'
PAYMENT_AUTHORISED = 'Payment authorised'
COMPLETE = 'Complete'
CANCELLED = 'Cancelled'

MAX_LINES = 50
MAX_QUANTITY = 100


def parse_lines(raw) -> list[tuple[int, int]]:
    if not isinstance(raw, list) or not raw:
        raise ApiProblem(400, 'invalid_request', '"lines" must be a non-empty list.')
    if len(raw) > MAX_LINES:
        raise ApiProblem(400, 'invalid_request', 'An order may have at most %d lines.' % MAX_LINES)
    lines = []
    for item in raw:
        if not isinstance(item, dict):
            raise ApiProblem(400, 'invalid_request', 'Each line needs "productId" and "quantity".')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if (not isinstance(product_id, int) or isinstance(product_id, bool)
                or not isinstance(quantity, int) or isinstance(quantity, bool)):
            raise ApiProblem(400, 'invalid_request', '"productId" and "quantity" must be integers.')
        if not 1 <= quantity <= MAX_QUANTITY:
            raise ApiProblem(400, 'invalid_request',
                             'Quantity must be between 1 and %d.' % MAX_QUANTITY)
        lines.append((product_id, quantity))
    return lines


def build_shipping_address(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ApiProblem(400, 'invalid_request', '"shippingAddress" must be an object.')
    required = ('firstName', 'lastName', 'line1', 'city', 'postcode', 'countryCode')
    missing = [k for k in required if not str(raw.get(k) or '').strip()]
    if missing:
        raise ApiProblem(400, 'invalid_request',
                         'shippingAddress is missing: %s.' % ', '.join(missing))
    try:
        country = Country.objects.get(iso_3166_1_a2=str(raw['countryCode']).upper())
    except Country.DoesNotExist:
        raise ApiProblem(400, 'invalid_request', 'Unknown countryCode.') from None
    return ShippingAddress(
        first_name=str(raw['firstName'])[:255], last_name=str(raw['lastName'])[:255],
        line1=str(raw['line1'])[:255], line2=str(raw.get('line2') or '')[:255],
        line4=str(raw['city'])[:255], state=str(raw.get('state') or '')[:255],
        postcode=str(raw['postcode'])[:64], country=country,
        phone_number=str(raw.get('phoneNumber') or '') or None)


def place_order(user, raw_lines, raw_address=None, request=None):
    """Create an Oscar order awaiting payment, priced from the catalogue."""
    lines = parse_lines(raw_lines)
    shipping_address = build_shipping_address(raw_address)
    currency = settings.PAYPAL_CURRENCY

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for product_id, quantity in lines:
            product = Product.objects.filter(pk=product_id, is_public=True).first()
            if product is None:
                raise ApiProblem(404, 'product_not_found', 'Product %s does not exist.' % product_id)
            if product.structure == Product.PARENT:
                raise ApiProblem(422, 'product_not_purchasable',
                                 'Product %s cannot be bought directly.' % product_id)
            info = basket.strategy.fetch_for_product(product)
            if info.stockrecord is None or not info.price.exists:
                raise ApiProblem(422, 'product_not_purchasable',
                                 'Product %s has no price.' % product_id)
            already = basket.product_quantity(product)
            allowed, reason = info.availability.is_purchase_permitted(already + quantity)
            if not allowed:
                raise ApiProblem(422, 'product_not_available', str(reason))
            basket.add_product(product, quantity)

        Applicator().apply(basket, user, request)
        shipping_method = Free() if basket.is_shipping_required() else NoShippingRequired()
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        if total.incl_tax is None:
            raise ApiProblem(422, 'tax_unknown', 'The order total could not be calculated.')
        if total.incl_tax <= 0:
            raise ApiProblem(422, 'zero_total', 'The order total must be greater than zero.')
        # The amount comes from catalogue prices; the currency from configuration.
        charged = prices.Price(currency=currency, excl_tax=total.excl_tax, incl_tax=total.incl_tax)
        if shipping_address is not None:
            shipping_address.save()
        order = OrderCreator().place_order(
            basket=basket, total=charged, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user, shipping_address=shipping_address,
            status=AWAITING_PAYMENT, request=request)
        basket.submit()
        OrderPayment.objects.create(order=order, currency=currency, amount=order.total_incl_tax)
    return order
