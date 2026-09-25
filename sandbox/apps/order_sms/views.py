"""
JSON API for order SMS notifications, mounted under ``/api/``.

Callers authenticate with Django's session login (``/api/login`` is a thin
wrapper over ``django.contrib.auth.login``); CSRF protection stays on, so a
client first fetches a token from ``/api/csrf``. Operator actions require
``is_staff``; everything else acts only on the caller's own data.
"""

import json
import logging
from datetime import timezone
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from oscar.core.loading import get_model

from . import orders, provider, services
from .models import ContactNumber, Notification, mask_number

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")

MAX_ORDER_LINES = 50
MAX_QUANTITY = 100


# Plumbing ------------------------------------------------------------------


def error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def json_404(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except NotFound:
            return error(404, "Not found.")
    return wrapped


def shopper(view):
    """Require a signed-in caller; answer 401 in JSON rather than redirecting."""
    @wraps(view)
    @json_404
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error(401, "Sign in first.")
        return view(request, *args, **kwargs)
    return wrapped


def operator(view):
    """Require a signed-in staff user."""
    @wraps(view)
    @json_404
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error(401, "Sign in first.")
        if not request.user.is_staff:
            return error(403, "This action is for shop operators only.")
        return view(request, *args, **kwargs)
    return wrapped


def provider_errors(view):
    """
    The API boundary for failures this app could not absorb. A provider
    failure is never passed through verbatim: the caller gets our message,
    our status, and whether the provider may nevertheless have acted.
    """
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except provider.ProviderError as exc:
            return error(exc.status_code, exc.message, outcomeUnknown=exc.outcome_unknown)
        except services.NotificationConflict as exc:
            return error(409, str(exc))
        except services.OrderStateConflict as exc:
            return error(409, str(exc))
    return wrapped


class NotFound(Exception):
    pass


def find(queryset_or_model, **lookup):
    """get_object_or_404 for a JSON API: a missing (or not-yours) object is a JSON 404."""
    manager = getattr(queryset_or_model, "_default_manager", queryset_or_model)
    obj = manager.filter(**lookup).first()
    if obj is None:
        raise NotFound()
    return obj


def read_json(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def iso(value):
    return value.isoformat() if value else None


def notification_json(notification, *, include_body=True):
    data = {
        "notificationId": notification.pk,
        "orderId": notification.order_id,
        "kind": notification.kind,
        "resendOf": notification.resend_of_id,
        "destination": mask_number(notification.destination),
        "outcome": notification.outcome,
        "providerStatus": notification.provider_status or None,
        "providerMessageSid": notification.provider_sid,
        "providerErrorCode": notification.provider_error_code,
        "failureReason": notification.failure_reason or None,
        "scheduledFor": iso(notification.scheduled_for),
        "claimedAt": iso(notification.claimed_at),
        "providerTime": iso(notification.provider_time),
        "lastCheckedAt": iso(notification.last_checked_at),
        "contentDisposed": notification.content_disposed_at is not None,
        "contentDisposedAt": iso(notification.content_disposed_at),
    }
    if notification.kind == Notification.KIND_FOLLOWUP:
        data["cancelState"] = notification.cancel_state or None
    if include_body:
        data["body"] = None if notification.content_disposed_at else notification.body
    return data


def order_json(order, notifications=None):
    data = {
        "orderId": order.pk,
        "number": str(order.number),
        "status": order.status,
        "currency": order.currency,
        "totalInclTax": str(order.total_incl_tax),
        "placedAt": iso(order.date_placed),
        "lines": [
            {"productId": line.product_id, "title": line.title, "quantity": line.quantity}
            for line in order.lines.all()
        ],
    }
    if notifications is not None:
        data["notifications"] = [notification_json(n) for n in notifications]
    return data


def contact_json(contact):
    return {
        "contactNumberId": contact.pk,
        "number": contact.e164,
        "countryCode": contact.country_code or None,
        "createdAt": iso(contact.created_at),
    }


# Session -------------------------------------------------------------------


@require_GET
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({"csrfToken": get_token(request)})


@require_POST
def login_view(request):
    payload = read_json(request)
    if payload is None:
        return error(400, "Send a JSON object.")
    username = payload.get("username") or payload.get("email")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return error(400, "username (or email) and password are required.")
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return error(401, "Invalid credentials.")
    login(request, user)
    return JsonResponse({"userId": user.pk, "isStaff": user.is_staff,
                         "csrfToken": get_token(request)})


@require_POST
def logout_view(request):
    logout(request)
    return JsonResponse({"signedOut": True})


# Flow 1 - contact numbers --------------------------------------------------


@require_http_methods(["GET", "POST"])
@shopper
@provider_errors
def contact_numbers(request):
    if request.method == "GET":
        numbers = services.active_numbers(request.user).order_by("-created_at", "-id")
        return JsonResponse({"contactNumbers": [contact_json(c) for c in numbers]})

    payload = read_json(request)
    if payload is None:
        return error(400, "Send a JSON object.")
    raw = payload.get("number")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 40:
        return error(400, "number is required.")
    try:
        contact, created = services.register_number(request.user, raw.strip())
    except provider.NumberRejected:
        return error(422, "That is not a phone number we can send messages to.")
    return JsonResponse(contact_json(contact), status=201 if created else 200)


@require_http_methods(["DELETE"])
@shopper
def contact_number_detail(request, contact_number_id):
    contact = find(
        ContactNumber, pk=contact_number_id, user=request.user, deleted_at__isnull=True)
    services.remove_number(contact)
    return JsonResponse({"contactNumberId": contact.pk, "deleted": True})


# Flow 2 - orders and their messages ----------------------------------------


def _parse_items(payload):
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines or len(lines) > MAX_ORDER_LINES:
        return None
    items = []
    for line in lines:
        if not isinstance(line, dict):
            return None
        product_id = line.get("productId")
        quantity = line.get("quantity", 1)
        if (not isinstance(product_id, int) or isinstance(product_id, bool)
                or not isinstance(quantity, int) or isinstance(quantity, bool)
                or not 1 <= quantity <= MAX_QUANTITY):
            return None
        items.append((product_id, quantity))
    return items


@require_POST
@shopper
def place_order(request):
    payload = read_json(request)
    items = _parse_items(payload) if payload is not None else None
    if items is None:
        return error(400, 'Send {"lines": [{"productId": <int>, "quantity": <1-%d>}, ...]}.'
                     % MAX_QUANTITY)
    try:
        order = orders.place_order(request.user, items)
    except orders.OrderRequestInvalid as exc:
        return error(400, str(exc))
    notification = services.notify_order_event(order, Notification.KIND_PLACED)
    body = order_json(order, [notification] if notification else [])
    return JsonResponse(body, status=201)


@require_GET
@shopper
def my_orders(request):
    result = []
    for order in Order.objects.filter(user=request.user).order_by("-date_placed", "-id"):
        notifications = services.refresh_all(order.sms_notifications.all())
        result.append(order_json(order, notifications))
    return JsonResponse({"orders": result})


@require_GET
@shopper
def order_notifications(request, order_id):
    order = find(Order, pk=order_id, user=request.user)
    notifications = services.refresh_all(order.sms_notifications.all())
    return JsonResponse({"orderId": order.pk,
                         "notifications": [notification_json(n) for n in notifications]})


@require_POST
@operator
@provider_errors
def dispatch_order(request, order_id):
    order = find(Order, pk=order_id)
    order = services.dispatch_order(order)
    return JsonResponse(order_json(order, order.sms_notifications.all()))


@require_POST
@operator
@provider_errors
def cancel_order(request, order_id):
    order = find(Order, pk=order_id)
    order = services.cancel_order(order)
    return JsonResponse(order_json(order, order.sms_notifications.all()))


# Flow 3 - operator tools ---------------------------------------------------


@require_POST
@operator
@provider_errors
def resend_notification(request, notification_id):
    original = find(Notification, pk=notification_id)
    payload = read_json(request) or {}
    key = request.headers.get("Idempotency-Key") or payload.get("idempotencyKey")
    if not isinstance(key, str) or not key.strip() or len(key) > 255:
        return error(400, "An idempotency key is required (Idempotency-Key header or "
                          "idempotencyKey field, up to 255 characters).")
    notification, sent_now = services.resend(original, key.strip())
    body = notification_json(notification)
    return JsonResponse(body, status=201 if sent_now else 200)


@require_http_methods(["DELETE"])
@operator
@provider_errors
def notification_content(request, notification_id):
    notification = find(Notification, pk=notification_id)
    notification = services.dispose_content(notification)
    return JsonResponse(notification_json(notification))


def _parse_instant(value):
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@require_GET
@operator
@provider_errors
def reconciliation(request):
    start = _parse_instant(request.GET.get("from"))
    end = _parse_instant(request.GET.get("to"))
    if start is None or end is None:
        return error(400, "from and to are required ISO-8601 date-times.")
    if not start < end:
        return error(400, "from must be earlier than to.")

    report = services.reconcile(start, end)
    return JsonResponse({
        "from": start.isoformat(),
        "to": end.isoformat(),
        "sender": mask_number(provider.from_number()),
        "summary": {
            "matched": len(report["matched"]),
            "statusMismatches": sum(1 for m in report["matched"] if not m["statusAgreed"]),
            "appOnly": len(report["appOnly"]),
            "providerOnly": len(report["providerOnly"]),
            "unsettled": len(report["unsettled"]),
            "inboundIgnored": report["inboundIgnored"],
        },
        "matched": [
            {**notification_json(m["notification"], include_body=False),
             "statusAgreed": m["statusAgreed"]}
            for m in report["matched"]
        ],
        "appOnly": [
            {**notification_json(a["notification"], include_body=False),
             "reason": a["reason"]}
            for a in report["appOnly"]
        ],
        "providerOnly": [
            {
                "providerMessageSid": s.sid,
                "providerStatus": s.status,
                "to": mask_number(s.to),
                "providerTime": iso(s.provider_time),
                "errorCode": s.error_code,
            }
            for s in report["providerOnly"]
        ],
        "unsettled": [notification_json(n, include_body=False) for n in report["unsettled"]],
    })
