"""HTTP API for the PayPal flows.

Plain Django JSON views (no DRF in the project). Callers authenticate with
Django's own session; identity is ``request.user``. Shopper endpoints act only
on the caller's own data; ``fulfil``/``cancel``/``reconciliation`` require
``is_staff``. State-changing endpoints are CSRF-protected; ``GET /api/session``
bootstraps the CSRF cookie and ``POST /api/session/login`` establishes a
session.
"""
from __future__ import annotations

import functools
import json

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from . import services
from .exceptions import PaymentError


def _json(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params={"indent": 2})


def _error(message, status=400, code="error", detail=None):
    body = {"error": {"code": code, "message": message}}
    if detail:
        body["error"]["detail"] = detail
    return _json(body, status=status)


def _read_json(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise PaymentError("Request body must be valid JSON")
    if not isinstance(data, dict):
        raise PaymentError("Request body must be a JSON object")
    return data


def _handle_payment_errors(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PaymentError as exc:
            return _error(
                exc.message, status=exc.http_status, code=exc.code, detail=exc.detail
            )

    return wrapper


def require_shopper(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error("Authentication required", status=401, code="unauthenticated")
        return view(request, *args, **kwargs)

    return wrapper


def require_staff(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error("Authentication required", status=401, code="unauthenticated")
        if not request.user.is_staff:
            return _error("Operator (staff) access required", status=403, code="forbidden")
        return view(request, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# session bootstrap
# ---------------------------------------------------------------------------
@ensure_csrf_cookie
@require_http_methods(["GET"])
def session_view(request):
    user = request.user
    return _json(
        {
            "authenticated": user.is_authenticated,
            "isStaff": bool(user.is_authenticated and user.is_staff),
            "username": user.get_username() if user.is_authenticated else None,
        }
    )


@require_http_methods(["POST"])
@_handle_payment_errors
def login_view(request):
    data = _read_json(request)
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return _error("username and password are required", status=400)
    user = authenticate(request, username=username, password=password)
    if user is None:
        return _error("Invalid credentials", status=401, code="invalid_credentials")
    login(request, user)
    return _json({"authenticated": True, "isStaff": bool(user.is_staff), "username": user.get_username()})


@require_http_methods(["POST"])
def logout_view(request):
    logout(request)
    return _json({"authenticated": False})


# ---------------------------------------------------------------------------
# Flow 1 — orders & payments
# ---------------------------------------------------------------------------
@require_http_methods(["POST"])
@require_shopper
@_handle_payment_errors
def orders_view(request):
    data = _read_json(request)
    items = data.get("items")
    if not isinstance(items, list):
        return _error("'items' must be a list of {productId, quantity}", status=400)
    payment = services.place_order(request.user, items)
    body = payment.describe()
    body["orderId"] = payment.order_id  # top-level identifier
    return _json(body, status=201)


@require_http_methods(["POST"])
@require_shopper
@_handle_payment_errors
def order_pay_view(request, order_id):
    data = _read_json(request)
    card = _extract_card(data)
    saved_card_id = data.get("savedCardId") or data.get("paymentMethodId")
    payment = services.pay(
        order_id, user=request.user, card=card, saved_card_id=saved_card_id
    )
    return _json(payment.describe())


@require_http_methods(["POST"])
@require_staff
@_handle_payment_errors
def order_fulfil_view(request, order_id):
    payment = services.fulfil(order_id)
    return _json(payment.describe())


@require_http_methods(["POST"])
@require_staff
@_handle_payment_errors
def order_cancel_view(request, order_id):
    payment = services.cancel(order_id)
    return _json(payment.describe())


@require_http_methods(["POST"])
@require_shopper
@_handle_payment_errors
def order_refunds_view(request, order_id):
    data = _read_json(request)
    idempotency_key = data.get("idempotencyKey") or data.get("idempotency_key")
    if not idempotency_key:
        return _error("An idempotencyKey is required", status=400, code="missing_idempotency_key")
    payment, refund_row = services.refund(
        order_id,
        user=request.user,
        amount=data.get("amount"),
        idempotency_key=idempotency_key,
    )
    body = refund_row.describe()
    body["refundId"] = refund_row.id  # top-level identifier
    body["payment"] = payment.describe()
    return _json(body, status=201)


@require_http_methods(["GET"])
@require_shopper
@_handle_payment_errors
def my_orders_view(request):
    return _json({"orders": services.my_orders(request.user)})


@require_http_methods(["GET"])
@require_staff
@_handle_payment_errors
def reconciliation_view(request):
    from_dt = request.GET.get("from")
    to_dt = request.GET.get("to")
    if not from_dt or not to_dt:
        return _error("'from' and 'to' query parameters are required (ISO-8601)", status=400)
    report = services.reconciliation(from_dt, to_dt)
    return _json(report)


# ---------------------------------------------------------------------------
# Flow 2 — saved cards
# ---------------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
@require_shopper
@_handle_payment_errors
def payment_methods_view(request):
    if request.method == "GET":
        return _json({"paymentMethods": services.list_saved_cards(request.user)})
    data = _read_json(request)
    card = _extract_card(data)
    if card is None:
        return _error("card details are required", status=400, code="missing_card")
    saved = services.save_card(request.user, card=card)
    body = saved.describe()  # already contains paymentMethodId
    return _json(body, status=201)


@require_http_methods(["DELETE"])
@require_shopper
@_handle_payment_errors
def payment_method_detail_view(request, payment_method_id):
    services.delete_saved_card(payment_method_id, user=request.user)
    return _json({"deleted": True, "paymentMethodId": int(payment_method_id)})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _extract_card(data):
    """Build a PayPal card dict from request JSON, or None if absent.

    Full card details are used transiently and never stored or logged.
    """
    card = data.get("card")
    if not isinstance(card, dict):
        return None
    number = card.get("number")
    expiry = card.get("expiry")
    if not number or not expiry:
        return None
    out = {"number": str(number), "expiry": str(expiry)}
    if card.get("securityCode") or card.get("security_code") or card.get("cvv"):
        out["security_code"] = str(
            card.get("securityCode") or card.get("security_code") or card.get("cvv")
        )
    if card.get("name"):
        out["name"] = str(card["name"])
    billing = card.get("billingAddress") or card.get("billing_address")
    if isinstance(billing, dict):
        out["billing_address"] = _billing_address(billing)
    return out


def _billing_address(billing):
    mapping = {
        "addressLine1": "address_line_1",
        "address_line_1": "address_line_1",
        "addressLine2": "address_line_2",
        "address_line_2": "address_line_2",
        "adminArea1": "admin_area_1",
        "admin_area_1": "admin_area_1",
        "state": "admin_area_1",
        "adminArea2": "admin_area_2",
        "admin_area_2": "admin_area_2",
        "city": "admin_area_2",
        "postalCode": "postal_code",
        "postal_code": "postal_code",
        "countryCode": "country_code",
        "country_code": "country_code",
    }
    out = {}
    for key, value in billing.items():
        target = mapping.get(key)
        if target and value:
            out[target] = str(value)
    return out
