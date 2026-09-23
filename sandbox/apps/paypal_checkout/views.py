"""HTTP endpoints for the PayPal checkout API (all under /api/).

Each capability is separately invocable. Shopper endpoints act only on the
caller's own data; ``fulfil``, ``cancel`` and ``reconciliation`` are operator
(``is_staff``) actions.
"""

from django.utils.dateparse import parse_datetime
from oscar.core.loading import get_model

from . import errors, serializers
from .http import api_endpoint, json_response, parse_json
from .models import PayPalPayment
from .services import cards, orders, reconciliation

Order = get_model("order", "Order")


def _owned_order(request, order_id):
    """Fetch an order that belongs to the caller, or raise NotFound."""
    try:
        order = Order.objects.get(id=order_id)
    except (Order.DoesNotExist, ValueError):
        raise errors.NotFound("No order with that id.")
    # Operators (staff) may act on any order; shoppers only on their own.
    if not request.user.is_staff and order.user_id != request.user.id:
        raise errors.NotFound("No order with that id.")
    return order


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@api_endpoint(["POST"])
def create_order(request):
    data = parse_json(request)
    items = data.get("items")
    if not isinstance(items, list):
        raise errors.ApiValidationError("'items' must be a list of {productId, quantity}.")
    normalized = [
        {"product_id": it.get("productId", it.get("product_id")),
         "quantity": it.get("quantity", 1)}
        for it in items
        if isinstance(it, dict)
    ]
    payment = orders.place_order(request.user, normalized)
    body = serializers.serialize_payment(payment)
    body["orderId"] = payment.order_id
    return json_response(body, status=201)


@api_endpoint(["POST"])
def pay_order(request, order_id):
    order = _owned_order(request, order_id)
    data = parse_json(request)
    card = data.get("card")
    payment_method_id = data.get("paymentMethodId", data.get("payment_method_id"))
    if card is None and payment_method_id is None:
        raise errors.ApiValidationError(
            "Provide either 'card' details or a 'paymentMethodId'."
        )
    payment = orders.pay(
        request.user, order, card=card, payment_method_id=payment_method_id
    )
    return json_response(serializers.serialize_payment(payment))


@api_endpoint(["POST"], staff=True)
def fulfil_order(request, order_id):
    order = _owned_order(request, order_id)
    payment = orders.fulfil(order)
    return json_response(serializers.serialize_payment(payment))


@api_endpoint(["POST"], staff=True)
def cancel_order(request, order_id):
    order = _owned_order(request, order_id)
    payment = orders.cancel(order)
    return json_response(serializers.serialize_payment(payment))


@api_endpoint(["POST"])
def refund_order(request, order_id):
    order = _owned_order(request, order_id)
    data = parse_json(request)
    idempotency_key = data.get("idempotencyKey", data.get("idempotency_key"))
    amount = data.get("amount")
    payment, refund = orders.refund(order, idempotency_key, amount=amount)
    body = serializers.serialize_refund(refund)
    body["refundId"] = refund.refund_id
    body["payment"] = serializers.serialize_payment(payment)
    return json_response(body, status=201)


@api_endpoint(["GET"])
def my_orders(request):
    payments = (
        PayPalPayment.objects.filter(order__user=request.user)
        .select_related("order")
        .prefetch_related("refunds", "order__lines")
    )
    return json_response(
        {"orders": [serializers.serialize_order_summary(p) for p in payments]}
    )


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

@api_endpoint(["POST", "GET"])
def payment_methods(request):
    if request.method == "GET":
        return json_response(
            {"paymentMethods": [serializers.serialize_bankcard(c) for c in cards.list_cards(request.user)]}
        )
    data = parse_json(request)
    card = data.get("card", data)
    bankcard = cards.save_card(request.user, card)
    body = serializers.serialize_bankcard(bankcard)
    body["paymentMethodId"] = bankcard.id
    return json_response(body, status=201)


@api_endpoint(["DELETE"])
def payment_method_detail(request, payment_method_id):
    cards.delete_card(request.user, payment_method_id)
    return json_response({"deleted": True, "paymentMethodId": int(payment_method_id)})


# ---------------------------------------------------------------------------
# Reconciliation (operator)
# ---------------------------------------------------------------------------

@api_endpoint(["GET"], staff=True)
def reconciliation_report(request):
    from_raw = request.GET.get("from")
    to_raw = request.GET.get("to")
    if not from_raw or not to_raw:
        raise errors.ApiValidationError("'from' and 'to' ISO-8601 date-times are required.")
    from_dt = parse_datetime(from_raw)
    to_dt = parse_datetime(to_raw)
    if from_dt is None or to_dt is None:
        raise errors.ApiValidationError("'from' and 'to' must be ISO-8601 date-times.")
    if from_dt.tzinfo is None or to_dt.tzinfo is None:
        from django.utils import timezone

        from_dt = timezone.make_aware(from_dt) if from_dt.tzinfo is None else from_dt
        to_dt = timezone.make_aware(to_dt) if to_dt.tzinfo is None else to_dt
    report = reconciliation.reconcile(from_dt, to_dt)
    return json_response(report)
