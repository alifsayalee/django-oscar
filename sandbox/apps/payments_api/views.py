"""HTTP endpoints for the PayPal integration.

Authentication is Django's own session login (the sandbox's existing mechanism).
Shopper endpoints act only on the caller's own data; fulfil/cancel/reconciliation
are operator actions restricted to ``is_staff`` users. Responses that create
something return the new identifier as a top-level field.

These are non-browser JSON endpoints driven by a session cookie, so they are
CSRF-exempt (there is no HTML form or CSRF token in the flow); every state change
still requires an authenticated — and, for operator actions, staff — session.
"""
import json
import logging
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from oscar.core.loading import get_model

from . import errors, services

logger = logging.getLogger("paypal")

Order = get_model("order", "Order")
PayPalPayment = services.PayPalPayment


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _BadRequest("Request body must be valid JSON.")
    if not isinstance(data, dict):
        raise _BadRequest("Request body must be a JSON object.")
    return data


class _BadRequest(Exception):
    pass


def _error(message, status):
    return JsonResponse({"error": message}, status=status)


def _handle_service_errors(fn):
    """Wrap a view method: turn our exception types into JSON responses."""

    def wrapper(self, request, *args, **kwargs):
        try:
            return fn(self, request, *args, **kwargs)
        except _BadRequest as exc:
            return _error(str(exc), 400)
        except services.OrderBuildError as exc:
            return _error(str(exc), 400)
        except errors.PayPalCallerError as exc:
            body = {"error": exc.message}
            if exc.issues:
                body["issues"] = exc.issues
            return JsonResponse(body, status=exc.status_code)
        except errors.PayPalError as exc:
            body = {"error": exc.message}
            if exc.outcome_unknown:
                body["outcome_unknown"] = True
            return JsonResponse(body, status=exc.status_code)

    return wrapper


@method_decorator(csrf_exempt, name="dispatch")
class BaseView(View):
    login_required = True
    staff_required = False

    def dispatch(self, request, *args, **kwargs):
        if self.login_required and not request.user.is_authenticated:
            return _error("Authentication required.", 401)
        if self.staff_required and not request.user.is_staff:
            return _error("Operator (staff) access required.", 403)
        return super().dispatch(request, *args, **kwargs)

    # Shopper-scoped lookup: only the caller's own payment.
    def get_own_payment(self, request, order_number):
        return (
            PayPalPayment.objects.select_related("order")
            .filter(order__number=order_number, user=request.user)
            .first()
        )

    # Operator lookup: any order's payment.
    def get_any_payment(self, order_number):
        return (
            PayPalPayment.objects.select_related("order")
            .filter(order__number=order_number)
            .first()
        )


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
class OrdersView(BaseView):
    @_handle_service_errors
    def post(self, request):
        data = _json_body(request)
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise _BadRequest("Provide 'items': a non-empty list of {product_id, quantity}.")
        payment = services.create_order(request.user, items)
        return JsonResponse(
            {
                "orderId": payment.order.number,
                "status": payment.status,
                "amount": str(payment.order_total),
                "currency": payment.currency,
            },
            status=201,
        )


class OrderPayView(BaseView):
    @_handle_service_errors
    def post(self, request, order_number):
        data = _json_body(request)
        payment = self.get_own_payment(request, order_number)
        if payment is None:
            return _error("Order not found.", 404)
        payment = services.authorize_payment(payment, request.user, data)
        return JsonResponse({"orderId": payment.order.number, "payment": payment.describe()})


class OrderFulfilView(BaseView):
    staff_required = True

    @_handle_service_errors
    def post(self, request, order_number):
        payment = self.get_any_payment(order_number)
        if payment is None:
            return _error("Order not found.", 404)
        payment = services.capture_payment(payment)
        return JsonResponse({"orderId": payment.order.number, "payment": payment.describe()})


class OrderCancelView(BaseView):
    staff_required = True

    @_handle_service_errors
    def post(self, request, order_number):
        payment = self.get_any_payment(order_number)
        if payment is None:
            return _error("Order not found.", 404)
        payment = services.cancel_payment(payment)
        return JsonResponse({"orderId": payment.order.number, "payment": payment.describe()})


class OrderRefundsView(BaseView):
    @_handle_service_errors
    def post(self, request, order_number):
        data = _json_body(request)
        payment = self.get_own_payment(request, order_number)
        if payment is None:
            return _error("Order not found.", 404)

        idempotency_key = (
            data.get("idempotency_key")
            or request.headers.get("Idempotency-Key")
            or request.headers.get("PayPal-Request-Id")
        )
        if not idempotency_key:
            raise _BadRequest(
                "A refund requires an 'idempotency_key' (in the body or the "
                "Idempotency-Key header)."
            )

        amount = None
        if data.get("amount") is not None:
            try:
                amount = Decimal(str(data["amount"]))
            except (InvalidOperation, ValueError):
                raise _BadRequest("'amount' must be a decimal number.")

        payment, refund = services.refund_payment(payment, amount, idempotency_key)
        return JsonResponse(
            {
                "refundId": refund.refund_id,
                "refund": refund.describe(),
                "payment": payment.describe(),
            },
            status=201,
        )


class MyOrdersView(BaseView):
    @_handle_service_errors
    def get(self, request):
        payments = (
            PayPalPayment.objects.select_related("order")
            .filter(user=request.user)
            .order_by("-created")
        )
        return JsonResponse(
            {
                "orders": [
                    {
                        "orderId": p.order.number,
                        "placed": p.created.isoformat(),
                        "payment": p.describe(),
                    }
                    for p in payments
                ]
            }
        )


# --------------------------------------------------------------------------- #
# Saved cards
# --------------------------------------------------------------------------- #
class PaymentMethodsView(BaseView):
    @_handle_service_errors
    def get(self, request):
        cards = services.list_saved_cards(request.user)
        return JsonResponse({"payment_methods": [c.describe() for c in cards]})

    @_handle_service_errors
    def post(self, request):
        data = _json_body(request)
        card = data.get("card", data)
        saved = services.save_card(request.user, card)
        return JsonResponse(
            {"paymentMethodId": saved.payment_token_id, "card": saved.describe()},
            status=201,
        )


class PaymentMethodDetailView(BaseView):
    @_handle_service_errors
    def delete(self, request, payment_method_id):
        removed = services.delete_saved_card(request.user, payment_method_id)
        if not removed:
            return _error("Saved card not found.", 404)
        return JsonResponse({"deleted": True, "paymentMethodId": payment_method_id})


# --------------------------------------------------------------------------- #
# Reconciliation (operator)
# --------------------------------------------------------------------------- #
class ReconciliationView(BaseView):
    staff_required = True

    @_handle_service_errors
    def get(self, request):
        start = request.GET.get("from")
        end = request.GET.get("to")
        if not start or not end:
            raise _BadRequest("Provide ISO-8601 'from' and 'to' query parameters.")
        report = services.reconcile(_parse_dt(start), _parse_dt(end))
        return JsonResponse({"from": start, "to": end, **report})


def _parse_dt(value):
    """Parse an ISO-8601 date-time; a value without an offset is treated as UTC."""
    import datetime as _dt

    from django.utils.dateparse import parse_datetime

    dt = parse_datetime(value)
    if dt is None:
        raise _BadRequest(f"Invalid ISO-8601 date-time: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt
