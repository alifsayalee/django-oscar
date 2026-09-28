"""
JSON endpoints under /api/.

Callers authenticate with Django's session login (``POST /api/session``, or
the storefront's own login page) and send the ``csrftoken`` cookie back as the
``X-CSRFToken`` header on unsafe methods. Fulfil, cancel and reconciliation
are for staff (``is_staff``); everything else acts on the caller's own data.
"""

from __future__ import annotations

import functools
import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from oscar.core.loading import get_model

from . import services
from .gateway import PaymentError
from .models import PayPalPayment, PayPalRefund, SavedCard
from .safe_write import DONE

logger = logging.getLogger("apps.paypal_payments")

Order = get_model("order", "Order")

View = Callable[..., JsonResponse]


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def error_response(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    body["error"].update({k: v for k, v in extra.items() if v})
    return JsonResponse(body, status=status)


def api(methods: dict[str, View], *, staff: bool = False) -> View:
    """One route, several methods. Runs outside ATOMIC_REQUESTS so a PayPal
    claim commits before the provider call; every PaymentError becomes JSON."""

    @transaction.non_atomic_requests
    @functools.wraps(next(iter(methods.values())))
    def view(request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        handler = methods.get(request.method or "")
        if handler is None:
            response = error_response(405, "method_not_allowed", f"{request.method} is not allowed here.")
            response["Allow"] = ", ".join(methods)
            return response
        needs_login = getattr(handler, "login_required", True)
        if needs_login and not request.user.is_authenticated:
            return error_response(401, "not_authenticated", "Sign in first (POST /api/session).")
        if staff and not request.user.is_staff:
            return error_response(403, "forbidden", "This action is for staff only.")
        try:
            return handler(request, *args, **kwargs)
        except PaymentError as exc:
            return error_response(
                exc.status_code, exc.code, exc.message,
                outcomeUnknown=exc.outcome_unknown, details=exc.details,
            )
        except ImproperlyConfigured as exc:
            logger.error("PayPal integration misconfigured: %s", exc)
            return error_response(503, "not_configured", "Payments are not configured on this site.")

    return view


def public(handler: View) -> View:
    handler.login_required = False  # type: ignore[attr-defined]
    return handler


def read_json(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise PaymentError(400, "The request body must be JSON.", code="invalid_json") from None
    if not isinstance(data, dict):
        raise PaymentError(400, "The request body must be a JSON object.", code="invalid_json")
    return data


def answer(outcome: str, body: dict[str, Any], *, created: bool = False, message: str = "",
           failed_status: int = 409) -> JsonResponse:
    """The one place a write's outcome becomes the HTTP status. Only ``done``
    is success; everything not yet finished is 202; unknown is 504."""
    body = {**body, "outcome": outcome}
    if message:
        body["message"] = message
    match outcome:
        case "done":
            return JsonResponse(body, status=201 if created else 200)
        case "pending" | "sending":
            body.setdefault("message", "Accepted by PayPal but not finished; repeat the request to check on it.")
            return JsonResponse(body, status=202)
        case "failed":
            return JsonResponse(body, status=failed_status)
        case "needs_review":
            return JsonResponse(body, status=409)
        case _:
            body["outcomeUnknown"] = True
            return JsonResponse(body, status=504)


def own_order(request: HttpRequest, order_id: str) -> Any:
    order = Order.objects.filter(number=order_id, user=request.user).first()
    if order is None:
        raise PaymentError(404, "Order not found.", code="not_found")
    return order


def any_order(order_id: str) -> Any:
    order = Order.objects.filter(number=order_id).first()
    if order is None:
        raise PaymentError(404, "Order not found.", code="not_found")
    return order


def idempotency_key(request: HttpRequest, data: dict[str, Any], *, required: bool) -> str | None:
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    if key is None:
        if required:
            raise PaymentError(400, "Send an Idempotency-Key header (or idempotencyKey field).",
                               code="idempotency_key_required")
        return None
    key = str(key).strip()
    if not 1 <= len(key) <= 255:
        raise PaymentError(400, "The idempotency key must be 1-255 characters.", code="invalid_idempotency_key")
    return key


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _amount(value: Any) -> str | None:
    return None if value is None else str(value)


def serialize_card(card: SavedCard) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.public_id),
        "brand": card.brand,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "cardholderName": card.cardholder_name,
        "label": f"{card.brand} ending {card.last_digits}".strip(),
        "createdAt": _iso(card.created),
    }


def serialize_refund(r: PayPalRefund) -> dict[str, Any]:
    return {
        "refundId": str(r.public_id),
        "amount": _amount(r.amount),
        "status": r.status,
        "paypalRefundId": r.paypal_refund_id or None,
        "paypalStatus": r.paypal_status or None,
        "refundedAt": _iso(r.refunded_at),
    }


def serialize_payment(p: PayPalPayment) -> dict[str, Any]:
    refundable = (p.captured_amount or 0) - p.refund_reserved if p.captured_amount is not None else None
    return {
        "state": p.status,
        "amount": _amount(p.amount),
        "currency": p.currency,
        "card": {"brand": p.card_brand, "lastDigits": p.card_last_digits} if p.card_last_digits else None,
        "savedCardId": str(p.saved_card.public_id) if p.saved_card_id and p.saved_card else None,
        "paypalOrderId": p.paypal_order_id or None,
        "authorization": {
            "id": p.authorization_id,
            "status": p.authorization_status,
            "authorizedAt": _iso(p.authorized_at),
            "expiresAt": _iso(p.authorization_expires_at),
        } if p.authorization_id else None,
        "capture": {
            "id": p.capture_id,
            "status": p.capture_status,
            "capturedAmount": _amount(p.captured_amount),
            "paypalFee": _amount(p.paypal_fee),
            "netAmount": _amount(p.net_amount),
            "capturedAt": _iso(p.captured_at),
        } if p.capture_id else None,
        "amountRefunded": _amount(p.amount_refunded),
        "refundableAmount": _amount(refundable),
        "refunds": [serialize_refund(r) for r in p.refunds.all()],
        "lastError": p.last_error or None,
    }


def serialize_order(order: Any) -> dict[str, Any]:
    payment = PayPalPayment.objects.filter(order=order).select_related("saved_card").first()
    return {
        "orderId": str(order.number),
        "status": order.status,
        "placedAt": _iso(order.date_placed),
        "total": {"amount": _amount(order.total_incl_tax), "currency": order.currency},
        "lines": [
            {
                "itemId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _amount(line.unit_price_incl_tax),
                "linePrice": _amount(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": serialize_payment(payment) if payment else {"state": "not_applicable"},
    }


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@public
def session_get(request: HttpRequest) -> JsonResponse:
    user = request.user
    return JsonResponse({
        "csrfToken": get_token(request),
        "user": {"id": user.pk, "username": user.get_username(), "email": user.email, "isStaff": user.is_staff}
        if user.is_authenticated else None,
    })


@public
@sensitive_variables("data", "password")
def session_post(request: HttpRequest) -> JsonResponse:
    data = read_json(request)
    identifier = str(data.get("username") or data.get("email") or "")
    password = str(data.get("password") or "")
    user = authenticate(request, username=identifier, password=password)
    if user is None or not user.is_active:
        return error_response(401, "invalid_credentials", "Unknown user or wrong password.")
    login(request, user)
    return JsonResponse({"csrfToken": get_token(request),
                         "user": {"id": user.pk, "username": user.get_username(), "email": user.email,
                                  "isStaff": user.is_staff}})


@public
def session_delete(request: HttpRequest) -> JsonResponse:
    logout(request)
    return JsonResponse({"user": None})


session = sensitive_post_parameters("password")(api({"GET": session_get, "POST": session_post,
                                                      "DELETE": session_delete}))


# ---------------------------------------------------------------------------
# Flow 1
# ---------------------------------------------------------------------------


def orders_post(request: HttpRequest) -> JsonResponse:
    data = read_json(request)
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise PaymentError(400, "items must be a list of {itemId, quantity}.", code="invalid_request")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise PaymentError(400, "items must be a list of {itemId, quantity}.", code="invalid_request")
        try:
            items.append(services.OrderItem(int(str(raw.get("itemId"))), int(str(raw.get("quantity", 1)))))
        except (TypeError, ValueError):
            raise PaymentError(400, "itemId and quantity must be integers.", code="invalid_request") from None
    shipping = data.get("shippingAddress")
    if shipping is not None and not isinstance(shipping, dict):
        raise PaymentError(400, "shippingAddress must be an object.", code="invalid_request")
    order = services.place_order(request.user, items, {k: str(v) for k, v in (shipping or {}).items()}, request)
    return JsonResponse(serialize_order(order), status=201)


def my_orders_get(request: HttpRequest) -> JsonResponse:
    orders = Order.objects.filter(user=request.user).order_by("-date_placed").prefetch_related("lines")
    return JsonResponse({"orders": [serialize_order(o) for o in orders]})


@sensitive_variables("data", "card")
def pay_post(request: HttpRequest, order_id: str) -> JsonResponse:
    order = own_order(request, order_id)
    data = read_json(request)
    method_id = data.get("paymentMethodId")
    if method_id is not None and data.get("card") is not None:
        raise PaymentError(400, "Send either card or paymentMethodId, not both.", code="invalid_request")
    card = None
    saved_id = None
    if method_id is not None:
        try:
            saved_id = str(uuid.UUID(str(method_id)))
        except ValueError:
            raise PaymentError(404, "Saved card not found.", code="unknown_payment_method") from None
    elif data.get("card") is not None:
        card = services.parse_card(data["card"])
    else:
        raise PaymentError(400, "Send card details or a paymentMethodId.", code="invalid_request")
    result = services.authorize(order, request.user, card, saved_id)
    return answer(result.outcome, serialize_order(result.order), message=result.message, failed_status=402)


def fulfil_post(request: HttpRequest, order_id: str) -> JsonResponse:
    result = services.fulfil(any_order(order_id))
    return answer(result.outcome, serialize_order(result.order), message=result.message)


def cancel_post(request: HttpRequest, order_id: str) -> JsonResponse:
    result = services.cancel(any_order(order_id))
    return answer(result.outcome, serialize_order(result.order), message=result.message)


def refunds_post(request: HttpRequest, order_id: str) -> JsonResponse:
    order = own_order(request, order_id)
    data = read_json(request)
    key = idempotency_key(request, data, required=True)
    assert key is not None
    note = str(data.get("note") or "")
    result, row = services.refund(order, data.get("amount"), key, note)
    body = {"refundId": str(row.public_id), "refund": serialize_refund(row), "order": serialize_order(result.order)}
    return answer(result.outcome, body, created=True)


def refunds_get(request: HttpRequest, order_id: str) -> JsonResponse:
    order = own_order(request, order_id)
    payment = services.payment_for(order)
    return JsonResponse({"refunds": [serialize_refund(r) for r in payment.refunds.all()]})


def reconciliation_get(request: HttpRequest) -> JsonResponse:
    start = _parse_instant(request.GET.get("from"), "from")
    end = _parse_instant(request.GET.get("to"), "to")
    if end <= start:
        raise PaymentError(400, "to must be after from.", code="invalid_range")
    if end - start > timedelta(days=3 * 366):
        raise PaymentError(400, "PayPal lists at most three years of transactions.", code="invalid_range")
    return JsonResponse(services.reconcile(start, end))


def _parse_instant(raw: str | None, name: str) -> datetime:
    value = parse_datetime(raw or "")
    if value is None:
        raise PaymentError(400, f"{name} must be an ISO-8601 date-time.", code="invalid_range")
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value


# ---------------------------------------------------------------------------
# Flow 2
# ---------------------------------------------------------------------------


def payment_methods_get(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"paymentMethods": [serialize_card(c) for c in services.list_cards(request.user)]})


@sensitive_variables("data", "card")
def payment_methods_post(request: HttpRequest) -> JsonResponse:
    data = read_json(request)
    # Without a key, a repeat is not recognised as a repeat.
    key = idempotency_key(request, data, required=False) or uuid.uuid4().hex
    card = services.parse_card(data.get("card", data))
    outcome, saved = services.save_card(request.user, card, key)
    if outcome == DONE and saved is not None:
        if saved.deleted_at is not None:
            raise PaymentError(409, "This card was saved and then removed; save it again with a new key.",
                               code="payment_method_removed")
        return answer(outcome, serialize_card(saved), created=True)
    return answer(outcome, {"paymentMethodId": str(saved.public_id) if saved else None})


def payment_method_delete(request: HttpRequest, payment_method_id: str) -> JsonResponse:
    try:
        public_id = str(uuid.UUID(payment_method_id))
    except ValueError:
        raise PaymentError(404, "Saved card not found.", code="unknown_payment_method") from None
    outcome, card = services.delete_card(request.user, public_id)
    body = {"paymentMethodId": str(card.public_id), "deleted": True,
            "removedAtPayPal": card.provider_deleted}
    if outcome != DONE:
        body["message"] = "Removed from your account; PayPal's copy will be removed on retry."
    return JsonResponse(body, status=200)


orders = api({"POST": orders_post})
my_orders = api({"GET": my_orders_get})
pay = api({"POST": pay_post})
fulfil = api({"POST": fulfil_post}, staff=True)
cancel = api({"POST": cancel_post}, staff=True)
refunds = api({"POST": refunds_post, "GET": refunds_get})
reconciliation = api({"GET": reconciliation_get}, staff=True)
payment_methods = api({"GET": payment_methods_get, "POST": payment_methods_post})
payment_method = api({"DELETE": payment_method_delete})
