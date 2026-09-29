"""JSON API for order SMS notifications, mounted under /api/.

Callers authenticate with Django's session login (``/api/session``); CSRF
protection applies to every state-changing request. Dispatch, cancel, resend,
content disposal and reconciliation are restricted to ``is_staff`` users;
everything else acts only on the caller's own data.

These views opt out of ATOMIC_REQUESTS: a provider write's claim must be
committed before the provider is called.
"""

import json
import logging
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie

from oscar.core.loading import get_model

from . import services
from .models import ContactNumber, Notification
from .outcomes import answer_status
from .provider import ProviderError, mask_number

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")

View = Callable[..., JsonResponse]


def error(status: int, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({"error": message, **extra}, status=status)


def api_view(methods: tuple[str, ...], *, staff: bool = False, login_required: bool = True) -> Callable[[View], View]:
    def decorator(view: View) -> View:
        @transaction.non_atomic_requests
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
            if request.method not in methods:
                response = error(405, "Method not allowed.")
                response["Allow"] = ", ".join(methods)
                return response
            if login_required and not request.user.is_authenticated:
                return error(401, "Authentication required.")
            if staff and not request.user.is_staff:
                return error(403, "Only staff operators may do this.")
            try:
                return view(request, *args, **kwargs)
            except services.ServiceError as e:
                return error(e.status_code, e.message, **e.extra)
            except ProviderError as e:
                return error(e.status_code, e.message, outcomeUnknown=e.outcome_unknown,
                             providerCode=e.provider_code)
            except BadRequest as e:
                return error(400, str(e))

        return wrapper

    return decorator


class BadRequest(Exception):
    pass


def json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise BadRequest("The request body must be JSON.")
    if not isinstance(data, dict):
        raise BadRequest("The request body must be a JSON object.")
    return data


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def notification_json(n: Notification) -> dict[str, Any]:
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "to": mask_number(n.to_number),
        "outcome": n.outcome,
        "providerStatus": n.provider_status or None,
        "messageSid": n.message_sid or None,
        "scheduledFor": iso(n.scheduled_for),
        "providerCreatedAt": iso(n.provider_created_at),
        "providerSentAt": iso(n.provider_sent_at),
        "errorCode": n.error_code,
        "errorMessage": n.error_message or None,
        "callOffOutcome": n.call_off_outcome or None,
        "contentDisposed": n.content_disposed_at is not None,
        "disposalOutcome": n.disposal_outcome or None,
        "body": None if n.content_disposed_at else n.body,
        "resendOf": n.resend_of_id,
        "createdAt": iso(n.created_at),
    }


def order_json(order: Any, notifications: list[Notification]) -> dict[str, Any]:
    return {
        "orderId": order.pk,
        "number": str(order.number),
        "status": order.status,
        "currency": order.currency,
        "totalInclTax": services.money(order.total_incl_tax),
        "datePlaced": iso(order.date_placed),
        "notifications": [notification_json(n) for n in notifications],
    }


def contact_json(number: ContactNumber) -> dict[str, Any]:
    return {
        "contactNumberId": number.pk,
        "phoneNumber": number.phone_number,
        "countryCode": number.country_code or None,
        "nationalFormat": number.national_format or None,
        "createdAt": iso(number.created_at),
    }


# -- session -----------------------------------------------------------------


@ensure_csrf_cookie
@api_view(("GET", "POST", "DELETE"), login_required=False)
def session(request: HttpRequest) -> JsonResponse:
    if request.method == "POST":
        data = json_body(request)
        identifier = str(data.get("username") or data.get("email") or "")
        password = str(data.get("password") or "")
        if "@" in identifier:
            authenticated = authenticate(request, email=identifier, password=password)
        else:
            authenticated = authenticate(request, username=identifier, password=password)
        if authenticated is None:
            return error(401, "Invalid credentials.")
        login(request, authenticated)
    elif request.method == "DELETE":
        logout(request)
    current = request.user
    return JsonResponse({
        "authenticated": current.is_authenticated,
        "userId": current.pk if current.is_authenticated else None,
        "isStaff": bool(current.is_authenticated and current.is_staff),
        "csrfToken": get_token(request),
    })


# -- contact numbers ---------------------------------------------------------


