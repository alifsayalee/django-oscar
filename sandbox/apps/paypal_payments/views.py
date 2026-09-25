"""
JSON API under /api/.

Callers authenticate with Django's session login (``POST /api/login``) and send
the CSRF token in ``X-CSRFToken`` on unsafe requests. The views are excluded from
``ATOMIC_REQUESTS`` so each PayPal claim is committed before the provider call.
"""

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any

import httpx
from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from oscar.core.loading import get_model
from paypal.core import ApiError

from . import reconciliation, service
from .cards import parse_card
from .errors import ApiProblem, translate
from .models import PayPalPayment, PayPalRefund, SavedCard

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")

View = Callable[..., HttpResponse]


# ---------------------------------------------------------------- plumbing


def api_view(*, staff: bool = False, auth: bool = True) -> Callable[[View], View]:
    def decorate(view: View) -> View:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if auth and not request.user.is_authenticated:
                return JsonResponse({"error": "not_authenticated", "message": "Log in first."}, status=401)
            if staff and not request.user.is_staff:
                return JsonResponse({"error": "forbidden", "message": "Staff only."}, status=403)
            try:
                return view(request, *args, **kwargs)
            except ApiProblem as problem:
                return JsonResponse(problem.body(), status=problem.status)
            except Exception:
                logger.exception("Unhandled error in %s", view.__name__)
                return JsonResponse({"error": "server_error", "message": "Internal error."}, status=500)

        return transaction.non_atomic_requests(wrapper)

    return decorate


@sensitive_variables("raw")
def _json_body(request: HttpRequest) -> dict[str, Any]:
    raw = request.body
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ApiProblem(400, "invalid_json", "The request body must be JSON.") from None
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_json", "The request body must be a JSON object.")
    return data


def _user_id(request: HttpRequest) -> int:
    pk = request.user.pk
    if pk is None:
        raise ApiProblem(401, "not_authenticated", "Log in first.")
    return int(pk)


def _uuid(value: object, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ApiProblem(404, f"{what}_not_found", f"No such {what.replace('_', ' ')}.") from None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def payment_json(payment: PayPalPayment | None) -> dict[str, Any] | None:
    if payment is None:
        return None
    refunds = list(payment.refunds.all())
    return {
        "state": payment.state,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "card": payment.card_label or None,
        "paypalOrderId": payment.paypal_order_id or None,
        "authorization": {
            "id": payment.authorization_id,
            "status": payment.authorization_status,
            "authorizedAt": _dt(payment.authorized_at),
            "expiresAt": _dt(payment.authorization_expires_at),
            "reauthorized": payment.reauthorized,
        }
        if payment.authorization_id
        else None,
        "capture": {
            "id": payment.capture_id,
            "status": payment.capture_status,
            "amount": _money(payment.captured_amount),
            "paypalFee": _money(payment.paypal_fee),
            "netAmount": _money(payment.net_amount),
            "capturedAt": _dt(payment.captured_at),
        }
        if payment.capture_id
        else None,
        "refundedAmount": str(payment.refunded_amount),
        "refunds": [refund_json(r) for r in refunds],
        "lastError": payment.last_error or None,
    }


def refund_json(refund: PayPalRefund) -> dict[str, Any]:
    return {
        "refundId": str(refund.public_id),
        "amount": str(refund.amount),
        "outcome": refund.outcome,
        "paypalRefundId": refund.paypal_refund_id or None,
        "paypalStatus": refund.paypal_status or None,
        "createdAt": _dt(refund.date_created),
    }


def order_json(order: Any) -> dict[str, Any]:
    payment = PayPalPayment.objects.filter(order=order).prefetch_related("refunds").first()
    return {
        "orderId": str(order.number),
        "status": order.status,
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "placedAt": _dt(order.date_placed),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "lineTotal": str(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": payment_json(payment),
    }


def card_json(card: SavedCard) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.public_id),
        "brand": card.brand or None,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "name": card.name,
        "label": card.label,
        "createdAt": _dt(card.date_created),
    }


def _payment_status(payment: PayPalPayment) -> int:
    pending = {
        PayPalPayment.AUTHORIZATION_PENDING,
        PayPalPayment.PAYER_ACTION_REQUIRED,
        PayPalPayment.CAPTURE_PENDING,
        PayPalPayment.VOIDING,
    }
    return 202 if payment.state in pending else 200


# ---------------------------------------------------------------- session


@require_GET
def csrf(request: HttpRequest) -> HttpResponse:
    return JsonResponse({"csrfToken": get_token(request)})


