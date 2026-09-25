"""
Placing an order through the API, using Oscar's own basket, pricing strategy,
shipping repository and ``OrderCreator`` - the same path checkout takes.
"""

from django.db import transaction
from oscar.core.loading import get_class, get_model

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Selector = get_class("partner.strategy", "Selector")
Repository = get_class("shipping.repository", "Repository")
SurchargeApplicator = get_class("checkout.applicator", "SurchargeApplicator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")


class OrderRequestInvalid(Exception):
    pass


def place_order(user, items):
    """
    Place an order for ``user`` from ``items``, a list of
    ``(product_id, quantity)`` pairs. Raises ``OrderRequestInvalid`` when a
    product cannot be bought in that quantity.
    """
    with transaction.atomic():
        strategy = Selector().strategy(user=user)
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in items:
            product = Product.objects.filter(pk=product_id, is_public=True).first()
            if product is None:
                raise OrderRequestInvalid("Product %s does not exist." % product_id)
            if product.is_parent:
                raise OrderRequestInvalid(
                    "Product %s has variants; order one of its variants." % product_id)
            info = strategy.fetch_for_product(product)
            if info.price is None or not info.price.exists:
                raise OrderRequestInvalid("Product %s is not for sale." % product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise OrderRequestInvalid("Product %s: %s" % (product_id, reason))
            basket.add_product(product, quantity)

        shipping_method = Repository().get_default_shipping_method(basket=basket, user=user)
        shipping_charge = shipping_method.calculate(basket)
        surcharges = SurchargeApplicator().get_applicable_surcharges(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge, surcharges=surcharges)
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            surcharges=surcharges,
        )
        basket.set_as_submitted()
    return order
