"""
JSON API for SMS order notifications, mounted under /api/.

Callers authenticate with the sandbox's Django session login (CSRF applies to
unsafe methods as everywhere else on the site). Views opt out of
ATOMIC_REQUESTS so a message's claim is committed before the provider is called.
"""

import json
import logging
from datetime import datetime, timezone as dt_timezone
from functools import wraps
from typing import Any, Callable

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from oscar.core.loading import get_model

from . import gateway, services
from .models import ContactNumber, Notification, mask_number
from .reconciliation import reconcile

log = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")
Outcome = Notification.Outcome

View = Callable[..., HttpResponse]

MAX_ORDER_ITEMS = 50
MAX_QUANTITY = 99
REFRESH_PER_REQUEST = 20


def error(status: int, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({"error": message, **extra}, status=status)


def api_view(methods: dict[str, View], *, staff_only: bool = False) -> View:
    """One route, one handler per HTTP method; session-authenticated; no request-wide transaction."""

    @transaction.non_atomic_requests
    def view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        handler = methods.get(request.method or "")
        if handler is None:
            response = error(405, "Method not allowed.")
            response["Allow"] = ", ".join(methods)
            return response
        if not request.user.is_authenticated:
            return error(401, "Sign in first (Django session login).")
        if staff_only and not request.user.is_staff:
            return error(403, "Operators only.")
        try:
            return handler(request, *args, **kwargs)
        except services.ServiceError as e:
            return error(e.status_code, e.message, **e.extra)
        except gateway.ProviderError as e:
            return error(e.status_code, e.message, outcomeUnknown=e.outcome_unknown)

    return view


def get_or_404(queryset: Any, what: str, **lookup: Any) -> Any:
    """Like get_object_or_404, but answers JSON -- and the same 404 for another shopper's object."""
    found = queryset.filter(**lookup).first()
    if found is None:
        raise services.ServiceError(404, f"{what} not found.")
    return found


def caller_id(request: HttpRequest) -> int:
    """The authenticated caller's user id (api_view has already rejected anonymous callers)."""
    pk = request.user.pk
    if not isinstance(pk, int):
        raise services.ServiceError(401, "Sign in first (Django session login).")
    return pk


def read_json(request: HttpRequest) -> dict[str, Any]:
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise services.ServiceError(400, "The request body must be JSON.")
    if not isinstance(data, dict):
        raise services.ServiceError(400, "The request body must be a JSON object.")
    return data


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def contact_json(c: ContactNumber) -> dict[str, Any]:
    return {
        "contactNumberId": c.pk,
        "phoneNumber": c.phone_number,
        "countryCode": c.country_code,
        "nationalFormat": c.national_format,
        "createdAt": iso(c.created_at),
    }


def notification_json(n: Notification) -> dict[str, Any]:
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "to": mask_number(n.to_number),
        "outcome": n.outcome,
        "outcomeDetail": n.outcome_detail or None,
        "provider": {
            "sid": n.provider_sid,
            "status": n.provider_status or None,
            "errorCode": n.provider_error_code,
            "errorMessage": n.provider_error_message or None,
            "dateCreated": iso(n.provider_date_created),
            "dateSent": iso(n.provider_date_sent),
        },
        "scheduledFor": iso(n.scheduled_for),
        "cancellation": (
            {"requestedAt": iso(n.cancel_requested_at), "outcome": n.cancel_outcome or None, "canceledAt": iso(n.canceled_at)}
            if n.cancel_requested_at
            else None
        ),
        "content": None if n.content_disposed_at else n.body,
        "contentDisposedAt": iso(n.content_disposed_at),
        "resendOf": n.resend_of_id,
        "lastCheckedAt": iso(n.last_checked_at),
        "createdAt": iso(n.created_at),
    }


def order_json(order: Any, notifications: list[Notification]) -> dict[str, Any]:
    return {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "placedAt": iso(order.date_placed),
        "lines": [{"productId": line.product_id, "title": line.title, "quantity": line.quantity} for line in order.lines.all()],
        "notifications": [notification_json(n) for n in notifications],
    }


def refreshed_notifications(order: Any) -> list[Notification]:
    services.refresh_many(list(order.sms_notifications.all()), REFRESH_PER_REQUEST)
    return list(order.sms_notifications.all())


# --- Contact numbers ---------------------------------------------------------


def list_contact_numbers(request: HttpRequest) -> HttpResponse:
    numbers = ContactNumber.objects.filter(user_id=caller_id(request), deleted_at__isnull=True)
    return JsonResponse({"contactNumbers": [contact_json(c) for c in numbers]})


def create_contact_number(request: HttpRequest) -> HttpResponse:
    data = read_json(request)
    raw = data.get("phoneNumber")
    country = data.get("countryCode")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 32:
        raise services.ServiceError(400, "phoneNumber is required.")
    if country is not None and (not isinstance(country, str) or len(country) != 2):
        raise services.ServiceError(400, "countryCode must be a two-letter ISO country code.")
    number, created = services.register_contact_number(request.user, raw.strip(), country.upper() if country else None)
    return JsonResponse(contact_json(number), status=201 if created else 200)


