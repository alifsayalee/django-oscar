"""JSON HTTP endpoints for the PayPal flows.

Plain Django views (no DRF dependency added). Session-authenticated the way the sandbox
already authenticates callers; operator endpoints additionally require ``is_staff``. The
payment-mutating views run outside the request's ambient transaction
(``transaction.non_atomic_requests``) so each durable claim commits before the PayPal call —
the sandbox sets ``ATOMIC_REQUESTS=True``, under which an in-view ``atomic()`` is only a
savepoint. CSRF is exempt: this is a cookie-session JSON API driven by a script/curl.
"""
from __future__ import annotations

import json
import logging
from functools import wraps

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import paypal_client as pp
from . import services
from .models import OrderPayment
from .services import ServiceError

log = logging.getLogger("payments_api")


def _json(data, status=200):
    return JsonResponse(data, status=status)


def _error(status, message):
    return JsonResponse({"error": message}, status=status)


def _body(request) -> dict:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ServiceError(400, "Request body must be valid JSON.")
    if not isinstance(data, dict):
        raise ServiceError(400, "Request body must be a JSON object.")
    return data


def api_view(methods, *, staff=False):
    """Wrap a view with method/auth/staff checks and error translation."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return _error(405, "Method not allowed.")
            if not request.user.is_authenticated:
                return _error(401, "Authentication required.")
            if staff and not request.user.is_staff:
                return _error(403, "Operator (staff) access required.")
            try:
                return fn(request, *args, **kwargs)
            except ServiceError as e:
                return _error(e.status_code, e.message)
            except pp.ProviderError as e:
                log.warning("PayPal error (%s): %s", e.provider_status, e.message)
                return _error(e.status_code, e.message)

        return csrf_exempt(transaction.non_atomic_requests(wrapper))

    return decorator


def _own_payment(request, order_id) -> OrderPayment:
    try:
        return OrderPayment.objects.select_related("order").get(
            order_id=order_id, user=request.user
        )
    except OrderPayment.DoesNotExist:
        raise ServiceError(404, "Order not found.")


def _any_payment(order_id) -> OrderPayment:
    try:
        return OrderPayment.objects.select_related("order").get(order_id=order_id)
    except OrderPayment.DoesNotExist:
        raise ServiceError(404, "Order not found.")


def _order_view(op: OrderPayment) -> dict:
    order = op.order
    return {
        "orderId": order.id,
        "orderNumber": order.number,
        "orderStatus": order.status,
        "total": str(order.total_incl_tax),
        "payment": op.payment_state(),
        "lines": [
            {"title": line.title, "quantity": line.quantity,
             "linePrice": str(line.line_price_incl_tax)}
            for line in order.lines.all()
        ],
    }


# ---------------------------------------------------------------------------
# Flow 1
# ---------------------------------------------------------------------------
@api_view(["POST"])
def create_order(request):
    data = _body(request)
    items = data.get("items")
    if not isinstance(items, list):
        raise ServiceError(400, "'items' must be a list of {productId, quantity}.")
    op = services.create_order(request.user, items)
    payload = _order_view(op)
    return _json({"orderId": op.order_id, **payload}, status=201)


@api_view(["POST"])
def pay_order(request, order_id):
    op = _own_payment(request, order_id)
    data = _body(request)
    card = data.get("card")
    saved_card_id = data.get("savedCardId")
    op = services.authorize(op, card=card, saved_card_id=saved_card_id)
    return _json(_order_view(op))


@api_view(["POST"], staff=True)
def fulfil_order(request, order_id):
    op = _any_payment(order_id)
    op = services.capture(op)
    return _json(_order_view(op))


@api_view(["POST"], staff=True)
def cancel_order(request, order_id):
    op = _any_payment(order_id)
    op = services.cancel(op)
    return _json(_order_view(op))


@api_view(["POST"])
def refund_order(request, order_id):
    op = _own_payment(request, order_id)
    data = _body(request)
    idempotency_key = data.get("idempotencyKey")
    if not idempotency_key:
        raise ServiceError(400, "'idempotencyKey' is required.")
    amount = data.get("amount")
    row = services.refund(op, amount=amount, idempotency_key=str(idempotency_key))
    op.refresh_from_db()
    return _json({"refundId": row.id, "refund": row.as_dict(), "payment": op.payment_state()},
                 status=201)


@api_view(["GET"])
def my_orders(request):
    payments = OrderPayment.objects.select_related("order").filter(
        user=request.user
    ).prefetch_related("order__lines")
    return _json({"orders": [_order_view(op) for op in payments]})


@api_view(["GET"], staff=True)
def reconciliation(request):
    from django.utils.dateparse import parse_datetime

    frm = request.GET.get("from")
    to = request.GET.get("to")
    if not frm or not to:
        raise ServiceError(400, "'from' and 'to' query parameters are required (ISO-8601).")
    from_dt = parse_datetime(frm)
    to_dt = parse_datetime(to)
    if from_dt is None or to_dt is None:
        raise ServiceError(400, "'from' and 'to' must be ISO-8601 date-times.")
    from datetime import timezone as dt_timezone

    from django.utils import timezone

    if from_dt.tzinfo is None:
        from_dt = timezone.make_aware(from_dt, dt_timezone.utc)
    if to_dt.tzinfo is None:
        to_dt = timezone.make_aware(to_dt, dt_timezone.utc)
    report = services.reconcile(from_dt, to_dt)
    return _json(report)


# ---------------------------------------------------------------------------
# Flow 2
# ---------------------------------------------------------------------------
@api_view(["POST", "GET"])
def payment_methods(request):
    if request.method == "GET":
        cards = services.list_cards(request.user)
        return _json({"paymentMethods": [c.as_dict() for c in cards]})
    data = _body(request)
    card = data.get("card")
    if not isinstance(card, dict):
        raise ServiceError(400, "'card' object is required.")
    saved = services.save_card(request.user, card)
    return _json({"paymentMethodId": saved.id, **saved.as_dict()}, status=201)


@api_view(["DELETE"])
def payment_method_detail(request, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return _json({"deleted": True, "paymentMethodId": int(payment_method_id)})
