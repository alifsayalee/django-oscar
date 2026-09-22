"""HTTP endpoints for the PayPal payment + saved-card API.

Authentication is the sandbox's own Django session login; the caller's identity is the
authenticated ``request.user``. Shopper endpoints act only on the caller's own data;
``fulfil``/``cancel``/``reconciliation`` are operator actions restricted to ``is_staff``.

These are JSON endpoints driven by API clients (not browser forms), so they are
``csrf_exempt`` while still requiring an authenticated session.
"""

from __future__ import annotations

import functools
import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .gateway import PayPalError
from . import services


def _json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise PayPalError(400, "invalid_json", "Request body must be valid JSON.")
    if not isinstance(data, dict):
        raise PayPalError(400, "invalid_json", "Request body must be a JSON object.")
    return data


def _endpoint(require_staff: bool = False):
    """Wrap a view: enforce auth, funnel PayPalError into a JSON error response."""

    def decorator(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse(
                    {"error": "authentication_required",
                     "message": "You must be signed in."}, status=401)
            if require_staff and not request.user.is_staff:
                return JsonResponse(
                    {"error": "forbidden",
                     "message": "This action is restricted to operators."}, status=403)
            try:
                return view(request, *args, **kwargs)
            except PayPalError as e:
                return JsonResponse(e.to_dict(), status=e.status)

        return wrapped

    return decorator


def _payment_view(payment) -> dict:
    order = payment.order
    return {
        "orderId": str(order.number),
        "status": order.status,
        "state": payment.state,
        "stateLabel": payment.get_state_display(),
        "currency": payment.currency,
        "amount": str(payment.amount),
        "paypalOrderId": payment.paypal_order_id or None,
        "authorizationId": payment.authorization_id or None,
        "authorizationStatus": payment.authorization_status or None,
        "authorizationExpiresAt": (
            payment.authorization_expires_at.isoformat()
            if payment.authorization_expires_at else None),
        "captureId": payment.capture_id or None,
        "captureStatus": payment.capture_status or None,
        "capturedAmount": (str(payment.captured_amount)
                           if payment.captured_amount is not None else None),
        "paypalFee": str(payment.paypal_fee) if payment.paypal_fee is not None else None,
        "netAmount": str(payment.net_amount) if payment.net_amount is not None else None,
        "totalRefunded": str(payment.total_refunded),
        "refundableAmount": str(payment.refundable_amount),
        "refunds": [
            {"refundId": r.refund_id, "amount": str(r.amount),
             "currency": r.currency, "status": r.status}
            for r in payment.refunds.all()
        ],
    }


# -- Flow 1: orders -----------------------------------------------------------------
@_endpoint()
@require_http_methods(["POST"])
def create_order(request):
    body = _json_body(request)
    order = services.place_order(request.user, body.get("items"))
    payment = order.paypal_payment
    return JsonResponse({**_payment_view(payment), "orderId": str(order.number)}, status=201)


@_endpoint()
@require_http_methods(["POST"])
def pay_order(request, order_number):
    body = _json_body(request)
    order = services.get_owned_order(request.user, order_number)
    payment = services.pay_order(request.user, order, body)
    return JsonResponse(_payment_view(payment), status=200)


@_endpoint(require_staff=True)
@require_http_methods(["POST"])
def fulfil_order(request, order_number):
    order = services.get_any_order(order_number)
    payment = services.fulfil_order(order)
    return JsonResponse(_payment_view(payment), status=200)


@_endpoint()
@require_http_methods(["POST"])
def cancel_order(request, order_number):
    order = services.get_owned_order(request.user, order_number)
    payment = services.cancel_order(order)
    return JsonResponse(_payment_view(payment), status=200)


@_endpoint(require_staff=True)
@require_http_methods(["POST"])
def refund_order(request, order_number):
    body = _json_body(request)
    order = services.get_any_order(order_number)
    refund = services.refund_order(
        order, amount=body.get("amount"),
        idempotency_key=body.get("idempotencyKey") or body.get("idempotency_key"))
    return JsonResponse({
        **_payment_view(refund.payment),
        "refundId": refund.refund_id,
        "refund": {
            "refundId": refund.refund_id,
            "amount": str(refund.amount),
            "currency": refund.currency,
            "status": refund.status,
        },
    }, status=201)


@_endpoint()
@require_http_methods(["GET"])
def my_orders(request):
    from .models import PayPalPayment
    payments = (PayPalPayment.objects.select_related("order")
                .filter(order__user=request.user).order_by("-created_at"))
    return JsonResponse({"orders": [_payment_view(p) for p in payments]}, status=200)


@_endpoint(require_staff=True)
@require_http_methods(["GET"])
def reconciliation(request):
    report = services.reconcile(request.GET.get("from"), request.GET.get("to"))
    return JsonResponse(report, status=200)


# -- Flow 2: saved cards ------------------------------------------------------------
@_endpoint()
@require_http_methods(["GET", "POST"])
def payment_methods(request):
    if request.method == "POST":
        body = _json_body(request)
        card = services.save_card(request.user, body)
        return JsonResponse({"paymentMethodId": card.id, **card.describe()}, status=201)
    cards = services.list_cards(request.user)
    return JsonResponse({"paymentMethods": [c.describe() for c in cards]}, status=200)


@_endpoint()
@require_http_methods(["DELETE"])
def delete_payment_method(request, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return JsonResponse({"deleted": True, "paymentMethodId": payment_method_id}, status=200)
