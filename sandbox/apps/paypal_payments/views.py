"""
JSON API for orders, PayPal payments and saved cards.

Authentication is Django's session login (the same session the storefront
uses); the caller is always ``request.user``. CSRF protection stays on for
every unsafe method: fetch a token from ``GET /api/session`` and send it as
``X-CSRFToken``.
"""
import json
import logging
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from . import services
from .services import ServiceError

logger = logging.getLogger("apps.paypal_payments")

MAX_BODY_BYTES = 64 * 1024


def error_response(status, code, message, **extra):
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status)


def api_view(methods, staff=False, anonymous=False):
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not anonymous and not request.user.is_authenticated:
                return error_response(401, "not_authenticated", "Sign in first (POST /api/session).")
            if staff and not request.user.is_staff:
                return error_response(403, "forbidden", "This action is restricted to staff.")
            try:
                return view(request, *args, **kwargs)
            except ServiceError as exc:
                return error_response(exc.http_status, exc.code, exc.message, **exc.extra)
        # Claims must commit before PayPal is called, not at the end of the request,
        # so these views opt out of the sandbox's ATOMIC_REQUESTS.
        return transaction.non_atomic_requests(require_http_methods(methods)(wrapper))
    return decorator


def read_json(request):
    if len(request.body) > MAX_BODY_BYTES:
        raise ServiceError(413, "body_too_large", "Request body too large.")
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ServiceError(400, "invalid_json", "Request body must be JSON.")
    if not isinstance(data, dict):
        raise ServiceError(400, "invalid_json", "Request body must be a JSON object.")
    return data


def idempotency_key(request, data=None):
    key = request.headers.get("Idempotency-Key") or (data or {}).get("idempotencyKey")
    return str(key).strip() if key else None


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@ensure_csrf_cookie
@sensitive_post_parameters()
@sensitive_variables("data")
@api_view(["GET", "POST", "DELETE"], anonymous=True)
def session(request):
    if request.method == "POST":
        data = read_json(request)
        user = authenticate(request, username=str(data.get("username") or data.get("email") or ""),
                            password=str(data.get("password") or ""))
        if user is None or not user.is_active:
            return error_response(401, "invalid_credentials", "Invalid username/email or password.")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
    user = request.user
    return JsonResponse({
        "authenticated": user.is_authenticated,
        "user": {"id": user.id, "email": user.email, "isStaff": user.is_staff} if user.is_authenticated else None,
        "csrfToken": get_token(request),
    })


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


@api_view(["POST"])
def orders(request):
    data = read_json(request)
    order = services.place_order(request.user, data.get("items"))
    return JsonResponse(services.serialize_order(order), status=201)


@api_view(["GET"])
def my_orders(request):
    qs = services.Order.objects.filter(user=request.user).order_by("-date_placed").prefetch_related("lines")
    return JsonResponse({"orders": [services.serialize_order(o) for o in qs]})


@sensitive_post_parameters()
@sensitive_variables("data", "card")
@api_view(["POST"])
def pay(request, order_id):
    data = read_json(request)
    card = data.get("card")
    payment_method_id = data.get("paymentMethodId")
    payment = services.pay(request.user, order_id, card_data=card, payment_method_id=payment_method_id)
    order = payment.order
    order.refresh_from_db()
    body = services.serialize_order(order)
    # 200 only when PayPal confirmed the hold; anything not yet settled is 202.
    return JsonResponse(body, status=200 if payment.status in ("authorized", "captured") else 202)


@api_view(["POST"], staff=True)
def fulfil(request, order_id):
    payment, order = services.fulfil_order(order_id)
    order.refresh_from_db()
    return JsonResponse(services.serialize_order(order), status=200 if payment.capture_state == "done" else 202)


@api_view(["POST"], staff=True)
def cancel(request, order_id):
    _payment, order = services.cancel_order(order_id)
    order.refresh_from_db()
    return JsonResponse(services.serialize_order(order))


@api_view(["POST"])
def refunds(request, order_id):
    data = read_json(request)
    refund = services.refund(request.user, order_id, idempotency_key(request, data), data.get("amount"))
    body = services.serialize_refund(refund)
    body["order"] = services.serialize_order(refund.payment.order)
    status = {"done": 201, "failed": 402}.get(refund.status, 202)
    return JsonResponse(body, status=status)


@api_view(["GET"], staff=True)
def reconciliation(request):
    return JsonResponse(services.reconcile(request.GET.get("from"), request.GET.get("to")))


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------


@sensitive_post_parameters()
@sensitive_variables("data", "card")
@api_view(["GET", "POST"])
def payment_methods(request):
    if request.method == "GET":
        cards = services.list_cards(request.user)
        return JsonResponse({"paymentMethods": [services.serialize_card(c) for c in cards]})
    data = read_json(request)
    card = data.get("card", data)
    saved, created = services.save_card(request.user, card, idempotency_key(request, data))
    return JsonResponse(services.serialize_card(saved), status=201 if created else 200)


@api_view(["DELETE"])
def payment_method(request, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return HttpResponse(status=204)
