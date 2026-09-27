"""Placing Oscar orders from catalogue items, and describing them to the API."""

from typing import Any

from django.db import transaction
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price

from . import gateway
from .errors import ApiProblem
from .models import PayPalOperation, PayPalPayment
from .money import from_minor, round_to_minor, wire

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
Order = get_model("order", "Order")
Selector = get_class("partner.strategy", "Selector")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Free = get_class("shipping.methods", "Free")
NoShippingRequired = get_class("shipping.methods", "NoShippingRequired")

AWAITING_PAYMENT = "Awaiting payment"
MAX_LINES = 50
MAX_QUANTITY = 100


def _int(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ApiProblem(400, "invalid_request", f"{field} must be an integer.")
    try:
        number = int(value)
    except ValueError:
        raise ApiProblem(400, "invalid_request", f"{field} must be an integer.") from None
    if not low <= number <= high:
        raise ApiProblem(400, "invalid_request", f"{field} must be between {low} and {high}.")
    return number


def parse_items(payload: dict[str, Any]) -> list[tuple[int, int]]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ApiProblem(400, "invalid_request", "items must be a non-empty list of {productId, quantity}.")
    if len(items) > MAX_LINES:
        raise ApiProblem(400, "invalid_request", f"An order can have at most {MAX_LINES} items.")
    quantities: dict[int, int] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ApiProblem(400, "invalid_request", "Each item must be an object {productId, quantity}.")
        product_id = _int(item.get("productId"), "productId", 1, 2**31)
        quantity = _int(item.get("quantity", 1), "quantity", 1, MAX_QUANTITY)
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    return list(quantities.items())


def _shipping_address(data: Any) -> Any:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_request", "shippingAddress must be an object.")
    code = str(data.get("countryCode", "")).upper()
    country = Country.objects.filter(iso_3166_1_a2=code).first()
    if country is None:
        raise ApiProblem(400, "invalid_request", "shippingAddress.countryCode is not a known country.")
    line1 = str(data.get("line1", "")).strip()
    if not line1:
        raise ApiProblem(400, "invalid_request", "shippingAddress.line1 is required.")
    address = ShippingAddress(
        first_name=str(data.get("firstName", ""))[:255],
        last_name=str(data.get("lastName", ""))[:255],
        line1=line1[:255],
        line2=str(data.get("line2", ""))[:255],
        line4=str(data.get("city", ""))[:255],
        state=str(data.get("state", ""))[:255],
        postcode=str(data.get("postcode", ""))[:64],
        country=country,
    )
    address.save()
    return address


def place_order(user: Any, payload: dict[str, Any]) -> Any:
    """Create an Oscar order (status 'Awaiting payment') through Oscar's own basket and OrderCreator."""
    currency = gateway.currency()
    items = parse_items(payload)
    strategy = Selector().strategy(user=user)

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in items:
            product = Product.objects.filter(pk=product_id).first()
            if product is None:
                raise ApiProblem(400, "unknown_product", f"Catalogue item {product_id} does not exist.")
            info = strategy.fetch_for_product(product)
            if not info.availability.is_available_to_buy or not info.price.exists:
                raise ApiProblem(400, "not_purchasable", f"Catalogue item {product_id} cannot be bought.")
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ApiProblem(400, "not_purchasable", f"Catalogue item {product_id}: {reason}")
            try:
                basket.add_product(product, quantity)
            except ValueError as e:
                raise ApiProblem(400, "not_purchasable", str(e)) from None
        basket.reset_offer_applications()

        method = Free() if basket.is_shipping_required() else NoShippingRequired()
        shipping_charge = method.calculate(basket)
        catalogue_total = OrderTotalCalculator().calculate(basket, shipping_charge)
        # The amounts are the catalogue's; the currency is this site's PayPal currency.
        amount_minor = round_to_minor(catalogue_total.incl_tax, currency)
        if amount_minor <= 0:
            raise ApiProblem(400, "invalid_request", "The order total must be greater than zero.")
        total = Price(
            currency=currency,
            excl_tax=from_minor(round_to_minor(catalogue_total.excl_tax, currency), currency),
            incl_tax=from_minor(amount_minor, currency),
        )

        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=_shipping_address(payload.get("shippingAddress")),
            order_number=OrderNumberGenerator().order_number(basket),
            status=AWAITING_PAYMENT,
        )
        basket.submit()
        PayPalPayment.objects.create(order=order, currency=currency, amount_minor=amount_minor)
    return order


def get_order(number: str, *, user: Any = None) -> Any:
    """An order by number; with ``user`` only that shopper's own order (others look absent)."""
    queryset = Order.objects.select_related("paypal_payment")
    if user is not None:
        queryset = queryset.filter(user=user)
    order = queryset.filter(number=str(number)).first()
    if order is None:
        raise ApiProblem(404, "not_found", "Order not found.")
    return order


def _money(minor: int | None, currency: str) -> str | None:
    return None if minor is None else wire(minor, currency)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def describe_payment(order: Any) -> dict[str, Any]:
    payment = getattr(order, "paypal_payment", None)
    if payment is None:
        return {"status": "not_paid_through_api"}
    cur = payment.currency
    refunds = [
        {
            "refundId": str(op.pk),
            "paypalRefundId": op.provider_id or None,
            "amount": _money(op.amount_minor, cur),
            "status": op.outcome,
            "paypalStatus": op.provider_status or None,
            "createdAt": _iso(op.provider_time or op.claimed_at),
        }
        for op in PayPalOperation.objects.filter(order=order, kind=PayPalOperation.REFUND)
        .exclude(outcome=PayPalOperation.FAILED, provider_id="")
    ]
    unresolved = PayPalOperation.objects.filter(
        order=order, outcome__in=(PayPalOperation.SENDING, PayPalOperation.UNKNOWN)
    ).values_list("kind", flat=True)
    return {
        "status": payment.state,
        "amount": wire(payment.amount_minor, cur),
        "currency": cur,
        "paypalOrderId": payment.paypal_order_id or None,
        "card": (
            {"brand": payment.card_brand or None, "lastDigits": payment.card_last_digits or None,
             "paymentMethodId": str(payment.saved_card_id) if payment.saved_card_id else None}
            if payment.authorization_id
            else None
        ),
        "authorization": (
            {
                "id": payment.authorization_id,
                "status": payment.authorization_status,
                "createdAt": _iso(payment.authorization_created_at),
                "expiresAt": _iso(payment.authorization_expires_at),
                "reauthorizedAt": _iso(payment.reauthorized_at),
                "originalAuthorizationId": payment.original_authorization_id or None,
            }
            if payment.authorization_id
            else None
        ),
        "capture": (
            {
                "id": payment.capture_id,
                "status": payment.capture_status,
                "amount": _money(payment.captured_minor, cur),
                "paypalFee": _money(payment.paypal_fee_minor, cur),
                "netAmount": _money(payment.net_minor, cur),
                "capturedAt": _iso(payment.captured_at),
            }
            if payment.capture_id
            else None
        ),
        "refunded": wire(payment.refunded_minor, cur),
        "refundable": wire(payment.captured_minor - payment.refund_reserved_minor, cur),
        "refunds": refunds,
        "unresolvedOperations": sorted(set(unresolved)),
    }


def describe_order(order: Any) -> dict[str, Any]:
    return {
        "orderId": order.number,
        "status": order.status,
        "placedAt": _iso(order.date_placed),
        "currency": order.currency,
        "total": str(order.total_incl_tax),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": str(line.unit_price_incl_tax) if line.unit_price_incl_tax is not None else None,
                "linePrice": str(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": describe_payment(order),
    }
