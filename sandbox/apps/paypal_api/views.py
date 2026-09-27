"""
JSON endpoints. Callers authenticate with the sandbox's own Django session
login; CSRF protection stays on (send the ``X-CSRFToken`` header).

Views are ``non_atomic_requests``: a PayPal claim must be committed before
PayPal is called, not held in a request-wide transaction.
"""

import functools
import json
import logging
from collections.abc import Callable
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from oscar.core.loading import get_model

from . import cards, orders, payments, reconciliation
from .errors import ApiProblem, OutcomeUnknown, ProviderError
from .models import PayPalOperation as Op

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")


def error_response(problem: ApiProblem) -> JsonResponse:
    body: dict[str, Any] = {"error": {"code": problem.code, "message": problem.message}}
    if isinstance(problem, OutcomeUnknown) or (isinstance(problem, ProviderError) and problem.outcome_unknown):
        body["error"]["outcomeUnknown"] = True
    if isinstance(problem, ProviderError) and problem.debug_id:
        body["error"]["paypalDebugId"] = problem.debug_id
    body["error"].update(problem.extra)
    return JsonResponse(body, status=problem.status_code)


def answer(outcome: str, body: dict[str, Any], *, created: bool = False) -> JsonResponse:
    """The one place an operation's outcome becomes the caller's HTTP answer."""
    match outcome:
        case Op.DONE:
            status = 201 if created else 200
        case Op.PENDING | Op.SENDING:
            status = 202  # accepted, not done
        case Op.FAILED | Op.NEEDS_REVIEW:
            status = 409
        case _:
            status = 504  # may have happened: never reported as "not done"
            body = {**body, "outcomeUnknown": True}
    return JsonResponse({**body, "outcome": outcome}, status=status)


def api(methods: tuple[str, ...], *, staff: bool = False) -> Callable[..., Any]:
    def decorate(view: Callable[..., JsonResponse]) -> Callable[..., HttpResponse]:
        @functools.wraps(view)
        @transaction.non_atomic_requests
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if request.method not in methods:
                return JsonResponse({"error": {"code": "method_not_allowed", "message": "Method not allowed."}},
                                    status=405, headers={"Allow": ", ".join(methods)})
            if not request.user.is_authenticated:
                return JsonResponse({"error": {"code": "not_authenticated", "message": "Log in first."}}, status=401)
            if staff and not request.user.is_staff:
                return JsonResponse({"error": {"code": "forbidden", "message": "Staff only."}}, status=403)
            try:
                if request.method == "POST":
                    request.json = _json_body(request)  # type: ignore[attr-defined]
                return view(request, *args, **kwargs)
            except ApiProblem as problem:
                return error_response(problem)
            except ImproperlyConfigured as e:
                logger.error("PayPal API misconfigured: %s", e)
                return JsonResponse({"error": {"code": "paypal_not_configured",
                                               "message": "PayPal is not configured on this site."}}, status=503)
            except Exception:
                logger.exception("Unhandled error in %s", view.__name__)
                return JsonResponse({"error": {"code": "internal_error", "message": "Internal error."}}, status=500)

        return wrapper

    return decorate


def _json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise ApiProblem(400, "invalid_json", "The request body must be JSON.") from None
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_json", "The request body must be a JSON object.")
    return data


def _order_body(order: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    order = Order.objects.select_related("paypal_payment").get(pk=order.pk)
    return {"orderId": order.number, "order": orders.describe_order(order), **(extra or {})}


# --- orders ------------------------------------------------------------------


@api(("POST",))
def orders_collection(request: HttpRequest) -> JsonResponse:
    order = orders.place_order(request.user, request.json)  # type: ignore[attr-defined]
    return JsonResponse(_order_body(order), status=201)


@api(("GET",))
def my_orders(request: HttpRequest) -> JsonResponse:
    mine = Order.objects.filter(user=request.user).select_related("paypal_payment").order_by("-date_placed")
    return JsonResponse({"orders": [orders.describe_order(o) for o in mine]})


@api(("POST",))
def order_pay(request: HttpRequest, number: str) -> JsonResponse:
    order = orders.get_order(number, user=request.user)
    op, _ = payments.pay_order(request.user, order, request.json)  # type: ignore[attr-defined]
    if op is None:
        return answer(Op.DONE, _order_body(order))
    return answer(op.outcome, _order_body(order, payments.outcome_payload(op)))


@api(("POST",), staff=True)
def order_fulfil(request: HttpRequest, number: str) -> JsonResponse:
    order = orders.get_order(number)
    op, _, note = payments.fulfil_order(order)
    extra = {"note": note} if note else {}
    if op is None:
        # already captured, or a renewal PayPal is still working on
        outcome = Op.DONE if order.paypal_payment.state == "captured" else Op.PENDING
        return answer(outcome, _order_body(order, extra))
    return answer(op.outcome, _order_body(order, {**payments.outcome_payload(op), **extra}))


@api(("POST",), staff=True)
def order_cancel(request: HttpRequest, number: str) -> JsonResponse:
    order = orders.get_order(number)
    op, _ = payments.cancel_order(order)
    if op is None:
        return answer(Op.DONE, _order_body(order))
    return answer(op.outcome, _order_body(order, payments.outcome_payload(op)))


@api(("POST",))
def order_refunds(request: HttpRequest, number: str) -> JsonResponse:
    order = orders.get_order(number, user=request.user)
    key = payments.idempotency_key(request.headers)
    op, _ = payments.refund_order(order, request.json, key)  # type: ignore[attr-defined]
    body = _order_body(order, {"refundId": str(op.pk), "paypalRefundId": op.provider_id or None,
                               **payments.outcome_payload(op)})
    return answer(op.outcome, body, created=True)


# --- saved cards -------------------------------------------------------------


@api(("GET", "POST"))
def payment_methods(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        return JsonResponse({"paymentMethods": [cards.describe_card(c) for c in cards.list_cards(request.user)]})
    key = payments.idempotency_key(request.headers)
    op, saved = cards.save_card(request.user, request.json, key)  # type: ignore[attr-defined]
    body: dict[str, Any] = {"paymentMethodId": str(saved.pk) if saved else None}
    if saved is not None:
        body["paymentMethod"] = cards.describe_card(saved)
    return answer(op.outcome, {**body, **payments.outcome_payload(op)}, created=True)


@api(("DELETE",))
def payment_method_detail(request: HttpRequest, payment_method_id: str) -> HttpResponse:
    op = cards.delete_card(request.user, payment_method_id)
    if op.outcome == Op.DONE:
        return HttpResponse(status=204)
    return answer(op.outcome, {"paymentMethodId": payment_method_id})


# --- reconciliation ----------------------------------------------------------


@api(("GET",), staff=True)
def reconciliation_report(request: HttpRequest) -> JsonResponse:
    start, end = reconciliation.parse_range(request.GET.get("from"), request.GET.get("to"))
    return JsonResponse(reconciliation.reconcile(start, end))
