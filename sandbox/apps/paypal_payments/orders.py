"""
Placing an order through Oscar's own machinery (basket, strategy, shipping
repository, total calculator, ``OrderCreator``), plus the helpers shared by the
payment flows: install-unique references and the per-order write lock.
"""
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price

from . import money
from .errors import ApiProblem, bad_request, not_found
from .models import IntegrationInstall, PayPalPayment

Basket = get_model("basket", "Basket")
Country = get_model("address", "Country")
Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
ShippingAddress = get_model("order", "ShippingAddress")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Repository = get_class("shipping.repository", "Repository")
Selector = get_class("partner.strategy", "Selector")

MAX_LINES = 50
MAX_QUANTITY = 99

_prefix: str | None = None


def reference_prefix() -> str:
    """Install-unique prefix for every reference sent to PayPal."""
    global _prefix
    configured = str(settings.PAYPAL_REFERENCE_PREFIX or "").strip()
    if configured:
        return configured
    if _prefix is None:
        install = IntegrationInstall.objects.order_by("pk").first()
        if install is None:
            try:
                with transaction.atomic():
                    IntegrationInstall.objects.create(pk=1, token=secrets.token_hex(4))
            except IntegrityError:
                pass  # another process created it first
            install = IntegrationInstall.objects.order_by("pk").first()
        assert install is not None
        _prefix = "osc" + install.token
    return _prefix


def currency() -> str:
    return str(settings.PAYPAL_CURRENCY).upper()


@contextmanager
def locked_payment(payment_pk: int) -> Iterator[PayPalPayment]:
    """Open a transaction holding the payment row's write lock.

    The UPDATE comes first so the lock is taken before anything is read, on
    SQLite (which serialises writers) as on PostgreSQL (row lock). Claims made
    inside commit when the block exits, before any PayPal call."""
    with transaction.atomic():
        PayPalPayment.objects.filter(pk=payment_pk).update(lock_version=F("lock_version") + 1)
        yield PayPalPayment.objects.select_for_update().select_related("order").get(pk=payment_pk)


def get_owned_payment(user: Any, order_number: str) -> PayPalPayment:
    """The PayPal payment of one of ``user``'s orders; 404 for anyone else's."""
    payment = (
        PayPalPayment.objects.select_related("order")
        .filter(order__number=order_number, order__user=user)
        .first()
    )
    if payment is None:
        raise not_found("Order")
    return payment


def get_payment(order_number: str) -> PayPalPayment:
    payment = PayPalPayment.objects.select_related("order").filter(order__number=order_number).first()
    if payment is None:
        raise not_found("Order")
    return payment


def _parse_lines(raw: object) -> list[tuple[int, int]]:
    if not isinstance(raw, list) or not raw:
        raise bad_request("'lines' must be a non-empty list of {productId, quantity}.")
    if len(raw) > MAX_LINES:
        raise bad_request("An order may have at most %d lines." % MAX_LINES)
    lines: dict[int, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise bad_request("Each line must be an object with productId and quantity.")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if isinstance(product_id, str) and product_id.isdigit():
            product_id = int(product_id)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise bad_request("productId must be a catalogue product id.")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise bad_request("quantity must be an integer between 1 and %d." % MAX_QUANTITY)
        lines[product_id] = lines.get(product_id, 0) + quantity
    return list(lines.items())


def _shipping_address(raw: object) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise bad_request("shippingAddress must be an object.")
    fields = {
        "first_name": raw.get("firstName", ""),
        "last_name": raw.get("lastName", ""),
        "line1": raw.get("line1", ""),
        "line2": raw.get("line2", ""),
        "line4": raw.get("city", ""),
        "state": raw.get("state", ""),
        "postcode": raw.get("postcode", ""),
    }
    if not all(isinstance(v, str) for v in fields.values()):
        raise bad_request("shippingAddress fields must be strings.")
    if not fields["line1"] or not fields["last_name"]:
        raise bad_request("shippingAddress needs at least lastName and line1.")
    country = Country.objects.filter(iso_3166_1_a2=str(raw.get("country", "")).upper()).first()
    if country is None:
        raise bad_request("shippingAddress.country must be an ISO 3166-1 alpha-2 code.")
    return ShippingAddress(country=country, **fields)


def place_order(user: Any, payload: dict[str, Any]) -> PayPalPayment:
    """Create an Oscar order for ``user`` from catalogue product ids, awaiting
    payment. Prices come from the catalogue (Oscar's pricing strategy); the
    currency is PAYPAL_CURRENCY."""
    lines = _parse_lines(payload.get("lines"))
    shipping_address = _shipping_address(payload.get("shippingAddress"))
    strategy = Selector().strategy(user=user)

    with transaction.atomic():
        basket = Basket(owner=user)
        basket.strategy = strategy
        basket.save()
        for product_id, quantity in lines:
            product = Product.objects.filter(pk=product_id, is_public=True).first()
            if product is None:
                raise bad_request("Product %s does not exist." % product_id)
            if not product.is_public or product.is_parent:
                raise bad_request("Product %s cannot be bought directly; choose a variant." % product_id)
            info = strategy.fetch_for_product(product)
            if not info.availability.is_available_to_buy or info.price is None or not info.price.exists:
                raise ApiProblem(409, "unavailable", "Product %s is not available to buy." % product_id)
            allowed, reason = info.availability.is_purchase_permitted(quantity)
            if not allowed:
                raise ApiProblem(409, "unavailable", "Product %s: %s" % (product_id, reason))
            basket.add_product(product, quantity)

        shipping_method = Repository().get_default_shipping_method(
            basket=basket, user=user, shipping_addr=shipping_address
        )
        shipping_charge = shipping_method.calculate(basket)
        oscar_total = OrderTotalCalculator().calculate(basket, shipping_charge)
        order_currency = currency()
        total_amount = money.quantize(Decimal(oscar_total.incl_tax), order_currency)
        if total_amount <= 0:
            raise ApiProblem(409, "nothing_to_pay", "The order total is zero; nothing to pay.")
        total = Price(
            currency=order_currency,
            excl_tax=money.quantize(Decimal(oscar_total.excl_tax), order_currency),
            incl_tax=total_amount,
        )
        if shipping_address is not None:
            shipping_address.save()
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=shipping_address,
            order_number=OrderNumberGenerator().order_number(basket),
        )
        basket.submit()
        return PayPalPayment.objects.create(order=order, amount=total_amount, currency=order_currency)
