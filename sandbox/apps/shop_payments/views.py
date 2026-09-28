"""
JSON endpoints under /api/.

Callers authenticate with Django's session login (``POST /api/login`` or the
storefront's own sign-in) and send the CSRF token on unsafe methods.  Shopper
endpoints act only on the caller's own orders and cards (anything else is a 404);
fulfil, cancel and reconciliation require ``is_staff``.
"""
from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.cache import never_cache
from oscar.core.loading import get_model

from . import cards, orders, payments, reconciliation
from .errors import ApiProblem
from .models import PayPalRefund

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")

View = Callable[..., HttpResponse]


def problem_response(problem: ApiProblem, extra: dict[str, Any] | None = None) -> JsonResponse:
    body: dict[str, Any] = {
        "error": {"code": problem.code, "message": problem.message, **problem.details},
        "outcomeUnknown": problem.outcome_unknown,
    }
    if extra:
        body.update(extra)
    return JsonResponse(body, status=problem.status_code)


def api(methods: tuple[str, ...], *, staff: bool = False, public: bool = False) -> Callable[[View], View]:
    """Method check, session auth, staff check, JSON body parsing and the error boundary."""

    def decorator(view: View) -> View:
        @functools.wraps(view)
        @never_cache
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if request.method not in methods:
                return problem_response(ApiProblem(405, "method_not_allowed", f"Use {', '.join(methods)}."))
            if not public and not request.user.is_authenticated:
                return problem_response(ApiProblem(401, "not_authenticated", "Sign in first (POST /api/login)."))
            if staff and not request.user.is_staff:
                return problem_response(ApiProblem(403, "forbidden", "This action is for staff operators only."))
            payload: dict[str, Any] = {}
            if request.method in ("POST", "PUT", "PATCH") and request.body:
                try:
                    parsed = json.loads(request.body)
                except ValueError:
                    return problem_response(ApiProblem(400, "invalid_json", "The request body must be JSON."))
                if not isinstance(parsed, dict):
                    return problem_response(ApiProblem(400, "invalid_json", "The request body must be a JSON object."))
                payload = parsed
            try:
                return view(request, payload, *args, **kwargs)
            except ApiProblem as problem:
                return problem_response(problem)
            except ImproperlyConfigured as exc:
                logger.error("Payments are not configured: %s", exc)
                return problem_response(ApiProblem(503, "not_configured", "Payments are not configured."))
            except Exception as exc:
                # Exception text can echo request input (card data): log the type only.
                logger.error("Unhandled %s in %s", type(exc).__name__, view.__name__)
                return problem_response(ApiProblem(500, "internal_error", "Something went wrong."))

        # The payment services commit each claim before calling PayPal and use their
        # own transactions; a request-wide transaction (ATOMIC_REQUESTS) would hide
        # the claim from other requests and could roll back a record of what PayPal did.
        return transaction.non_atomic_requests(wrapper)

    return decorator


def _own_order(request: HttpRequest, order_id: str) -> Any:
    order = Order.objects.filter(number=order_id, user=request.user).first()
    if order is None:
        raise ApiProblem(404, "order_not_found", "No such order.")
    return order


def _any_order(order_id: str) -> Any:
    order = Order.objects.filter(number=order_id).first()
    if order is None:
        raise ApiProblem(404, "order_not_found", "No such order.")
    return order


def _order_body(order: Any, **extra: Any) -> dict[str, Any]:
    order.refresh_from_db()
    return {"orderId": order.number, **extra, "order": orders.serialize_order(order)}


def _with_order(order: Any, call: Callable[[], Any], status: int = 200) -> JsonResponse:
    """Run a payment action, answering with the order's current state either way."""
    try:
        call()
    except ApiProblem as problem:
        return problem_response(problem, _order_body(order))
    return JsonResponse(_order_body(order), status=status)


# --- session -----------------------------------------------------------------------------------


@api(("GET",), public=True)
def csrf(request: HttpRequest, payload: dict[str, Any]) -> HttpResponse:
    return JsonResponse({"csrfToken": get_token(request)})


