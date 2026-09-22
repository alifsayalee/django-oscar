"""HTTP endpoints for the PayPal payments API (routed under ``/api/``).

Plain Django class-based views returning JSON. Callers authenticate with the
sandbox's own Django session login; the caller's identity is taken from
``request.user``. Operator endpoints (fulfil, cancel, reconciliation) require
``is_staff``; every other endpoint is shopper-scoped to the caller's own data.

The views are ``csrf_exempt`` because this is a JSON API driven by API clients
(not browser forms) and authenticated by the session cookie; authorization is
enforced per request by authentication + ownership/staff checks below.
"""
from __future__ import annotations

import datetime as dt
import json

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from oscar.core.loading import get_model

from . import services
from .gateway import PayPalError
from .models import OrderPayment, SavedPaymentMethod
from .services import OrderPlacementError

Order = get_model("order", "Order")


def _json(data, status=200):
    return JsonResponse(data, status=status)


def _error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


@method_decorator(csrf_exempt, name="dispatch")
class ApiView(View):
    """Base view: JSON parsing, authentication and error translation."""

    require_staff = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error(401, "Authentication required.")
        if self.require_staff and not request.user.is_staff:
            return _error(403, "This action is restricted to staff users.")
        try:
            return super().dispatch(request, *args, **kwargs)
        except PayPalError as exc:
            return _error(exc.http_status, exc.message, issues=exc.issues)
        except OrderPlacementError as exc:
            return _error(exc.http_status, exc.message)

    def body(self, request):
        if not request.body:
            return {}
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            raise OrderPlacementError(400, "Request body must be valid JSON.")
        if not isinstance(data, dict):
            raise OrderPlacementError(400, "Request body must be a JSON object.")
        return data

    # Order lookup helpers -------------------------------------------------- #
    def shopper_order(self, request, order_number):
        """Return the caller's own order, or None."""
        return Order.objects.filter(
            number=order_number, user=request.user
        ).first()

    def any_order(self, order_number):
        """Return any order (operator scope)."""
        return Order.objects.filter(number=order_number).first()


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
class PlaceOrderView(ApiView):
    def post(self, request):
        data = self.body(request)
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return _error(400, "Provide a non-empty 'items' list.")
        parsed = []
        for it in items:
            if not isinstance(it, dict) or "product_id" not in it:
                return _error(400, "Each item needs a 'product_id'.")
            parsed.append(
                {"product_id": it["product_id"], "quantity": it.get("quantity", 1)}
            )
        order = services.place_order(request.user, parsed, request)
        payment = order.paypal_payment
        return _json(
            {
                "orderId": str(order.number),
                "status": order.status,
                "payment": payment.describe(),
            },
            status=201,
        )


class MyOrdersView(ApiView):
    def get(self, request):
        orders = (
            Order.objects.filter(user=request.user)
            .order_by("-date_placed")
            .prefetch_related("paypal_payment")
        )
        out = []
        for order in orders:
            payment = getattr(order, "paypal_payment", None)
            out.append(
                {
                    "orderId": str(order.number),
                    "status": order.status,
                    "date_placed": order.date_placed.isoformat(),
                    "total": f"{order.total_incl_tax:.2f}"
                    if order.total_incl_tax is not None
                    else None,
                    "payment": payment.describe() if payment else None,
                }
            )
        return _json({"orders": out})


def _extract_card(data):
    card = data.get("card")
    if not isinstance(card, dict):
        return None
    if not card.get("number") or not card.get("expiry"):
        raise OrderPlacementError(
            400, "A card needs at least 'number' and 'expiry' (YYYY-MM)."
        )
    return {
        "number": str(card["number"]).replace(" ", ""),
        "expiry": str(card["expiry"]),
        "security_code": card.get("security_code"),
        "name": card.get("name"),
    }


class PayView(ApiView):
    def post(self, request, order_number):
        order = self.shopper_order(request, order_number)
        if order is None:
            return _error(404, "Order not found.")
        data = self.body(request)

        saved_method = None
        payment_method_id = data.get("paymentMethodId")
        card = _extract_card(data)

        if payment_method_id is not None:
            saved_method = SavedPaymentMethod.objects.filter(
                pk=payment_method_id, user=request.user
            ).first()
            if saved_method is None:
                return _error(404, "Saved card not found.")
        elif card is None:
            return _error(
                400,
                "Provide either 'card' details or a 'paymentMethodId' to pay with.",
            )

        payment = services.authorize(
            order, card=card, saved_method=saved_method, request=request
        )
        return _json({"orderId": str(order.number), "payment": payment.describe()})


class FulfilView(ApiView):
    require_staff = True

    def post(self, request, order_number):
        order = self.any_order(order_number)
        if order is None:
            return _error(404, "Order not found.")
        payment = services.fulfil(order)
        return _json({"orderId": str(order.number), "payment": payment.describe()})


class CancelView(ApiView):
    require_staff = True

    def post(self, request, order_number):
        order = self.any_order(order_number)
        if order is None:
            return _error(404, "Order not found.")
        payment = services.cancel(order)
        return _json({"orderId": str(order.number), "payment": payment.describe()})


class RefundView(ApiView):
    def post(self, request, order_number):
        order = self.shopper_order(request, order_number)
        if order is None:
            return _error(404, "Order not found.")
        data = self.body(request)
        idempotency_key = data.get("idempotencyKey") or request.headers.get(
            "Idempotency-Key"
        )
        if not idempotency_key:
            return _error(
                400,
                "A refund requires an idempotency key ('idempotencyKey' in the body "
                "or an 'Idempotency-Key' header).",
            )
        amount = data.get("amount")
        refund = services.refund(
            order, amount=amount, idempotency_key=str(idempotency_key)
        )
        return _json(
            {
                "refundId": refund.refund_id,
                "orderId": str(order.number),
                "refund": refund.describe(),
                "payment": order.paypal_payment.describe(),
            },
            status=201,
        )


# --------------------------------------------------------------------------- #
# Saved cards
# --------------------------------------------------------------------------- #
class PaymentMethodsView(ApiView):
    def get(self, request):
        cards = services.list_cards(request.user)
        return _json({"paymentMethods": [c.describe() for c in cards]})

    def post(self, request):
        data = self.body(request)
        card = _extract_card(data)
        if card is None:
            return _error(
                400, "A card needs at least 'number' and 'expiry' (YYYY-MM)."
            )
        spm = services.save_card(request.user, card, label=data.get("label", ""))
        result = spm.describe()
        return _json({"paymentMethodId": spm.pk, **result}, status=201)


class PaymentMethodDetailView(ApiView):
    def delete(self, request, pk):
        ok = services.delete_card(request.user, pk)
        if not ok:
            return _error(404, "Saved card not found.")
        return _json({"deleted": True, "paymentMethodId": int(pk)})


# --------------------------------------------------------------------------- #
# Reconciliation (operator)
# --------------------------------------------------------------------------- #
class ReconciliationView(ApiView):
    require_staff = True

    def get(self, request):
        raw_from = request.GET.get("from")
        raw_to = request.GET.get("to")
        if not raw_from or not raw_to:
            return _error(400, "Provide ISO-8601 'from' and 'to' query parameters.")
        try:
            start = _parse_dt(raw_from)
            end = _parse_dt(raw_to)
        except ValueError:
            return _error(400, "'from' and 'to' must be ISO-8601 date-times.")
        if end <= start:
            return _error(400, "'to' must be after 'from'.")
        report = services.reconcile(start, end)
        return _json(report)


def _parse_dt(value):
    # Accept a trailing 'Z' as UTC.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed
