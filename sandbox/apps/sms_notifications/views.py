"""
JSON API for SMS order notifications. Callers authenticate with Django's own
session login (``/api/session``); CSRF protection stays on for every unsafe
method, so send the ``csrftoken`` cookie's value as ``X-CSRFToken``.
"""
import json
import logging
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.http import HttpResponse, JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import services
from .models import ContactNumber, Notification
from .services import ServiceError

logger = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")

MAX_ORDERS_PER_PAGE = 100


def error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def api_view(*methods, staff=False):
    """Method routing + JSON errors + authentication (and ``is_staff`` for operator actions)."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return error(401, "Authentication required.")
            if staff and not request.user.is_staff:
                return error(403, "This action is restricted to staff.")
            try:
                return view(request, *args, **kwargs)
            except ServiceError as e:
                return error(e.status_code, e.message, **e.extra)
        return require_http_methods(list(methods))(wrapper)
    return decorator


def read_json(request):
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ServiceError(400, "Request body must be JSON.")
    if not isinstance(payload, dict):
        raise ServiceError(400, "Request body must be a JSON object.")
    return payload


def _iso(value):
    return value.isoformat() if value else None


def contact_json(contact):
    return {
        "contactNumberId": contact.pk,
        "phoneNumber": contact.phone_number,
        "countryCode": contact.country_code or None,
        "nationalFormat": contact.national_format or None,
        "createdAt": _iso(contact.created_at),
    }


def notification_json(n):
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "state": n.state,
        "deliveryOutcome": n.delivery_outcome,
        "providerSid": n.provider_sid,
        "providerStatus": n.provider_status or None,
        "providerErrorCode": n.provider_error_code,
        "failureReason": n.failure_reason or None,
        "to": services.mask_number(n.to_number) or None,
        "body": None if n.content_disposed_at else n.body,
        "contentDisposed": n.content_disposed_at is not None,
        "contentDisposedAt": _iso(n.content_disposed_at),
        "disposalState": n.disposal_state or None,
        "scheduledFor": _iso(n.scheduled_for),
        "cancelState": n.cancel_state or None,
        "dateSent": _iso(n.provider_date_sent),
        "providerCheckedAt": _iso(n.provider_checked_at),
        "resendOf": n.resend_of_id,
        "createdAt": _iso(n.created_at),
    }


def order_json(order, notifications=None):
    data = {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "currency": order.currency,
        "totalInclTax": str(order.total_incl_tax),
        "datePlaced": _iso(order.date_placed),
        "lines": [{"productId": line.product_id, "title": line.title, "quantity": line.quantity}
                  for line in order.lines.all()],
    }
    if notifications is not None:
        data["notifications"] = [notification_json(n) for n in notifications]
    return data


def _order_notifications(order):
    services.settle_order(order)
    return list(Notification.objects.filter(order=order))


def _visible_order(request, order_id):
    """Staff see every order; a shopper only their own (others look like 404)."""
    qs = Order.objects.all() if request.user.is_staff else Order.objects.filter(user=request.user)
    try:
        return qs.get(pk=order_id)
    except Order.DoesNotExist:
        raise ServiceError(404, "Order not found.")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def _session_json(request):
    user = request.user
    if not user.is_authenticated:
        return {"authenticated": False}
    return {"authenticated": True, "userId": user.pk, "username": user.get_username(), "isStaff": user.is_staff}


@ensure_csrf_cookie
@require_http_methods(["GET", "POST", "DELETE"])
def session(request):
    if request.method == "GET":
        return JsonResponse(_session_json(request))
    if request.method == "DELETE":
        logout(request)
        return JsonResponse({"authenticated": False})
    try:
        payload = read_json(request)
    except ServiceError as e:
        return error(e.status_code, e.message)
    username = payload.get("username") or payload.get("email")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return error(400, "username (or email) and password are required.")
    user = authenticate(request, username=username, password=password)
    if user is None:
        return error(401, "Invalid credentials.")
    login(request, user)
    return JsonResponse(_session_json(request))


# ---------------------------------------------------------------------------
# Flow 1 - contact numbers
# ---------------------------------------------------------------------------

@api_view("GET", "POST")
def contact_numbers(request):
    if request.method == "GET":
        numbers = ContactNumber.objects.filter(user=request.user)
        return JsonResponse({"contactNumbers": [contact_json(c) for c in numbers]})
    payload = read_json(request)
    contact = services.register_contact_number(
        request.user, payload.get("phoneNumber"), payload.get("countryCode"))
    return JsonResponse(contact_json(contact), status=201)


@api_view("DELETE")
def contact_number_detail(request, contact_number_id):
    services.delete_contact_number(request.user, contact_number_id)
    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Flow 2 - orders
# ---------------------------------------------------------------------------

@api_view("POST")
def orders(request):
    payload = read_json(request)
    order = services.place_order(request.user, payload.get("lines"))
    return JsonResponse(order_json(order, Notification.objects.filter(order=order)), status=201)


@api_view("GET")
def my_orders(request):
    try:
        limit = min(max(int(request.GET.get("limit", 20)), 1), MAX_ORDERS_PER_PAGE)
    except ValueError:
        raise ServiceError(400, "limit must be an integer.")
    orders_qs = Order.objects.filter(user=request.user).order_by("-date_placed", "-pk")[:limit]
    return JsonResponse({"orders": [order_json(o, _order_notifications(o)) for o in orders_qs]})


@api_view("POST", staff=True)
def dispatch_order(request, order_id):
    order = services.dispatch_order(order_id, request.user)
    return JsonResponse(order_json(order, Notification.objects.filter(order=order)))


@api_view("POST", staff=True)
def cancel_order(request, order_id):
    order, already = services.cancel_order(order_id, request.user)
    data = order_json(order, Notification.objects.filter(order=order))
    data["alreadyCancelled"] = already
    return JsonResponse(data)


@api_view("GET")
def order_notifications(request, order_id):
    order = _visible_order(request, order_id)
    return JsonResponse({"orderId": order.pk, "orderStatus": order.status,
                         "notifications": [notification_json(n) for n in _order_notifications(order)]})


# ---------------------------------------------------------------------------
# Flow 3 - operator actions
# ---------------------------------------------------------------------------

@api_view("POST", staff=True)
def resend_notification(request, notification_id):
    payload = read_json(request)
    key = payload.get("idempotencyKey") or request.headers.get("Idempotency-Key")
    resend, replayed = services.resend_notification(notification_id, key, request.user)
    data = notification_json(resend)
    data["replayed"] = replayed
    return JsonResponse(data, status=200 if replayed else 201)


@api_view("DELETE", staff=True)
def notification_content(request, notification_id):
    notification = services.dispose_content(notification_id, request.user)
    return JsonResponse(notification_json(notification))


def _parse_instant(value, name):
    parsed = parse_datetime(value or "")
    if parsed is None:
        raise ServiceError(400, "'%s' must be an ISO-8601 date-time." % name)
    if parsed.tzinfo is None:
        raise ServiceError(400, "'%s' must include a UTC offset (e.g. Z or +00:00)." % name)
    return parsed


@api_view("GET", staff=True)
def reconciliation(request):
    start = _parse_instant(request.GET.get("from"), "from")
    end = _parse_instant(request.GET.get("to"), "to")
    return JsonResponse(services.reconcile(start, end))