def delete_contact_number(request: HttpRequest, contact_number_id: int) -> HttpResponse:
    number = get_or_404(ContactNumber.objects, "Contact number", pk=contact_number_id, user_id=caller_id(request), deleted_at__isnull=True)
    services.remove_contact_number(number)
    return HttpResponse(status=204)


# --- Orders ------------------------------------------------------------------


def create_order(request: HttpRequest) -> HttpResponse:
    data = read_json(request)
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not 0 < len(raw_items) <= MAX_ORDER_ITEMS:
        raise services.ServiceError(400, f"items must be a list of 1 to {MAX_ORDER_ITEMS} entries.")
    items = []
    for entry in raw_items:
        product_id = entry.get("productId") if isinstance(entry, dict) else None
        quantity = entry.get("quantity", 1) if isinstance(entry, dict) else None
        if type(product_id) is not int or type(quantity) is not int or not 0 < quantity <= MAX_QUANTITY:
            raise services.ServiceError(
                400, f"Each item needs an integer productId and a quantity between 1 and {MAX_QUANTITY}."
            )
        items.append(services.OrderItem(product_id=product_id, quantity=quantity))
    order, _ = services.place_order(request.user, items, request)
    return JsonResponse(order_json(order, list(order.sms_notifications.all())), status=201)


def my_orders(request: HttpRequest) -> HttpResponse:
    orders = list(Order.objects.filter(user_id=caller_id(request)).order_by("-date_placed", "-id")[:100])
    pending = [n for o in orders for n in o.sms_notifications.all()]
    services.refresh_many(pending, REFRESH_PER_REQUEST)
    return JsonResponse({"orders": [order_json(o, list(o.sms_notifications.all())) for o in orders]})


def dispatch(request: HttpRequest, order_id: int) -> HttpResponse:
    get_or_404(Order.objects, "Order", pk=order_id)
    order, changed = services.dispatch_order(order_id)
    return JsonResponse({**order_json(order, list(order.sms_notifications.all())), "changed": changed})


def cancel(request: HttpRequest, order_id: int) -> HttpResponse:
    get_or_404(Order.objects, "Order", pk=order_id)
    order, changed = services.cancel_order(order_id)
    return JsonResponse({**order_json(order, list(order.sms_notifications.all())), "changed": changed})


def order_notifications(request: HttpRequest, order_id: int) -> HttpResponse:
    if request.user.is_staff:
        order = get_or_404(Order.objects, "Order", pk=order_id)
    else:
        order = get_or_404(Order.objects, "Order", pk=order_id, user_id=caller_id(request))
    return JsonResponse({"orderId": order.pk, "notifications": [notification_json(n) for n in refreshed_notifications(order)]})


# --- Operator actions on notifications ---------------------------------------

RESEND_STATUS: dict[str, int] = {
    Outcome.DONE: 201,
    Outcome.PENDING: 202,
    Outcome.SENDING: 202,
    Outcome.FAILED: 502,
    Outcome.NEEDS_REVIEW: 502,
    Outcome.UNKNOWN: 504,
}


def resend(request: HttpRequest, notification_id: int) -> HttpResponse:
    original = get_or_404(Notification.objects, "Notification", pk=notification_id)
    key = request.headers.get("Idempotency-Key") or read_json(request).get("idempotencyKey")
    if not isinstance(key, str) or not 0 < len(key) <= 255:
        raise services.ServiceError(400, "An Idempotency-Key header (or idempotencyKey field) of 1-255 characters is required.")
    n, repeat = services.resend(original, key, request.user)
    body = {**notification_json(n), "repeat": repeat, "outcomeUnknown": n.outcome == Outcome.UNKNOWN}
    return JsonResponse(body, status=RESEND_STATUS.get(n.outcome, 502))


def dispose_content(request: HttpRequest, notification_id: int) -> HttpResponse:
    n = get_or_404(Notification.objects, "Notification", pk=notification_id)
    return JsonResponse(notification_json(services.dispose_content(n)))


def parse_instant(value: str | None, name: str) -> datetime:
    if not value:
        raise services.ServiceError(400, f"'{name}' is required (ISO-8601 date-time).")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "+"))
    except ValueError:
        raise services.ServiceError(400, f"'{name}' is not an ISO-8601 date-time.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)  # a date-time without an offset is read as UTC
    return parsed.astimezone(dt_timezone.utc)


def reconciliation(request: HttpRequest) -> HttpResponse:
    start = parse_instant(request.GET.get("from"), "from")
    end = parse_instant(request.GET.get("to"), "to")
    if end <= start:
        raise services.ServiceError(400, "'to' must be after 'from'.")
    return JsonResponse(reconcile(start, end).as_json())


contact_numbers_view = api_view({"GET": list_contact_numbers, "POST": create_contact_number})
contact_number_view = api_view({"DELETE": delete_contact_number})
orders_view = api_view({"POST": create_order})
my_orders_view = api_view({"GET": my_orders})
dispatch_view = api_view({"POST": dispatch}, staff_only=True)
cancel_view = api_view({"POST": cancel}, staff_only=True)
order_notifications_view = api_view({"GET": order_notifications})
resend_view = api_view({"POST": resend}, staff_only=True)
content_view = api_view({"DELETE": dispose_content}, staff_only=True)
reconciliation_view = api_view({"GET": reconciliation}, staff_only=True)
