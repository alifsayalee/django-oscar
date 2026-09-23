"""
JSON API for order SMS notifications.

Callers authenticate with the sandbox's own Django session login (CSRF applies
to unsafe methods as everywhere else on the site). Operator endpoints require
``is_staff``. Views that talk to the provider opt out of ATOMIC_REQUESTS so the
rows they write are committed before, and survive, each provider call.
"""

import functools
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from oscar.core.loading import get_model

from . import services
from . import twilio_gateway as gateway
from .models import ContactNumber, Notification

logger = logging.getLogger("order_notifications")

Order = get_model("order", "Order")

MAX_RECONCILIATION_RANGE = timedelta(days=366)


class BadRequest(Exception):
    pass


def _error(status, message):
    return JsonResponse({"error": message}, status=status)


def api_view(methods, staff=False):
    """Method check, session auth, staff check and uniform error translation."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                response = _error(405, "Method not allowed.")
                response["Allow"] = ", ".join(methods)
                return response
            if not request.user.is_authenticated:
                return _error(401, "Authentication required.")
            if staff and not request.user.is_staff:
                return _error(403, "Operator access required.")
            try:
                return view(request, *args, **kwargs)
            except Http404:
                return _error(404, "Not found.")
            except BadRequest as e:
                return _error(400, str(e))
            except services.ServiceError as e:
                return _error(e.status_code, e.message)
            except gateway.NumberNotUsable:
                return _error(422, "The messaging provider does not recognise that number.")
            except gateway.ProviderError as e:
                return _error(e.status_code, e.message)

        return transaction.non_atomic_requests(wrapped)

    return decorator


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as e:
        raise BadRequest("Request body must be JSON.") from e
    if not isinstance(data, dict):
        raise BadRequest("Request body must be a JSON object.")
    return data


def _iso(value):
    return value.isoformat() if value else None


def contact_number_json(number):
    return {
        "contactNumberId": number.pk,
        "phoneNumber": number.phone_number,
        "countryCode": number.country_code or None,
        "createdAt": _iso(number.created_at),
    }


def notification_json(notification):
    number = notification.contact_number
    return {
        "notificationId": notification.pk,
        "orderId": notification.order_id,
        "kind": notification.kind,
        "outcome": notification.outcome,
        "to": gateway.mask_number(number.phone_number) if number else None,
        "body": None if notification.content_disposed_at else notification.body,
        "contentDisposedAt": _iso(notification.content_disposed_at),
        "providerSid": notification.provider_sid,
        "providerStatus": notification.provider_status or None,
        "errorCode": notification.error_code,
        "errorMessage": notification.error_message or None,
        "scheduledFor": _iso(notification.scheduled_for),
        "cancelRequestedAt": _iso(notification.cancel_requested_at),
        "resendOf": notification.resend_of_id,
        "createdAt": _iso(notification.created_at),
        "providerCreatedAt": _iso(notification.provider_created_at),
        "providerSentAt": _iso(notification.provider_sent_at),
        "lastCheckedAt": _iso(notification.last_checked_at),
    }


def _notifications(order, refresh=True):
    notifications = list(
        Notification.objects.filter(order=order).select_related("contact_number")
    )
    if refresh:
        services.refresh_all(notifications)
    return notifications


def order_json(order, notifications):
    latest = {}
    for n in notifications:
        latest[n.kind] = n.outcome
    return {
        "orderId": order.pk,
        "orderNumber": str(order.number),
        "status": order.status,
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "datePlaced": _iso(order.date_placed),
        "lines": [
            {"productId": line.product_id, "title": line.title, "quantity": line.quantity}
            for line in order.lines.all()
        ],
        "notificationSummary": latest,
        "notifications": [notification_json(n) for n in notifications],
    }


def _order_for(request, order_id):
    """The order, if the caller owns it or is an operator; 404 otherwise."""
    order = get_object_or_404(Order, pk=order_id)
    if order.user_id != request.user.pk and not request.user.is_staff:
        raise Http404
    return order


# ---------------------------------------------------------------------------
# Flow 1 — contact numbers
# ---------------------------------------------------------------------------


@api_view(["GET", "POST"])
def contact_numbers(request):
    if request.method == "GET":
        numbers = services.active_numbers(request.user).order_by("-created_at", "-id")
        return JsonResponse({"contactNumbers": [contact_number_json(n) for n in numbers]})

    data = _json_body(request)
    raw = data.get("phoneNumber")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 32:
        raise BadRequest("phoneNumber is required.")
    country = data.get("countryCode")
    if country is not None and (not isinstance(country, str) or len(country) != 2):
        raise BadRequest("countryCode must be a two-letter ISO country code.")
    number, created = services.register_number(
        request.user, raw.strip(), country.upper() if country else None
    )
    return JsonResponse(contact_number_json(number), status=201 if created else 200)


@api_view(["DELETE"])
def contact_number_detail(request, contact_number_id):
    number = get_object_or_404(
        ContactNumber, pk=contact_number_id, user=request.user, removed_at__isnull=True
    )
    services.remove_number(number)
    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Flow 2 — orders
# ---------------------------------------------------------------------------


def _parse_lines(data):
    lines = data.get("lines")
    if not isinstance(lines, list) or not lines:
        raise BadRequest("lines must be a non-empty list of {productId, quantity}.")
    items = []
    for line in lines:
        if not isinstance(line, dict):
            raise BadRequest("Each line must be an object.")
        product_id, quantity = line.get("productId"), line.get("quantity", 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise BadRequest("productId must be an integer.")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 100:
            raise BadRequest("quantity must be an integer between 1 and 100.")
        items.append((product_id, quantity))
    return items


@api_view(["POST"])
def orders(request):
    items = _parse_lines(_json_body(request))
    order = services.place_order(request.user, items, request)
    return JsonResponse(order_json(order, _notifications(order, refresh=False)), status=201)


@api_view(["POST"], staff=True)
def order_dispatch(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    order, changed = services.dispatch(order, request.user)
    body = order_json(order, _notifications(order, refresh=False))
    body["changed"] = changed
    return JsonResponse(body)


@api_view(["POST"], staff=True)
def order_cancel(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    order, changed = services.cancel(order, request.user)
    body = order_json(order, _notifications(order, refresh=False))
    body["changed"] = changed
    return JsonResponse(body)


@api_view(["GET"])
def my_orders(request):
    try:
        limit = min(max(int(request.GET.get("limit", 20)), 1), 100)
    except ValueError as e:
        raise BadRequest("limit must be an integer.") from e
    result = []
    for order in Order.objects.filter(user=request.user).order_by("-date_placed", "-id")[:limit]:
        result.append(order_json(order, _notifications(order)))
    return JsonResponse({"orders": result})


@api_view(["GET"])
def order_notifications(request, order_id):
    order = _order_for(request, order_id)
    return JsonResponse(
        {
            "orderId": order.pk,
            "orderNumber": str(order.number),
            "status": order.status,
            "notifications": [notification_json(n) for n in _notifications(order)],
        }
    )


# ---------------------------------------------------------------------------
# Flow 3 — operator actions
# ---------------------------------------------------------------------------


@api_view(["POST"], staff=True)
def notification_resend(request, notification_id):
    original = get_object_or_404(Notification, pk=notification_id)
    data = _json_body(request)
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise BadRequest("An idempotencyKey (1-128 characters) is required.")
    notification, created = services.resend(original, key.strip(), request.user)
    body = notification_json(notification)
    body["replayed"] = not created
    return JsonResponse(body, status=201 if created else 200)


@api_view(["DELETE"], staff=True)
def notification_content(request, notification_id):
    notification = get_object_or_404(Notification, pk=notification_id)
    notification = services.dispose_content(notification)
    return JsonResponse(notification_json(notification))


def _parse_instant(request, name):
    raw = request.GET.get(name)
    if raw:
        # An unencoded "+01:00" offset arrives as " 01:00".
        raw = raw.strip().replace(" ", "+")
    if not raw:
        raise BadRequest("%s is required (ISO-8601 date-time)." % name)
    try:
        value = parse_datetime(raw)
    except ValueError:
        value = None
    if value is None:
        raise BadRequest("%s must be an ISO-8601 date-time." % name)
    if value.tzinfo is None:
        raise BadRequest("%s must include a UTC offset (e.g. 'Z')." % name)
    return value


@api_view(["GET"], staff=True)
def reconciliation(request):
    start, end = _parse_instant(request, "from"), _parse_instant(request, "to")
    if start >= end:
        raise BadRequest("from must be before to.")
    if end - start > MAX_RECONCILIATION_RANGE:
        raise BadRequest("The range may span at most 366 days.")
    return JsonResponse(services.reconcile(start, end))
