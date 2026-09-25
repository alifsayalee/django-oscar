"""
JSON API under /api/. Callers authenticate with the sandbox's own Django session
login (CSRF applies to unsafe methods as for any session-authenticated form).

Views that talk to the provider are ``non_atomic_requests``: a send's claim row
must be committed before the provider call, not held in the request transaction.
"""
import json
import logging
from datetime import timezone as dt_timezone
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import services
from .gateway import ProviderError
from .models import SmsNotification
from .services import Conflict, Invalid, mask_number

logger = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


def error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def api_view(methods, staff=False):
    """Method filter + session auth (401) + staff check (403) + one error ladder."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return error(401, "Authentication required.")
            if staff and not request.user.is_staff:
                return error(403, "This action is restricted to staff.")
            try:
                return view(request, *args, **kwargs)
            except Invalid as exc:
                return error(exc.status_code, str(exc))
            except Conflict as exc:
                return error(409, str(exc))
            except ProviderError as exc:
                logger.warning("provider error in %s: %s (HTTP %s, code %s)", view.__name__,
                               type(exc).__name__, exc.provider_status, exc.provider_code)
                return error(exc.status_code, exc.message, outcomeUnknown=exc.outcome_unknown)

        return transaction.non_atomic_requests(require_http_methods(methods)(wrapped))

    return decorator


def json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise Invalid("Request body must be JSON.")
    if not isinstance(data, dict):
        raise Invalid("Request body must be a JSON object.")
    return data


def iso(value):
    return value.isoformat() if value else None


def number_json(number):
    return {
        "contactNumberId": number.pk,
        "phoneNumber": number.phone_number,  # the owner's own number, as the provider canonicalised it
        "countryCode": number.country_code,
        "createdAt": iso(number.created_at),
    }


def notification_json(row):
    return {
        "notificationId": row.pk,
        "orderId": row.order_id,
        "kind": row.kind,
        "outcome": row.outcome,
        "providerStatus": row.provider_status or None,
        "providerMessageSid": row.provider_sid,
        "errorCode": row.error_code,
        "to": mask_number(row.to_number),
        "body": None if row.content_disposed_at else row.body,
        "contentDisposedAt": iso(row.content_disposed_at),
        "scheduledFor": iso(row.scheduled_for),
        "sentAt": iso(row.provider_sent_at),
        "lastCheckedAt": iso(row.last_checked_at),
        "cancelState": row.cancel_state or None,
        "resendOf": row.resend_of_id,
        "createdAt": iso(row.created_at),
    }


def order_json(order, notifications=None):
    data = {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "currency": order.currency,
        "totalInclTax": str(order.total_incl_tax),
        "datePlaced": iso(order.date_placed),
        "lines": [{"productId": line.product_id, "title": line.title, "quantity": line.quantity}
                  for line in order.lines.all()],
    }
    if notifications is not None:
        data["notifications"] = [notification_json(n) for n in notifications]
    return data


def order_notifications(order):
    rows = list(order.sms_notifications.select_related("contact_number").all())
    services.refresh_all(rows)
    return rows


def parse_instant(value, name):
    parsed = parse_datetime(value or "")
    if parsed is None:
        raise Invalid("%s must be an ISO-8601 date-time." % name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


# --------------------------------------------------------------------------
# Flow 1 - contact numbers
# --------------------------------------------------------------------------


@api_view(["GET", "POST"])
def contact_numbers(request):
    if request.method == "GET":
        return JsonResponse({"contactNumbers": [number_json(n) for n in services.active_numbers(request.user)]})
    data = json_body(request)
    raw = data.get("phoneNumber")
    country = data.get("countryCode")
    if not isinstance(raw, str) or (country is not None and not isinstance(country, str)):
        raise Invalid("phoneNumber (string) is required; countryCode is optional.")
    number, created = services.register_number(request.user, raw, country)
    return JsonResponse(number_json(number), status=201 if created else 200)


@api_view(["DELETE"])
def contact_number_detail(request, contact_number_id):
    if services.delete_number(request.user, contact_number_id) is None:
        return error(404, "No such contact number.")
    return HttpResponse(status=204)


# --------------------------------------------------------------------------
# Flow 2 - orders
# --------------------------------------------------------------------------


@api_view(["POST"])
def orders(request):
    data = json_body(request)
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 50:
        raise Invalid("items must be a list of 1-50 {productId, quantity} objects.")
    items = []
    for item in raw_items:
        product_id = item.get("productId") if isinstance(item, dict) else None
        quantity = item.get("quantity", 1) if isinstance(item, dict) else None
        if (not isinstance(product_id, int) or isinstance(product_id, bool)
                or not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 99):
            raise Invalid("Each item needs an integer productId and a quantity from 1 to 99.")
        items.append((product_id, quantity))
    order = services.place_order(request.user, items)
    body = order_json(order, list(order.sms_notifications.all()))
    return JsonResponse(body, status=201)


@api_view(["GET"])
def my_orders(request):
    rows = Order.objects.filter(user=request.user).order_by("-date_placed").prefetch_related("lines")
    return JsonResponse({"orders": [order_json(o, order_notifications(o)) for o in rows]})


@api_view(["GET"])
def order_notification_list(request, order_id):
    order = Order.objects.filter(pk=order_id, user=request.user).first()
    if order is None:
        return error(404, "No such order.")
    return JsonResponse({"orderId": order.pk, "notifications": [notification_json(n)
                                                                 for n in order_notifications(order)]})


@api_view(["POST"], staff=True)
def dispatch(request, order_id):
    order = services.dispatch_order(order_id)
    if order is None:
        return error(404, "No such order.")
    return JsonResponse(order_json(order, list(order.sms_notifications.all())))


@api_view(["POST"], staff=True)
def cancel(request, order_id):
    order = services.cancel_order(order_id)
    if order is None:
        return error(404, "No such order.")
    return JsonResponse(order_json(order, list(order.sms_notifications.all())))


# --------------------------------------------------------------------------
# Flow 3 - operator actions
# --------------------------------------------------------------------------


@api_view(["POST"], staff=True)
def resend(request, notification_id):
    data = json_body(request)
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    if not isinstance(key, str) or not 1 <= len(key) <= 255:
        raise Invalid("An idempotency key (Idempotency-Key header or idempotencyKey field, 1-255 chars) "
                      "is required.")
    result = services.resend(notification_id, key)
    if result is None:
        return error(404, "No such notification.")
    row = result.notification
    body = {"notificationId": row.pk, "repeated": result.repeated, "notification": notification_json(row)}
    if row.outcome in (SmsNotification.SENDING, SmsNotification.UNKNOWN):
        # Not confirmed either way: never reported as "not sent".
        return JsonResponse({**body, "outcomeUnknown": True}, status=202 if row.outcome == "sending" else 504)
    if row.outcome == SmsNotification.FAILED and not row.provider_sid:
        return JsonResponse({**body, "error": "The provider did not accept the message."}, status=502)
    return JsonResponse(body, status=200 if result.repeated else 201)


@api_view(["DELETE"], staff=True)
def notification_content(request, notification_id):
    row = services.dispose_content(notification_id)
    if row is None:
        return error(404, "No such notification.")
    return JsonResponse(notification_json(row))


@api_view(["GET"], staff=True)
def reconciliation(request):
    start = parse_instant(request.GET.get("from"), "from")
    end = parse_instant(request.GET.get("to"), "to")
    if end <= start:
        raise Invalid("to must be after from.")
    report = services.reconcile(start, end)

    def provider_json(message):
        return {"providerMessageSid": message.sid, "status": message.status, "sentAt": iso(message.date_sent),
                "to": mask_number(message.to)}

    def local_json(row):
        return {"notificationId": row.pk, "orderId": row.order_id, "kind": row.kind, "outcome": row.outcome,
                "providerMessageSid": row.provider_sid, "providerStatus": row.provider_status or None,
                "sentAt": iso(row.provider_sent_at), "createdAt": iso(row.created_at),
                "cancelState": row.cancel_state or None}

    matched = [{"provider": provider_json(m), "local": local_json(n),
                "statusDiffers": (m.status or "") != (n.provider_status or "")} for m, n in report.matched]
    return JsonResponse({
        "from": iso(report.start), "to": iso(report.end),
        "sendingNumber": mask_number(settings.TWILIO_FROM_NUMBER),
        "counts": {"provider": len(report.matched) + len(report.provider_only), "matched": len(matched),
                   "providerOnly": len(report.provider_only), "localOnly": len(report.local_only),
                   "notSent": len(report.not_sent), "unsettled": len(report.unsettled)},
        "matched": matched,
        "providerOnly": [provider_json(m) for m, __ in report.provider_only],
        "localOnly": [local_json(n) for n in report.local_only],
        "notSent": [local_json(n) for n in report.not_sent],
        "unsettled": [local_json(n) for n in report.unsettled],
    })
