"""
JSON API for orders, PayPal payments and saved cards.

Callers authenticate with Django's own session login (``POST /api/session``
or the storefront's login page) and send the ``X-CSRFToken`` header on
unsafe requests, exactly as the rest of the sandbox does. Fulfil, cancel and
reconciliation are for staff (``is_staff``); everything else acts only on the
caller's own orders and cards, and another shopper's are answered with 404.

Views run outside ``ATOMIC_REQUESTS``: a payment claim must be committed
before PayPal is called, and a PayPal effect must never be rolled back
locally just because a later step failed.
"""

import functools
import json
import logging
from collections.abc import Callable
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_model

from . import services
from .gateway import ProviderError
from .models import PayPalPayment, PayPalRefund, SavedCard
from .money import to_wire

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")

View = Callable[..., JsonResponse]


def _error(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status)


def api_view(*methods: str, staff: bool = False, anonymous: bool = False) -> Callable[[View], View]:
    """Method check, session auth, JSON body parsing and error translation for one endpoint."""

    def decorator(view: View) -> View:
        @functools.wraps(view)
        @sensitive_variables()  # request bodies carry card details
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
            if request.method not in methods:
                response = _error(405, "method_not_allowed", "use %s" % " or ".join(methods))
                response["Allow"] = ", ".join(methods)
                return response
            if not anonymous and not request.user.is_authenticated:
                return _error(401, "not_authenticated", "sign in first (POST /api/session)")
            if staff and not request.user.is_staff:
                return _error(403, "forbidden", "this action is for staff only")
            body: dict[str, Any] = {}
            if request.method in ("POST", "PUT", "PATCH") and request.body:
                try:
                    parsed = json.loads(request.body)
                except (ValueError, UnicodeDecodeError):
                    return _error(400, "invalid_json", "the request body must be JSON")
                if not isinstance(parsed, dict):
                    return _error(400, "invalid_json", "the request body must be a JSON object")
                body = parsed
            request.json = body  # type: ignore[attr-defined]
            try:
                return view(request, *args, **kwargs)
            except services.ServiceError as e:
                return _error(e.status_code, e.code, e.message, **e.extra)
            except ProviderError as e:
                return _error(
                    e.status_code, "payment_provider_error", e.message,
                    outcomeUnknown=e.outcome_unknown, paypalIssue=e.issue or None,
                    paypalDebugId=e.debug_id or None,
                )
            except ImproperlyConfigured as e:
                logger.error("PayPal is not configured: %s", e)
                return _error(503, "payments_unavailable", "payments are not configured on this site")

        # A PayPal call must not sit inside the request-wide transaction
        return transaction.non_atomic_requests(wrapper)

    return decorator


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _amount(value: Any, currency: str) -> str | None:
    return to_wire(value, currency) if value is not None else None


def refund_json(refund: PayPalRefund) -> dict[str, Any]:
    return {
        "refundId": str(refund.public_id),
        "status": refund.status,
        "amount": _amount(refund.amount, refund.currency),
        "currency": refund.currency,
        "idempotencyKey": refund.idempotency_key,
        "paypalRefundId": refund.paypal_refund_id or None,
        "paypalStatus": refund.paypal_status or None,
        "createdAt": _iso(refund.date_created),
        "error": refund.last_error or None,
    }


def payment_json(payment: PayPalPayment) -> dict[str, Any]:
    cur = payment.currency
    refundable = None
    if payment.captured_amount is not None and payment.state in services.REFUNDABLE:
        refundable = _amount(payment.captured_amount - payment.refund_reserved, cur)
    return {
        "state": payment.state,
        "description": payment.get_state_display(),
        "amount": _amount(payment.amount, cur),
        "currency": cur,
        "card": {"brand": payment.card_brand or None, "lastDigits": payment.card_last_digits}
        if payment.card_last_digits else None,
        "paymentMethodId": str(payment.saved_card.public_id) if payment.saved_card is not None else None,
        "paypalOrderId": payment.paypal_order_id or None,
        "authorization": {
            "id": payment.authorization_id,
            "status": payment.authorization_status or None,
            "createdAt": _iso(payment.authorization_created_at),
            "expiresAt": _iso(payment.authorization_expires_at),
            "reauthorized": payment.reauthorized,
        } if payment.authorization_id else None,
        "capture": {
            "id": payment.capture_id,
            "status": payment.capture_status or None,
            "amount": _amount(payment.captured_amount, cur),
            "paypalFee": _amount(payment.paypal_fee, cur),
            "netAmount": _amount(payment.net_amount, cur),
            "capturedAt": _iso(payment.captured_at),
        } if payment.capture_id else None,
        "refundedAmount": _amount(payment.refunded_amount, cur),
        "refundableAmount": refundable,
        "refunds": [refund_json(r) for r in payment.refunds.all()],
        "error": payment.last_error or None,
    }


