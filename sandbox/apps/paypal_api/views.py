"""HTTP boundary for the PayPal payments API.

Plain Django JSON views (no extra framework). Callers authenticate with the
sandbox's own session login; the caller's identity is the authenticated request
user. Operator actions (fulfil, cancel, reconciliation) require ``is_staff``;
every other endpoint is scoped to the caller's own data.

The views are CSRF-exempt so the API is drivable by a programmatic caller that
holds a session cookie; they still require an authenticated session.
"""

import functools
import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import services
from .gateway import PayPalError
from .models import PayPalPayment
from .services import ServiceError

logger = logging.getLogger("paypal_api")


def _error(message, status=400, code="invalid", detail=None):
    body = {"error": {"code": code, "message": message}}
    if detail is not None:
        body["error"]["detail"] = detail
    return JsonResponse(body, status=status)


def api_view(methods, staff=False):
    """Wrap a view: enforce method, session auth, optional staff, JSON errors."""

    def decorator(func):
        @csrf_exempt
        @functools.wraps(func)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return _error("Method not allowed", 405, "method_not_allowed")
            if not request.user.is_authenticated:
                return _error("Authentication required", 401, "unauthenticated")
            if staff and not request.user.is_staff:
                return _error(
                    "This action is restricted to staff operators", 403, "forbidden"
                )
            try:
                return func(request, *args, **kwargs)
            except ServiceError as e:
                return _error(e.message, e.http_status, e.code)
            except PayPalError as e:
                return _error(e.message, e.http_status, e.code, detail=e.detail)
            except Exception:  # pragma: no cover - unexpected
                logger.exception("Unhandled error in %s", func.__name__)
                return _error("Internal error", 500, "internal")

        return wrapper

    return decorator


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        raise ServiceError("Request body must be valid JSON")
    if not isinstance(data, dict):
        raise ServiceError("Request body must be a JSON object")
    return data


# --------------------------------------------------------------------------
# Orders / payments
# --------------------------------------------------------------------------

@api_view(["POST"])
def orders(request):
    data = _json_body(request)
    items = data.get("items")
    if not isinstance(items, list):
        raise ServiceError("'items' must be a list of {productId, quantity}")
    order, payment = services.create_order_for_items(request.user, items)
    body = {"orderId": order.id, "orderNumber": order.number}
    body.update(payment.as_dict())
    return JsonResponse(body, status=201)


@api_view(["POST"])
def pay(request, order_id):
    data = _json_body(request)
    payment = services.pay_order(
        request.user,
        order_id,
        card_payload=data.get("card"),
        saved_card_id=data.get("savedCardId"),
    )
    return JsonResponse(payment.as_dict(), status=200)


@api_view(["POST"], staff=True)
def fulfil(request, order_id):
    payment = services.fulfil_order(order_id)
    return JsonResponse(payment.as_dict(), status=200)


@api_view(["POST"], staff=True)
def cancel(request, order_id):
    payment = services.cancel_order(order_id)
    return JsonResponse(payment.as_dict(), status=200)


@api_view(["POST"])
def refunds(request, order_id):
    data = _json_body(request)
    payment, refund_row = services.refund_order(
        request.user,
        order_id,
        amount=data.get("amount"),
        idempotency_key=data.get("idempotencyKey"),
    )
    body = {"refundId": refund_row.id}
    body.update(refund_row.as_dict())
    body["payment"] = payment.as_dict()
    return JsonResponse(body, status=201)


@api_view(["GET"])
def my_orders(request):
    payments = (
        PayPalPayment.objects.select_related("order")
        .filter(user=request.user)
        .order_by("-created")
    )
    return JsonResponse({"orders": [p.as_dict() for p in payments]}, status=200)


@api_view(["GET"], staff=True)
def reconciliation(request):
    report = services.reconcile(request.GET.get("from"), request.GET.get("to"))
    return JsonResponse(report, status=200)


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------

@api_view(["GET", "POST"])
def payment_methods(request):
    if request.method == "POST":
        data = _json_body(request)
        saved = services.save_card(request.user, data.get("card") or data)
        body = {"paymentMethodId": saved.id}
        body.update(saved.as_dict())
        return JsonResponse(body, status=201)
    cards = services.list_cards(request.user)
    return JsonResponse(
        {"paymentMethods": [c.as_dict() for c in cards]}, status=200
    )


@api_view(["DELETE"])
def payment_method_detail(request, card_id):
    services.delete_card(request.user, card_id)
    return JsonResponse({"deleted": True, "paymentMethodId": card_id}, status=200)
