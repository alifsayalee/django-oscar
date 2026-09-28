"""
JSON endpoints under /api/.

Callers authenticate with Django's session login (the sandbox's own
mechanism); POST/DELETE requests carry the CSRF token from the ``csrftoken``
cookie in an ``X-CSRFToken`` header. Views run outside ATOMIC_REQUESTS so a
payment claim is committed before PayPal is called.
"""

import json
import logging
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie

from . import services
from .gateway import ProviderError
from .models import OrderPayment, ProviderWrite
from .services import ApiProblem

logger = logging.getLogger("apps.paypal_payments")


def _error(status, code, message, **extra):
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status)


def api(methods, *, staff=False, login_required=True):
    """Method check, session authentication, staff check and one error boundary for every endpoint."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = _error(405, "method_not_allowed", "Use %s." % " or ".join(methods))
                response["Allow"] = ", ".join(methods)
                return response
            if login_required and not request.user.is_authenticated:
                return _error(401, "not_authenticated", "Log in first (POST /api/session).")
            if staff and not request.user.is_staff:
                return _error(403, "forbidden", "This action is restricted to staff operators.")
            try:
                return view(request, *args, **kwargs)
            except ApiProblem as e:
                return _error(e.status_code, e.code, e.message, **e.extra)
            except ProviderError as e:
                extra = {"outcomeUnknown": e.outcome_unknown}
                if e.issue:
                    extra["paypalIssue"] = e.issue
                if e.debug_id:
                    extra["paypalDebugId"] = e.debug_id
                return _error(e.status_code, e.code, e.message, **extra)
            except ImproperlyConfigured as e:
                logger.error("PayPal is not configured: %s", e)
                return _error(503, "paypal_not_configured", "PayPal is not configured on this server.")

        return transaction.non_atomic_requests(wrapper)

    return decorator


def _body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise ApiProblem(400, "invalid_json", "The request body must be JSON.") from None
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_json", "The request body must be a JSON object.")
    return data


def _idempotency_key(request, data):
    return (request.headers.get("Idempotency-Key") or str(data.get("idempotencyKey") or "")).strip()


def answer(outcome, body, *, done_status=200, failed_status=409):
    """The one place a PayPal outcome becomes the caller's HTTP status."""
    status = {
        ProviderWrite.DONE: done_status,
        ProviderWrite.PENDING: 202,
        ProviderWrite.SENDING: 202,
        ProviderWrite.FAILED: failed_status,
        ProviderWrite.NEEDS_REVIEW: 409,
    }.get(outcome, 504)
    return JsonResponse({"outcome": outcome, **body}, status=status)


# =======
# Session
# =======


@ensure_csrf_cookie
@api(["GET", "POST", "DELETE"], login_required=False)
def session(request):
    if request.method == "POST":
        data = _body(request)
        identifier = str(data.get("username") or data.get("email") or "")
        user = authenticate(request, username=identifier, email=identifier, password=str(data.get("password", "")))
        if user is None:
            return _error(401, "invalid_credentials", "Unknown user or wrong password.")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
    user = request.user
    return JsonResponse(
        {
            "authenticated": user.is_authenticated,
            "userId": user.pk if user.is_authenticated else None,
            "email": user.email if user.is_authenticated else None,
            "isStaff": bool(user.is_authenticated and user.is_staff),
            "csrfToken": get_token(request),
        }
    )


# ======
# Orders
# ======


@api(["POST"])
def orders(request):
    order = services.place_order(request, request.user, _body(request))
    return JsonResponse({"orderId": str(order.number), "order": order_json(order)}, status=201)


@api(["GET"])
def my_orders(request):
    placed = services.Order.objects.filter(user=request.user).order_by("-date_placed")
    return JsonResponse({"orders": [order_json(o) for o in placed.prefetch_related("lines")]})


@api(["POST"])
def pay(request, order_id):
    outcome, payment, write = services.pay_order(request.user, order_id, _body(request))
    body = {"orderId": order_id, "order": order_json(payment.order)}
    if outcome != ProviderWrite.DONE:
        body["message"] = (
            payment.last_error if outcome == ProviderWrite.FAILED
            else "PayPal has not finished authorizing; repeat the request to check."
        )
    return answer(outcome, body, failed_status=402)


@api(["POST"], staff=True)
def fulfil(request, order_id):
    outcome, payment, write = services.fulfil_order(order_id)
    body = {"orderId": order_id, "order": order_json(payment.order)}
    if outcome != ProviderWrite.DONE:
        body["message"] = payment.last_error or "The capture is %s at PayPal; repeat the request to check." % outcome
    return answer(outcome, body)


