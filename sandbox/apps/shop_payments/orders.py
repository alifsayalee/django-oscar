"""
Placing Oscar orders from an API request, and describing them.

An order is placed the same way Oscar's checkout places one: a basket priced by
the partner strategy, offers applied, the shipping repository's method, the
checkout total calculator and ``OrderCreator`` — so lines, discounts and stock
allocations are Oscar's own.  The order starts in Oscar's initial status
(``Pending``), which this API reports as awaiting payment.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from oscar.core.loading import get_class, get_model

from .errors import ApiProblem
from .models import PayPalPayment
from .money import is_exact
from .paypal_client import configured_currency

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
Order = get_model("order", "Order")
Selector = get_class("partner.strategy", "Selector")
Applicator = get_class("offer.applicator", "Applicator")
Repository = get_class("shipping.repository", "Repository")
SurchargeApplicator = get_class("checkout.applicator", "SurchargeApplicator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")

MAX_LINES = 50
MAX_QUANTITY = 99


def _parse_items(raw: object) -> list[tuple[int, int]]:
    if not isinstance(raw, list) or not raw:
        raise ApiProblem(400, "invalid_items", "items must be a non-empty list of {productId, quantity}.")
    if len(raw) > MAX_LINES:
        raise ApiProblem(400, "invalid_items", f"An order may have at most {MAX_LINES} lines.")
    quantities: dict[int, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ApiProblem(400, "invalid_items", "Each item must be an object.")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if isinstance(product_id, str) and product_id.isdigit():
            product_id = int(product_id)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise ApiProblem(400, "invalid_items", "productId must be a catalogue product id.")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise ApiProblem(400, "invalid_items", f"quantity must be an integer from 1 to {MAX_QUANTITY}.")
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    return list(quantities.items())


def _shipping_address(raw: object) -> Any:
    if not isinstance(raw, dict):
        raise ApiProblem(400, "shipping_address_required", "These items need a shippingAddress.")

    def text(key: str, required: bool = False) -> str:
        value = raw.get(key, "")
        if not isinstance(value, str):
            raise ApiProblem(400, "invalid_shipping_address", f"shippingAddress.{key} must be a string.")
        if required and not value.strip():
            raise ApiProblem(400, "invalid_shipping_address", f"shippingAddress.{key} is required.")
        return value.strip()

    country = Country.objects.filter(iso_3166_1_a2=text("countryCode", required=True).upper()).first()
    if country is None:
        raise ApiProblem(400, "invalid_shipping_address", "shippingAddress.countryCode is not a known country.")
    address = ShippingAddress(
        first_name=text("firstName"),
        last_name=text("lastName", required=True),
        line1=text("line1", required=True),
        line2=text("line2"),
        line4=text("city", required=True),
        state=text("state"),
        postcode=text("postcode"),
        country=country,
    )
    try:
        address.clean()
    except ValidationError as exc:
        raise ApiProblem(400, "invalid_shipping_address", " ".join(exc.messages)) from None
    return address


def place_order(request: Any, payload: dict[str, Any]) -> Any:
    user = request.user
    currency = configured_currency()
    items = _parse_items(payload.get("items"))
    products = {p.pk: p for p in Product.objects.filter(pk__in=[pid for pid, _ in items])}

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for product_id, quantity in items:
            product = products.get(product_id)
            if product is None:
                raise ApiProblem(400, "unknown_product", f"Product {product_id} does not exist.")
            info = basket.strategy.fetch_for_product(product)
            if info.stockrecord is None or info.price is None or not info.price.exists:
                raise ApiProblem(400, "not_for_sale", f"Product {product_id} has no price and cannot be bought.")
            allowed, message = info.availability.is_purchase_permitted(quantity)
            if not allowed:
                raise ApiProblem(409, "not_available", f"Product {product_id}: {message}")
            basket.add_product(product, quantity)

        Applicator().apply(basket, user, request)

        shipping_address = None
        if basket.is_shipping_required():
            shipping_address = _shipping_address(payload.get("shippingAddress"))
        method = Repository().get_default_shipping_method(
            basket=basket, shipping_addr=shipping_address, user=user, request=request
        )
        shipping_charge = method.calculate(basket)
        surcharges = SurchargeApplicator(request).get_applicable_surcharges(basket, shipping_charge=shipping_charge)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge, surcharges)
        if not total.is_tax_known:
            raise ApiProblem(409, "tax_unknown", "The order total cannot be determined.")
        if total.incl_tax <= Decimal("0.00"):
            raise ApiProblem(400, "nothing_to_pay", "The order total is zero; there is nothing to pay.")
        if not is_exact(total.incl_tax, currency):
            raise ApiProblem(409, "amount_not_representable", f"The total cannot be charged exactly in {currency}.")

        if shipping_address is not None:
            shipping_address.save()
        # Amounts are the catalogue's; the currency is the configured one.
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=shipping_address,
            surcharges=surcharges,
            request=request,
            currency=currency,
        )
        basket.submit()
        PayPalPayment.objects.create(order=order, amount=order.total_incl_tax, currency=order.currency)
    return order


def serialize_order(order: Any) -> dict[str, Any]:
    payment = getattr(order, "paypal_payment", None)
    lines = [
        {
            "productId": line.product_id,
            "title": line.title,
            "quantity": line.quantity,
            "unitPrice": str(line.unit_price_incl_tax),
            "lineTotal": str(line.line_price_incl_tax),
        }
        for line in order.lines.all()
    ]
    return {
        "orderId": order.number,
        "status": order.status,
        "awaitingPayment": payment is None or payment.state == PayPalPayment.AWAITING_PAYMENT,
        "currency": order.currency,
        "total": str(order.total_incl_tax),
        "shipping": str(order.shipping_incl_tax),
        "placedAt": order.date_placed.isoformat() if order.date_placed else None,
        "lines": lines,
        "payment": serialize_payment(payment) if payment is not None else None,
    }


def serialize_payment(payment: PayPalPayment) -> dict[str, Any]:
    refunds = [
        {
            "refundId": str(r.public_id),
            "amount": str(r.amount),
            "status": r.outcome,
            "paypalRefundId": r.paypal_refund_id or None,
            "paypalStatus": r.paypal_status or None,
            "createdAt": r.created.isoformat(),
        }
        for r in payment.refunds.order_by("created")
    ]
    refundable = payment.captured_amount - payment.refund_reserved
    return {
        "state": payment.state,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "card": payment.card_label or None,
        "paymentMethodId": str(payment.bankcard_id) if payment.bankcard_id else None,
        "paypalOrderId": payment.paypal_order_id or None,
        "authorization": {
            "id": payment.authorization_id,
            "status": payment.authorization_status,
            "createdAt": payment.authorization_created_at.isoformat() if payment.authorization_created_at else None,
            "expiresAt": payment.authorization_expires_at.isoformat() if payment.authorization_expires_at else None,
            "reauthorizedAt": payment.reauthorized_at.isoformat() if payment.reauthorized_at else None,
        }
        if payment.authorization_id
        else None,
        "capture": {
            "id": payment.capture_id,
            "status": payment.capture_status,
            "amount": str(payment.captured_amount),
            "paypalFee": str(payment.paypal_fee) if payment.paypal_fee is not None else None,
            "netAmount": str(payment.net_amount) if payment.net_amount is not None else None,
            "capturedAt": payment.captured_at.isoformat() if payment.captured_at else None,
        }
        if payment.capture_id
        else None,
        "refundedAmount": str(payment.refunded_amount),
        "refundableAmount": str(max(refundable, Decimal("0.00"))) if payment.capture_id else "0.00",
        "refunds": refunds,
        "lastError": payment.last_error or None,
    }
