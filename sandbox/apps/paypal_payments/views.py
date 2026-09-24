"""
JSON endpoints of the PayPal payments API (mounted under /api/).

Callers authenticate with the sandbox's own Django session login; the caller's
identity is always ``request.user``. Fulfil, cancel and reconciliation are
restricted to staff. Everything else acts only on the caller's own orders and
cards (another shopper's order or card answers 404, never 403, so its existence
is not disclosed).

Views that call PayPal opt out of ATOMIC_REQUESTS: their claim rows must be
committed before PayPal is called, and must survive a failure after it.
"""
import datetime
import functools
import json
import logging
import re
import uuid
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods

from . import gateway, services
from .gateway import CardDetails
from .models import PayPalPayment, PayPalRefund, PayPalSavedCard
from .services import Outcome, ServiceError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def error_response(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status)


def api_view(*, staff: bool = False):
    """Authentication, authorisation and error translation for an API view."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args, **kwargs):
            if not request.user.is_authenticated:
                return error_response(401, "not_authenticated", "Sign in first (session login).")
            if staff and not request.user.is_staff:
                return error_response(403, "forbidden", "This action is restricted to staff.")
            try:
                return view(request, *args, **kwargs)
            except ServiceError as exc:
                return error_response(exc.status_code, exc.code, exc.message, **exc.extra)

        return wrapper

    return decorator


def read_json(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ServiceError(400, "invalid_json", "The request body must be JSON.")
    if not isinstance(data, dict):
        raise ServiceError(400, "invalid_json", "The request body must be a JSON object.")
    return data


def fmt(amount, currency: str) -> str | None:
    return None if amount is None else gateway.format_amount(amount, currency)


def iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value else None


def outcome_response(outcome: Outcome, body: dict[str, Any]) -> JsonResponse:
    if outcome.notice:
        body["notice"] = outcome.notice
    return JsonResponse(body, status=outcome.status_code)


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def refund_json(refund: PayPalRefund) -> dict[str, Any]:
    return {
        "refundId": str(refund.public_id),
        "status": refund.status,
        "amount": fmt(refund.amount, refund.currency),
        "currency": refund.currency,
        "paypalRefundId": refund.paypal_refund_id or None,
        "paypalStatus": refund.paypal_status or None,
        "refundedAt": iso(refund.refunded_at),
        "failure": {"code": refund.failure_code, "message": refund.failure_message}
        if refund.failure_message else None,
        "createdAt": iso(refund.created_at),
    }


def payment_json(payment: PayPalPayment) -> dict[str, Any]:
    c = payment.currency
    return {
        "status": payment.status,
        "amount": fmt(payment.amount, c),
        "currency": c,
        "card": {"brand": payment.card_brand, "lastDigits": payment.card_last_digits}
        if payment.card_last_digits else None,
        "paymentMethodId": str(payment.saved_card.public_id) if payment.saved_card_id else None,
        "paypal": {
            "orderId": payment.paypal_order_id or None,
            "orderStatus": payment.paypal_order_status or None,
            "authorizationId": payment.authorization_id or None,
            "authorizationStatus": payment.authorization_status or None,
            "authorizedAt": iso(payment.authorized_at),
            "authorizationExpiresAt": iso(payment.authorization_expires_at),
            "previousAuthorizationIds": payment.previous_authorization_ids,
            "captureId": payment.capture_id or None,
            "captureStatus": payment.capture_status or None,
        },
        "capturedAmount": fmt(payment.captured_amount, c),
        "paypalFee": fmt(payment.paypal_fee, c),
        "netAmount": fmt(payment.net_amount, c),
        "capturedAt": iso(payment.captured_at),
        "refundedAmount": fmt(payment.refunded_amount, c),
        "refundableAmount": fmt(payment.refundable_amount, c)
        if payment.status in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED) else fmt(Decimal("0"), c),
        "refunds": [refund_json(r) for r in payment.refunds.all()],
        "voidedAt": iso(payment.voided_at),
        "failure": {"code": payment.failure_code, "message": payment.failure_message}
        if payment.failure_message else None,
        "createdAt": iso(payment.created_at),
    }


def order_json(order) -> dict[str, Any]:
    payments = list(
        PayPalPayment.objects.filter(order=order).select_related("saved_card").prefetch_related("refunds")
    )
    live = next((p for p in payments if p.status not in PayPalPayment.CLOSED_STATUSES), None)
    current = live or (payments[0] if payments else None)
    c = order.currency
    return {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "paymentState": current.status if current else "awaiting_payment",
        "currency": c,
        "total": fmt(order.total_incl_tax, c),
        "datePlaced": iso(order.date_placed),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": fmt(line.unit_price_incl_tax, c),
                "linePrice": fmt(line.line_price_incl_tax, c),
            }
            for line in order.lines.all()
        ],
        "payment": payment_json(current) if current else None,
        "previousPayments": [payment_json(p) for p in payments if p is not current],
    }


def card_json(card: PayPalSavedCard) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.public_id),
        "status": card.status,
        "brand": card.brand,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "label": ("%s ending %s" % (card.brand or "Card", card.last_digits)).strip(),
        "createdAt": iso(card.created_at),
    }


# ---------------------------------------------------------------------------
# Card input
# ---------------------------------------------------------------------------

EXPIRY_RE = re.compile(r"^(?:(\d{4})-(\d{2})|(\d{2})\s*/\s*(\d{2,4}))$")


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, digit in enumerate(reversed(number)):
        d = int(digit)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@sensitive_variables("raw", "number", "cvc")
def parse_card(raw: Any) -> CardDetails:
    if not isinstance(raw, dict):
        raise ServiceError(400, "invalid_card", "card must be an object.")
    number = re.sub(r"[\s-]", "", str(raw.get("number", "")))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise ServiceError(400, "invalid_card", "card.number is not a valid card number.")
    cvc = str(raw.get("securityCode", raw.get("cvc", raw.get("cvv", "")))).strip()
    if not cvc.isdigit() or len(cvc) not in (3, 4):
        raise ServiceError(400, "invalid_card", "card.securityCode must be 3 or 4 digits.")
    name = str(raw.get("name", "")).strip()
    if not name or len(name) > 300:
        raise ServiceError(400, "invalid_card", "card.name is required.")
    return CardDetails(
        number=number, expiry=_parse_expiry(raw.get("expiry")), security_code=cvc, name=name,
        billing_address=_parse_billing_address(raw.get("billingAddress")),
    )


def _parse_expiry(raw: Any) -> str:
    """Accept YYYY-MM or MM/YY(YY); return PayPal's YYYY-MM for a month not in the past."""
    match = EXPIRY_RE.match(str(raw or "").strip())
    if not match:
        raise ServiceError(400, "invalid_card", "card.expiry must be YYYY-MM or MM/YY.")
    if match.group(1):
        year, month = int(match.group(1)), int(match.group(2))
    else:
        month, year = int(match.group(3)), int(match.group(4))
        year = year + 2000 if year < 100 else year
    today = datetime.date.today()
    if not 1 <= month <= 12 or (year, month) < (today.year, today.month):
        raise ServiceError(400, "invalid_card", "card.expiry is not a valid future month.")
    return "%04d-%02d" % (year, month)


