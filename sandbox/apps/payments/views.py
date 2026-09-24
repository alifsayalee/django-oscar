"""
JSON endpoints of the payments API.

Callers authenticate with Django's session login (the sandbox's own login
page, or ``POST /api/auth/login``); CSRF protection applies to every write.
Views run outside ``ATOMIC_REQUESTS`` so that each payment claim is committed
before PayPal is called.
"""

import json
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured, RequestDataTooBig
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables

from . import presenters, reconciliation, services
from .errors import BadRequest, PaymentError

logger = logging.getLogger("apps.payments")

View = Callable[..., HttpResponse]


def _error(status: int, code: str, message: str) -> JsonResponse:
    return JsonResponse({"error": code, "message": message}, status=status)


def api_view(*methods: str, staff: bool = False, login_required: bool = True) -> Callable[[View], View]:
    def decorate(fn: View) -> View:
        @wraps(fn)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if request.method not in methods:
                response = _error(405, "method_not_allowed", f"Use {' or '.join(methods)}.")
                response["Allow"] = ", ".join(methods)
                return response
            if login_required and not request.user.is_authenticated:
                return _error(401, "not_authenticated", "Sign in first (POST /api/auth/login).")
            if staff and not request.user.is_staff:
                return _error(403, "forbidden", "This action is restricted to staff operators.")
            try:
                return fn(request, *args, **kwargs)
            except PaymentError as exc:
                return JsonResponse(exc.as_dict(), status=exc.status_code)
            except ImproperlyConfigured as exc:
                logger.error("Payments API misconfigured: %s", exc)
                return _error(503, "not_configured", "Payments are not configured on this site.")
            except Exception as exc:
                # Log the type only: request bodies here can carry card data.
                logger.exception("Unhandled %s in %s", type(exc).__name__, fn.__name__)
                return _error(500, "internal_error", "Something went wrong; the error was logged.")

        return transaction.non_atomic_requests(wrapper)

    return decorate


@sensitive_variables("raw")
def _json_body(request: HttpRequest) -> dict[str, Any]:
    try:
        raw = request.body
    except RequestDataTooBig:
        raise BadRequest("The request body is too large.") from None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("The request body must be JSON.") from None
    if not isinstance(data, dict):
        raise BadRequest("The request body must be a JSON object.")
    return data


def _user(request: HttpRequest) -> dict[str, Any]:
    user: Any = request.user
    return {"id": user.pk, "username": user.get_username(), "email": user.email, "isStaff": user.is_staff}


# --- session ------------------------------------------------------------------


@ensure_csrf_cookie
@api_view("GET", login_required=False)
def csrf(request: HttpRequest) -> HttpResponse:
    body: dict[str, Any] = {"csrfToken": get_token(request)}
    if request.user.is_authenticated:
        body["user"] = _user(request)
    return JsonResponse(body)


@api_view("POST", login_required=False)
@sensitive_variables("data", "password")
def login_view(request: HttpRequest) -> HttpResponse:
    data = _json_body(request)
    username, password = data.get("username") or data.get("email"), data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise BadRequest('Send "username" (or "email") and "password".')
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return _error(401, "invalid_credentials", "The username or password is incorrect.")
    login(request, user)
    return JsonResponse({"user": _user(request), "csrfToken": get_token(request)})


@api_view("POST")
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return HttpResponse(status=204)


# --- orders -------------------------------------------------------------------


@api_view("POST")
def orders(request: HttpRequest) -> HttpResponse:
    outcome = services.place_order(request.user, _json_body(request))
    return JsonResponse(presenters.order(outcome.obj), status=outcome.status_code)


@api_view("GET")
def order_detail(request: HttpRequest, order_id: str) -> HttpResponse:
    return JsonResponse(presenters.order(services.get_owned_order(request.user, order_id)))


@api_view("GET")
def my_orders(request: HttpRequest) -> HttpResponse:
    return JsonResponse({"orders": [presenters.order(o) for o in services.orders_for(request.user)]})


@api_view("POST")
@sensitive_variables("payload")
def pay(request: HttpRequest, order_id: str) -> HttpResponse:
    payload = _json_body(request)
    outcome = services.pay_order(request.user, order_id, payload)
    return JsonResponse(presenters.order(outcome.obj), status=outcome.status_code)


@api_view("POST", staff=True)
def fulfil(request: HttpRequest, order_id: str) -> HttpResponse:
    outcome = services.fulfil_order(request.user, order_id)
    return JsonResponse(presenters.order(outcome.obj), status=outcome.status_code)


@api_view("POST", staff=True)
def cancel(request: HttpRequest, order_id: str) -> HttpResponse:
    outcome = services.cancel_order(request.user, order_id)
    return JsonResponse(presenters.order(outcome.obj), status=outcome.status_code)


@api_view("POST")
def refunds(request: HttpRequest, order_id: str) -> HttpResponse:
    outcome = services.refund_order(
        request.user, order_id, _json_body(request), request.headers.get("Idempotency-Key")
    )
    order, record = outcome.obj
    body = presenters.refund(record)
    body["order"] = presenters.order(order)
    return JsonResponse(body, status=outcome.status_code)


# --- saved cards --------------------------------------------------------------


@api_view("GET", "POST")
@sensitive_variables("payload")
def payment_methods(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        cards = services.saved_cards_for(request.user)
        return JsonResponse({"paymentMethods": [presenters.saved_card(c) for c in cards]})
    payload = _json_body(request)
    outcome = services.save_card(request.user, payload)
    return JsonResponse(presenters.saved_card(outcome.obj), status=outcome.status_code)


@api_view("DELETE")
def payment_method_detail(request: HttpRequest, payment_method_id: str) -> HttpResponse:
    outcome = services.delete_saved_card(request.user, payment_method_id)
    if outcome.status_code == 204:
        return HttpResponse(status=204)
    return JsonResponse(
        {
            "paymentMethodId": payment_method_id,
            "removed": True,
            "vaultDeletion": "pending",
            "message": "The card is removed and can no longer be used; PayPal did not confirm deleting it "
            "from the vault yet. Repeat this request to retry.",
        },
        status=outcome.status_code,
    )


# --- operator -----------------------------------------------------------------


@api_view("GET", staff=True)
def reconciliation_report(request: HttpRequest) -> HttpResponse:
    report = reconciliation.reconcile(request.GET.get("from"), request.GET.get("to"))
    return JsonResponse(report)
