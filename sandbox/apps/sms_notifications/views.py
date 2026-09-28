"""
JSON API for order SMS notifications, under /api/.

Callers authenticate with Django's session login (the sandbox's own
authentication); the caller's identity is always ``request.user``. Operator
actions require ``is_staff``; everything else acts on the caller's own data.
"""

import functools
import json
import logging
from datetime import datetime, timezone as dt_timezone

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import services
from . import twilio_gateway as gw
from .models import ContactNumber, Notification, ProviderWrite
from .safe_write import answer_status, worst_outcome

logger = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


def error(status, message):
    return JsonResponse({"error": message}, status=status)


def api_view(*methods, staff=False, login_required=True):
    def decorator(view):
        @require_http_methods(list(methods))
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if login_required and not request.user.is_authenticated:
                return error(401, "Authentication required.")
            if staff and not request.user.is_staff:
                return error(403, "This action is restricted to staff.")
            try:
                return view(request, *args, **kwargs)
            except services.ServiceError as e:
                return error(e.status_code, e.message)
            except gw.ProviderError as e:
                return JsonResponse({"error": e.message, "outcomeUnknown": e.outcome_unknown}, status=e.status_code)

        return wrapper

    return decorator


def read_json(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise services.ServiceError(400, "The request body is not valid JSON.")
    if not isinstance(data, dict):
        raise services.ServiceError(400, "The request body must be a JSON object.")
    return data


def iso(value):
    return value.isoformat() if value else None


def contact_view(contact):
    return {
        "contactNumberId": contact.pk,
        "phoneNumber": contact.phone_number,
        "countryCode": contact.country_code,
        "createdAt": iso(contact.created_at),
    }


def notification_view(notification):
    outcome = services.notification_outcome(notification)
    view = {
        "notificationId": notification.pk,
        "orderId": notification.order_id,
        "kind": notification.kind,
        # Our reading of the provider's status: done (delivered), pending,
        # failed or unknown - never inferred from having an id.
        "outcome": outcome,
        "providerMessageSid": notification.provider_sid or None,
        "providerStatus": notification.provider_status or None,
        "providerErrorCode": notification.provider_error_code,
        "sentAt": iso(notification.provider_date_sent),
        "scheduledFor": iso(notification.send_at),
        "lastCheckedAt": iso(notification.last_checked_at),
        "contentDisposed": notification.content_redacted_at is not None,
        "resendOf": notification.resend_of_id,
        "createdAt": iso(notification.created_at),
    }
    call_off = notification.writes.filter(operation=ProviderWrite.OP_CANCEL).first()
    if call_off is not None:
        view["callOff"] = call_off.outcome
    return view


def call_off_view(call_off):
    return {
        "notificationId": call_off.notification.pk,
        "outcome": call_off.outcome,
        "detail": call_off.detail or None,
        "providerStatus": call_off.notification.provider_status or None,
    }


def order_view(order, notifications=None):
    if notifications is None:
        notifications = list(order.sms_notifications.all())
    return {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "placedAt": iso(order.date_placed),
        "notifications": [notification_view(n) for n in notifications],
    }


def own_order(request, order_id):
    try:
        return Order.objects.get(pk=order_id, user=request.user)
    except Order.DoesNotExist:
        # Another shopper's order is indistinguishable from none at all.
        raise services.ServiceError(404, "Order not found.")


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@ensure_csrf_cookie
@api_view("GET", login_required=False)
def csrf(request):
    return JsonResponse({"csrfToken": get_token(request)})


@api_view("POST", "DELETE", login_required=False)
def session(request):
    if request.method == "DELETE":
        logout(request)
        return JsonResponse({}, status=200)
    data = read_json(request)
    user = authenticate(request, username=data.get("username", ""), password=data.get("password", ""))
    if user is None or not user.is_active:
        return error(401, "Invalid credentials.")
    login(request, user)
    return JsonResponse({"userId": user.pk, "isStaff": user.is_staff, "csrfToken": get_token(request)})


# --------------------------------------------------------------------------
# Flow 1 - contact numbers
# --------------------------------------------------------------------------


@api_view("GET", "POST")
def contact_numbers(request):
    if request.method == "GET":
        numbers = ContactNumber.objects.filter(user=request.user, deleted_at__isnull=True)
        return JsonResponse({"contactNumbers": [contact_view(c) for c in numbers]})
    data = read_json(request)
    contact, created = services.register_contact_number(request.user, str(data.get("phoneNumber", "")))
    return JsonResponse(contact_view(contact), status=201 if created else 200)


@api_view("DELETE")
def contact_number(request, contact_id):
    contact, call_offs = services.remove_contact_number(request.user, contact_id)
    outcome = worst_outcome([c.outcome for c in call_offs])
    # The number is gone either way; the status says whether every follow-up
    # still scheduled for it is confirmed called off.
    return JsonResponse(
        {
            "contactNumberId": contact.pk,
            "removed": True,
            "callOffOutcome": outcome,
            "followUpCallOffs": [call_off_view(c) for c in call_offs],
        },
        status=answer_status(outcome),
    )


# --------------------------------------------------------------------------
# Flow 2 - orders
# --------------------------------------------------------------------------


@api_view("POST")
def orders(request):
    data = read_json(request)
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list):
        raise services.ServiceError(400, "lines must be a list of {productId, quantity}.")
    lines = []
    for raw in raw_lines:
        try:
            product_id, quantity = int(raw["productId"]), int(raw.get("quantity", 1))
        except (TypeError, ValueError, KeyError):
            raise services.ServiceError(400, "Each line needs an integer productId and quantity.")
        if quantity < 1:
            raise services.ServiceError(400, "quantity must be at least 1.")
        lines.append(services.LineRequest(product_id, quantity))
    order, notification = services.place_order(request.user, lines)
    return JsonResponse(order_view(order, [notification] if notification else []), status=201)