@sensitive_post_parameters()
@sensitive_variables("data", "password")
@require_POST
@api_view(auth=False)
def login_view(request: HttpRequest) -> HttpResponse:
    data = _json_body(request)
    username, password = data.get("username") or data.get("email"), data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ApiProblem(400, "invalid_request", "'username' (or 'email') and 'password' are required.")
    user = authenticate(request, username=username, password=password)
    if user is None:
        raise ApiProblem(401, "invalid_credentials", "Wrong username or password.")
    login(request, user)
    return JsonResponse({"userId": user.pk, "isStaff": user.is_staff, "csrfToken": get_token(request)})


@require_POST
@api_view()
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return JsonResponse({"loggedOut": True})


# ---------------------------------------------------------------- orders


@require_POST
@api_view()
def orders(request: HttpRequest) -> HttpResponse:
    data = _json_body(request)
    items = service.parse_items(data.get("items"))
    order = service.place_order(request.user, items, request)
    return JsonResponse(order_json(order), status=201)


@sensitive_variables("data", "card")
@require_POST
@api_view()
def pay(request: HttpRequest, number: str) -> HttpResponse:
    data = _json_body(request)
    card = parse_card(data["card"]) if data.get("card") is not None else None
    method_id = data.get("paymentMethodId")
    if (card is None) == (method_id is None):
        raise ApiProblem(400, "invalid_request", "Provide exactly one of 'card' or 'paymentMethodId'.")
    saved_id = _uuid(method_id, "payment_method") if method_id is not None else None
    payment = service.pay_order(request.user, number, card, saved_id)
    return JsonResponse({"orderId": number, "payment": payment_json(payment)}, status=_payment_status(payment))


@require_POST
@api_view(staff=True)
def fulfil(request: HttpRequest, number: str) -> HttpResponse:
    payment = service.fulfil_order(number)
    return JsonResponse({"orderId": number, "payment": payment_json(payment)}, status=_payment_status(payment))


@require_POST
@api_view(staff=True)
def cancel(request: HttpRequest, number: str) -> HttpResponse:
    payment = service.cancel_order(number)
    return JsonResponse({"orderId": number, "payment": payment_json(payment)}, status=_payment_status(payment))


@require_POST
@api_view()
def refunds(request: HttpRequest, number: str) -> HttpResponse:
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key or len(key) > 255:
        raise ApiProblem(400, "idempotency_key_required", "Send an Idempotency-Key header (at most 255 characters).")
    data = _json_body(request)
    amount: Decimal | None = None
    if data.get("amount") is not None:
        try:
            amount = Decimal(str(data["amount"]))
        except InvalidOperation:
            raise ApiProblem(400, "invalid_amount", "'amount' must be a decimal string such as \"5.00\".") from None
        if not amount.is_finite() or amount <= 0:
            raise ApiProblem(400, "invalid_amount", "'amount' must be positive.")
    refund = service.refund_order(request.user, number, key, amount)
    body: dict[str, Any] = {"refundId": str(refund.public_id), "orderId": number, "refund": refund_json(refund)}
    payment = PayPalPayment.objects.get(pk=refund.payment_id)
    body["payment"] = payment_json(payment)
    return JsonResponse(body, status=202 if refund.outcome == "pending" else 201)


@require_GET
@api_view()
def my_orders(request: HttpRequest) -> HttpResponse:
    qs = Order.objects.filter(user_id=_user_id(request)).order_by("-date_placed").prefetch_related("lines")
    return JsonResponse({"orders": [order_json(o) for o in qs[:200]]})


@require_GET
@api_view(staff=True)
def reconciliation_view(request: HttpRequest) -> HttpResponse:
    start = parse_datetime(request.GET.get("from", "") or "")
    end = parse_datetime(request.GET.get("to", "") or "")
    if start is None or end is None or start.tzinfo is None or end.tzinfo is None:
        raise ApiProblem(400, "invalid_range", "'from' and 'to' must be ISO-8601 date-times with a UTC offset.")
    if end <= start:
        raise ApiProblem(400, "invalid_range", "'to' must be after 'from'.")
    try:
        report = reconciliation.build_report(start, end, service.install_prefix())
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc
    return JsonResponse(report)


# ---------------------------------------------------------------- saved cards


@sensitive_variables("data", "card")
@require_http_methods(["GET", "POST"])
@api_view()
def payment_methods(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        cards = SavedCard.objects.filter(user_id=_user_id(request), deleted_at__isnull=True)
        return JsonResponse({"paymentMethods": [card_json(c) for c in cards]})
    data = _json_body(request)
    card = parse_card(data.get("card", data))
    # Without a key every request is a new save, by definition.
    key = (request.headers.get("Idempotency-Key") or "").strip()[:255] or uuid.uuid4().hex
    saved = service.save_card(request.user, card, key)
    return JsonResponse(card_json(saved), status=201)


@require_http_methods(["DELETE"])
@api_view()
def payment_method_detail(request: HttpRequest, method_id: str) -> HttpResponse:
    service.delete_card(request.user, _uuid(method_id, "payment_method"))
    return HttpResponse(status=204)
