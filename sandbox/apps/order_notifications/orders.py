"""
Order placement and status changes on Oscar's own order models.

Orders are placed through Oscar's basket, pricing strategy and ``OrderCreator``
(the same path checkout takes), so the result is an ordinary Oscar order.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from oscar.apps.order.signals import order_status_changed
from oscar.core.loading import get_class, get_model

Basket = get_model('basket', 'Basket')
Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
Selector = get_class('partner.strategy', 'Selector')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
FreeShipping = get_class('shipping.methods', 'Free')

STATUS_DISPATCHED = 'Dispatched'
STATUS_CANCELLED = 'Cancelled'


class OrderRequestInvalid(Exception):
    pass


class TransitionRefused(Exception):
    pass


@dataclass(frozen=True)
class LineRequest:
    product_id: int
    quantity: int


def place_order(user, lines: list[LineRequest], request=None):
    """Price the catalogue items for ``user`` and place an Oscar order for them."""
    strategy = Selector().strategy(request=request, user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for line in lines:
            product = Product.objects.filter(pk=line.product_id).first()
            if product is None:
                raise OrderRequestInvalid(f'Unknown catalogue item {line.product_id}.')
            if not product.is_public or product.is_parent:
                raise OrderRequestInvalid(f'Catalogue item {line.product_id} cannot be bought.')
            info = strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(line.quantity)
            if not info.price.exists or not permitted:
                raise OrderRequestInvalid(
                    f'Catalogue item {line.product_id} cannot be bought: {reason or "no price"}.')
            basket.add_product(product, line.quantity)

        shipping_method = FreeShipping()
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            order_number=OrderNumberGenerator().order_number(basket),
            request=request,
        )
        basket.submit()
    return order


def transition(order_id: int, new_status: str):
    """Move an order to ``new_status`` if its pipeline allows it; exactly one caller wins."""
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None:
            raise Order.DoesNotExist()
        old_status = order.status
        if new_status not in order.available_statuses():
            raise TransitionRefused(
                f'Order {order.number} cannot move from "{old_status}" to "{new_status}".')
        # Compare-and-set: a concurrent transition that got there first makes this match nothing.
        won = Order.objects.filter(pk=order.pk, status=old_status).update(status=new_status)
        if not won:
            raise TransitionRefused(f'Order {order.number} was changed by another request.')
        order.status = new_status
        order.status_changes.create(old_status=old_status, new_status=new_status)
    order_status_changed.send(sender=order, order=order, old_status=old_status,
                              new_status=new_status)
    return order
