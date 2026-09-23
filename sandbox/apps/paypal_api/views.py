"""HTTP API for the PayPal payments + saved-cards flows.

Authentication is Django's own session login; the caller's identity is taken
from ``request.user``. Shopper endpoints act only on the caller's own data;
operator endpoints (fulfil, cancel, reconciliation) require ``is_staff``.

These are programmatic JSON endpoints authenticated by session, so they are
CSRF-exempt; authorization is enforced by the login/staff/ownership checks.
"""
import datetime
import functools
import json
import re
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from oscar.core.loading import get_model

from . import services
from .errors import PayPalError
from .models import OrderPayment, PaymentMethod
from .services import ServiceError

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _json(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params={"indent": 2})


def _error(status, message, **extra):
    payload = {"error": message}
    payload.update(extra)
    return _json(payload, status=status)


def _body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "Request body must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "Request body must be a JSON object")
    return data


def endpoint(*, login=True, staff=False):
    """Wrap a handler with auth checks and uniform error translation."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, request, *args, **kwargs):
            if login and not request.user.is_authenticated:
                return _error(401, "Authentication required")
            if staff and not request.user.is_staff:
                return _error(403, "Operator (staff) access required")
            try:
                return func(self, request, *args, **kwargs)
            except ApiError as exc:
                return _error(exc.status, exc.message)
            except ServiceError as exc:
                return _error(exc.http_status, exc.message)
            except PayPalError as exc:
                extra = {}
                if getattr(exc, "outcome_unknown", False):
                    extra["outcomeUnknown"] = True
                return _error(exc.http_status, exc.message, **extra)

        return wrapper

    return decorator


def _get_owned_order(request, order_id):
    try:
        order = Order.objects.get(pk=order_id)
    except (Order.DoesNotExist, ValueError, TypeError):
        raise ApiError(404, "Order not found")
    if order.user_id != request.user.id:
        # Do not reveal another shopper's order.
        raise ApiError(404, "Order not found")
    return order


def _get_any_order(order_id):
    try:
        return Order.objects.get(pk=order_id)
    except (Order.DoesNotExist, ValueError, TypeError):
        raise ApiError(404, "Order not found")


def _normalize_card(raw):
    if not isinstance(raw, dict):
        raise ApiError(400, "card must be an object")
    number = raw.get("number")
    expiry = raw.get("expiry")
    if not number or not expiry:
        raise ApiError(400, "card requires 'number' and 'expiry' (YYYY-MM)")
    billing = raw.get("billingAddress") or {}
    card = {
        "number": str(number).replace(" ", ""),
        "expiry": str(expiry),
        "security_code": raw.get("securityCode") or raw.get("cvv"),
        "name": raw.get("name"),
        "billing_address": {
            "country_code": billing.get("countryCode") or billing.get("country_code") or "US",
            "address_line_1": billing.get("addressLine1") or billing.get("line1"),
            "city": billing.get("city"),
            "state": billing.get("state"),
            "postal_code": billing.get("postalCode") or billing.get("postcode"),
        },
    }
    return {k: v for k, v in card.items() if v is not None}


def _order_payment_dict(order):
    payment = OrderPayment.objects.filter(order=order).first()
    if payment is None:
        return {"orderId": order.pk, "orderNumber": order.number, "status": "no_payment"}
    return payment.as_dict()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
@method_decorator(csrf_exempt, name="dispatch")
class OrdersView(View):
    @endpoint()
    def post(self, request):
        data = _body(request)
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "'items' must be a non-empty list")
        specs = []
        for entry in items:
            if not isinstance(entry, dict):
                raise ApiError(400, "each item must be an object")
            product_id = entry.get("productId", entry.get("id"))
            quantity = entry.get("quantity", 1)
            if product_id is None:
                raise ApiError(400, "each item requires 'productId'")
            try:
                quantity = int(quantity)
            except (TypeError, ValueError):
                raise ApiError(400, "'quantity' must be an integer")
            if quantity < 1:
                raise ApiError(400, "'quantity' must be at least 1")
            try:
                product = Product.objects.get(pk=product_id)
            except (Product.DoesNotExist, ValueError, TypeError):
                raise ApiError(404, f"Product {product_id} not found")
            specs.append((product, quantity))

        try:
            order = services.place_order(request.user, specs)
        except ValueError as exc:
            # e.g. a product with no stockrecord/price is not purchasable
            raise ApiError(400, f"Cannot place order: {exc}")

        return _json(
            {
                "orderId": order.pk,
                "orderNumber": order.number,
                "status": OrderPayment.AWAITING_PAYMENT,
                "currency": order.currency,
                "amount": str(order.total_incl_tax),
            },
            status=201,
        )


@method_decorator(csrf_exempt, name="dispatch")
class MyOrdersView(View):
    @endpoint()
    def get(self, request):
        orders = (
            Order.objects.filter(user=request.user)
            .order_by("-date_placed")
            .prefetch_related("paypal_payment")
        )
        results = []
        for order in orders:
            entry = {
                "orderId": order.pk,
                "orderNumber": order.number,
                "orderStatus": order.status,
                "total": str(order.total_incl_tax),
                "currency": order.currency,
                "datePlaced": order.date_placed.isoformat(),
                "payment": _order_payment_dict(order),
            }
            results.append(entry)
        return _json({"orders": results})


@method_decorator(csrf_exempt, name="dispatch")
class PayView(View):
    @endpoint()
    def post(self, request, order_id):
        order = _get_owned_order(request, order_id)
        data = _body(request)
        payment_method = None
        card = None
        method_id = data.get("paymentMethodId")
        if method_id is not None:
            payment_method = PaymentMethod.objects.filter(
                pk=method_id, user=request.user
            ).first()
            if payment_method is None:
                raise ApiError(404, "Saved card not found")
        elif "card" in data:
            card = _normalize_card(data.get("card"))
        else:
            raise ApiError(400, "Provide either 'card' or 'paymentMethodId'")

        payment = services.authorize_payment(
            order, card=card, payment_method=payment_method
        )
        return _json({"payment": payment.as_dict()})


@method_decorator(csrf_exempt, name="dispatch")
class FulfilView(View):
    @endpoint(staff=True)
    def post(self, request, order_id):
        order = _get_any_order(order_id)
        payment = services.capture_payment(order)
        return _json({"payment": payment.as_dict()})


@method_decorator(csrf_exempt, name="dispatch")
class CancelView(View):
    @endpoint(staff=True)
    def post(self, request, order_id):
        order = _get_any_order(order_id)
        payment = services.cancel_payment(order)
        return _json({"payment": payment.as_dict()})


@method_decorator(csrf_exempt, name="dispatch")
class RefundsView(View):
    @endpoint()
    def post(self, request, order_id):
        order = _get_owned_order(request, order_id)
        data = _body(request)
        idempotency_key = data.get("idempotencyKey")
        if not idempotency_key:
            raise ApiError(400, "'idempotencyKey' is required for refunds")
        amount = data.get("amount")
        if amount is not None:
            try:
                amount = Decimal(str(amount))
            except (InvalidOperation, ValueError):
                raise ApiError(400, "'amount' must be a number")
            if amount <= 0:
                raise ApiError(400, "'amount' must be positive")

        refund = services.refund_payment(
            order, amount=amount, idempotency_key=str(idempotency_key)
        )
        body = refund.as_dict()
        body["orderId"] = order.pk
        return _json(body, status=201)


# ---------------------------------------------------------------------------
# Payment methods (saved cards)
# ---------------------------------------------------------------------------
@method_decorator(csrf_exempt, name="dispatch")
class PaymentMethodsView(View):
    @endpoint()
    def get(self, request):
        methods = PaymentMethod.objects.filter(user=request.user)
        return _json({"paymentMethods": [m.as_dict() for m in methods]})

    @endpoint()
    def post(self, request):
        data = _body(request)
        card = _normalize_card(data.get("card"))
        method = services.save_card(request.user, card)
        body = method.as_dict()
        return _json(body, status=201)


@method_decorator(csrf_exempt, name="dispatch")
class PaymentMethodDetailView(View):
    @endpoint()
    def delete(self, request, method_id):
        method = PaymentMethod.objects.filter(pk=method_id, user=request.user).first()
        if method is None:
            raise ApiError(404, "Saved card not found")
        services.delete_card(request.user, method)
        return _json({"deleted": True, "paymentMethodId": int(method_id)})


# ---------------------------------------------------------------------------
# Reconciliation (operator)
# ---------------------------------------------------------------------------
@method_decorator(csrf_exempt, name="dispatch")
class ReconciliationView(View):
    @endpoint(staff=True)
    def get(self, request):
        from_raw = request.GET.get("from")
        to_raw = request.GET.get("to")
        if not from_raw or not to_raw:
            raise ApiError(400, "'from' and 'to' ISO-8601 date-times are required")
        from_dt = _parse_iso(from_raw, "from")
        to_dt = _parse_iso(to_raw, "to")
        if from_dt >= to_dt:
            raise ApiError(400, "'from' must be before 'to'")
        report = services.reconcile(from_dt, to_dt)
        return _json(report)


def _parse_iso(value, field):
    text = value.strip()
    # A "+HH:MM" timezone offset arrives as " HH:MM" when a client forgets to
    # URL-encode the "+"; restore it so a well-formed instant still parses.
    text = re.sub(r"\s(\d{2}:\d{2})$", r"+\1", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise ApiError(400, f"'{field}' must be an ISO-8601 date-time")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed
