"""
JSON API for PayPal payments and saved cards.

Callers authenticate with Django's session login (``POST /api/auth/login`` or
the storefront login form) and send the ``csrftoken`` cookie value back in an
``X-CSRFToken`` header on every unsafe request. Operator endpoints need ``is_staff``.

The views run outside the sandbox's per-request transaction (ATOMIC_REQUESTS):
a payment claim must be committed before PayPal is called, and PayPal's answer
recorded even when the request later fails.
"""

import json
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .errors import ProviderError
from .gateway import CardDetails

logger = logging.getLogger("apps.paypal_payments")

MAX_BODY_BYTES = 64 * 1024
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,255}$")
EXPIRY_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


class BadRequest(Exception):
    pass


def error(status, message, code=None, **extra):
    body = {"error": {"message": message}}
    if code:
        body["error"]["code"] = code
    body["error"].update(extra)
    return JsonResponse(body, status=status)


def api(view):
    """Turn every failure a view can hit into a JSON answer. Never echoes request bodies."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except BadRequest as e:
            return error(400, str(e), "BAD_REQUEST")
        except services.PaymentError as e:
            extra = {"payment": payment_json(e.payment)} if e.payment is not None else {}
            return error(e.status_code, e.message, e.code, **extra)
        except ProviderError as e:
            extra = {"outcomeUnknown": e.outcome_unknown}
            if e.provider_error:
                extra["paypalError"] = e.provider_error
            return error(e.status_code, e.message, "PAYPAL_ERROR", **extra)

    return wrapper


def login_required_json(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error(401, "Sign in first.", "NOT_AUTHENTICATED")
        return view(request, *args, **kwargs)

    return wrapper


def staff_required_json(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error(401, "Sign in first.", "NOT_AUTHENTICATED")
        if not request.user.is_staff:
            return error(403, "This action is restricted to staff.", "FORBIDDEN")
        return view(request, *args, **kwargs)

    return wrapper


def read_json(request):
    if len(request.body) > MAX_BODY_BYTES:
        raise BadRequest("Request body too large.")
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("Request body must be JSON.") from None
    if not isinstance(data, dict):
        raise BadRequest("Request body must be a JSON object.")
    return data


def idempotency_key(request, data, required):
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    if key is None:
        if required:
            raise BadRequest("An Idempotency-Key header (or idempotencyKey field) is required.")
        return None
    if not isinstance(key, str) or not IDEMPOTENCY_KEY_RE.match(key):
        raise BadRequest("Idempotency-Key must be 1-255 characters of letters, digits and . _ : -")
    return key


def parse_card(data):
    if not isinstance(data, dict):
        raise BadRequest("card must be an object.")
    number = re.sub(r"[\s-]", "", str(data.get("number", "")))
    if not number.isdigit() or not 12 <= len(number) <= 19:
        raise BadRequest("card.number must be 12-19 digits.")
    expiry = str(data.get("expiry", ""))
    if not EXPIRY_RE.match(expiry):
        raise BadRequest("card.expiry must be YYYY-MM.")
    security_code = str(data.get("securityCode", ""))
    if not security_code.isdigit() or not 3 <= len(security_code) <= 4:
        raise BadRequest("card.securityCode must be 3 or 4 digits.")
    name = data.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 300):
        raise BadRequest("card.name must be a string.")
    address = data.get("billingAddress")
    billing = None
    if address is not None:
        if not isinstance(address, dict):
            raise BadRequest("card.billingAddress must be an object.")
        mapping = {
            "addressLine1": "address_line_1", "addressLine2": "address_line_2", "adminArea1": "admin_area_1",
            "adminArea2": "admin_area_2", "postalCode": "postal_code", "countryCode": "country_code",
        }
        billing = {wire: str(address[k]) for k, wire in mapping.items() if address.get(k)}
        if "country_code" not in billing:
            raise BadRequest("card.billingAddress.countryCode is required.")
    return CardDetails(number=number, expiry=expiry, security_code=security_code, name=name, billing_address=billing)


def amount_str(value):
    return str(value) if value is not None else None


def ts(value):
    return value.isoformat() if value else None


def payment_json(payment):
    if payment is None:
        return None
    captured = payment.captured_amount
    return {
        "status": payment.status,
        "amount": amount_str(payment.amount),
        "currency": payment.currency,
        "paypalOrderId": payment.paypal_order_id or None,
        "card": {"brand": payment.card_brand or None, "lastDigits": payment.card_last_digits}
        if payment.card_last_digits else None,
        "paymentMethodId": payment.bankcard_id,
        "authorization": {
            "id": payment.authorization_id,
            "status": payment.authorization_status or None,
            "amount": amount_str(payment.authorized_amount),
            "createdAt": ts(payment.authorized_at),
            "expiresAt": ts(payment.authorization_expires_at),
            "reauthorizations": payment.reauthorization_count,
        } if payment.authorization_id else None,
        "capture": {
            "id": payment.capture_id,
            "status": payment.capture_status or None,
            "amount": amount_str(captured),
            "paypalFee": amount_str(payment.paypal_fee),
            "netAmount": amount_str(payment.net_amount),
            "capturedAt": ts(payment.captured_at),
        } if payment.capture_id else None,
        "refundedAmount": amount_str(payment.refunded_amount),
        "refundableAmount": amount_str(captured - payment.refund_reserved) if captured is not None else None,
        "refunds": [refund_json(r) for r in payment.refunds.order_by("created")],
        "pendingOperation": payment.pending_operation or None,
        "lastError": payment.last_error or None,
    }


def refund_json(refund):
    return {
        "refundId": str(refund.reference),
        "status": refund.status,
        "amount": amount_str(refund.amount),
        "paypalRefundId": refund.paypal_refund_id or None,
        "idempotencyKey": refund.idempotency_key,
        "refundedAt": ts(refund.refunded_at),
        "lastError": refund.last_error or None,
    }


def order_json(order):
    try:
        payment = order.paypal_payment
    except services.PayPalPayment.DoesNotExist:
        payment = None
    return {
        "orderId": str(order.number),
        "status": order.status,
        "total": amount_str(order.total_incl_tax),
        "currency": order.currency,
        "placedAt": ts(order.date_placed),
        "lines": [
            {
                "itemId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": amount_str(line.unit_price_incl_tax),
                "linePrice": amount_str(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": payment_json(payment),
    }


def card_json(bankcard):
    return {
        "paymentMethodId": bankcard.pk,
        "brand": bankcard.card_type,
        "lastDigits": bankcard.number[-4:],
        "expiry": bankcard.expiry_date.strftime("%Y-%m"),
        "label": f"{bankcard.card_type} ending {bankcard.number[-4:]} (expires {bankcard.expiry_date:%m/%Y})",
    }


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@transaction.non_atomic_requests
@require_GET
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({"csrfToken": get_token(request)})


@transaction.non_atomic_requests
@require_POST
@api
def session_login(request):
    data = read_json(request)
    user = authenticate(request, username=str(data.get("username", "")), password=str(data.get("password", "")))
    if user is None or not user.is_active:
        return error(401, "Invalid credentials.", "INVALID_CREDENTIALS")
    login(request, user)
    return JsonResponse({"userId": user.pk, "username": user.get_username(), "isStaff": user.is_staff,
                         "csrfToken": get_token(request)})


@transaction.non_atomic_requests
@require_POST
def session_logout(request):
    logout(request)
    return JsonResponse({"signedOut": True})


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


@transaction.non_atomic_requests
@require_POST
@login_required_json
@api
def orders(request):
    data = read_json(request)
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise BadRequest("items must be a list of {itemId, quantity}.")
    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise BadRequest("items must be a list of {itemId, quantity}.")
        item_id, quantity = item.get("itemId"), item.get("quantity", 1)
        if not isinstance(item_id, int) or isinstance(item_id, bool) or not isinstance(quantity, int):
            raise BadRequest("itemId and quantity must be integers.")
        items.append((item_id, quantity))
    order = services.place_order(request.user, items)
    return JsonResponse({"orderId": str(order.number), "order": order_json(order)}, status=201)


@transaction.non_atomic_requests
@require_GET
@login_required_json
@api
def my_orders(request):
    return JsonResponse({"orders": [order_json(o) for o in services.orders_for(request.user)]})


@transaction.non_atomic_requests
@require_POST
@login_required_json
@api
def pay(request, order_id):
    order = services.get_order_for(request.user, order_id)
    data = read_json(request)
    card, method_id = data.get("card"), data.get("paymentMethodId")
    if (card is None) == (method_id is None):
        raise BadRequest("Send exactly one of card or paymentMethodId.")
    if method_id is not None and (not isinstance(method_id, int) or isinstance(method_id, bool)):
        raise BadRequest("paymentMethodId must be an integer.")
    payment = services.pay_order(
        request.user, order, card=parse_card(card) if card is not None else None, bankcard_id=method_id
    )
    order.refresh_from_db()
    return JsonResponse({"orderId": str(order.number), "status": order.status, "payment": payment_json(payment)})


@transaction.non_atomic_requests
@require_POST
@staff_required_json
@api
def fulfil(request, order_id):
    order = services.get_order_any(order_id)
    payment = services.fulfil_order(order)
    order.refresh_from_db()
    return JsonResponse({"orderId": str(order.number), "status": order.status, "payment": payment_json(payment)})


@transaction.non_atomic_requests
@require_POST
@staff_required_json
@api
def cancel(request, order_id):
    order = services.get_order_any(order_id)
    payment = services.cancel_order(order)
    order.refresh_from_db()
    return JsonResponse({"orderId": str(order.number), "status": order.status, "payment": payment_json(payment)})


@transaction.non_atomic_requests
@require_POST
@login_required_json
@api
def refunds(request, order_id):
    order = services.get_order_for(request.user, order_id)
    data = read_json(request)
    key = idempotency_key(request, data, required=True)
    amount = data.get("amount")
    if amount is not None:
        try:
            amount = Decimal(str(amount))
        except InvalidOperation:
            raise BadRequest("amount must be a decimal string such as \"5.00\".") from None
        if not amount.is_finite():
            raise BadRequest("amount must be a finite number.")
    refund, created = services.refund_order(request.user, order, idempotency_key=key, amount=amount)
    refund.payment.refresh_from_db()
    return JsonResponse(
        {"refundId": str(refund.reference), "orderId": str(order.number), "refund": refund_json(refund),
         "payment": payment_json(refund.payment)},
        status=201 if created else 200,
    )


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------


@transaction.non_atomic_requests
@require_http_methods(["GET", "POST"])
@login_required_json
@api
def payment_methods(request):
    if request.method == "GET":
        return JsonResponse({"paymentMethods": [card_json(c) for c in services.cards_for(request.user)]})
    data = read_json(request)
    key = idempotency_key(request, data, required=False)
    card = parse_card(data.get("card"))
    bankcard, created = services.save_card(request.user, card, key)
    return JsonResponse(
        {"paymentMethodId": bankcard.pk, "paymentMethod": card_json(bankcard)}, status=201 if created else 200
    )


@transaction.non_atomic_requests
@require_http_methods(["DELETE"])
@login_required_json
@api
def payment_method(request, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return JsonResponse({"paymentMethodId": payment_method_id, "deleted": True})


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def _parse_datetime(name, value):
    if not value:
        raise BadRequest(f"{name} is required (ISO-8601 date-time).")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "+"))
    except ValueError:
        raise BadRequest(f"{name} must be an ISO-8601 date-time.") from None


@transaction.non_atomic_requests
@require_GET
@staff_required_json
@api
def reconciliation(request):
    start = _parse_datetime("from", request.GET.get("from"))
    end = _parse_datetime("to", request.GET.get("to"))
    if (start.tzinfo is None) != (end.tzinfo is None):
        raise BadRequest("from and to must both carry a UTC offset, or neither.")
    if start >= end:
        raise BadRequest("from must be before to.")
    return JsonResponse(services.reconcile(start, end))
