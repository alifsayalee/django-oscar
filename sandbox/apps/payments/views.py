"""
JSON endpoints under /api/.

Callers authenticate with Django's session login (``POST /api/login`` or the
storefront's login page); CSRF protection stays on for every unsafe method.
Views run outside ``ATOMIC_REQUESTS`` because each PayPal write must commit its
claim row before PayPal is called.
"""

import functools
import json
import logging
import uuid
from decimal import Decimal

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import gateway, reconciliation, services
from .gateway import PayPalError
from .models import PayPalPayment, PayPalRefund
from .services import ServiceError
from .validation import (
    InvalidRequest,
    parse_amount,
    parse_card,
    parse_datetime,
    parse_idempotency_key,
)

logger = logging.getLogger("apps.payments")


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _error(status, code, message, **extra):
    body = {"error": code, "message": message}
    body.update(extra)
    return JsonResponse(body, status=status)


class _Encoder(json.JSONEncoder):
    """Decimals as exact strings, datetimes as ISO-8601."""

    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, uuid.UUID):
            return str(o)
        return super().default(o)


def _ok(body, status=200):
    return JsonResponse(body, status=status, encoder=_Encoder)


def api_view(*, staff=False, login_required=True):
    """Session auth, JSON errors, and the error boundary for every endpoint."""

    def decorator(view):
        @functools.wraps(view)
        @never_cache
        @transaction.non_atomic_requests
        def wrapper(request, *args, **kwargs):
            if login_required and not request.user.is_authenticated:
                return _error(401, "not_authenticated", "Sign in first.")
            if staff and not request.user.is_staff:
                return _error(403, "forbidden", "This action is restricted to staff.")
            try:
                return view(request, *args, **kwargs)
            except InvalidRequest as exc:
                return _error(400, "invalid_request", exc.message, field=exc.field)
            except ServiceError as exc:
                return JsonResponse(exc.as_dict(), status=exc.http_status, encoder=_Encoder)
            except PayPalError as exc:
                return JsonResponse(exc.as_dict(), status=exc.http_status)
        return wrapper

    return decorator


def _body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise InvalidRequest("Request body must be JSON.")
    if not isinstance(data, dict):
        raise InvalidRequest("Request body must be a JSON object.")
    return data


def _idempotency_header(request):
    return parse_idempotency_key(request.headers.get("Idempotency-Key"), required=False, field="Idempotency-Key")


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def _amount(value, currency):
    """Money as a string with the currency's own number of decimals."""
    if value is None:
        return None
    return str(gateway.quantize(Decimal(value), currency))


def _card_json(card):
    return {
        "paymentMethodId": str(card.public_id),
        "brand": card.brand,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "label": "%s ending %s" % (card.brand or "Card", card.last_digits),
        "createdAt": card.date_created,
    }


def _refund_json(refund):
    return {
        "refundId": str(refund.public_id),
        "amount": _amount(refund.amount, refund.currency),
        "currency": refund.currency,
        "status": refund.state,
        "paypalRefundId": refund.paypal_refund_id or None,
        "paypalStatus": refund.paypal_status or None,
        "reason": refund.reason or None,
        "createdAt": refund.date_created,
    }


def _payment_status(payment):
    if payment.state == PayPalPayment.CAPTURED:
        totals = services.refund_totals(payment)
        refunded = totals[PayPalRefund.COMPLETED]
        if payment.captured_amount and refunded >= payment.captured_amount:
            return "refunded"
        if refunded > 0:
            return "partially_refunded"
    return payment.state


def _payment_json(payment):
    if payment is None:
        return None
    totals = services.refund_totals(payment) if payment.capture_id else None
    code = payment.currency
    return {
        "status": _payment_status(payment),
        "amount": _amount(payment.amount, code),
        "currency": payment.currency,
        "card": payment.card_label or None,
        "paymentMethodId": str(payment.saved_card.public_id) if payment.saved_card_id else None,
        "paypalOrderId": payment.paypal_order_id or None,
        "authorization": {
            "id": payment.authorization_id,
            "status": payment.authorization_status,
            "createdAt": payment.authorization_time,
            "expiresAt": payment.authorization_expires,
            "reauthorizations": payment.reauthorization_count,
        } if payment.authorization_id else None,
        "capture": {
            "id": payment.capture_id,
            "status": payment.capture_status,
            "capturedAt": payment.capture_time,
            "grossAmount": _amount(payment.captured_amount, code),
            "paypalFee": _amount(payment.paypal_fee, code),
            "netAmount": _amount(payment.net_amount, code),
        } if payment.capture_id else None,
        "refunded": _amount(totals[PayPalRefund.COMPLETED] if totals else 0, code),
        "refundPending": _amount(totals[PayPalRefund.PENDING] if totals else 0, code),
        "refundableRemaining": _amount(services.refundable_remaining(payment) if payment.capture_id else 0, code),
        "refunds": [_refund_json(r) for r in payment.refunds.all()],
        "failureReason": payment.failure_reason or None,
    }


def _order_json(order):
    payment = services.live_payment(order)
    if payment is None:
        payment = order.paypal_payments.first()  # the latest failed attempt, if any
    return {
        "orderId": str(order.number),
        "status": order.status,
        "currency": order.currency,
        "total": _amount(order.total_incl_tax, order.currency),
        "placedAt": order.date_placed,
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _amount(line.unit_price_incl_tax, order.currency),
                "linePrice": _amount(line.line_price_incl_tax, order.currency),
            }
            for line in order.lines.all()
        ],
        "payment": _payment_json(payment),
    }


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@require_GET
@ensure_csrf_cookie
@never_cache
def csrf(request):
    """Issues the CSRF cookie; send its value back in the X-CSRFToken header."""
    return JsonResponse({"csrfToken": get_token(request)})