@api_view(("GET", "POST"))
def contact_numbers(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        return JsonResponse({"contactNumbers": [contact_json(n) for n in services.active_numbers(request.user)]})
    data = json_body(request)
    raw = data.get("phoneNumber")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 32:
        raise BadRequest("phoneNumber is required.")
    country = data.get("countryCode")
    if country is not None and (not isinstance(country, str) or len(country) != 2):
        raise BadRequest("countryCode must be a two-letter ISO country code.")
    number, created = services.register_number(request.user, raw.strip(), country.upper() if country else None)
    return JsonResponse(contact_json(number), status=201 if created else 200)


@api_view(("DELETE",))
def contact_number_detail(request: HttpRequest, contact_number_id: int) -> JsonResponse:
    number = ContactNumber.objects.filter(pk=contact_number_id).first()
    if number is None or number.user_id != request.user.pk:  # never reveal another shopper's number
        return error(404, "Contact number not found.")
    result = services.remove_number(number)
    body = {
        "contactNumberId": contact_number_id,
        "removed": True,
        "erased": result.erased,
        "outcome": result.outcome,
        "followUpCallOffs": [
            {"notificationId": n.pk, "callOffOutcome": n.call_off_outcome or None} for n in result.call_offs
        ],
    }
    return JsonResponse(body, status=200 if result.erased else answer_status(result.outcome))


# -- orders ------------------------------------------------------------------


@api_view(("POST",))
def orders(request: HttpRequest) -> JsonResponse:
    data = json_body(request)
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise BadRequest("items must be a non-empty list of {productId, quantity}.")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise BadRequest("Each item must be an object.")
        product_id, quantity = raw.get("productId"), raw.get("quantity", 1)
        if not isinstance(product_id, int) or not isinstance(quantity, int) or quantity < 1:
            raise BadRequest("Each item needs an integer productId and a positive integer quantity.")
        items.append(services.OrderItem(product_id=product_id, quantity=quantity))
    address = data.get("shippingAddress")
    if address is not None and not isinstance(address, dict):
        raise BadRequest("shippingAddress must be an object.")
    order = services.place_order(request.user, items, address)
    return JsonResponse(order_json(order, list(order.sms_notifications.all())), status=201)


@api_view(("GET",))
def my_orders(request: HttpRequest) -> JsonResponse:
    user_orders = list(Order.objects.filter(user=request.user).order_by("-date_placed")[:50])
    notifications = list(Notification.objects.filter(order__in=user_orders).select_related("order"))
    services.refresh_many(notifications)
    services.settle_cancelled_orders_follow_ups(notifications)
    by_order: dict[int, list[Notification]] = {}
    for n in notifications:
        by_order.setdefault(n.order_id, []).append(n)
    return JsonResponse({"orders": [order_json(o, by_order.get(o.pk, [])) for o in user_orders]})


def _staff_order(order_id: int) -> Any:
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        raise services.ServiceError(404, "Order not found.")
    return order


@api_view(("POST",), staff=True)
def order_dispatch(request: HttpRequest, order_id: int) -> JsonResponse:
    order = _staff_order(order_id)
    notifications = services.dispatch_order(order)
    return JsonResponse(order_json(order, notifications))


@api_view(("POST",), staff=True)
def order_cancel(request: HttpRequest, order_id: int) -> JsonResponse:
    order = _staff_order(order_id)
    notifications = services.cancel_order(order)
    return JsonResponse(order_json(order, notifications))


@api_view(("GET",))
def order_notifications(request: HttpRequest, order_id: int) -> JsonResponse:
    order = Order.objects.filter(pk=order_id, user=request.user).first()
    if order is None:
        return error(404, "Order not found.")
    notifications = list(order.sms_notifications.select_related("order"))
    services.refresh_many(notifications)
    services.settle_cancelled_orders_follow_ups(notifications)
    return JsonResponse({"orderId": order.pk, "status": order.status,
                         "notifications": [notification_json(n) for n in notifications]})


# -- operator actions on notifications ---------------------------------------


def _notification(notification_id: int) -> Notification:
    n = Notification.objects.select_related("order").filter(pk=notification_id).first()
    if n is None:
        raise services.ServiceError(404, "Notification not found.")
    return n


@api_view(("POST",), staff=True)
def notification_resend(request: HttpRequest, notification_id: int) -> JsonResponse:
    original = _notification(notification_id)
    key = request.headers.get("Idempotency-Key") or json_body(request).get("idempotencyKey")
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise BadRequest("An Idempotency-Key header (or idempotencyKey field) is required.")
    n = services.resend(original, key.strip())
    return JsonResponse({"notificationId": n.pk, "resendOf": original.pk, "outcome": n.outcome,
                         "notification": notification_json(n)}, status=answer_status(n.outcome))


@api_view(("DELETE",), staff=True)
def notification_content(request: HttpRequest, notification_id: int) -> JsonResponse:
    n = _notification(notification_id)
    outcome = services.dispose_content(n)
    n.refresh_from_db()
    return JsonResponse({"notificationId": n.pk, "outcome": outcome, "notification": notification_json(n)},
                        status=answer_status(outcome))


def _parse_datetime(name: str, value: str | None) -> datetime:
    if not value:
        raise BadRequest("%s is required (ISO-8601 date-time)." % name)
    try:
        # An unencoded '+' in a query string arrives as a space.
        parsed = datetime.fromisoformat(value.strip().replace(" ", "+"))
    except ValueError:
        raise BadRequest("%s must be an ISO-8601 date-time." % name)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@api_view(("GET",), staff=True)
def reconciliation(request: HttpRequest) -> JsonResponse:
    start = _parse_datetime("from", request.GET.get("from"))
    end = _parse_datetime("to", request.GET.get("to"))
    if end <= start:
        raise BadRequest("'to' must be after 'from'.")
    return JsonResponse(services.reconcile(start, end))