def _parse_billing_address(address: Any) -> dict[str, str]:
    if not address:
        return {}
    if not isinstance(address, dict):
        raise ServiceError(400, "invalid_card", "card.billingAddress must be an object.")
    country = str(address.get("countryCode", "")).strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ServiceError(400, "invalid_card", "card.billingAddress.countryCode must be a 2-letter ISO code.")
    return {
        "address_line_1": str(address.get("addressLine1", "")).strip()[:300],
        "address_line_2": str(address.get("addressLine2", "")).strip()[:300],
        "admin_area_2": str(address.get("city", "")).strip()[:120],
        "admin_area_1": str(address.get("state", "")).strip()[:300],
        "postal_code": str(address.get("postalCode", "")).strip()[:60],
        "country_code": country,
    }


def idempotency_key(request: HttpRequest, data: dict[str, Any]) -> str:
    return str(request.headers.get("Idempotency-Key") or data.get("idempotencyKey") or "").strip()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@require_GET
def csrf(request):
    """Hands a non-browser client the CSRF token its unsafe requests must echo."""
    return JsonResponse({"csrfToken": get_token(request)})


@require_http_methods(["POST"])
@api_view()
def orders(request):
    data = read_json(request)
    items = services.parse_items(data.get("items"))
    order = services.place_order(request.user, items, request=request)
    return JsonResponse(order_json(order), status=201)