@api(["POST"], staff=True)
def cancel(request, order_id):
    outcome, payment, write = services.cancel_order(order_id)
    body = {"orderId": order_id, "order": order_json(payment.order)}
    if outcome != ProviderWrite.DONE:
        body["message"] = payment.last_error or "Releasing the hold is %s at PayPal; repeat the request to check." % outcome
    return answer(outcome, body)


@api(["POST"])
def refunds(request, order_id):
    data = _body(request)
    outcome, refund, write = services.refund_order(request.user, order_id, _idempotency_key(request, data), data)
    refund.payment.refresh_from_db()
    body = {"refundId": str(refund.pk), "refund": refund_json(refund), "order": order_json(refund.payment.order)}
    if outcome != ProviderWrite.DONE and write is not None and write.detail:
        body["message"] = write.detail
    return answer(outcome, body, done_status=201)


@api(["GET"], staff=True)
def reconciliation(request):
    start = services.parse_instant(request.GET.get("from"), "from")
    end = services.parse_instant(request.GET.get("to"), "to")
    return JsonResponse(services.reconcile(start, end))


# ===============
# Payment methods
# ===============


@api(["GET", "POST"])
def payment_methods(request):
    if request.method == "GET":
        return JsonResponse({"paymentMethods": [card_json(c) for c in services.list_cards(request.user)]})
    data = _body(request)
    outcome, card, write = services.save_card(request.user, _idempotency_key(request, data), data)
    body = {"paymentMethodId": str(card.pk) if card else None}
    if card is not None:
        body["paymentMethod"] = card_json(card)
    if outcome != ProviderWrite.DONE:
        body["message"] = write.detail or "PayPal has not confirmed the saved card; repeat the request to check."
    return answer(outcome, body, done_status=201)


@api(["DELETE"])
def payment_method(request, payment_method_id):
    card, provider_deletion = services.delete_card(request.user, payment_method_id)
    return JsonResponse({"paymentMethodId": str(card.pk), "removed": True, "providerDeletion": provider_deletion})


# ============
# Presentation
# ============


def _money(value):
    return None if value is None else str(value)


def card_json(card):
    return {
        "paymentMethodId": str(card.pk),
        "brand": card.brand,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "createdAt": card.created_at.isoformat(),
    }


def refund_json(refund):
    return {
        "refundId": str(refund.pk),
        "amount": _money(refund.amount),
        "currency": refund.currency,
        "outcome": refund.outcome,
        "paypalRefundId": refund.refund_id or None,
        "paypalStatus": refund.status or None,
        "createdAt": refund.created_at.isoformat(),
    }


def payment_json(payment):
    if payment is None:
        return None
    return {
        "state": payment.state,
        "attempt": payment.attempt,
        "currency": payment.currency,
        "card": (
            {"brand": payment.card_brand, "lastDigits": payment.card_last_digits,
             "paymentMethodId": str(payment.saved_card_id) if payment.saved_card_id else None}
            if payment.card_last_digits else None
        ),
        "paypalOrderId": payment.paypal_order_id or None,
        "authorization": (
            {
                "id": payment.authorization_id,
                "status": payment.authorization_status,
                "amount": _money(payment.authorized_amount),
                "createdAt": payment.authorization_created_at.isoformat() if payment.authorization_created_at else None,
                "expiresAt": payment.authorization_expires_at.isoformat() if payment.authorization_expires_at else None,
                "reauthorized": payment.reauthorized,
            }
            if payment.authorization_id else None
        ),
        "capture": (
            {
                "id": payment.capture_id,
                "status": payment.capture_status,
                "amount": _money(payment.captured_amount),
                "paypalFee": _money(payment.paypal_fee),
                "netAmount": _money(payment.net_amount),
                "capturedAt": payment.captured_at.isoformat() if payment.captured_at else None,
            }
            if payment.capture_id else None
        ),
        "refundedAmount": _money(payment.refunded_amount),
        "refundableAmount": _money(
            (payment.captured_amount - payment.refunded_amount) if payment.captured_amount is not None else None
        ),
        "refunds": [refund_json(r) for r in payment.refunds.all()],
        "lastError": payment.last_error or None,
    }


def order_json(order):
    payment = OrderPayment.objects.filter(order=order).first()
    return {
        "orderId": str(order.number),
        "status": order.status,
        "currency": order.currency,
        "total": _money(order.total_incl_tax),
        "placedAt": order.date_placed.isoformat() if order.date_placed else None,
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _money(line.unit_price_incl_tax),
                "linePrice": _money(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": payment_json(payment),
    }
