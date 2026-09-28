"""
JSON API for SMS order notifications.

Callers authenticate with the sandbox's Django session login. Views that write
to the provider are non-atomic so each claim commits before its provider call.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from datetime import timezone as dt_timezone
from functools import wraps
from typing import Any

import httpx
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods
from twilio_sdk.core import ApiError

from . import services
from .models import ContactNumber, Notification, Outcome, ProviderAction
from .safe_write import answer_status, delivery_outcome

logger = logging.getLogger(__name__)

View = Callable[..., HttpResponse]


def error(status: int, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({"error": message, **extra}, status=status)


def api_view(methods: list[str], *, staff: bool = False) -> Callable[[View], View]:
    """Session-authenticated JSON endpoint; `staff` restricts it to operators."""

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if not request.user.is_authenticated:
                return error(401, "Authentication required.")
            if staff and not request.user.is_staff:
                return error(403, "Operator (staff) access required.")
            try:
                return view(request, *args, **kwargs)
            except services.ServiceError as e:
                return error(e.status_code, e.message)
            except ImproperlyConfigured:
                logger.exception("SMS provider is not configured")
                return error(503, "SMS provider is not configured.")

        return transaction.non_atomic_requests(require_http_methods(methods)(wrapper))

    return decorator


def json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise services.ServiceError(400, "Request body must be JSON.")
    if not isinstance(data, dict):
        raise services.ServiceError(400, "Request body must be a JSON object.")
    return data


def current_user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):  # api_view has already refused anonymous callers
        raise services.ServiceError(401, "Authentication required.")
    return user


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# --- representations ----------------------------------------------------------


def contact_json(contact: ContactNumber) -> dict[str, Any]:
    return {
        "contactNumberId": contact.pk,
        "phoneNumber": contact.phone_number,
        "countryCode": contact.country_code or None,
        "createdAt": iso(contact.created_at),
    }


def notification_json(n: Notification) -> dict[str, Any]:
    actions = {a.action: a for a in n.actions.all()}
    cancel, redact = actions.get(ProviderAction.CANCEL), actions.get(ProviderAction.REDACT)
    return {
        "notificationId": n.pk,
        "orderId": str(n.order.number),
        "kind": n.kind,
        "resendOf": n.resend_of_id,
        # The send write's outcome, then where delivery got to.
        "sendOutcome": n.outcome,
        "deliveryOutcome": delivery_outcome(n.provider_status)
        if n.provider_status
        else Outcome.UNKNOWN,
        "messageSid": n.provider_sid or None,
        "providerStatus": n.provider_status or None,
        "errorCode": n.provider_error_code,
        "errorMessage": n.provider_error_message or None,
        "scheduledFor": iso(n.send_at),
        "dateCreated": iso(n.provider_date_created),
        "dateSent": iso(n.provider_date_sent),
        "lastCheckedAt": iso(n.provider_checked_at),
        "text": n.text if n.content_disposed_at is None else None,
        "contentDisposedAt": iso(n.content_disposed_at),
        "contentDisposal": redact.outcome if redact else None,
        "cancelRequestedAt": iso(n.cancel_requested_at),
        "cancellation": cancel.outcome if cancel else None,
    }


def order_json(order: Any, notifications: list[Notification]) -> dict[str, Any]:
    return {
        "orderId": str(order.number),
        "status": order.status,
        "dispatched": services.is_dispatched(order),
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "placedAt": iso(order.date_placed),
        "lines": [
            {"productId": line.product_id, "title": line.title, "quantity": line.quantity}
            for line in order.lines.all()
        ],
        "notifications": [notification_json(n) for n in notifications],
    }


def cancel_json(result: services.CancelResult) -> dict[str, Any]:
    return {
        "notificationId": result.notification_id,
        "outcome": result.outcome,
        "detail": result.detail or None,
    }


# --- lookups scoped to the caller ---------------------------------------------


def get_order_for(request: HttpRequest, order_id: str, *, operator: bool) -> Any:
    orders = services.Order.objects.all()
    if not operator:
        orders = orders.filter(user=current_user(request))
    try:
        return orders.get(number=order_id)
    except services.Order.DoesNotExist:
        raise services.ServiceError(404, "No such order.")


def get_notification(notification_id: int) -> Notification:
    try:
        return Notification.objects.select_related("order", "contact_number").get(
            pk=notification_id
        )
    except Notification.DoesNotExist:
        raise services.ServiceError(404, "No such notification.")


# --- Flow 1: contact numbers --------------------------------------------------


@api_view(["GET", "POST"])
def contact_numbers(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        numbers = ContactNumber.objects.filter(user=current_user(request), removed_at__isnull=True)
        return JsonResponse({"contactNumbers": [contact_json(c) for c in numbers]})
    data = json_body(request)
    raw = data.get("phoneNumber")
    country = data.get("countryCode")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 40:
        return error(400, "phoneNumber is required.")
    if country is not None and (not isinstance(country, str) or len(country) != 2):
        return error(400, "countryCode must be a two-letter ISO country code.")
    contact, created = services.register_contact_number(
        current_user(request), raw.strip(), country.upper() if country else None
    )
    return JsonResponse(contact_json(contact), status=201 if created else 200)


@api_view(["DELETE"])
def contact_number_detail(request: HttpRequest, contact_number_id: int) -> HttpResponse:
    try:
        contact = ContactNumber.objects.get(
            pk=contact_number_id, user=current_user(request), removed_at__isnull=True
        )
    except ContactNumber.DoesNotExist:
        return error(404, "No such contact number.")
    cancellations = services.remove_contact_number(contact)
    return JsonResponse(
        {
            "contactNumberId": contact.pk,
            "removed": True,
            "followUpCancellations": [cancel_json(c) for c in cancellations],
        }
    )


# --- Flow 2: orders -----------------------------------------------------------


@api_view(["POST"])
def orders(request: HttpRequest) -> HttpResponse:
    data = json_body(request)
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines or len(raw_lines) > 50:
        return error(400, "lines must be a non-empty list of {productId, quantity}.")
    lines: list[tuple[int, int]] = []
    for line in raw_lines:
        product_id = line.get("productId") if isinstance(line, dict) else None
        quantity = line.get("quantity", 1) if isinstance(line, dict) else None
        if (
            not isinstance(product_id, int)
            or isinstance(product_id, bool)
            or not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or not 1 <= quantity <= 100
        ):
            return error(400, "Each line needs an integer productId and a quantity of 1-100.")
        lines.append((product_id, quantity))
    order, notification = services.place_order(current_user(request), lines)
    body = order_json(order, [notification] if notification else [])
    body["notification"] = notification_json(notification) if notification else None
    return JsonResponse(body, status=201)


@api_view(["POST"], staff=True)
def dispatch_order(request: HttpRequest, order_id: str) -> HttpResponse:
    order = get_order_for(request, order_id, operator=True)
    result = services.dispatch_order(order)
    return JsonResponse(order_json(result.order, result.notifications))


@api_view(["POST"], staff=True)
def cancel_order(request: HttpRequest, order_id: str) -> HttpResponse:
    order = get_order_for(request, order_id, operator=True)
    result = services.cancel_order(order)
    body = order_json(result.order, result.notifications)
    body["followUpCancellations"] = [cancel_json(c) for c in result.follow_up_cancellations]
    return JsonResponse(body)


@api_view(["GET"])
def my_orders(request: HttpRequest) -> HttpResponse:
    user_orders = services.Order.objects.filter(user=current_user(request)).order_by("-date_placed")
    return JsonResponse(
        {
            "orders": [
                order_json(order, services.refresh_order_notifications(order))
                for order in user_orders
            ]
        }
    )


@api_view(["GET"])
def order_notifications(request: HttpRequest, order_id: str) -> HttpResponse:
    order = get_order_for(request, order_id, operator=request.user.is_staff)
    notifications = services.refresh_order_notifications(order)
    return JsonResponse(
        {
            "orderId": str(order.number),
            "notifications": [notification_json(n) for n in notifications],
        }
    )


# --- Flow 3: operator actions -------------------------------------------------


@api_view(["POST"], staff=True)
def resend_notification(request: HttpRequest, notification_id: int) -> HttpResponse:
    data = json_body(request)
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        return error(400, "An Idempotency-Key header (or idempotencyKey field) is required.")
    original = get_notification(notification_id)
    notification = services.resend(original, key.strip())
    body = notification_json(notification)
    body["outcome"] = notification.outcome
    # The resend's own outcome decides the status: 200 delivered, 202 in flight,
    # 409 failed, 504 unknown.
    return JsonResponse(body, status=answer_status(notification.outcome))


@api_view(["DELETE"], staff=True)
def notification_content(request: HttpRequest, notification_id: int) -> HttpResponse:
    notification = get_notification(notification_id)
    action = services.dispose_content(notification)
    notification.refresh_from_db()
    body = notification_json(notification)
    if action is None:
        body["outcome"] = (
            Outcome.DONE if notification.outcome == Outcome.FAILED else Outcome.UNKNOWN
        )
        body["detail"] = (
            "The message never reached the provider."
            if notification.outcome == Outcome.FAILED
            else "The provider's record of this message is not settled yet."
        )
    else:
        body["outcome"] = action.outcome
    return JsonResponse(body, status=answer_status(body["outcome"]))


@api_view(["GET"], staff=True)
def reconciliation(request: HttpRequest) -> HttpResponse:
    start = parse_datetime(request.GET.get("from", ""))
    end = parse_datetime(request.GET.get("to", ""))
    if start is None or end is None:
        return error(400, "from and to must be ISO-8601 date-times.")
    if timezone.is_naive(start):
        start = start.replace(tzinfo=dt_timezone.utc)
    if timezone.is_naive(end):
        end = end.replace(tzinfo=dt_timezone.utc)
    if end < start:
        return error(400, "to must not be before from.")
    try:
        report = services.reconcile(start, end)
    except ApiError as e:
        logger.warning("reconciliation list failed: HTTP %s", e.status_code)
        return error(502, "The messaging provider could not list messages.")
    except (httpx.RequestError, ValueError):
        return error(502, "The messaging provider could not be reached.")
    return JsonResponse(report)
