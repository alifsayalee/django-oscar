"""
JSON endpoints under /api/.

Callers authenticate with Django's own session login (the sandbox's normal
login, or POST /api/auth/login) and send the CSRF token on every unsafe
request. Shopper endpoints act only on the caller's own data; fulfil, cancel
and reconciliation are for staff (``is_staff``) only.
"""

import functools
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from . import services
from .errors import PaymentError
from .models import SavedCard
from .reconciliation import reconcile

logger = logging.getLogger(__name__)

# Upper bound on one reconciliation request (PayPal is queried in 31-day windows).
MAX_RECONCILIATION_RANGE = timedelta(days=366)


def _error(status, code, message, **extra):
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status)


def api(*methods, staff=False, login_required=True):
    """
    Wrap a view as a JSON endpoint: method check, authentication, JSON body
    parsing and PaymentError -> JSON error response. Runs outside the
    per-request transaction (ATOMIC_REQUESTS) so that each payment claim is
    committed before PayPal is called.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            refusal = _preflight(request, methods, staff, login_required)
            if refusal is not None:
                return refusal
            try:
                return view(request, *args, **kwargs)
            except PaymentError as e:
                return JsonResponse({"error": e.as_dict()}, status=e.status_code)

        return transaction.non_atomic_requests(wrapper)

    return decorator


def _preflight(request, methods, staff, login_required):
    """Method, authentication and body checks; returns an error response or None."""
    if request.method not in methods:
        response = _error(405, "method_not_allowed", f"Use {', '.join(methods)}.")
        response["Allow"] = ", ".join(methods)
        return response
    if login_required and not request.user.is_authenticated:
        return _error(401, "not_authenticated", "Log in first (Django session).")
    if staff and not request.user.is_staff:
        return _error(403, "forbidden", "This action is restricted to staff.")
    request.json = {}
    if request.method in ("POST", "PUT", "PATCH") and request.body:
        try:
            request.json = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            return _error(400, "invalid_json", "The request body must be JSON.")
        if not isinstance(request.json, dict):
            return _error(400, "invalid_json", "The request body must be a JSON object.")
    return None


def _idempotency_key(request):
    key = request.headers.get("Idempotency-Key", "").strip()
    if len(key) > 200:
        raise PaymentError(400, "invalid_idempotency_key", "Idempotency-Key must be at most 200 characters.")
    return key or None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


@ensure_csrf_cookie
@api("GET", login_required=False)
def csrf(request):
    return JsonResponse({"csrfToken": get_token(request)})


@api("POST", login_required=False)
@sensitive_post_parameters()
@sensitive_variables("password")
def session_login(request):
    username = request.json.get("username") or request.json.get("email")
    password = request.json.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return _error(400, "invalid_request", "Send username (or email) and password.")
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return _error(401, "invalid_credentials", "Invalid username or password.")
    login(request, user)
    return JsonResponse({"userId": user.pk, "username": user.get_username(), "isStaff": user.is_staff,
                         "csrfToken": get_token(request)})


@api("POST", login_required=False)
def session_logout(request):
    logout(request)
    return JsonResponse({"loggedOut": True})


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@api("POST")
def orders(request):
    order = services.place_order(
        request.user, request.json.get("items"), idempotency_key=_idempotency_key(request), request=request
    )
    return JsonResponse(services.order_to_dict(order), status=201)


@api("GET")
def order_detail(request, order_id):
    return JsonResponse(services.order_to_dict(services.shopper_order(request.user, order_id)))


@api("GET")
def my_orders(request):
    qs = (
        services.Order.objects.filter(user=request.user)
        .select_related("paypal_payment")
        .prefetch_related("lines", "paypal_payment__refunds")
        .order_by("-date_placed")
    )
    return JsonResponse({"orders": [services.order_to_dict(o) for o in qs]})


@api("POST")
@sensitive_variables("body", "card")
def pay(request, order_id):
    body = request.json
    card = services.parse_card(body["card"]) if "card" in body else None
    payment_method_id = body.get("paymentMethodId")
    if payment_method_id is not None:
        try:
            payment_method_id = uuid.UUID(str(payment_method_id))
        except ValueError:
            raise PaymentError(404, "payment_method_not_found", "No such saved payment method.") from None
    order = services.pay_order(request.user, order_id, card=card, payment_method_id=payment_method_id)
    return JsonResponse(services.order_to_dict(order))


@api("POST", staff=True)
def fulfil(request, order_id):
    return JsonResponse(services.order_to_dict(services.fulfil_order(request.user, order_id)))


@api("POST", staff=True)
def cancel(request, order_id):
    return JsonResponse(services.order_to_dict(services.cancel_order(request.user, order_id)))


@api("POST")
def refunds(request, order_id):
    refund, created = services.refund_order(
        request.user,
        order_id,
        idempotency_key=_idempotency_key(request),
        amount=request.json.get("amount"),
        note=request.json.get("note"),
    )
    body = services.refund_to_dict(refund)
    body["order"] = services.order_to_dict(refund.payment.order)
    return JsonResponse(body, status=201 if created else 200)


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


@api("GET", "POST")
@sensitive_variables("card")
def payment_methods(request):
    if request.method == "GET":
        cards = SavedCard.objects.active().filter(user=request.user)
        return JsonResponse({"paymentMethods": [services.card_to_dict(c) for c in cards]})
    if "card" not in request.json:
        raise PaymentError(400, "invalid_request", "Send the card to save as {\"card\": {...}}.")
    card = services.parse_card(request.json["card"])
    saved, created = services.save_card(
        request.user, card, idempotency_key=_idempotency_key(request) or str(uuid.uuid4())
    )
    return JsonResponse(services.card_to_dict(saved), status=201 if created else 200)


@api("DELETE")
def payment_method_detail(request, payment_method_id):
    vault_removed = services.delete_card(request.user, payment_method_id)
    return JsonResponse({"paymentMethodId": str(payment_method_id), "deleted": True,
                         "removedFromPayPalVault": vault_removed})


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _parse_moment(raw, name):
    if not raw:
        raise PaymentError(400, "invalid_request", f"'{name}' is required (ISO-8601 date-time).")
    try:
        moment = datetime.fromisoformat(raw.replace(" ", "+"))
    except ValueError:
        raise PaymentError(400, "invalid_request", f"'{name}' must be an ISO-8601 date-time.") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@api("GET", staff=True)
def reconciliation(request):
    start = _parse_moment(request.GET.get("from"), "from")
    end = _parse_moment(request.GET.get("to"), "to")
    if start >= end:
        raise PaymentError(400, "invalid_request", "'from' must be before 'to'.")
    if end - start > MAX_RECONCILIATION_RANGE:
        raise PaymentError(400, "invalid_request", "The range may span at most 366 days.")
    return JsonResponse(reconcile(start, end))
