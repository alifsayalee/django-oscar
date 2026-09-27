"""JSON API for SMS order notifications. Session-authenticated (Django login), CSRF-protected."""

import json
import re
from datetime import datetime, timezone as dt_timezone
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import provider, services
from .models import ContactNumber, Notification

Order = get_model("order", "Order")


def error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def api_login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error(401, "Sign in first (POST /api/session).")
        return view(request, *args, **kwargs)

    return wrapper


def staff_required(view):
    @wraps(view)
    @api_login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            return error(403, "This action is restricted to staff.")
        return view(request, *args, **kwargs)

    return wrapper


def handles_service_errors(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except services.ServiceError as e:
            return error(e.status_code, str(e), **e.extra)

    return wrapper


def json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as e:
        raise services.ServiceError(400, "The request body must be JSON.") from e
    if not isinstance(data, dict):
        raise services.ServiceError(400, "The request body must be a JSON object.")
    return data


# ---------------------------------------------------------------------------
# Session (Django's own login, exposed for API callers)
# ---------------------------------------------------------------------------


@ensure_csrf_cookie
@require_http_methods(["GET", "POST", "DELETE"])
@handles_service_errors
def session(request):
    if request.method == "POST":
        data = json_body(request)
        user = authenticate(request, username=data.get("email") or data.get("username"), password=data.get("password"))
        if user is None:
            return error(401, "Invalid credentials.")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
    user = request.user
    return JsonResponse(
        {
            "authenticated": user.is_authenticated,
            "userId": user.pk if user.is_authenticated else None,
            "email": user.email if user.is_authenticated else None,
            "isStaff": bool(user.is_authenticated and user.is_staff),
            "csrfToken": get_token(request),
        }
    )


# ---------------------------------------------------------------------------
# Flow 1: contact numbers
# ---------------------------------------------------------------------------


def contact_json(number):
    return {
        "contactNumberId": number.pk,
        "phoneNumber": number.phone_number,
        "countryCode": number.country_code or None,
        "createdAt": number.created_at.isoformat(),
    }


@require_http_methods(["GET", "POST"])
@api_login_required
@handles_service_errors
def contact_numbers(request):
    if request.method == "POST":
        number, created = services.register_contact_number(request.user, json_body(request).get("phoneNumber"))
        return JsonResponse(contact_json(number), status=201 if created else 200)
    numbers = ContactNumber.objects.filter(user=request.user)
    return JsonResponse({"contactNumbers": [contact_json(n) for n in numbers]})


@require_http_methods(["DELETE"])
@api_login_required
def contact_number_detail(request, contact_number_id):
    deleted, _ = ContactNumber.objects.filter(pk=contact_number_id, user=request.user).delete()
    if not deleted:
        return error(404, "No such contact number.")
    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Flow 2: orders
# ---------------------------------------------------------------------------


def _notifications_for(order):
    return list(order.sms_notifications.select_related("contact_number").prefetch_related("actions"))


@require_http_methods(["POST"])
@api_login_required
@handles_service_errors
def orders(request):
    data = json_body(request)
    order, _ = services.place_order(request.user, data.get("lines"), data.get("shippingAddress"), request=request)
    return JsonResponse(services.order_json(order, _notifications_for(order)), status=201)


@require_http_methods(["POST"])
@staff_required
@handles_service_errors
def dispatch_order(request, order_id):
    try:
        order, _ = services.dispatch_order(order_id, request.user)
    except Order.DoesNotExist:
        return error(404, "No such order.")
    order.refresh_from_db()
    return JsonResponse(services.order_json(order, _notifications_for(order)))


@require_http_methods(["POST"])
@staff_required
@handles_service_errors
def cancel_order(request, order_id):
    try:
        order, _, follow_up = services.cancel_order(order_id, request.user)
    except Order.DoesNotExist:
        return error(404, "No such order.")
    order.refresh_from_db()
    return JsonResponse({**services.order_json(order, _notifications_for(order)), "followUpCancellation": follow_up})


@require_http_methods(["GET"])
@api_login_required
def my_orders(request):
    placed = list(Order.objects.filter(user=request.user).order_by("-date_placed")[:50])
    failed = services.refresh_many(list(Notification.objects.filter(order__in=placed).select_related("contact_number")))
    by_order: dict[int, list] = {}
    for n in Notification.objects.filter(order__in=placed).select_related("contact_number").prefetch_related("actions"):
        by_order.setdefault(n.order_id, []).append(n)
    return JsonResponse(
        {"orders": [services.order_json(o, by_order.get(o.pk, []), refresh_failed=failed) for o in placed]}
    )


@require_http_methods(["GET"])
@api_login_required
def order_notifications(request, order_id):
    orders_visible = Order.objects.all() if request.user.is_staff else Order.objects.filter(user=request.user)
    order = orders_visible.filter(pk=order_id).first()
    if order is None:
        return error(404, "No such order.")
    failed = services.refresh_many(_notifications_for(order))
    return JsonResponse(
        {
            "orderId": order.pk,
            "notifications": [
                services.notification_json(n, refresh_failed=n.pk in failed) for n in _notifications_for(order)
            ],
        }
    )


# ---------------------------------------------------------------------------
# Flow 3: operator actions
# ---------------------------------------------------------------------------


@require_http_methods(["POST"])
@staff_required
@handles_service_errors
def resend(request, notification_id):
    key = request.headers.get("Idempotency-Key") or json_body(request).get("idempotencyKey")
    notification, outcome = services.resend(notification_id, key)
    return JsonResponse(
        {"notificationId": notification.pk, "outcome": outcome, "notification": services.notification_json(notification)},
        status=provider.answer_status(outcome),
    )


@require_http_methods(["DELETE"])
@staff_required
@handles_service_errors
def notification_content(request, notification_id):
    notification, outcome = services.dispose_content(notification_id)
    return JsonResponse(
        {"notificationId": notification.pk, "outcome": outcome, "notification": services.notification_json(notification)},
        status=provider.answer_status(outcome),
    )


def _parse_instant(value, name):
    if not value:
        raise services.ServiceError(400, f"'{name}' is required (ISO-8601 date-time).")
    # An unencoded "+01:00" offset arrives as " 01:00" in a query string.
    text = re.sub(r" (\d{2}:?\d{2})$", r"+\1", value.strip())
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise services.ServiceError(400, f"'{name}' is not an ISO-8601 date-time.") from e
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


@require_http_methods(["GET"])
@staff_required
@handles_service_errors
def reconciliation(request):
    start = _parse_instant(request.GET.get("from"), "from")
    end = _parse_instant(request.GET.get("to"), "to")
    if end <= start:
        return error(400, "'to' must be after 'from'.")
    if (end - start).days > 366:
        return error(400, "The range may span at most 366 days.")
    return JsonResponse(services.reconcile(start.astimezone(dt_timezone.utc), end.astimezone(dt_timezone.utc)))