def order_json(order: Any) -> dict[str, Any]:
    payment = getattr(order, "paypal_payment", None)
    if payment is not None:
        payment.refresh_from_db()
    currency = payment.currency if payment is not None else order.currency
    return {
        "orderId": order.number,
        "status": order.status,
        "placedAt": _iso(order.date_placed),
        "currency": currency,
        "total": _amount(order.total_incl_tax, currency),
        "lines": [
            {
                "itemId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _amount(line.unit_price_incl_tax, currency),
                "lineTotal": _amount(line.line_price_incl_tax, currency),
            }
            for line in order.lines.all()
        ],
        "payment": payment_json(payment) if payment is not None else None,
    }


def card_json(card: SavedCard) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.public_id),
        "brand": card.brand or None,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "cardholderName": card.cardholder_name or None,
        "createdAt": _iso(card.date_created),
    }


def _order_response(order: Any, status: int = 200, **extra: Any) -> JsonResponse:
    order.refresh_from_db()
    return JsonResponse({**order_json(order), **extra}, status=status)


def _in_progress(order: Any, error: services.InProgress) -> JsonResponse:
    return _order_response(order, 202, message=error.message, code=error.code)


# ---------------------------------------------------------------------------
# Session helpers (Django's own session login, as JSON)
# ---------------------------------------------------------------------------


@ensure_csrf_cookie
@api_view("GET", anonymous=True)
def csrf(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"csrfToken": get_token(request)})


@api_view("GET", "POST", "DELETE", anonymous=True)
def session(request: HttpRequest) -> JsonResponse:
    if request.method == "POST":
        data = request.json  # type: ignore[attr-defined]
        username = data.get("username") or data.get("email")
        password = data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return _error(422, "invalid_request", "send username (or email) and password")
        user = authenticate(request, username=username, password=password)
        if user is None:
            return _error(401, "invalid_credentials", "wrong username or password")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
        return JsonResponse({"authenticated": False})
    if not request.user.is_authenticated:
        return JsonResponse({"authenticated": False})
    return JsonResponse({
        "authenticated": True,
        "userId": request.user.pk,
        "username": request.user.get_username(),
        "isStaff": request.user.is_staff,
        "csrfToken": get_token(request),
    })


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@api_view("POST")
def orders(request: HttpRequest) -> JsonResponse:
    order = services.place_order(request.user, request.json.get("items"), request)  # type: ignore[attr-defined]
    return _order_response(order, 201)


@api_view("POST")
@sensitive_variables()
def pay(request: HttpRequest, number: str) -> JsonResponse:
    order = services.get_order_for(request.user, number)
    try:
        services.pay(order, request.user, request.json)  # type: ignore[attr-defined]
    except services.InProgress as e:
        return _in_progress(order, e)
    return _order_response(order)


@api_view("POST", staff=True)
def fulfil(request: HttpRequest, number: str) -> JsonResponse:
    order = services.get_order_for(request.user, number, staff=True)
    try:
        services.fulfil(order)
    except services.InProgress as e:
        return _in_progress(order, e)
    return _order_response(order)


@api_view("POST", staff=True)
def cancel(request: HttpRequest, number: str) -> JsonResponse:
    order = services.get_order_for(request.user, number, staff=True)
    try:
        services.cancel(order)
    except services.InProgress as e:
        return _in_progress(order, e)
    return _order_response(order)


@api_view("POST")
def refunds(request: HttpRequest, number: str) -> JsonResponse:
    order = services.get_order_for(request.user, number)
    data = request.json  # type: ignore[attr-defined]
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    try:
        refund, created = services.refund(order, request.user, data.get("amount"), key)
    except services.InProgress as e:
        return _in_progress(order, e)
    order.refresh_from_db()
    payment = order.paypal_payment
    payment.refresh_from_db()
    status = 201 if created else 200
    if refund.status in (PayPalRefund.PENDING, PayPalRefund.UNKNOWN):
        status = 202
    return JsonResponse({**refund_json(refund), "orderId": order.number, "payment": payment_json(payment)},
                        status=status)


@api_view("GET")
def my_orders(request: HttpRequest) -> JsonResponse:
    qs = (Order.objects.filter(user=request.user).select_related("paypal_payment")
          .prefetch_related("lines").order_by("-date_placed"))
    return JsonResponse({"orders": [order_json(o) for o in qs]})


@api_view("GET", staff=True)
def reconciliation(request: HttpRequest) -> JsonResponse:
    start = services.parse_range_bound(request.GET.get("from"), "from")
    end = services.parse_range_bound(request.GET.get("to"), "to")
    return JsonResponse(services.reconcile(start, end))


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


@api_view("GET", "POST")
@sensitive_variables()
def payment_methods(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        return JsonResponse({"paymentMethods": [card_json(c) for c in services.list_cards(request.user)]})
    data = request.json  # type: ignore[attr-defined]
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey") or ""
    try:
        card, created = services.save_card(request.user, request.json, key)  # type: ignore[attr-defined]
    except services.InProgress as e:
        return _error(202, e.code, e.message)
    return JsonResponse(card_json(card), status=201 if created else 200)


@api_view("DELETE")
def payment_method(request: HttpRequest, payment_method_id: str) -> JsonResponse:
    services.delete_card(request.user, payment_method_id)
    return JsonResponse({"paymentMethodId": payment_method_id, "deleted": True})
