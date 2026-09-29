"""
JSON endpoints under /api/. Callers authenticate with Django's session login;
fulfil, cancel and reconciliation are restricted to ``is_staff`` users, every
other endpoint acts only on the caller's own data.

Payment views are not wrapped in the sandbox's ATOMIC_REQUESTS transaction:
a claim must be committed before PayPal is called and must survive whatever
happens afterwards.
"""

import functools
import json
import logging
from collections.abc import Callable
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import cards, reconciliation, services
from .cardinput import CardInput
from .common import ClientError, ServiceResult
from .paypal import ProviderError, translate

logger = logging.getLogger("apps.payments")

Order = get_model("order", "Order")

View = Callable[..., JsonResponse]


def _error(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({"error": code, "message": message, **extra}, status=status)


def _respond(result: ServiceResult) -> JsonResponse:
    return JsonResponse(result.body, status=result.status)


def api_view(methods: list[str], *, staff: bool = False, auth: bool = True) -> Callable[[View], View]:
    def decorate(view: View) -> View:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
            if auth and not request.user.is_authenticated:
                return _error(401, "not_authenticated", "Sign in first (POST /api/login)")
            if staff and not request.user.is_staff:
                return _error(403, "forbidden", "This action is restricted to staff users")
            try:
                return view(request, *args, **kwargs)
            except ClientError as e:
                return _error(e.status, e.code, e.message, **e.extra)
            except ProviderError as e:
                return _error(e.status_code, e.code, e.message, outcomeUnknown=e.outcome_unknown)
            except Exception as e:  # a PayPal read that failed somewhere unguarded
                try:
                    err = translate(e)
                except Exception:
                    logger.exception("Unhandled error in %s", view.__name__)
                    return _error(500, "internal_error", "Unexpected error")
                return _error(err.status_code, err.code, err.message, outcomeUnknown=err.outcome_unknown)

        return transaction.non_atomic_requests(require_http_methods(methods)(wrapper))

    return decorate


@sensitive_variables("data")
def _json(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise ClientError(400, "invalid_json", "The request body must be JSON") from None
    if not isinstance(data, dict):
        raise ClientError(400, "invalid_json", "The request body must be a JSON object")
    return data


def _own_order(request: HttpRequest, order_id: str) -> Any:
    order = Order.objects.filter(number=order_id, user=request.user).first()
    if order is None or not hasattr(order, "paypal_payment"):
        raise ClientError(404, "order_not_found", "No such order")
    return order


def _any_order(order_id: str) -> Any:
    order = Order.objects.filter(number=order_id).first()
    if order is None or not hasattr(order, "paypal_payment"):
        raise ClientError(404, "order_not_found", "No such order")
    return order


# --- session ---------------------------------------------------------------------

@ensure_csrf_cookie
@require_http_methods(["GET"])
def csrf(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"csrfToken": get_token(request)})


@api_view(["POST"], auth=False)
@sensitive_variables("data", "password")
def login_view(request: HttpRequest) -> JsonResponse:
    data = _json(request)
    username = str(data.get("username") or data.get("email") or "")
    password = str(data.get("password") or "")
    user = authenticate(request, username=username, password=password)
    if user is None:
        return _error(401, "invalid_credentials", "Wrong username or password")
    login(request, user)
    return JsonResponse({"user": user.get_username(), "isStaff": user.is_staff, "csrfToken": get_token(request)})


@api_view(["POST"], auth=False)
def logout_view(request: HttpRequest) -> JsonResponse:
    logout(request)
    return JsonResponse({"loggedOut": True})


# --- orders ----------------------------------------------------------------------

@api_view(["POST"])
def create_order(request: HttpRequest) -> JsonResponse:
    data = _json(request)
    items = data.get("items", data.get("lines"))
    if not isinstance(items, list) or not items:
        raise ClientError(422, "invalid_items", "'items' must be a non-empty list of {productId, quantity}")
    lines = []
    for item in items:
        if not isinstance(item, dict):
            raise ClientError(422, "invalid_items", "Each item must be an object")
        try:
            product_id = int(str(item.get("productId", item.get("id", ""))))
            quantity = int(str(item.get("quantity", 1)))
        except (TypeError, ValueError):
            raise ClientError(422, "invalid_items", "productId and quantity must be integers") from None
        if not 1 <= quantity <= 1000:
            raise ClientError(422, "invalid_items", "quantity must be between 1 and 1000")
        lines.append((product_id, quantity))
    order = services.place_order(request.user, request, lines)
    body = services.serialize_order(order)
    return JsonResponse(body, status=201)


@api_view(["GET"])
def my_orders(request: HttpRequest) -> JsonResponse:
    orders = (
        Order.objects.filter(user=request.user)
        .select_related("paypal_payment")
        .prefetch_related("lines", "paypal_payment__refunds")
        .order_by("-date_placed")
    )
    return JsonResponse({"orders": [services.serialize_order(o) for o in orders]})


@api_view(["POST"])
@sensitive_variables("data", "card")
def pay(request: HttpRequest, order_id: str) -> JsonResponse:
    order = _own_order(request, order_id)
    data = _json(request)
    payment_method_id = data.get("paymentMethodId")
    card_data = data.get("card")
    if bool(payment_method_id) == bool(card_data):
        raise ClientError(422, "payment_source_required", "Send exactly one of 'card' or 'paymentMethodId'")
    saved = cards.usable_card(request.user, str(payment_method_id)) if payment_method_id else None
    card = CardInput.parse(card_data) if card_data else None
    return _respond(services.pay(order, card=card, saved_card=saved))


@api_view(["POST"], staff=True)
def fulfil(request: HttpRequest, order_id: str) -> JsonResponse:
    return _respond(services.fulfil(_any_order(order_id)))


@api_view(["POST"], staff=True)
def cancel(request: HttpRequest, order_id: str) -> JsonResponse:
    return _respond(services.cancel(_any_order(order_id)))


@api_view(["POST"])
def refunds(request: HttpRequest, order_id: str) -> JsonResponse:
    order = _own_order(request, order_id)
    data = _json(request)
    key = str(request.headers.get("Idempotency-Key") or data.get("idempotencyKey") or "").strip()
    if not key or len(key) > 128:
        raise ClientError(422, "idempotency_key_required", "Send an Idempotency-Key header (1-128 characters)")
    amount = None
    if data.get("amount") is not None:
        try:
            amount = Decimal(str(data["amount"]))
        except InvalidOperation:
            raise ClientError(422, "invalid_amount", "amount must be a decimal string such as \"5.00\"") from None
        if not amount.is_finite() or amount <= 0:
            raise ClientError(422, "invalid_amount", "amount must be greater than zero")
    return _respond(services.refund(order, idempotency_key=key, amount=amount))


# --- saved cards --------------------------------------------------------------------

@api_view(["GET", "POST"])
@sensitive_variables("data", "card")
def payment_methods(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        return JsonResponse({"paymentMethods": [cards.serialize_card(c) for c in cards.list_cards(request.user)]})
    data = _json(request)
    card = CardInput.parse(data.get("card", data))
    key = str(request.headers.get("Idempotency-Key") or "").strip()[:128] or None
    result = cards.save_card(request.user, card, key)
    if result.status == 200:
        result.status = 201
    return _respond(result)


@api_view(["DELETE"])
def payment_method(request: HttpRequest, payment_method_id: Any) -> JsonResponse:
    return _respond(cards.delete_card(request.user, str(payment_method_id)))


# --- reconciliation -------------------------------------------------------------------

def _parse_when(value: str | None, name: str) -> Any:
    if not value:
        raise ClientError(422, "invalid_range", f"'{name}' is required (ISO-8601 date-time)")
    parsed = parse_datetime(value.replace(" ", "+"))  # a '+' offset arrives as a space when not URL-encoded
    if parsed is None:
        raise ClientError(422, "invalid_range", f"'{name}' must be an ISO-8601 date-time")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


@api_view(["GET"], staff=True)
def reconciliation_report(request: HttpRequest) -> JsonResponse:
    start = _parse_when(request.GET.get("from"), "from")
    end = _parse_when(request.GET.get("to"), "to")
    return JsonResponse(reconciliation.reconcile(start, end))