@require_GET
@api_view()
def my_orders(request):
    user_orders = services.Order.objects.filter(user=request.user).order_by("-date_placed", "-pk")
    return JsonResponse({"orders": [order_json(o) for o in user_orders.prefetch_related("lines")]})


@transaction.non_atomic_requests
@sensitive_post_parameters()
@sensitive_variables("data", "card")
@require_http_methods(["POST"])
@api_view()
def pay(request, order_id):
    data = read_json(request)
    payment_method_id = data.get("paymentMethodId")
    has_card = data.get("card") is not None
    if bool(payment_method_id) == has_card:
        raise ServiceError(400, "invalid_payment_source", "Send either card or paymentMethodId (exactly one).")
    card = parse_card(data["card"]) if has_card else None
    outcome = services.pay(request.user, order_id, card=card, payment_method_id=payment_method_id)
    order = outcome.obj.order
    return outcome_response(outcome, order_json(order))


@transaction.non_atomic_requests
@require_http_methods(["POST"])
@api_view(staff=True)
def fulfil(request, order_id):
    outcome = services.fulfil(order_id)
    return outcome_response(outcome, order_json(outcome.obj.order))


@transaction.non_atomic_requests
@require_http_methods(["POST"])
@api_view(staff=True)
def cancel(request, order_id):
    outcome = services.cancel(order_id)
    order = outcome.obj.order if isinstance(outcome.obj, PayPalPayment) else outcome.obj
    return outcome_response(outcome, order_json(order))


@transaction.non_atomic_requests
@require_http_methods(["POST"])
@api_view()
def refunds(request, order_id):
    data = read_json(request)
    outcome = services.refund(
        request.user, order_id, amount_raw=data.get("amount"), idempotency_key=idempotency_key(request, data)
    )
    record = outcome.obj
    body = refund_json(record)
    body["order"] = order_json(record.payment.order)
    return outcome_response(outcome, body)


@transaction.non_atomic_requests
@sensitive_post_parameters()
@sensitive_variables("data", "card")
@require_http_methods(["GET", "POST"])
@api_view()
def payment_methods(request):
    if request.method == "GET":
        return JsonResponse({"paymentMethods": [card_json(c) for c in services.list_cards(request.user)]})
    data = read_json(request)
    card = parse_card(data.get("card", data))
    key = idempotency_key(request, data) or str(uuid.uuid4())
    if len(key) > 128:
        raise ServiceError(400, "invalid_idempotency_key", "Idempotency-Key can be at most 128 characters.")
    outcome = services.save_card(request.user, card, idempotency_key=key)
    return outcome_response(outcome, card_json(outcome.obj))


@transaction.non_atomic_requests
@require_http_methods(["DELETE"])
@api_view()
def payment_method(request, payment_method_id):
    outcome = services.delete_card(request.user, payment_method_id)
    if outcome.status_code == 204:
        return HttpResponse(status=204)
    return outcome_response(outcome, card_json(outcome.obj))


@transaction.non_atomic_requests
@require_GET
@api_view(staff=True)
def reconciliation(request):
    start = services.parse_datetime_param(request.GET.get("from"), "from")
    end = services.parse_datetime_param(request.GET.get("to"), "to")
    return JsonResponse(services.reconcile(start, end))
