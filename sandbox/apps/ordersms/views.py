"""HTTP endpoints for the SMS order-notifications API (all under /api/).

Authentication is Django's own session login; the caller's identity is taken
from ``request.user``. Shopper endpoints act only on the caller's own data;
operator endpoints require ``is_staff``. Each action is a separate route.

These JSON endpoints are CSRF-exempt: they are a session-authenticated API
driven by a client, not HTML forms.
"""

from __future__ import annotations

import datetime as dt
import functools
import json

from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from oscar.core.loading import get_model

from . import serializers, services

Order = get_model("order", "Order")
OrderNotification = get_model("ordersms", "OrderNotification")


def _err(message, status):
    return JsonResponse({"error": message}, status=status)


def _require(methods):
    """Restrict a view to the given HTTP methods and require authentication."""

    def decorator(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return _err("method not allowed", 405)
            if not request.user.is_authenticated:
                return _err("authentication required", 401)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def _staff_only(request):
    if not request.user.is_staff:
        return _err("operator (staff) access required", 403)
    return None


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise services.ServiceError("request body must be valid JSON")
    if not isinstance(data, dict):
        raise services.ServiceError("request body must be a JSON object")
    return data


# --------------------------------------------------------------------------- #
# Flow 1 — contact numbers
# --------------------------------------------------------------------------- #
@_require({"GET", "POST"})
def contact_numbers(request):
    if request.method == "GET":
        numbers = services.list_contact_numbers(request.user)
        return JsonResponse({"contactNumbers": [serializers.contact_number(n) for n in numbers]})
    try:
        data = _json_body(request)
        obj = services.register_contact_number(request.user, data.get("number", ""))
    except services.ServiceError as exc:
        return _err(str(exc), exc.status_code)
    return JsonResponse(serializers.contact_number(obj), status=201)


@_require({"DELETE"})
def contact_number_detail(request, pk):
    try:
        services.delete_contact_number(request.user, pk)
    except services.ServiceError as exc:
        return _err(str(exc), exc.status_code)
    return JsonResponse({"deleted": True})


# --------------------------------------------------------------------------- #
# Flow 2 — orders
# --------------------------------------------------------------------------- #
@_require({"POST"})
def orders(request):
    try:
        data = _json_body(request)
        items = data.get("items")
        if not isinstance(items, list):
            raise services.ServiceError("'items' must be a list of {product_id, quantity}")
        order = services.place_order(request.user, items)
    except services.ServiceError as exc:
        return _err(str(exc), exc.status_code)
    notifications = list(order.sms_notifications.all())
    body = serializers.order_summary(order, notifications)
    body["orderId"] = order.pk  # top-level identifier
    return JsonResponse(body, status=201)


def _get_owned_order(request, pk, *, staff_ok=True):
    try:
        order = Order.objects.get(pk=pk)
    except Order.DoesNotExist:
        return None, _err("no such order", 404)
    is_owner = order.user_id == request.user.id
    if not is_owner and not (staff_ok and request.user.is_staff):
        return None, _err("no such order", 404)
    return order, None


@_require({"POST"})
def order_dispatch(request, pk):
    denied = _staff_only(request)
    if denied:
        return denied
    try:
        order = Order.objects.get(pk=pk)
    except Order.DoesNotExist:
        return _err("no such order", 404)
    try:
        order, changed = services.dispatch_order(order)
    except services.ServiceError as exc:
        return _err(str(exc), exc.status_code)
    notifications = list(order.sms_notifications.all())
    return JsonResponse(
        {"orderId": order.pk, "status": order.status, "changed": changed,
         "notifications": [serializers.notification(n) for n in notifications]}
    )


@_require({"POST"})
def order_cancel(request, pk):
    denied = _staff_only(request)
    if denied:
        return denied
    try:
        order = Order.objects.get(pk=pk)
    except Order.DoesNotExist:
        return _err("no such order", 404)
    try:
        order, changed = services.cancel_order(order)
    except services.ServiceError as exc:
        return _err(str(exc), exc.status_code)
    notifications = list(order.sms_notifications.all())
    return JsonResponse(
        {"orderId": order.pk, "status": order.status, "changed": changed,
         "notifications": [serializers.notification(n) for n in notifications]}
    )


@_require({"GET"})
def my_orders(request):
    orders_qs = Order.objects.filter(user=request.user).prefetch_related("lines", "sms_notifications")
    result = []
    for order in orders_qs:
        notifications = [services.refresh_status(n) for n in order.sms_notifications.all()]
        result.append(serializers.order_summary(order, notifications))
    return JsonResponse({"orders": result})


@_require({"GET"})
def order_notifications(request, pk):
    order, denied = _get_owned_order(request, pk)
    if denied:
        return denied
    notifications = [services.refresh_status(n) for n in order.sms_notifications.all()]
    return JsonResponse(
        {"orderId": order.pk, "notifications": [serializers.notification(n) for n in notifications]}
    )


# --------------------------------------------------------------------------- #
# Flow 3 — operator actions on notifications
# --------------------------------------------------------------------------- #
def _get_notification(pk):
    try:
        return OrderNotification.objects.get(pk=pk), None
    except OrderNotification.DoesNotExist:
        return None, _err("no such notification", 404)


@_require({"POST"})
def notification_resend(request, pk):
    denied = _staff_only(request)
    if denied:
        return denied
    notif, missing = _get_notification(pk)
    if missing:
        return missing
    try:
        data = _json_body(request)
        key = data.get("idempotencyKey") or data.get("idempotency_key") or ""
        result, created = services.resend_notification(notif, key)
    except services.ServiceError as exc:
        return _err(str(exc), exc.status_code)
    payload = serializers.notification(result)
    payload["notificationId"] = result.pk  # top-level identifier of the produced message
    payload["created"] = payload["created"]
    payload["resent"] = created
    return JsonResponse(payload, status=201 if created else 200)


@_require({"DELETE"})
def notification_content(request, pk):
    denied = _staff_only(request)
    if denied:
        return denied
    notif, missing = _get_notification(pk)
    if missing:
        return missing
    try:
        notif = services.dispose_content(notif)
    except services.ServiceError as exc:
        return _err(str(exc), exc.status_code)
    return JsonResponse(serializers.notification(notif))


@_require({"GET"})
def notification_reconciliation(request):
    denied = _staff_only(request)
    if denied:
        return denied
    raw_from = request.GET.get("from")
    raw_to = request.GET.get("to")
    if not raw_from or not raw_to:
        return _err("'from' and 'to' ISO-8601 date-times are required", 400)
    date_from = parse_datetime(raw_from)
    date_to = parse_datetime(raw_to)
    if date_from is None or date_to is None:
        return _err("'from' and 'to' must be ISO-8601 date-times", 400)
    date_from = _aware(date_from)
    date_to = _aware(date_to)
    if date_from >= date_to:
        return _err("'from' must be before 'to'", 400)
    try:
        report = services.reconcile(date_from, date_to)
    except services.ServiceError as exc:
        return _err(str(exc), exc.status_code)
    except Exception:  # provider unavailable etc.
        return _err("could not build the reconciliation report", 502)
    return JsonResponse(report)


def _aware(value: dt.datetime) -> dt.datetime:
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value
