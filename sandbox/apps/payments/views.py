"""JSON API under /api/. Session-authenticated (Django login); CSRF enforced on unsafe methods.

Every view opts out of ATOMIC_REQUESTS: a payment claim must be committed before PayPal is called.
"""

import functools
import json
import logging
import re
from datetime import date

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from oscar.core.loading import get_model

from . import paypal_gateway as gw
from . import serializers, services
from .models import PaymentOperation, PayPalPayment
from .services import ApiProblem

log = logging.getLogger(__name__)
Order = get_model("order", "Order")
Bankcard = get_model("payment", "Bankcard")


def problem(status, code, message, **extra):
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status)


def api(methods, *, staff=False, authenticated=True):
    """Method, authentication and role checks, JSON errors, and no request-wide transaction."""

    def decorate(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return problem(405, "method_not_allowed", f"Use {' or '.join(methods)}.")
            if authenticated and not request.user.is_authenticated:
                return problem(401, "not_authenticated", "Log in first (POST /api/login).")
            if staff and not request.user.is_staff:
                return problem(403, "forbidden", "This action is restricted to staff.")
            try:
                return view(request, *args, **kwargs)
            except ApiProblem as e:
                return problem(e.status_code, e.code, e.message, **e.extra)
            except gw.ProviderError as e:
                extra = {"outcomeUnknown": e.outcome_unknown}
                if e.issues:
                    extra["paypalIssues"] = e.issues
                if e.debug_id:
                    extra["paypalDebugId"] = e.debug_id
                return problem(e.status_code, e.code, e.message, **extra)
            except gw.ConfigurationError as e:
                log.error("PayPal configuration error: %s", e)
                return problem(503, "payments_unavailable", "Payments are not configured on this site.")
            except Exception:
                # Answered here so Django's DEBUG error page never renders frame locals (card data).
                log.exception("Unhandled error in %s", view.__name__)
                return problem(500, "internal_error", "Unexpected error; it has been logged.")

        return transaction.non_atomic_requests(wrapper)

    return decorate


def _body(request):
    if not request.body:
        return {}
    if request.content_type != "application/json":
        raise ApiProblem(415, "unsupported_media_type", "Send a JSON body (Content-Type: application/json).")
    try:
        data = json.loads(request.body)
    except ValueError as e:
        raise ApiProblem(400, "invalid_json", "The request body is not valid JSON.") from e
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_json", "The request body must be a JSON object.")
    return data


def _idempotency_key(request, data):
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 255):
        raise ApiProblem(400, "invalid_idempotency_key", "Idempotency-Key must be 1-255 characters.")
    return key


def _luhn_ok(number):
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


@sensitive_variables()
def _card(data):
    """Validate card input. The values live only in this request; errors never echo them."""
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_card", "card must be an object.")
    number = re.sub(r"[\s-]", "", str(data.get("number") or ""))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise ApiProblem(400, "invalid_card", "card.number is not a valid card number.")
    expiry = str(data.get("expiry") or "")
    match = re.fullmatch(r"(\d{4})-(\d{2})", expiry) or re.fullmatch(r"(\d{2})/(\d{2,4})", expiry)
    if not match:
        raise ApiProblem(400, "invalid_card", "card.expiry must be YYYY-MM (or MM/YY).")
    if "-" in expiry:
        year, month = int(match.group(1)), int(match.group(2))
    else:
        month, year = int(match.group(1)), int(match.group(2))
        year = year + 2000 if year < 100 else year
    today = date.today()
    if not 1 <= month <= 12 or (year, month) < (today.year, today.month):
        raise ApiProblem(400, "invalid_card", "card.expiry must be a current month or later.")
    cvc = str(data.get("securityCode") or data.get("cvc") or data.get("cvv") or "")
    if not cvc.isdigit() or not 3 <= len(cvc) <= 4:
        raise ApiProblem(400, "invalid_card", "card.securityCode must be 3 or 4 digits.")
    billing = data.get("billingAddress")
    address = None
    if billing is not None:
        if not isinstance(billing, dict) or not re.fullmatch(r"[A-Za-z]{2}", str(billing.get("countryCode") or "")):
            raise ApiProblem(400, "invalid_card", "card.billingAddress.countryCode must be a 2-letter code.")
        address = gw.BillingAddress(
            country_code=billing["countryCode"].upper(),
            address_line_1=billing.get("line1"),
            address_line_2=billing.get("line2"),
            admin_area_2=billing.get("city"),
            admin_area_1=billing.get("state"),
            postal_code=billing.get("postalCode"),
        )
    name = data.get("name")
    return gw.CardInput(
        number=number,
        expiry=f"{year:04d}-{month:02d}",
        security_code=cvc,
        name=str(name)[:300] if name else None,
        billing_address=address,
    )


