"""HTTP layer for the checkout API.

Plain Django JSON views (no extra dependencies). Callers authenticate with the
sandbox's own Django session login; the caller's identity is taken from
``request.user``. Operator actions (fulfil, cancel, reconciliation) require
``is_staff``; every other endpoint acts only on the caller's own data.
"""

import datetime
import functools
import json
import logging

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from . import errors, services

logger = logging.getLogger("api.views")


def _error(status, message, **extra):
    payload = {"error": message}
    payload.update(extra)
    return JsonResponse(payload, status=status)


def endpoint(*, methods, staff=False):
    """Wrap a view with method/auth checks and uniform error handling."""

    def decorator(func):
        @functools.wraps(func)
        @csrf_exempt
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return _error(405, "Method not allowed", allowed=methods)
            if not request.user.is_authenticated:
                return _error(401, "Authentication required")
            if staff and not request.user.is_staff:
                return _error(403, "Operator (staff) access required")
            try:
                result = func(request, *args, **kwargs)
            except errors.ApiError as exc:
                body = {"error": exc.message}
                if exc.code:
                    body["code"] = exc.code
                if exc.outcome_unknown:
                    body["outcomeUnknown"] = True
                if exc.extra:
                    body.update(exc.extra)
                return JsonResponse(body, status=exc.status_code)
            except Exception:  # noqa: BLE001
                logger.exception("Unhandled error in %s", func.__name__)
                return _error(500, "Internal error")
            if isinstance(result, JsonResponse):
                return result
            data, status = result if isinstance(result, tuple) else (result, 200)
            return JsonResponse(data, status=status, safe=not isinstance(data, list))

        return wrapper

    return decorator


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise errors.BadRequest("Request body must be valid JSON")
    if not isinstance(data, dict):
        raise errors.BadRequest("Request body must be a JSON object")
    return data


# ---------------------------------------------------------------------------
# Session (Django's own session login, exposed as JSON so the API is drivable on
# its own without rendering the storefront's login page)
# ---------------------------------------------------------------------------


@csrf_exempt
def session(request):
    if request.method == "POST":
        data = _json_body(request)
        identifier = data.get("username") or data.get("email")
        password = data.get("password")
        if not identifier or not password:
            return _error(400, "username (or email) and password are required")
        # Uses the sandbox's configured auth backends (username or email + password).
        user = authenticate(request, username=identifier, password=password)
        if user is None:
            return _error(401, "Invalid credentials")
        login(request, user)
        return JsonResponse(
            {"username": user.get_username(), "isStaff": user.is_staff}, status=200
        )
    if request.method == "DELETE":
        logout(request)
        return JsonResponse({"loggedOut": True}, status=200)
    return _error(405, "Method not allowed", allowed=["POST", "DELETE"])


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@endpoint(methods=["POST"])
def create_order(request):
    data = _json_body(request)
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise errors.BadRequest("items must be a non-empty list")
    order = services.create_order(request.user, items, request)
    return {"orderId": order.number, "amount": str(order.total_incl_tax),
            "currency": order.currency}, 201


@endpoint(methods=["POST"])
def pay_order(request, order_number):
    data = _json_body(request)
    card = data.get("card")
    payment_method_id = data.get("paymentMethodId")
    payment = services.authorize_order(
        request.user, order_number, card=card, payment_method_id=payment_method_id
    )
    return services.serialize_payment(payment)


@endpoint(methods=["POST"], staff=True)
def fulfil_order(request, order_number):
    payment = services.fulfil_order(order_number)
    return services.serialize_payment(payment)


@endpoint(methods=["POST"], staff=True)
def cancel_order(request, order_number):
    payment = services.cancel_order(order_number)
    return services.serialize_payment(payment)


@endpoint(methods=["POST"])
def refund_order(request, order_number):
    data = _json_body(request)
    refund = services.refund_order(
        request.user, order_number, data.get("amount"), data.get("idempotencyKey")
    )
    payment = refund.payment
    return {
        "refundId": refund.refund_id,
        "status": refund.status,
        "amount": str(refund.amount),
        "currency": refund.currency,
        "orderId": order_number,
        "payment": services.serialize_payment(payment),
    }, 201


@endpoint(methods=["GET"])
def my_orders(request):
    return services.list_orders(request.user)


@endpoint(methods=["GET"], staff=True)
def reconciliation(request):
    from_raw = request.GET.get("from")
    to_raw = request.GET.get("to")
    if not from_raw or not to_raw:
        raise errors.BadRequest("from and to query parameters are required (ISO-8601)")
    # A '+' in a timezone offset (e.g. +00:00) arrives decoded as a space when the
    # query string was not percent-encoded; ISO-8601 has no real spaces, so restore it.
    from_dt = parse_datetime(from_raw) or parse_datetime(from_raw.replace(" ", "+"))
    to_dt = parse_datetime(to_raw) or parse_datetime(to_raw.replace(" ", "+"))
    if from_dt is None or to_dt is None:
        raise errors.BadRequest("from and to must be ISO-8601 date-times")
    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=datetime.timezone.utc)
    if to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=datetime.timezone.utc)
    if to_dt <= from_dt:
        raise errors.BadRequest("to must be after from")
    return services.reconcile(from_dt, to_dt)


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


@endpoint(methods=["POST", "GET"])
def payment_methods(request):
    if request.method == "GET":
        return services.list_cards(request.user)
    data = _json_body(request)
    card = data.get("card")
    if card is None:
        raise errors.BadRequest("card is required")
    bankcard = services.save_card(request.user, card)
    return {"paymentMethodId": bankcard.id, **services.serialize_card(bankcard)}, 201


@endpoint(methods=["DELETE"])
def payment_method_detail(request, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return {"deleted": True, "paymentMethodId": int(payment_method_id)}
