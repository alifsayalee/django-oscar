"""HTTP API for PayPal payments and saved cards.

Plain Django JSON views (the sandbox ships no DRF). Callers authenticate with the
sandbox's own session login; identity is taken from ``request.user``. Operator
actions (fulfil, cancel, reconciliation) require ``is_staff``; every other endpoint
is scoped to the caller's own data.

The views are ``csrf_exempt`` -- they are a session-authenticated JSON API driven by
programmatic callers, not HTML forms -- and ``non_atomic_requests`` so that PayPal
calls are never wrapped in a request-level transaction that a later error could roll
back (see ``services.py``).
"""

import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from oscar.core.loading import get_model

from . import services
from .gateway import PayPalError
from .services import ServiceError

logger = logging.getLogger("sandbox.payments")

Order = get_model("order", "Order")


def _json(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params={"indent": 2})


def _error(message, status=400, issues=None):
    payload = {"error": message}
    if issues:
        payload["issues"] = issues
    return _json(payload, status=status)


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(transaction.non_atomic_requests, name="dispatch")
class ApiView(View):
    """Base view: JSON parsing, auth enforcement, uniform error handling."""

    staff_only = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error("Authentication required.", status=401)
        if self.staff_only and not request.user.is_staff:
            return _error("Operator (staff) privileges are required.", status=403)
        try:
            return super().dispatch(request, *args, **kwargs)
        except ServiceError as exc:
            return _error(exc.message, status=exc.status_code, issues=exc.issues)
        except PayPalError as exc:
            logger.warning("PayPal error: %s", exc.message)
            return _error(exc.message, status=exc.status_code, issues=exc.issues)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in payments API")
            return _error("An unexpected server error occurred.", status=500)

    def body(self, request):
        if not request.body:
            return {}
        try:
            data = json.loads(request.body)
        except (ValueError, TypeError):
            raise ServiceError("Request body must be valid JSON.", 400)
        if not isinstance(data, dict):
            raise ServiceError("Request body must be a JSON object.", 400)
        return data

    # order lookup helpers -------------------------------------------------
    def order_for_shopper(self, request, order_id):
        order = Order.objects.filter(number=order_id, user=request.user).first()
        if order is None:
            # Do not leak the existence of another shopper's order.
            raise ServiceError("Order not found.", 404)
        return order

    def order_for_operator(self, order_id):
        order = Order.objects.filter(number=order_id).first()
        if order is None:
            raise ServiceError("Order not found.", 404)
        return order


# ---------------------------------------------------------------------------
# Flow 1 -- orders & payments
# ---------------------------------------------------------------------------


class OrdersView(ApiView):
    def post(self, request):
        data = self.body(request)
        items = data.get("items")
        if not isinstance(items, list):
            raise ServiceError("'items' must be a list of {product_id, quantity}.", 400)
        order = services.place_order(request.user, items)
        payload = services.order_payment_state(order)
        payload["orderId"] = order.number
        return _json(payload, status=201)


class PayView(ApiView):
    def post(self, request, order_id):
        order = self.order_for_shopper(request, order_id)
        data = self.body(request)
        raw_card = data.get("card")
        saved_card_id = data.get("paymentMethodId")
        if raw_card is not None and not isinstance(raw_card, dict):
            raise ServiceError("'card' must be an object.", 400)
        services.pay_order(
            order, raw_card=raw_card, saved_card_id=saved_card_id
        )
        order.refresh_from_db()
        return _json(services.order_payment_state(order))


class FulfilView(ApiView):
    staff_only = True

    def post(self, request, order_id):
        order = self.order_for_operator(order_id)
        services.fulfil_order(order)
        order.refresh_from_db()
        return _json(services.order_payment_state(order))


class CancelView(ApiView):
    staff_only = True

    def post(self, request, order_id):
        order = self.order_for_operator(order_id)
        services.cancel_order(order)
        order.refresh_from_db()
        return _json(services.order_payment_state(order))


class RefundsView(ApiView):
    def post(self, request, order_id):
        order = self.order_for_shopper(request, order_id)
        data = self.body(request)
        idempotency_key = (
            data.get("idempotencyKey")
            or request.headers.get("Idempotency-Key")
            or request.headers.get("PayPal-Request-Id")
        )
        refund = services.refund_order(
            order, amount=data.get("amount"), idempotency_key=idempotency_key
        )
        order.refresh_from_db()
        payload = refund.as_dict()
        payload["refundId"] = refund.refund_id
        payload["order"] = services.order_payment_state(order)
        return _json(payload, status=201)


class MyOrdersView(ApiView):
    def get(self, request):
        orders = (
            Order.objects.filter(user=request.user)
            .select_related("paypal_payment")
            .prefetch_related("lines", "paypal_payment__refunds")
            .order_by("-date_placed")
        )
        return _json(
            {"orders": [services.order_payment_state(o) for o in orders]}
        )


# ---------------------------------------------------------------------------
# Flow 2 -- saved cards
# ---------------------------------------------------------------------------


class PaymentMethodsView(ApiView):
    def get(self, request):
        cards = services.list_cards(request.user)
        return _json({"paymentMethods": [c.as_dict() for c in cards]})

    def post(self, request):
        data = self.body(request)
        raw_card = data.get("card", data)
        if not isinstance(raw_card, dict):
            raise ServiceError("Card details are required.", 400)
        card = services.save_card(request.user, raw_card)
        payload = card.as_dict()
        payload["paymentMethodId"] = card.vault_id
        return _json(payload, status=201)


class PaymentMethodDetailView(ApiView):
    def delete(self, request, payment_method_id):
        services.delete_card(request.user, payment_method_id)
        return _json({"deleted": True, "paymentMethodId": payment_method_id})


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------


class ReconciliationView(ApiView):
    staff_only = True

    def get(self, request):
        from datetime import timezone as std_timezone

        from django.utils import timezone
        from django.utils.dateparse import parse_datetime

        from_raw = request.GET.get("from")
        to_raw = request.GET.get("to")
        if not from_raw or not to_raw:
            raise ServiceError("'from' and 'to' query parameters are required.", 400)
        from_dt = parse_datetime(from_raw)
        to_dt = parse_datetime(to_raw)
        if from_dt is None or to_dt is None:
            raise ServiceError(
                "'from' and 'to' must be ISO-8601 date-times.", 400
            )
        if timezone.is_naive(from_dt):
            from_dt = timezone.make_aware(from_dt, std_timezone.utc)
        if timezone.is_naive(to_dt):
            to_dt = timezone.make_aware(to_dt, std_timezone.utc)
        report = services.reconcile(from_dt, to_dt)
        return _json(report)