@api(("POST",), public=True)
def login_view(request: HttpRequest, payload: dict[str, Any]) -> HttpResponse:
    username = payload.get("username") or payload.get("email")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ApiProblem(400, "invalid_credentials", "Send username (or email) and password.")
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        raise ApiProblem(401, "invalid_credentials", "Wrong username or password.")
    login(request, user)
    return JsonResponse({"user": _user(user), "csrfToken": get_token(request)})


@api(("POST",))
def logout_view(request: HttpRequest, payload: dict[str, Any]) -> HttpResponse:
    logout(request)
    return HttpResponse(status=204)


@api(("GET",))
def me(request: HttpRequest, payload: dict[str, Any]) -> HttpResponse:
    return JsonResponse({"user": _user(request.user)})


def _user(user: Any) -> dict[str, Any]:
    return {"id": user.pk, "username": user.get_username(), "email": user.email, "isStaff": user.is_staff}


# --- orders ------------------------------------------------------------------------------------


@api(("POST",))
def create_order(request: HttpRequest, payload: dict[str, Any]) -> HttpResponse:
    order = orders.place_order(request, payload)
    return JsonResponse(_order_body(order), status=201)


@api(("GET",))
def my_orders(request: HttpRequest, payload: dict[str, Any]) -> HttpResponse:
    mine = (
        Order.objects.filter(user=request.user)
        .select_related("paypal_payment")
        .prefetch_related("lines")
        .order_by("-date_placed")
    )
    return JsonResponse({"orders": [orders.serialize_order(o) for o in mine]})


@api(("POST",))
def pay(request: HttpRequest, payload: dict[str, Any], order_id: str) -> HttpResponse:
    order = _own_order(request, order_id)
    return _with_order(order, lambda: payments.pay(order, request.user, payload))


@api(("POST",), staff=True)
def fulfil(request: HttpRequest, payload: dict[str, Any], order_id: str) -> HttpResponse:
    order = _any_order(order_id)
    return _with_order(order, lambda: payments.fulfil(order, request.user))


@api(("POST",), staff=True)
def cancel(request: HttpRequest, payload: dict[str, Any], order_id: str) -> HttpResponse:
    order = _any_order(order_id)
    return _with_order(order, lambda: payments.cancel(order, request.user))


@api(("POST",))
def refunds(request: HttpRequest, payload: dict[str, Any], order_id: str) -> HttpResponse:
    order = _own_order(request, order_id)
    key = request.headers.get("Idempotency-Key") or payload.get("idempotencyKey")
    key = key.strip() if isinstance(key, str) else ""
    try:
        record, created = payments.refund(order, request.user, payload, key)
    except ApiProblem as problem:
        extra = _order_body(order)
        refund = PayPalRefund.objects.filter(payment__order=order, idempotency_key=key).first() if key else None
        if refund is not None:
            extra["refundId"] = str(refund.public_id)
        return problem_response(problem, extra)
    return JsonResponse(
        _order_body(order, refundId=str(record.public_id), refund={
            "refundId": str(record.public_id),
            "amount": str(record.amount),
            "currency": record.currency,
            "status": record.outcome,
            "paypalRefundId": record.paypal_refund_id or None,
            "paypalStatus": record.paypal_status or None,
        }),
        status=201 if created else 200,
    )


# --- saved cards ---------------------------------------------------------------------------------


@api(("GET", "POST"))
def payment_methods(request: HttpRequest, payload: dict[str, Any]) -> HttpResponse:
    if request.method == "GET":
        mine = cards.usable_cards(request.user).order_by("pk")
        return JsonResponse({"paymentMethods": [cards.serialize_card(c) for c in mine]})
    card, created = cards.save_card(request.user, payload, request.headers.get("Idempotency-Key"))
    return JsonResponse(cards.serialize_card(card), status=201 if created else 200)


@api(("DELETE",))
def payment_method(request: HttpRequest, payload: dict[str, Any], payment_method_id: str) -> HttpResponse:
    cards.delete_card(request.user, payment_method_id)
    return HttpResponse(status=204)


# --- reconciliation ------------------------------------------------------------------------------


@api(("GET",), staff=True)
def reconciliation_report(request: HttpRequest, payload: dict[str, Any]) -> HttpResponse:
    start = reconciliation.parse_instant(request.GET.get("from"), "from")
    end = reconciliation.parse_instant(request.GET.get("to"), "to")
    return JsonResponse(reconciliation.reconcile(start, end))