@require_POST
@sensitive_post_parameters()
@sensitive_variables("data", "password")
@api_view(login_required=False)
def login_view(request):
    data = _body(request)
    username = data.get("username") or data.get("email")
    password = data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise InvalidRequest("username (or email) and password are required.")
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return _error(401, "invalid_credentials", "Invalid username or password.")
    login(request, user)
    return _ok({"userId": user.pk, "username": user.get_username(), "isStaff": user.is_staff,
                "csrfToken": get_token(request)})


@require_POST
@api_view()
def logout_view(request):
    logout(request)
    return _ok({"loggedOut": True})


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@require_GET
@api_view(login_required=False)
def products(request):
    """Purchasable catalogue items, priced by Oscar's strategy, in the configured currency."""
    try:
        limit = min(max(int(request.GET.get("limit", 50)), 1), 200)
        offset = max(int(request.GET.get("offset", 0)), 0)
    except ValueError:
        raise InvalidRequest("limit and offset must be integers.")
    items, has_more = services.purchasable_products(request, request.GET.get("q", ""), limit, offset)
    return _ok({"products": items, "limit": limit, "offset": offset, "hasMore": has_more})


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@require_POST
@api_view()
def orders(request):
    data = _body(request)
    order, created = services.place_order(
        request, request.user, data.get("items"), data.get("shippingAddress"), _idempotency_header(request),
    )
    return _ok(_order_json(order), status=201 if created else 200)


@require_GET
@api_view()
def my_orders(request):
    return _ok({"orders": [_order_json(order) for order in services.orders_for(request.user)]})


@require_POST
@sensitive_post_parameters()
@sensitive_variables("data", "card")
@api_view()
def pay(request, order_id):
    data = _body(request)
    card_data = data.get("card")
    payment_method_id = data.get("paymentMethodId")
    if (card_data is None) == (payment_method_id is None):
        raise InvalidRequest("Provide exactly one of card or paymentMethodId.")
    card = parse_card(card_data) if card_data is not None else None
    if payment_method_id is not None and not isinstance(payment_method_id, str):
        raise InvalidRequest("paymentMethodId must be a string.", "paymentMethodId")
    if payment_method_id is not None:
        payment_method_id = _uuid_or_404(payment_method_id, "payment_method_not_found", "Saved card not found.")
    payment = services.pay(request.user, order_id, card=card, payment_method_id=payment_method_id)
    order = payment.order
    order.refresh_from_db()
    status = 202 if payment.state == PayPalPayment.AUTH_PENDING else 200
    return _ok(_order_json(order), status=status)


@require_POST
@api_view(staff=True)
def fulfil(request, order_id):
    payment = services.fulfil(order_id)
    order = payment.order
    order.refresh_from_db()
    return _ok(_order_json(order), status=202 if payment.state == PayPalPayment.CAPTURE_PENDING else 200)


@require_POST
@api_view(staff=True)
def cancel(request, order_id):
    order, _ = services.cancel(order_id)
    order.refresh_from_db()
    return _ok(_order_json(order))


@require_POST
@api_view()
def refunds(request, order_id):
    data = _body(request)
    key = parse_idempotency_key(
        data.get("idempotencyKey") or request.headers.get("Idempotency-Key"), required=True
    )
    amount = parse_amount(data.get("amount"))
    reason = data.get("reason") or ""
    if not isinstance(reason, str):
        raise InvalidRequest("reason must be a string.", "reason")
    refund, created = services.refund(request.user, order_id, key, amount=amount, reason=reason)
    body = _refund_json(refund)
    body["orderId"] = order_id
    body["refundableRemaining"] = _amount(services.refundable_remaining(
        PayPalPayment.objects.get(pk=refund.payment_id)), refund.currency)
    status = 202 if refund.state in (PayPalRefund.PENDING, PayPalRefund.UNKNOWN) else (201 if created else 200)
    return _ok(body, status=status)


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


def _uuid_or_404(value, code, message):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ServiceError(404, code, message)


@require_http_methods(["GET", "POST"])
@sensitive_post_parameters()
@sensitive_variables("data", "card")
@api_view()
def payment_methods(request):
    if request.method == "GET":
        return _ok({"paymentMethods": [_card_json(card) for card in services.list_cards(request.user)]})
    data = _body(request)
    card = parse_card(data.get("card"))
    key = _idempotency_header(request) or str(uuid.uuid4())
    saved, created = services.save_card(request.user, card, key)
    return _ok(_card_json(saved), status=201 if created else 200)


@require_http_methods(["DELETE"])
@api_view()
def payment_method_detail(request, payment_method_id):
    public_id = _uuid_or_404(payment_method_id, "payment_method_not_found", "Saved card not found.")
    services.delete_card(request.user, public_id)
    return _ok({"paymentMethodId": str(public_id), "deleted": True})


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@require_GET
@api_view(staff=True)
def reconciliation_report(request):
    start = parse_datetime(request.GET.get("from"), "from")
    end = parse_datetime(request.GET.get("to"), "to")
    try:
        report = reconciliation.build_report(start, end)
    except reconciliation.InvalidRange as exc:
        raise InvalidRequest(str(exc))
    return _ok(report)
