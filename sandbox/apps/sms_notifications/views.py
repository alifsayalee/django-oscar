"""
JSON API for order SMS notifications, mounted under ``/api/``.

Callers authenticate with Django's own session login (the storefront login
page, or ``POST /api/session``); CSRF protection stays on, so unsafe requests
carry the ``X-CSRFToken`` header. Operator actions require ``is_staff``.
"""
import datetime as dt
import functools
import json
import logging

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from oscar.core.loading import get_model

from . import gateway as gw
from . import services
from .models import ContactNumber, Notification

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")


class BadRequest(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def api(methods, *, staff=False, auth=True):
    """
    Method dispatch, authentication/authorisation and error translation.

    Views opt out of ATOMIC_REQUESTS: the services own their transactions, so
    a claim row commits before its provider call, and the messages an order
    change triggers (on commit) are sent before the response is built.
    """

    def decorator(view):
        @transaction.non_atomic_requests
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = _error(405, "Method not allowed.")
                response["Allow"] = ", ".join(methods)
                return response
            if auth and not request.user.is_authenticated:
                return _error(401, "Authentication required.")
            if staff and not request.user.is_staff:
                return _error(403, "Operator (staff) access required.")
            try:
                return view(request, *args, **kwargs)
            except BadRequest as e:
                return _error(e.status, e.message)
            except services.ServiceError as e:
                return _error(e.status_code, e.message, field=e.field)
            except gw.ProviderError as e:
                # The provider's failure, never passed through as the caller's.
                return _error(
                    e.status_code,
                    str(e),
                    outcomeUnknown=e.outcome_unknown,
                    providerStatus=e.provider_status,
                    providerErrorCode=e.twilio_code,
                )

        return wrapper

    return decorator


def _body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("The request body must be JSON.")
    if not isinstance(data, dict):
        raise BadRequest("The request body must be a JSON object.")
    return data


def _iso(value):
    return value.isoformat() if value else None


def _mask(number):
    return services._mask(number)


# --------------------------------------------------------------------------
# Serialisers
# --------------------------------------------------------------------------


def contact_json(contact):
    return {
        "contactNumberId": contact.pk,
        "phoneNumber": contact.phone_number,  # the caller's own number
        "countryCode": contact.country_code,
        "createdAt": _iso(contact.created_at),
    }


def notification_json(n):
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "status": n.status,
        "providerStatus": n.provider_status or None,
        "providerMessageSid": n.provider_sid,
        "errorCode": n.error_code,
        "failureReason": n.failure_reason or None,
        "contactNumberId": n.contact_number_id,
        "destination": _mask(n.contact_number.phone_number),
        "scheduledFor": _iso(n.scheduled_for),
        "sentAt": _iso(n.provider_date_sent),
        "createdAt": _iso(n.created_at),
        "lastCheckedAt": _iso(n.last_checked_at),
        "cancelState": n.cancel_state or None,
        "contentDisposed": n.content_disposed_at is not None,
        "contentDisposedAt": _iso(n.content_disposed_at),
        "resendOf": n.resend_of_id,
    }


def order_json(order, notifications=None):
    if notifications is None:
        notifications = order.sms_notifications.select_related("contact_number").all()
    notifications = list(notifications)
    latest = {}
    for n in notifications:  # ordered oldest first: the last one per kind wins
        latest[n.kind] = n.status
    return {
        "orderId": order.pk,
        "number": str(order.number),
        "status": order.status,
        "total": services.money(order.total_incl_tax),
        "currency": order.currency,
        "datePlaced": _iso(order.date_placed),
        "lines": [
            {"productId": line.product_id, "title": line.title, "quantity": line.quantity}
            for line in order.lines.all()
        ],
        "notificationSummary": latest,
        "notifications": [notification_json(n) for n in notifications],
    }


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@ensure_csrf_cookie
@api(["GET"], auth=False)
def csrf(request):
    return JsonResponse({"csrfToken": get_token(request)})


@api(["POST", "DELETE", "GET"], auth=False)
def session(request):
    if request.method == "GET":
        user = request.user
        if not user.is_authenticated:
            return _error(401, "Not signed in.")
        return JsonResponse({"userId": user.pk, "email": user.email, "isStaff": user.is_staff})
    if request.method == "DELETE":
        logout(request)
        return JsonResponse({}, status=200)
    data = _body(request)
    identifier = str(data.get("email") or data.get("username") or "")
    password = str(data.get("password") or "")
    user = authenticate(request, username=identifier, password=password)
    if user is None:
        user = authenticate(request, email=identifier, password=password)
    if user is None or not user.is_active:
        return _error(401, "Invalid credentials.")
    login(request, user)
    return JsonResponse({"userId": user.pk, "email": user.email, "isStaff": user.is_staff})


# --------------------------------------------------------------------------
# Flow 1 - contact numbers
# --------------------------------------------------------------------------


@api(["GET", "POST"])
def contact_numbers(request):
    if request.method == "GET":
        numbers = ContactNumber.objects.filter(user=request.user, deleted_at__isnull=True)
        return JsonResponse({"contactNumbers": [contact_json(c) for c in numbers]})
    data = _body(request)
    number = data.get("phoneNumber")
    if not isinstance(number, str):
        raise BadRequest("phoneNumber (string) is required.")
    country = data.get("countryCode")
    if country is not None and not isinstance(country, str):
        raise BadRequest("countryCode must be a string.")
    contact, created = services.register_contact_number(request.user, number, country)
    return JsonResponse(contact_json(contact), status=201 if created else 200)


