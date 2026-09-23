"""
JSON API for orders, PayPal payments and saved cards.

Callers authenticate with Django's session login (``POST /api/session`` or the
storefront's own login page) and send the CSRF token as ``X-CSRFToken`` on
unsafe methods. Shopper endpoints only ever see the caller's own data; fulfil,
cancel and reconciliation are restricted to ``is_staff`` users.
"""

import functools
import json
import logging

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_http_methods

from . import reconciliation, serializers, services
from .gateway import ProviderError
from .models import PaymentState, RefundState
from .services import ApiProblem

logger = logging.getLogger("apps.paypal_payments")


def _problem(status, message, **extra):
    body = {"error": message}
    body.update(extra)
    return JsonResponse(body, status=status)


def api_view(methods, staff=False, auth=True):
    """Method check, JSON errors, authentication and staff gating for one view."""

    def decorator(view):
        @functools.wraps(view)
        @require_http_methods(methods)
        def wrapper(request, *args, **kwargs):
            if auth and not request.user.is_authenticated:
                return _problem(
                    401, "Authentication required: log in with POST /api/session."
                )
            if staff and not request.user.is_staff:
                return _problem(403, "This action is restricted to staff.")
            try:
                return view(request, *args, **kwargs)
            except ApiProblem as e:
                return JsonResponse(e.as_dict(), status=e.status)
            except ProviderError as e:
                return JsonResponse(e.as_dict(), status=e.status_code)
            except ImproperlyConfigured as e:
                logger.error("PayPal is not configured: %s", e)
                return _problem(503, "Payments are not configured on this server.")

        return wrapper

    return decorator


@sensitive_variables("data")
def _json(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ApiProblem(400, "Request body must be JSON") from None
    if not isinstance(data, dict):
        raise ApiProblem(400, "Request body must be a JSON object")
    return data


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@ensure_csrf_cookie
@api_view(["GET"], auth=False)
def csrf(request):
    return JsonResponse({"csrfToken": get_token(request)})


@sensitive_variables("data", "password")
@api_view(["GET", "POST", "DELETE"], auth=False)
def session(request):
    if request.method == "POST":
        data = _json(request)
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return _problem(400, "username and password are required")
        user = authenticate(request, username=username, password=password)
        if user is None or not user.is_active:
            return _problem(401, "Invalid credentials")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
        return HttpResponse(status=204)
    if not request.user.is_authenticated:
        return _problem(401, "Not logged in")
    return JsonResponse(
        {
            "userId": request.user.pk,
            "username": request.user.get_username(),
            "isStaff": request.user.is_staff,
            "csrfToken": get_token(request),
        }
    )


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


def _order_response(order, status=200):
    order = services.get_any_order(order.number)
    body = serializers.order(order)
    return JsonResponse(body, status=status)


def _payment_status(payment):
    pending = {
        PaymentState.AUTHORIZATION_PENDING,
        PaymentState.CAPTURE_PENDING,
    }
    return 202 if payment.state in pending else 200


@api_view(["POST"])
def orders(request):
    data = _json(request)
    order = services.place_order(
        request.user, data.get("lines"), data.get("shippingAddress")
    )
    return _order_response(order, status=201)


@api_view(["GET"])
def order_detail(request, order_id):
    if request.user.is_staff:
        order = services.get_any_order(order_id)
    else:
        order = services.get_user_order(request.user, order_id)
    return _order_response(order)


@sensitive_variables("data", "card")
@api_view(["POST"])
def pay(request, order_id):
    data = _json(request)
    card = data.get("card")
    method_id = data.get("paymentMethodId")
    payment = services.pay(
        request.user, order_id, card_data=card, payment_method_id=method_id
    )
    return _order_response(payment.order, status=_payment_status(payment))


@api_view(["POST"], staff=True)
def fulfil(request, order_id):
    payment = services.fulfil(order_id)
    return _order_response(payment.order, status=_payment_status(payment))


@api_view(["POST"], staff=True)
def cancel(request, order_id):
    payment = services.cancel(order_id)
    return _order_response(payment.order)


@api_view(["POST"])
def refunds(request, order_id):
    data = _json(request)
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    record, created = services.refund(
        request.user,
        order_id,
        key,
        amount_raw=data.get("amount"),
        reason=data.get("reason", ""),
    )
    body = serializers.refund(record)
    body["order"] = serializers.order(services.get_any_order(order_id))
    if record.state == RefundState.PENDING:
        status = 202
    elif record.state == RefundState.FAILED:
        status = 422 if created else 200
    else:
        status = 201 if created else 200
    return JsonResponse(body, status=status)


@api_view(["GET"])
def my_orders(request):
    return JsonResponse(
        {
            "orders": [
                serializers.order(o) for o in services.order_queryset_for(request.user)
            ]
        }
    )


@api_view(["GET"], staff=True)
def reconciliation_report(request):
    try:
        start, end = reconciliation.parse_range(
            request.GET.get("from"), request.GET.get("to")
        )
    except ValueError as e:
        return _problem(400, str(e))
    return JsonResponse(reconciliation.build_report(start, end))


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------


@sensitive_variables("data")
@api_view(["GET", "POST"])
def payment_methods(request):
    if request.method == "GET":
        return JsonResponse(
            {
                "paymentMethods": [
                    serializers.saved_card(c) for c in services.list_cards(request.user)
                ]
            }
        )
    data = _json(request)
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    record, created = services.save_card(request.user, data.get("card"), key)
    return JsonResponse(serializers.saved_card(record), status=201 if created else 200)


@api_view(["DELETE"])
def payment_method_detail(request, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return HttpResponse(status=204)
