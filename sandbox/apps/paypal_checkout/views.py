"""HTTP JSON endpoints for the PayPal checkout API.

Plain Django views (no extra dependencies). Callers authenticate with the
sandbox's own Django session login; the caller's identity is taken from
``request.user``. Shopper endpoints act only on the caller's own data; ``fulfil``,
``cancel`` and ``reconciliation`` are restricted to ``is_staff`` operators.
"""
import functools
import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from oscar.core.loading import get_model

from . import exceptions as exc
from . import services
from .models import PayPalPayment

log = logging.getLogger("paypal_checkout")

Order = get_model("order", "Order")


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #
def _error(status, message, *, code=None, details=None):
    body = {"error": message}
    if code:
        body["code"] = code
    if details:
        body["details"] = details
    return JsonResponse(body, status=status)


def api(methods, *, staff_required=False, login_required=True):
    """Wrap a view: method check, auth check, JSON body parse, error mapping."""
    allowed = {m.upper() for m in methods}

    def decorator(func):
        @csrf_exempt
        @functools.wraps(func)
        def wrapper(request, *args, **kwargs):
            if request.method not in allowed:
                return _error(405, f"Method {request.method} not allowed.")
            if login_required and not request.user.is_authenticated:
                return _error(401, "Authentication required.")
            if staff_required and not request.user.is_staff:
                return _error(403, "Operator (staff) privileges required.")

            request.json = {}
            content_type = (request.content_type or "").split(";")[0].strip()
            if request.method in ("POST", "PUT", "PATCH") and request.body and content_type == "application/json":
                try:
                    request.json = json.loads(request.body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return _error(400, "Request body must be valid JSON.")
                if not isinstance(request.json, dict):
                    return _error(400, "Request body must be a JSON object.")
            try:
                return func(request, *args, **kwargs)
            except exc.PayPalCheckoutError as e:
                return _error(e.status_code, e.message, code=e.code, details=e.details)
            except Exception:  # noqa: BLE001
                log.exception("Unhandled error in %s", func.__name__)
                return _error(500, "An unexpected error occurred.")

        return wrapper

    return decorator


def _get_order_for_shopper(request, order_number):
    try:
        return Order.objects.get(number=order_number, user=request.user)
    except Order.DoesNotExist:
        raise exc.NotFound("Order not found.")


def _get_order_any(order_number):
    try:
        return Order.objects.get(number=order_number)
    except Order.DoesNotExist:
        raise exc.NotFound("Order not found.")


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def _payment_dict(order, payment):
    return {
        "orderId": str(order.number),
        "orderStatus": order.status,
        "paymentStatus": payment.status,
        "currency": payment.currency,
        "orderTotal": str(payment.order_total),
        "authorizedAmount": str(payment.authorized_amount),
        "capturedAmount": str(payment.captured_amount),
        "paypalFee": str(payment.paypal_fee),
        "netAmount": str(payment.net_amount),
        "amountRefunded": str(payment.amount_refunded),
        "amountAvailableForRefund": str(payment.amount_available_for_refund),
        "paypalOrderId": payment.paypal_order_id or None,
        "authorizationId": payment.authorization_id or None,
        "captureId": payment.capture_id or None,
        "refunds": [_refund_dict(r) for r in payment.refunds.all()],
        "createdAt": payment.created_at.isoformat(),
        "updatedAt": payment.updated_at.isoformat(),
    }


def _refund_dict(refund):
    return {
        "refundId": refund.id,
        "paypalRefundId": refund.paypal_refund_id or None,
        "amount": str(refund.amount),
        "currency": refund.currency,
        "status": refund.status,
        "idempotencyKey": refund.idempotency_key,
        "createdAt": refund.created_at.isoformat(),
    }


def _card_dict(card):
    return {
        "paymentMethodId": card.id,
        "brand": card.brand or None,
        "lastDigits": card.last_digits or None,
        "expiry": card.expiry or None,
        "cardholderName": card.cardholder_name or None,
        "label": card.label,
        "createdAt": card.created_at.isoformat(),
    }


def _order_summary(order):
    payment = getattr(order, "paypal_payment", None)
    lines = [
        {
            "productId": line.product_id,
            "title": line.title,
            "quantity": line.quantity,
            "linePrice": str(line.line_price_incl_tax),
        }
        for line in order.lines.all()
    ]
    data = {
        "orderId": str(order.number),
        "orderStatus": order.status,
        "currency": order.currency,
        "total": str(order.total_incl_tax),
        "datePlaced": order.date_placed.isoformat() if order.date_placed else None,
        "lines": lines,
    }
    if payment is not None:
        data["payment"] = _payment_dict(order, payment)
    else:
        data["payment"] = None
    return data


# --------------------------------------------------------------------------- #
# Flow 1 -- orders & payments
# --------------------------------------------------------------------------- #
@api(["POST"])
def create_order(request):
    items = request.json.get("items")
    if not isinstance(items, list):
        raise exc.BadRequest("'items' must be a list of {product_id, quantity}.")
    order = services.place_order(user=request.user, items=items)
    payment = order.paypal_payment
    body = _payment_dict(order, payment)
    body["orderId"] = str(order.number)
    return JsonResponse(body, status=201)


@api(["POST"])
def pay_order(request, order_number):
    order = _get_order_for_shopper(request, order_number)
    payment = services.pay_order(
        user=request.user,
        order=order,
        card=request.json.get("card"),
        saved_card_id=request.json.get("payment_method_id") or request.json.get("paymentMethodId"),
    )
    return JsonResponse(_payment_dict(order, payment), status=200)


@api(["POST"], staff_required=True)
def fulfil_order(request, order_number):
    order = _get_order_any(order_number)
    payment = services.fulfil_order(order=order)
    return JsonResponse(_payment_dict(order, payment), status=200)


@api(["POST"], staff_required=True)
def cancel_order(request, order_number):
    order = _get_order_any(order_number)
    payment = services.cancel_order(order=order)
    return JsonResponse(_payment_dict(order, payment), status=200)


@api(["POST"])
def create_refund(request, order_number):
    order = _get_order_for_shopper(request, order_number)
    idempotency_key = (
        request.json.get("idempotency_key")
        or request.json.get("idempotencyKey")
        or request.headers.get("Idempotency-Key")
    )
    payment, refund = services.create_refund(
        order=order,
        amount=request.json.get("amount"),
        idempotency_key=idempotency_key,
    )
    body = _payment_dict(order, payment)
    body["refundId"] = refund.id
    body["refund"] = _refund_dict(refund)
    return JsonResponse(body, status=201)


@api(["GET"])
def my_orders(request):
    orders = (
        Order.objects.filter(user=request.user)
        .select_related("paypal_payment")
        .prefetch_related("lines", "paypal_payment__refunds")
        .order_by("-date_placed")
    )
    return JsonResponse({"orders": [_order_summary(o) for o in orders]}, status=200)


# --------------------------------------------------------------------------- #
# Flow 2 -- saved cards
# --------------------------------------------------------------------------- #
@api(["POST", "GET"])
def payment_methods(request):
    if request.method == "GET":
        cards = services.list_cards(user=request.user)
        return JsonResponse({"paymentMethods": [_card_dict(c) for c in cards]}, status=200)
    # POST
    card = request.json.get("card") or request.json
    saved = services.save_card(user=request.user, card=card)
    body = _card_dict(saved)
    body["paymentMethodId"] = saved.id
    return JsonResponse(body, status=201)


@api(["DELETE"])
def delete_payment_method(request, method_id):
    services.delete_card(user=request.user, card_id=method_id)
    return JsonResponse({"deleted": True, "paymentMethodId": method_id}, status=200)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
@api(["GET"], staff_required=True)
def reconciliation(request):
    start = request.GET.get("from")
    end = request.GET.get("to")
    if not start or not end:
        raise exc.BadRequest("Both 'from' and 'to' ISO-8601 date-times are required.")
    report = services.reconcile(start_date=start, end_date=end)
    report["from"] = start
    report["to"] = end
    return JsonResponse(report, status=200)
