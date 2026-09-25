"""
Order operations built on Oscar's own models and services: a basket priced by
the partner strategy, ``OrderCreator`` to place it, a shipping event for
dispatch and the sandbox's status pipeline for dispatch and cancellation.
"""
from typing import Any

from django.db import transaction
from oscar.apps.order.exceptions import InvalidOrderStatus, InvalidShippingEvent
from oscar.core.loading import get_class, get_model

from .errors import Conflict, InvalidRequest, NotFound

Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
Order = get_model('order', 'Order')
ShippingEventType = get_model('order', 'ShippingEventType')
Selector = get_class('partner.strategy', 'Selector')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
EventHandler = get_class('order.processing', 'EventHandler')
FreeShipping = get_class('shipping.methods', 'Free')

# The sandbox pipeline (settings.OSCAR_ORDER_STATUS_PIPELINE): Pending -> Being processed
# -> Complete, and Cancelled reachable from both Pending and Being processed. A dispatched
# order is 'Being processed' until an operator completes it, so it can still be cancelled.
STATUS_DISPATCHED = 'Being processed'
STATUS_CANCELLED = 'Cancelled'
DISPATCH_EVENT_CODE = 'dispatched'
MAX_LINES = 50
MAX_QUANTITY = 99


def parse_items(payload: Any) -> list[tuple[int, int]]:
    items = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise InvalidRequest('"items" must be a non-empty list of {"productId", "quantity"}.')
    if len(items) > MAX_LINES:
        raise InvalidRequest('Too many items (maximum %d).' % MAX_LINES)
    parsed: list[tuple[int, int]] = []
    for item in items:
        if not isinstance(item, dict):
            raise InvalidRequest('Each item must be an object.')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise InvalidRequest('"productId" must be an integer catalogue item id.')
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise InvalidRequest('"quantity" must be an integer between 1 and %d.' % MAX_QUANTITY)
        parsed.append((product_id, quantity))
    return parsed


def place_order(user: Any, items: list[tuple[int, int]]) -> Any:
    """Price a basket with the user's strategy and place it through Oscar's OrderCreator."""
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(user=user)
        for product_id, quantity in items:
            product = Product.objects.filter(pk=product_id).first()
            if product is None:
                raise InvalidRequest('Catalogue item %d does not exist.' % product_id)
            if not product.is_public or not product.get_product_class() or product.is_parent:
                raise InvalidRequest('Catalogue item %d cannot be bought directly.' % product_id)
            info = basket.strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise Conflict('Catalogue item %d is not available: %s' % (product_id, reason))
            basket.add_product(product, quantity)
        shipping_method = FreeShipping()
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user,
            order_number=OrderNumberGenerator().order_number(basket))
        basket.submit()
    return order


def get_order_for_update(order_id: int) -> Any:
    order = Order.objects.select_for_update().filter(pk=order_id).first()
    if order is None:
        raise NotFound('Order not found.')
    return order


def is_dispatched(order: Any) -> bool:
    return bool(order.shipping_events.filter(event_type__code=DISPATCH_EVENT_CODE).exists())


def dispatch_order(order_id: int, operator: Any) -> tuple[Any, bool]:
    """Record a 'Dispatched' shipping event for every line. Returns (order, newly_dispatched)."""
    with transaction.atomic():
        order = get_order_for_update(order_id)
        if order.status == STATUS_CANCELLED:
            raise Conflict('A cancelled order cannot be dispatched.')
        if is_dispatched(order):
            return order, False
        event_type, _ = ShippingEventType.objects.get_or_create(
            code=DISPATCH_EVENT_CODE, defaults={'name': 'Dispatched'})
        lines = list(order.lines.all())
        handler = EventHandler(operator)
        try:
            handler.handle_shipping_event(order, event_type, lines, [line.quantity for line in lines])
            if order.status != STATUS_DISPATCHED:
                handler.handle_order_status_change(order, STATUS_DISPATCHED, note_msg='Dispatched via API')
        except (InvalidOrderStatus, InvalidShippingEvent) as e:
            raise Conflict('Order %s cannot be dispatched from status %r.' % (order.number, order.status)) from e
    return order, True


def cancel_order(order_id: int, operator: Any) -> tuple[Any, bool]:
    """Move the order to 'Cancelled'. Returns (order, newly_cancelled)."""
    with transaction.atomic():
        order = get_order_for_update(order_id)
        if order.status == STATUS_CANCELLED:
            return order, False
        try:
            EventHandler(operator).handle_order_status_change(
                order, STATUS_CANCELLED, note_msg='Cancelled via API')
        except InvalidOrderStatus as e:
            raise Conflict('Order %s cannot be cancelled from status %r.' % (order.number, order.status)) from e
    return order, True