# --------------------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------------------


@csrf_exempt  # JSON-only (a cross-site form cannot send application/json without a CORS preflight)
@sensitive_variables("data", "password")
@api(["POST"], authenticated=False)
def login_view(request):
    data = _body(request)
    identifier, password = data.get("username") or data.get("email"), data.get("password")
    if not identifier or not password:
        raise ApiProblem(400, "invalid_request", "username (or email) and password are required.")
    user = authenticate(request, username=identifier, password=password)
    if user is None or not user.is_active:
        raise ApiProblem(401, "invalid_credentials", "Invalid username/email or password.")
    login(request, user)
    return JsonResponse(
        {"userId": user.pk, "email": user.email, "isStaff": user.is_staff, "csrfToken": get_token(request)}
    )


@api(["POST"])
def logout_view(request):
    logout(request)
    return JsonResponse({"loggedOut": True})


# --------------------------------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------------------------------


def _load_order(order):
    return Order.objects.select_related("paypal_payment").get(pk=order.pk)


@api(["POST"])
def orders_view(request):
    data = _body(request)
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ApiProblem(400, "invalid_request", "items must be a non-empty list of {itemId, quantity}.")
    items = []
    for raw in raw_items:
        item_id = raw.get("itemId", raw.get("productId")) if isinstance(raw, dict) else None
        quantity = raw.get("quantity", 1) if isinstance(raw, dict) else None
        if not isinstance(item_id, int) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
            raise ApiProblem(400, "invalid_request", "Each item needs an integer itemId and quantity (1-100).")
        items.append(services.OrderItem(product_id=item_id, quantity=quantity))
    shipping = data.get("shippingAddress")
    if shipping is not None and not isinstance(shipping, dict):
        raise ApiProblem(400, "invalid_request", "shippingAddress must be an object.")
    order, created = services.place_order(
        request.user, items, shipping_address=shipping, idempotency_key=_idempotency_key(request, data)
    )
    if order.user_id != request.user.pk:
        raise ApiProblem(404, "order_not_found", "Order not found.")
    return JsonResponse(serializers.order_json(order), status=201 if created else 200)


@sensitive_variables()
@api(["POST"])
def pay_view(request, order_id):
    order = services.order_for(request.user, order_id)
    data = _body(request)
    card_data, method_id = data.get("card"), data.get("paymentMethodId")
    if (card_data is None) == (method_id is None):
        raise ApiProblem(400, "invalid_request", "Send exactly one of card or paymentMethodId.")
    if method_id is not None and not isinstance(method_id, int):
        raise ApiProblem(400, "invalid_request", "paymentMethodId must be an integer.")
    card = _card(card_data) if card_data is not None else None
    op = services.pay(order, request.user, card=card, bankcard_id=method_id)
    order = _load_order(order)
    body = {
        "orderId": order.number,
        "orderStatus": order.status,
        "payment": serializers.payment_json(order.paypal_payment),
    }
    if op is None:  # another request is starting this order's payment right now
        return JsonResponse(body, status=202)
    if op.outcome == PaymentOperation.DONE:
        return JsonResponse(body, status=200)
    if op.outcome == PaymentOperation.FAILED:
        return problem(
            402,
            "payment_declined",
            "The card was declined or the payment was refused; no money is held.",
            **body,
            paypalIssues=op.detail.get("issues") or [],
        )
    if op.detail.get("payer_action_required"):
        return problem(
            409,
            "payer_action_required",
            "PayPal requires the shopper to approve this card payment in a browser (e.g. 3-D Secure). "
            "That challenge flow is not supported by this integration; the attempt is recorded, and the "
            "order can be cancelled.",
            **body,
        )
    return JsonResponse(body, status=202)  # pending at PayPal, or another request's attempt in flight


