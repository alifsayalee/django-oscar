"""
JSON API for orders, PayPal payments and saved cards.

Callers authenticate with Django's session login (``/api/session`` or the
storefront's own login page); CSRF protection applies to every unsafe
method, so clients send the ``csrftoken`` cookie value in ``X-CSRFToken``.
"""

import functools
import json
import logging
import re
from datetime import date, datetime, timezone as dt_timezone

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables
from oscar.apps.payment import bankcards

from . import services
from .gateway import AddressInput, CardInput, PayPalError
from .models import PayPalPayment

logger = logging.getLogger("apps.paypal_payments")


# =======
# Helpers
# =======


def error(status, code, message, **extra):
    return JsonResponse(
        {"error": {"code": code, "message": message, **extra}}, status=status
    )


class BadRequest(Exception):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.message = message
        self.code = code


def api(methods, staff=False, login_required=True):
    """
    Method dispatch, session authentication, staff gating and error mapping.

    Views run outside ATOMIC_REQUESTS: the services commit PayPal request ids
    before calling PayPal, and those must survive a failed call.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = error(
                    405, "method_not_allowed", "Use %s." % ", ".join(methods)
                )
                response["Allow"] = ", ".join(methods)
                return response
            if login_required and not request.user.is_authenticated:
                return error(
                    401, "not_authenticated", "Sign in first (POST /api/session)."
                )
            if staff and not request.user.is_staff:
                return error(
                    403, "forbidden", "This action is restricted to staff operators."
                )
            try:
                return view(request, *args, **kwargs)
            except BadRequest as e:
                return error(400, e.code, e.message)
            except services.ServiceError as e:
                return error(e.http_status, e.code, e.message, **e.extra)
            except PayPalError as e:
                return error(e.http_status, e.code, e.message)
            except ImproperlyConfigured as e:
                logger.error("PayPal configuration error: %s", e)
                return error(
                    503,
                    "paypal_not_configured",
                    "Payments are not configured on this site.",
                )

        return transaction.non_atomic_requests(wrapper)

    return decorator


def json_body(request):
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("Request body must be JSON.")
    if not isinstance(body, dict):
        raise BadRequest("Request body must be a JSON object.")
    return body


def _str(data, key, *, required=False, max_length=255):
    value = data.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise BadRequest("%s must be a string." % key)
    value = value.strip()
    if required and not value:
        raise BadRequest("%s is required." % key)
    if len(value) > max_length:
        raise BadRequest("%s is too long." % key)
    return value


def _money(value):
    return None if value is None else str(value)


def _iso(value):
    return value.isoformat() if value else None


def _idempotency_key(request, body):
    key = request.headers.get("Idempotency-Key") or body.get("idempotencyKey") or ""
    if not isinstance(key, str):
        raise BadRequest("idempotencyKey must be a string.")
    key = key.strip()
    if len(key) > 128:
        raise BadRequest("Idempotency-Key is too long (max 128).")
    return key


# ==============
# Input parsing
# ==============


def _address(data, field):
    if not isinstance(data, dict):
        raise BadRequest("%s must be an object." % field)
    country = _str(data, "countryCode", required=True, max_length=2).upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise BadRequest(
            "%s.countryCode must be a two-letter ISO country code." % field
        )
    return country


@sensitive_variables("data", "number", "security_code")
def parse_card(data):
    """Validate raw card details. Never logged; never stored."""
    if not isinstance(data, dict):
        raise BadRequest("card must be an object.")
    number = re.sub(r"[\s-]", "", str(data.get("number") or ""))
    if not re.fullmatch(r"\d{12,19}", number) or not bankcards.luhn(number):
        raise BadRequest("card.number is not a valid card number.", code="invalid_card")
    expiry = str(data.get("expiry") or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})", expiry)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise BadRequest("card.expiry must be YYYY-MM.", code="invalid_card")
    today = date.today()
    if (int(match.group(1)), int(match.group(2))) < (today.year, today.month):
        raise BadRequest("card.expiry is in the past.", code="invalid_card")
    security_code = str(data.get("securityCode") or "").strip()
    if not re.fullmatch(r"\d{3,4}", security_code):
        raise BadRequest(
            "card.securityCode must be 3 or 4 digits.", code="invalid_card"
        )
    name = _str(data, "name", max_length=300)
    billing = None
    if data.get("billingAddress") is not None:
        raw = data["billingAddress"]
        country = _address(raw, "card.billingAddress")
        billing = AddressInput(
            country_code=country,
            line1=_str(raw, "line1", max_length=300),
            line2=_str(raw, "line2", max_length=300),
            city=_str(raw, "city", max_length=120),
            state=_str(raw, "state", max_length=300),
            postal_code=_str(raw, "postalCode", max_length=60),
        )
    return CardInput(
        number=number,
        expiry=expiry,
        security_code=security_code,
        name=name,
        billing_address=billing,
    )


def _shipping_address(data):
    country = _address(data, "shippingAddress")
    return services.AddressData(
        first_name=_str(data, "firstName"),
        last_name=_str(data, "lastName"),
        line1=_str(data, "line1", required=True),
        line2=_str(data, "line2"),
        city=_str(data, "city"),
        state=_str(data, "state"),
        postcode=_str(data, "postalCode", max_length=64),
        country_code=country,
        phone_number=_str(data, "phoneNumber", max_length=32),
    )


def _datetime_param(request, name):
    raw = request.GET.get(name)
    if not raw:
        raise BadRequest(
            "Query parameter '%s' is required (ISO-8601 date-time)." % name
        )
    value = parse_datetime(raw.replace(" ", "+"))
    if value is None:
        raise BadRequest("Query parameter '%s' must be an ISO-8601 date-time." % name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


# =============
# Serialization
# =============


def payment_json(payment):
    if payment is None:
        return None
    capture = None
    if payment.capture_id:
        capture = {
            "id": payment.capture_id,
            "status": payment.capture_status,
            "amount": _money(payment.captured_amount),
            "paypalFee": _money(payment.paypal_fee),
            "netAmount": _money(payment.net_amount),
            "capturedAt": _iso(payment.captured_at),
        }
    return {
        "status": payment.status,
        "amount": _money(payment.amount),
        "currency": payment.currency,
        "card": (
            {
                "brand": payment.card_brand or None,
                "lastDigits": payment.card_last_digits or None,
            }
            if payment.card_last_digits
            else None
        ),
        "paymentMethodId": str(payment.bankcard_id) if payment.bankcard_id else None,
        "authorization": (
            {
                "id": payment.authorization_id,
                "status": payment.authorization_status,
                "paypalOrderId": payment.paypal_order_id,
                "authorizedAt": _iso(payment.authorized_at),
                "expiresAt": _iso(payment.authorization_expires_at),
            }
            if payment.authorization_id
            else None
        ),
        "capture": capture,
        "refunds": [refund_json(r) for r in payment.refunds.all()],
        "refundedAmount": (
            _money(payment.refunded_amount) if payment.capture_id else "0.00"
        ),
        "refundableAmount": _money(payment.refundable_amount),
        "lastError": payment.last_error or None,
    }


def refund_json(refund):
    return {
        "refundId": refund.paypal_refund_id or None,
        "status": refund.status,
        "paypalStatus": refund.paypal_status or None,
        "amount": _money(refund.amount),
        "idempotencyKey": refund.idempotency_key,
        "createdAt": _iso(refund.date_created),
    }


def _fresh(order_pk):
    return services.Order.objects.select_related("paypal_payment").get(pk=order_pk)


def order_json(order):
    try:
        payment = order.paypal_payment
    except PayPalPayment.DoesNotExist:
        payment = None
    return {
        "orderId": str(order.number),
        "status": order.status,
        "placedAt": _iso(order.date_placed),
        "currency": order.currency,
        "total": _money(order.total_incl_tax),
        "shippingTotal": _money(order.shipping_incl_tax),
        "lines": [
            {
                "itemId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _money(line.unit_price_incl_tax),
                "lineTotal": _money(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": payment_json(payment),
    }


def card_json(bankcard):
    return {
        "paymentMethodId": str(bankcard.pk),
        "brand": bankcard.card_type,
        "lastDigits": bankcard.number[-4:],
        "expiry": bankcard.expiry_date.strftime("%Y-%m"),
        "label": "%s ending %s" % (bankcard.card_type, bankcard.number[-4:]),
    }


def user_json(user):
    return {
        "id": user.pk,
        "username": user.get_username(),
        "email": user.email,
        "isStaff": user.is_staff,
    }


# =======
# Session
# =======


@ensure_csrf_cookie
@api(["GET", "POST", "DELETE"], login_required=False)
@sensitive_variables("body", "password")
def session(request):
    if request.method == "GET":
        return JsonResponse(
            {
                "authenticated": request.user.is_authenticated,
                "user": (
                    user_json(request.user) if request.user.is_authenticated else None
                ),
                "csrfToken": get_token(request),
            }
        )
    if request.method == "DELETE":
        logout(request)
        return JsonResponse({"authenticated": False, "csrfToken": get_token(request)})
    body = json_body(request)
    username = _str(body, "username") or _str(body, "email")
    password = body.get("password")
    if not username or not isinstance(password, str) or not password:
        raise BadRequest("username (or email) and password are required.")
    user = authenticate(request, username=username, password=password)
    if user is None:
        return error(401, "invalid_credentials", "Invalid username or password.")
    login(request, user)
    return JsonResponse(
        {
            "authenticated": True,
            "user": user_json(user),
            "csrfToken": get_token(request),
        }
    )


# ======
# Orders
# ======


@api(["POST"])
def orders(request):
    body = json_body(request)
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise BadRequest("items must be a non-empty list of {itemId, quantity}.")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise BadRequest("Each item must be an object.")
        item_id = raw.get("itemId", raw.get("productId"))
        quantity = raw.get("quantity", 1)
        if isinstance(item_id, str) and item_id.isdigit():
            item_id = int(item_id)
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            raise BadRequest("itemId must be a catalogue item id.")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            raise BadRequest("quantity must be a positive integer.")
        items.append((item_id, quantity))
    address = None
    if body.get("shippingAddress") is not None:
        address = _shipping_address(body["shippingAddress"])
    order = services.place_order(request.user, items, address)
    return JsonResponse(order_json(order), status=201)


@api(["POST"])
@sensitive_variables("body", "card")
def pay(request, order_id):
    body = json_body(request)
    card = parse_card(body["card"]) if body.get("card") is not None else None
    method_id = body.get("paymentMethodId")
    if method_id is not None and not isinstance(method_id, (str, int)):
        raise BadRequest("paymentMethodId must be a string.")
    if (card is None) == (method_id is None):
        raise BadRequest("Send exactly one of card or paymentMethodId.")
    payment, created = services.pay(
        request.user,
        order_id,
        card=card,
        payment_method_id=str(method_id) if method_id is not None else None,
    )
    return JsonResponse(
        order_json(_fresh(payment.order_id)), status=201 if created else 200
    )


@api(["POST"], staff=True)
def fulfil(request, order_id):
    payment, _ = services.fulfil(order_id)
    return JsonResponse(order_json(_fresh(payment.order_id)))


@api(["POST"], staff=True)
def cancel(request, order_id):
    order, _ = services.cancel(order_id)
    return JsonResponse(order_json(_fresh(order.pk)))


@api(["POST"])
def refunds(request, order_id):
    body = json_body(request)
    key = _idempotency_key(request, body)
    if not key:
        raise BadRequest(
            "An Idempotency-Key header (or idempotencyKey field) is required.",
            code="idempotency_key_required",
        )
    amount = (
        services.parse_amount(body["amount"])
        if body.get("amount") is not None
        else None
    )
    refund, created = services.refund(
        request.user,
        order_id,
        idempotency_key=key,
        amount=amount,
        reason=_str(body, "reason"),
    )
    order = refund.payment.order
    return JsonResponse(
        {
            "refundId": refund.paypal_refund_id,
            **refund_json(refund),
            "orderId": str(order.number),
            "payment": payment_json(refund.payment),
        },
        status=201 if created else 200,
    )


@api(["GET"])
def my_orders(request):
    qs = (
        services.Order.objects.filter(user=request.user)
        .select_related("paypal_payment")
        .prefetch_related("lines", "paypal_payment__refunds")
        .order_by("-date_placed")
    )
    return JsonResponse({"orders": [order_json(o) for o in qs]})


@api(["GET"], staff=True)
def reconciliation(request):
    start = _datetime_param(request, "from")
    end = _datetime_param(request, "to")
    if end <= start:
        raise BadRequest("'to' must be after 'from'.")
    now = datetime.now(dt_timezone.utc)
    if end > now:
        end = now
    if start >= end:
        raise BadRequest("'from' must be in the past.")
    return JsonResponse(services.reconciliation(start, end))


# ===========
# Saved cards
# ===========


@api(["GET", "POST"])
@sensitive_variables("body", "card")
def payment_methods(request):
    if request.method == "GET":
        return JsonResponse(
            {
                "paymentMethods": [
                    card_json(c) for c in services.saved_cards(request.user)
                ]
            }
        )
    body = json_body(request)
    if body.get("card") is None:
        raise BadRequest("card is required.")
    card = parse_card(body["card"])
    bankcard, created = services.save_card(
        request.user, card, idempotency_key=_idempotency_key(request, body) or None
    )
    return JsonResponse(card_json(bankcard), status=201 if created else 200)


@api(["DELETE"])
def payment_method(request, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return HttpResponse(status=204)
