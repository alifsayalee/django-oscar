"""
JSON endpoints under /api/.

Callers authenticate with Django's session login (the sandbox's own
mechanism); ``/api/session`` offers the same login for API clients. Unsafe
methods need the CSRF token (cookie ``csrftoken`` -> header ``X-CSRFToken``).

Views are non-atomic on purpose: a claim must be committed before PayPal is
called, so each service manages its own transactions.
"""

import json
import logging
import uuid
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import reconciliation, services
from .errors import ApiProblem
from .gateway import PayPalError
from .models import OrderPayment, SavedCard

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")

View = Callable[..., HttpResponse]

MAX_BODY_BYTES = 64 * 1024


def _problem(problem: ApiProblem) -> JsonResponse:
    return JsonResponse(problem.as_dict(), status=problem.status_code)


def api_view(*, methods: list[str], staff: bool = False, login_required: bool = True) -> Callable[[View], View]:
    """JSON error handling, method check, authentication and staff checks for one endpoint."""

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if login_required and not request.user.is_authenticated:
                return _problem(ApiProblem(401, "not_authenticated", "Sign in first (POST /api/session)."))
            if staff and not request.user.is_staff:
                return _problem(ApiProblem(403, "forbidden", "This action is restricted to staff."))
            try:
                return view(request, *args, **kwargs)
            except ApiProblem as problem:
                return _problem(problem)
            except PayPalError as e:
                return JsonResponse({"error": "paypal_error", **e.as_dict()}, status=e.status_code)
            except ImproperlyConfigured as e:
                logger.error("PayPal integration is misconfigured: %s", e)
                return _problem(ApiProblem(503, "not_configured", "Payments are not configured on this server."))

        return transaction.non_atomic_requests(require_http_methods(methods)(wrapper))

    return decorator


def _json_body(request: HttpRequest) -> dict[str, Any]:
    if len(request.body) > MAX_BODY_BYTES:
        raise ApiProblem(413, "body_too_large", "Request body too large.")
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ApiProblem(400, "invalid_json", "The request body must be JSON.") from None
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_json", "The request body must be a JSON object.")
    return data


def _shopper(request: HttpRequest) -> User:
    user = request.user
    if isinstance(user, AnonymousUser):
        raise ApiProblem(401, "not_authenticated", "Sign in first (POST /api/session).")
    return user


def _user_dict(user: Any) -> dict[str, Any]:
    return {"id": user.pk, "email": user.email, "username": user.get_username(), "isStaff": user.is_staff}


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@ensure_csrf_cookie
@api_view(methods=["GET", "POST", "DELETE"], login_required=False)
@sensitive_variables("data", "password")
def session(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        data = _json_body(request)
        username, password = data.get("username") or data.get("email"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ApiProblem(400, "invalid_request", "Send 'username' (or 'email') and 'password'.")
        user = authenticate(request, username=username, password=password)
        if user is None:
            raise ApiProblem(401, "invalid_credentials", "Unknown user or wrong password.")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
    body: dict[str, Any] = {"authenticated": request.user.is_authenticated, "csrfToken": get_token(request)}
    if request.user.is_authenticated:
        body["user"] = _user_dict(request.user)
    return JsonResponse(body)


# ---------------------------------------------------------------------------
# Flow 1 - orders and payments
# ---------------------------------------------------------------------------


@api_view(methods=["POST"])
def orders(request: HttpRequest) -> HttpResponse:
    payment = services.place_order(_shopper(request), _json_body(request))
    return JsonResponse(services.order_dict(payment.order, payment), status=201)


@api_view(methods=["POST"])
@sensitive_variables("payload")
def pay(request: HttpRequest, order_id: str) -> HttpResponse:
    payload = _json_body(request)
    payment, created = services.authorize(_shopper(request), order_id, payload)
    body = services.order_dict(payment.order, payment)
    if payment.state == OrderPayment.AUTHORIZED:
        return JsonResponse(body, status=201 if created else 200)
    # Accepted, but not (yet) an authorization: pending, needs review, unknown.
    return JsonResponse(body, status=202)


@api_view(methods=["POST"], staff=True)
def fulfil(request: HttpRequest, order_id: str) -> HttpResponse:
    payment, _ = services.fulfil(order_id)
    body = services.order_dict(payment.order, payment)
    return JsonResponse(body, status=200 if payment.state in OrderPayment.CAPTURED_STATES else 202)


@api_view(methods=["POST"], staff=True)
def cancel(request: HttpRequest, order_id: str) -> HttpResponse:
    payment, _ = services.cancel(order_id)
    return JsonResponse(services.order_dict(payment.order, payment))


@api_view(methods=["POST"])
def refunds(request: HttpRequest, order_id: str) -> HttpResponse:
    data = _json_body(request)
    key = services.parse_idempotency_key(request.headers.get("Idempotency-Key") or data.get("idempotencyKey"))
    record, created = services.refund(_shopper(request), order_id, data.get("amount"), key)
    payment = OrderPayment.objects.select_related("order").get(pk=record.order_payment_id)
    body = {**services.refund_dict(record), "order": services.order_dict(payment.order, payment)}
    if record.status == "done":
        status = 201 if created else 200
    elif record.status == "failed":
        status = 200 if not created else 422
    else:
        status = 202
    return JsonResponse(body, status=status)


@api_view(methods=["GET"])
def my_orders(request: HttpRequest) -> HttpResponse:
    queryset = (
        Order.objects.filter(user=_shopper(request))
        .select_related("paypal_payment", "paypal_payment__saved_card")
        .prefetch_related("lines", "paypal_payment__refunds")
        .order_by("-date_placed")[:100]
    )
    items = [
        services.order_dict(order, getattr(order, "paypal_payment", None)) for order in queryset
    ]
    return JsonResponse({"orders": items})


@api_view(methods=["GET"], staff=True)
def reconciliation_report(request: HttpRequest) -> HttpResponse:
    start = reconciliation.parse_instant(request.GET.get("from"), "from")
    end = reconciliation.parse_instant(request.GET.get("to"), "to")
    return JsonResponse(reconciliation.build_report(start, end))


# ---------------------------------------------------------------------------
# Flow 2 - saved cards
# ---------------------------------------------------------------------------


@api_view(methods=["GET", "POST"])
@sensitive_variables("payload")
def payment_methods(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        payload = _json_body(request)
        key = request.headers.get("Idempotency-Key") or payload.get("idempotencyKey")
        idempotency_key = services.parse_idempotency_key(key) if key is not None else None
        card, created = services.save_card(_shopper(request), payload, idempotency_key)
        return JsonResponse(services.card_dict(card), status=201 if created else 200)
    cards = SavedCard.objects.filter(
        user=_shopper(request), state__in=(SavedCard.ACTIVE, SavedCard.DELETING)
    )
    return JsonResponse({"paymentMethods": [services.card_dict(card) for card in cards]})


@api_view(methods=["DELETE"])
def payment_method(request: HttpRequest, payment_method_id: uuid.UUID) -> HttpResponse:
    services.delete_card(_shopper(request), payment_method_id)
    return HttpResponse(status=204)