@api(["POST"], staff=True)
def fulfil_view(request, order_id):
    order = services.order_for(request.user, order_id, staff=True)
    services.fulfil(order)
    order = _load_order(order)
    payment = order.paypal_payment
    status = 200 if payment.lifecycle == PayPalPayment.CAPTURED else 202
    return JsonResponse(serializers.order_json(order), status=status)


@api(["POST"], staff=True)
def cancel_view(request, order_id):
    order = services.order_for(request.user, order_id, staff=True)
    services.cancel(order)
    order = _load_order(order)
    payment = order.paypal_payment
    status = 200 if payment.lifecycle in (PayPalPayment.VOIDED, PayPalPayment.CANCELLED) else 202
    return JsonResponse(serializers.order_json(order), status=status)


@api(["POST"])
def refunds_view(request, order_id):
    order = services.order_for(request.user, order_id)
    data = _body(request)
    amount = data.get("amount")
    if amount is not None and not isinstance(amount, str | int | float):
        raise ApiProblem(400, "invalid_amount", 'amount must be a decimal string such as "5.00".')
    note = data.get("note")
    op = services.refund(
        order,
        request.user,
        idempotency_key=_idempotency_key(request, data),
        amount=None if amount is None else str(amount),
        note=str(note)[:255] if note else None,
    )
    order = _load_order(order)
    body = {
        **serializers.refund_json(op),
        "orderId": order.number,
        "payment": serializers.payment_json(order.paypal_payment),
    }
    if op.outcome == PaymentOperation.DONE:
        return JsonResponse(body, status=201)
    if op.outcome == PaymentOperation.FAILED:
        return problem(
            402, "refund_failed", f"PayPal reported the refund as {op.provider_status or 'refused'}.", **body
        )
    return JsonResponse(body, status=202)


@api(["GET"])
def my_orders_view(request):
    orders = (
        Order.objects.filter(user=request.user, paypal_payment__isnull=False)
        .select_related("paypal_payment")
        .prefetch_related("lines", "paypal_payment__operations")
        .order_by("-date_placed")
    )
    return JsonResponse({"orders": [serializers.order_json(o) for o in orders]})


@api(["GET"], staff=True)
def reconciliation_view(request):
    start = services.parse_instant(request.GET.get("from"), "from")
    end = services.parse_instant(request.GET.get("to"), "to")
    return JsonResponse(services.reconcile(start, end))


# --------------------------------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------------------------------


@sensitive_variables()
@api(["GET", "POST"])
def payment_methods_view(request):
    if request.method == "GET":
        cards = Bankcard.objects.filter(user=request.user).exclude(partner_reference="").order_by("pk")
        return JsonResponse({"paymentMethods": [serializers.card_json(c) for c in cards]})
    data = _body(request)
    card = _card(data.get("card", data))
    op, bankcard = services.save_card(request.user, card, _idempotency_key(request, data))
    if op.outcome == PaymentOperation.DONE and bankcard is not None:
        return JsonResponse(serializers.card_json(bankcard), status=201)
    if op.outcome == PaymentOperation.FAILED:
        return problem(
            402, "card_not_saved", "PayPal refused to save the card.", paypalIssues=op.detail.get("issues") or []
        )
    return JsonResponse(
        {
            "status": op.outcome,
            "message": "The card is still being saved; repeat the request with the same Idempotency-Key.",
        },
        status=202,
    )


@api(["DELETE"])
def payment_method_view(request, payment_method_id):
    op = services.delete_card(request.user, payment_method_id)
    if op.outcome == PaymentOperation.DONE:
        return JsonResponse({"paymentMethodId": payment_method_id, "deleted": True, "providerDeletion": "done"})
    # Already removed here (never listed, never usable); PayPal's copy is retried by paypal_retry_deletions.
    return JsonResponse(
        {"paymentMethodId": payment_method_id, "deleted": True, "providerDeletion": op.outcome}, status=202
    )
