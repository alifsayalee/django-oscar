"""HTTP API for SMS order notifications.

Plain Django JSON views (the sandbox ships no DRF). Callers are authenticated the
way the sandbox already authenticates them -- Django session login -- and every
action takes the caller's identity from ``request.user``. Operator actions
(dispatch, cancel, resend, reconciliation) require ``is_staff``. Every other
endpoint is scoped to the caller's own data.
"""

import functools
import json

from django.db import IntegrityError
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from oscar.apps.order.exceptions import InvalidOrderStatus
from oscar.core.loading import get_model

from . import exceptions, gateway, services
from .models import ContactNumber, Notification
from .serializers import serialize_contact_number, serialize_notification, serialize_order

Order = get_model("order", "Order")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _json(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params={"indent": 2})


def _error(message, status):
    return _json({"error": message}, status=status)


def _body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Request body is not valid JSON.")
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    return data


def api(methods, *, staff=False):
    """Decorator: CSRF-exempt JSON endpoint with method/auth/role gating."""

    def decorator(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return _error("Method not allowed.", 405)
            if not request.user.is_authenticated:
                return _error("Authentication required.", 401)
            if staff and not request.user.is_staff:
                return _error("Operator privileges required.", 403)
            try:
                return view(request, *args, **kwargs)
            except ValueError as e:
                return _error(str(e), 400)
            except exceptions.ProviderRejected as e:
                # Caller-actionable rejection (e.g. not a usable destination).
                code = 400 if e.status_code in (400, 404, 409, 422) else e.status_code
                return _error(e.message, code)
            except exceptions.ProviderError as e:
                return _error(e.message, e.status_code)

        return wrapper

    return decorator


def _get_owned_order(request, order_id):
    """Fetch an order the caller may act on: their own, or any if staff."""
    qs = Order.objects.all() if request.user.is_staff else Order.objects.filter(user=request.user)
    try:
        return qs.get(pk=order_id)
    except (Order.DoesNotExist, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Flow 1 -- contact numbers
# --------------------------------------------------------------------------- #


@api(["GET", "POST"])
def contact_numbers(request):
    if request.method == "GET":
        numbers = ContactNumber.objects.filter(owner=request.user)
        return _json({"contactNumbers": [serialize_contact_number(n) for n in numbers]})

    data = _body(request)
    raw_number = (data.get("number") or data.get("phoneNumber") or "").strip()
    if not raw_number:
        raise ValueError("A 'number' is required.")
    country_code = (data.get("countryCode") or "").strip() or None

    # Reject an unusable destination HERE, and store the provider's canonical form.
    canonical = gateway.validate_number(raw_number, country_code=country_code)

    cn, created = ContactNumber.objects.get_or_create(
        owner=request.user, phone_number=canonical
    )
    return _json(serialize_contact_number(cn), status=201 if created else 200)


@api(["DELETE"])
def contact_number_detail(request, contact_number_id):
    try:
        cn = ContactNumber.objects.get(pk=contact_number_id, owner=request.user)
    except (ContactNumber.DoesNotExist, ValueError):
        return _error("Contact number not found.", 404)
    services.delete_contact_number(cn)
    return _json({"deleted": True})


# --------------------------------------------------------------------------- #
# Flow 2 -- orders and their notifications
# --------------------------------------------------------------------------- #


@api(["POST"])
def create_order(request):
    data = _body(request)
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("An 'items' array is required.")
    normalized = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("Each item must be an object with productId and quantity.")
        normalized.append(
            {
                "product_id": it.get("productId") or it.get("product_id"),
                "quantity": it.get("quantity", 1),
            }
        )
    try:
        order = services.place_order(request.user, normalized)
    except services.OrderPlacementError as e:
        raise ValueError(str(e))
    notifs = list(order.sms_notifications.all())
    return _json(serialize_order(order, notifs), status=201)


@api(["GET"])
def my_orders(request):
    orders = Order.objects.filter(user=request.user).prefetch_related(
        "sms_notifications", "lines"
    )
    out = []
    for order in orders:
        notifs = list(order.sms_notifications.all())
        for n in notifs:
            services.refresh_notification(n)
        out.append(serialize_order(order, notifs))
    return _json({"orders": out})


@api(["GET"])
def order_notifications(request, order_id):
    order = _get_owned_order(request, order_id)
    if order is None:
        return _error("Order not found.", 404)
    notifs = list(order.sms_notifications.all())
    for n in notifs:
        services.refresh_notification(n)
    return _json(
        {
            "orderId": order.pk,
            "notifications": [serialize_notification(n) for n in notifs],
        }
    )


# --------------------------------------------------------------------------- #
# Flow 2 -- operator order transitions
# --------------------------------------------------------------------------- #


@api(["POST"], staff=True)
def dispatch_order(request, order_id):
    order = _get_owned_order(request, order_id)
    if order is None:
        return _error("Order not found.", 404)
    try:
        services.dispatch_order(order)
    except InvalidOrderStatus as e:
        return _error(str(e), 409)
    notifs = list(order.sms_notifications.all())
    return _json(serialize_order(order, notifs))


@api(["POST"], staff=True)
def cancel_order(request, order_id):
    order = _get_owned_order(request, order_id)
    if order is None:
        return _error("Order not found.", 404)
    try:
        services.cancel_order(order)
    except InvalidOrderStatus as e:
        return _error(str(e), 409)
    notifs = list(order.sms_notifications.all())
    return _json(serialize_order(order, notifs))


# --------------------------------------------------------------------------- #
# Flow 3 -- operator notification actions + shopper content disposal
# --------------------------------------------------------------------------- #


def _get_notification_any(notification_id):
    try:
        return Notification.objects.select_related("order").get(pk=notification_id)
    except (Notification.DoesNotExist, ValueError):
        return None


@api(["POST"], staff=True)
def resend_notification(request, notification_id):
    notif = _get_notification_any(notification_id)
    if notif is None:
        return _error("Notification not found.", 404)
    data = _body(request)
    key = (data.get("idempotencyKey") or data.get("idempotency_key") or "").strip()
    if not key:
        raise ValueError("An 'idempotencyKey' is required.")
    try:
        resend, created_now = services.resend_notification(notif, key)
    except services.RecipientNotRegistered as e:
        return _error(str(e), 409)
    return _json(
        {
            "notificationId": resend.pk,
            "created": created_now,
            "notification": serialize_notification(resend),
        },
        status=201 if created_now else 200,
    )


@api(["DELETE"])
def notification_content(request, notification_id):
    notif = _get_notification_any(notification_id)
    if notif is None:
        return _error("Notification not found.", 404)
    # A shopper may dispose of content of a message about them; operators too.
    if not request.user.is_staff and notif.order.user_id != request.user.id:
        return _error("Notification not found.", 404)
    services.dispose_content(notif)
    return _json(serialize_notification(notif))


@api(["GET"], staff=True)
def reconciliation(request):
    from_raw = request.GET.get("from")
    to_raw = request.GET.get("to")
    if not from_raw or not to_raw:
        raise ValueError("Both 'from' and 'to' ISO-8601 date-times are required.")
    date_from = parse_datetime(from_raw)
    date_to = parse_datetime(to_raw)
    if date_from is None or date_to is None:
        raise ValueError("'from' and 'to' must be ISO-8601 date-times.")
    if timezone.is_naive(date_from):
        date_from = timezone.make_aware(date_from)
    if timezone.is_naive(date_to):
        date_to = timezone.make_aware(date_to)
    if date_from > date_to:
        raise ValueError("'from' must not be after 'to'.")
    from_number = gateway._required("TWILIO_FROM_NUMBER")
    report = services.reconcile(date_from, date_to, from_number)
    return _json(report)