@api(["DELETE"])
def contact_number_detail(request, contact_number_id):
    contact = ContactNumber.objects.filter(
        pk=contact_number_id, user=request.user, deleted_at__isnull=True
    ).first()
    if contact is None:
        return _error(404, "Contact number not found.")
    services.delete_contact_number(contact)
    return HttpResponse(status=204)


# --------------------------------------------------------------------------
# Flow 2 - orders
# --------------------------------------------------------------------------


def _int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest("%s must be an integer." % name)
    return value


@api(["POST"])
def orders(request):
    data = _body(request)
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise BadRequest("lines must be a non-empty list of {productId, quantity}.")
    lines = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise BadRequest("Each line must be an object.")
        quantity = _int(raw.get("quantity", 1), "quantity")
        if quantity < 1 or quantity > 100:
            raise BadRequest("quantity must be between 1 and 100.")
        lines.append(services.OrderLineRequest(product_id=_int(raw.get("productId"), "productId"),
                                               quantity=quantity))
    address = data.get("shippingAddress")
    if not isinstance(address, dict):
        raise BadRequest("shippingAddress is required.")
    order = services.place_order(request.user, lines, address)
    return JsonResponse(order_json(order), status=201)


def _order_for(request, order_id, *, staff_ok):
    qs = Order.objects.all()
    if not (staff_ok and request.user.is_staff):
        qs = qs.filter(user=request.user)
    order = qs.filter(pk=order_id).first()
    if order is None:
        raise BadRequest("Order not found.", status=404)
    return order


@api(["POST"], staff=True)
def order_dispatch(request, order_id):
    order = _order_for(request, order_id, staff_ok=True)
    changed = services.transition(order, services.STATUS_DISPATCHED)
    return JsonResponse({"changed": changed, **order_json(order)})


@api(["POST"], staff=True)
def order_cancel(request, order_id):
    order = _order_for(request, order_id, staff_ok=True)
    changed = services.transition(order, services.STATUS_CANCELLED)
    if not changed:
        # A repeat sends nothing, but still pushes any unconfirmed cancel.
        services.request_followup_cancellation(order=order)
    return JsonResponse({"changed": changed, **order_json(order)})


@api(["GET"])
def order_notifications(request, order_id):
    # The shopper sees their own; operators need these ids to act on them.
    order = _order_for(request, order_id, staff_ok=True)
    sync = services.sync_notifications(Notification.objects.filter(order=order))
    rows = order.sms_notifications.select_related("contact_number").all()
    return JsonResponse({
        "orderId": order.pk,
        "number": str(order.number),
        "orderStatus": order.status,
        "statusRefreshErrors": sync.errors,
        "notifications": [notification_json(n) for n in rows],
    })


@api(["GET"])
def my_orders(request):
    try:
        limit = min(max(int(request.GET.get("limit", 20)), 1), 100)
    except ValueError:
        raise BadRequest("limit must be an integer.")
    user_orders = list(
        Order.objects.filter(user=request.user).order_by("-date_placed", "-pk")[:limit]
    )
    sync = services.sync_notifications(Notification.objects.filter(order__in=user_orders))
    return JsonResponse({
        "statusRefreshErrors": sync.errors,
        "orders": [order_json(o) for o in user_orders],
    })


# --------------------------------------------------------------------------
# Flow 3 - operator actions
# --------------------------------------------------------------------------


def _notification(notification_id):
    n = Notification.objects.select_related("order", "contact_number").filter(pk=notification_id).first()
    if n is None:
        raise BadRequest("Notification not found.", status=404)
    return n


@api(["POST"], staff=True)
def notification_resend(request, notification_id):
    key = request.headers.get("Idempotency-Key") or _body(request).get("idempotencyKey")
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise BadRequest("An idempotency key (Idempotency-Key header or idempotencyKey) of 1-128 "
                         "characters is required.")
    original = _notification(notification_id)
    row, replayed = services.resend(original, key.strip())
    row = Notification.objects.select_related("contact_number").get(pk=row.pk)
    return JsonResponse({"replayed": replayed, **notification_json(row)}, status=200 if replayed else 201)


@api(["DELETE"], staff=True)
def notification_content(request, notification_id):
    n = services.dispose_content(_notification(notification_id))
    return JsonResponse(notification_json(n))


def _parse_when(request, name):
    raw = request.GET.get(name)
    if not raw:
        raise BadRequest("'%s' (ISO-8601 date-time) is required." % name)
    # An unencoded '+' in a query string arrives as a space.
    value = parse_datetime(raw.strip().replace(" ", "+"))
    if value is None:
        raise BadRequest("'%s' must be an ISO-8601 date-time." % name)
    if timezone.is_naive(value):
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


@api(["GET"], staff=True)
def reconciliation(request):
    start = _parse_when(request, "from")
    end = _parse_when(request, "to")
    return JsonResponse(services.reconcile(start, end))
