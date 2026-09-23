"""Order creation and money bookkeeping on top of Oscar's own models.

Orders are built from catalogue items with an in-memory Oscar basket and placed
through Oscar's ``OrderCreator`` — the same ``order.Order``/``order.Line`` models
the storefront uses. Money movement is recorded against Oscar's
``payment.Source``/``payment.Transaction`` (allocate on authorize, debit on
capture, refund on refund), so the order carries real payment state.

The numeric amount charged comes from the catalogue prices (the order total);
the currency charged comes from configuration (``PAYPAL_CURRENCY``), per the task.
"""

from decimal import Decimal

from django.conf import settings
from oscar.core import prices
from oscar.core.loading import get_class, get_model

Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
Order = get_model('order', 'Order')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')

Selector = get_class('partner.strategy', 'Selector')
Free = get_class('shipping.methods', 'Free')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')

SOURCE_TYPE_NAME = 'PayPal'


class OrderError(Exception):
    """A problem building or placing an order (bad items, empty basket, ...)."""

    def __init__(self, message, http_status=400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def build_basket(user, items):
    """Build and persist an Oscar basket from ``[{id, quantity}, ...]``."""
    if not items:
        raise OrderError('No items supplied.')
    basket = Basket()
    basket.strategy = Selector().strategy(user=user)
    for item in items:
        try:
            product_id = int(item['id'])
            quantity = int(item.get('quantity', 1))
        except (KeyError, TypeError, ValueError):
            raise OrderError('Each item needs an integer "id" and "quantity".')
        if quantity < 1:
            raise OrderError('Item quantity must be at least 1.')
        try:
            product = Product.objects.get(id=product_id)
        except Product.DoesNotExist:
            raise OrderError('Product %s does not exist.' % product_id, http_status=404)
        try:
            basket.add_product(product, quantity=quantity)
        except ValueError as exc:
            # No price or no stock for this product under the current strategy.
            raise OrderError(
                'Product %s cannot be ordered: %s' % (product_id, exc))
    if basket.is_empty:
        raise OrderError('The basket is empty.')
    return basket


def place_order(user, items, status=None):
    """Create an order for ``user`` from ``items``. Returns the Oscar Order."""
    basket = build_basket(user, items)
    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    total = prices.Price(
        currency=basket.currency,
        excl_tax=basket.total_excl_tax + shipping_charge.excl_tax,
        incl_tax=basket.total_incl_tax + shipping_charge.incl_tax,
    )
    order_number = OrderNumberGenerator().order_number(basket)
    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        user=user,
        order_number=order_number,
        status=status or settings.OSCAR_INITIAL_ORDER_STATUS,
    )
    basket.submit()
    return order


def get_source(order):
    """Return the PayPal payment Source for an order, or None."""
    return order.sources.filter(source_type__name=SOURCE_TYPE_NAME).first()


def _source_type():
    source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
    return source_type


def record_authorization(order, amount, currency, reference):
    """Record a held (allocated) amount on an Oscar Source."""
    source = get_source(order)
    if source is None:
        source = Source.objects.create(
            order=order,
            source_type=_source_type(),
            currency=currency,
            reference=reference,
            label='PayPal',
        )
    else:
        source.reference = reference
        source.save()
    # allocate() records the AUTHORISE transaction and updates amount_allocated.
    source.allocate(Decimal(amount), reference=reference, status='authorized')
    return source


def record_capture(order, amount, reference):
    """Record captured (debited) funds on the Oscar Source."""
    source = get_source(order)
    if source is None:
        return None
    source.debit(Decimal(amount), reference=reference, status='captured')
    return source


def record_refund(order, amount, reference):
    """Record a refund on the Oscar Source."""
    source = get_source(order)
    if source is None:
        return None
    source.refund(Decimal(amount), reference=reference, status='refunded')
    return source