@api_view("POST", staff=True)
def dispatch_order(request, order_id):
    order, notifications = services.dispatch_order(order_id)
    return JsonResponse(order_view(order, notifications))


@api_view("POST", staff=True)
def cancel_order(request, order_id):
    order, call_offs, notification = services.cancel_order(order_id)
    body = order_view(order, [notification] if notification else [])
    body["followUpCallOffs"] = [call_off_view(c) for c in call_offs]
    return JsonResponse(body)


@api_view("GET")
def my_orders(request):
    orders_ = list(Order.objects.filter(user=request.user).order_by("-date_placed")[:50])
    notifications = Notification.objects.filter(order__in=orders_)
    services.refresh_notifications(list(notifications))
    return JsonResponse({"orders": [order_view(o) for o in orders_]})


@api_view("GET")
def order_notifications(request, order_id):
    order = own_order(request, order_id)
    notifications = list(order.sms_notifications.all())
    services.refresh_notifications(notifications)
    return JsonResponse(
        {"orderId": order.pk, "notifications": [notification_view(n) for n in order.sms_notifications.all()]}
    )


# --------------------------------------------------------------------------
# Flow 3 - operator actions
# --------------------------------------------------------------------------


@api_view("POST", staff=True)
def resend_notification(request, notification_id):
    data = read_json(request)
    key = request.headers.get("Idempotency-Key") or str(data.get("idempotencyKey", ""))
    notification, write = services.resend(notification_id, key)
    body = notification_view(notification)
    body["resendOf"] = notification.resend_of_id
    return JsonResponse(body, status=answer_status(write.outcome))


@api_view("DELETE", staff=True)
def notification_content(request, notification_id):
    notification, outcome = services.dispose_content(notification_id)
    body = notification_view(notification)
    body["disposalOutcome"] = outcome
    return JsonResponse(body, status=answer_status(outcome))


def parse_instant(value, name):
    parsed = parse_datetime(value or "")
    if parsed is None:
        raise services.ServiceError(400, "%s must be an ISO-8601 date-time." % name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


@api_view("GET", staff=True)
def reconciliation(request):
    start = parse_instant(request.GET.get("from"), "from")
    end = parse_instant(request.GET.get("to"), "to")
    if not start < end:
        raise services.ServiceError(400, "from must be before to.")
    report = services.reconcile(start, end)

    def provider_view(record):
        return {
            "providerMessageSid": record.sid,
            "providerStatus": record.status_text or None,
            "sentAt": iso(record.date_sent),
            "errorCode": record.error_code,
        }

    def local_view(notification):
        return {
            "notificationId": notification.pk,
            "orderId": notification.order_id,
            "kind": notification.kind,
            "providerMessageSid": notification.provider_sid or None,
            "providerStatus": notification.provider_status or None,
            "sentAt": iso(notification.provider_date_sent),
        }

    return JsonResponse(
        {
            "from": iso(start),
            "to": iso(end),
            "fromNumberOnly": True,
            "summary": {
                "matched": len(report.matched),
                "providerOnly": len(report.provider_only),
                "localOnly": len(report.local_only),
                "unsettled": len(report.unsettled),
            },
            "matched": [dict(local_view(n), providerStatus=r.status_text or None) for n, r in report.matched],
            "providerOnly": [provider_view(r) for r in report.provider_only],
            "localOnly": [local_view(n) for n in report.local_only],
            "unsettled": [dict(local_view(n), outcome=services.notification_outcome(n)) for n in report.unsettled],
        }
    )
