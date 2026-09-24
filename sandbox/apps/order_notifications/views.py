"""
JSON API for order SMS notifications.

Callers authenticate with the sandbox's Django session login (CSRF applies
to unsafe methods as everywhere else on the site). Operator endpoints need
``is_staff``; every other endpoint acts only on the caller's own data.
"""

import json
import logging
from datetime import timezone as dt_timezone
from functools import wraps

from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from oscar.core.loading import get_model

from . import gateway as gw
from . import services
from .models import ContactNumber, Notification

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")

MAX_ORDER_LINES = 50
MAX_QUANTITY = 99
MAX_IDEMPOTENCY_KEY = 128


def _error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def api_endpoint(methods, staff_only=False):
    """
    Method dispatch, JSON error handling and access control for an endpoint.
    Views manage their own transactions so a record written before a
    provider call is committed before that call is made.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = _error(405, "Method not allowed.")
                response["Allow"] = ", ".join(methods)
                return response
            if not request.user.is_authenticated:
                return _error(401, "Authentication required.")
            if staff_only and not request.user.is_staff:
                return _error(403, "This action is restricted to staff.")
            try:
                return view(request, *args, **kwargs)
            except services.ServiceError as e:
                return _error(e.http_status, str(e), **e.extra)
            except Http404:
                return _error(404, "Not found.")
        return transaction.non_atomic_requests(wrapper)
    return decorator


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise services.ServiceError(400, "Request body must be JSON.")
    if not isinstance(data, dict):
        raise services.ServiceError(400, "Request body must be a JSON object.")
    return data


def _iso(value):
    return value.isoformat() if value else None


# -- Serialisers ----------------------------------------------------------------

def contact_number_data(contact):
    return {
        "contactNumberId": contact.pk,
        "phoneNumber": contact.phone_number,
        "countryCode": contact.country_code or None,
        "createdAt": _iso(contact.created_at),
    }


def notification_data(notification):
    return {
        "notificationId": notification.pk,
        "orderId": notification.order_id,
        "kind": notification.kind,
        "outcome": notification.outcome,
        "submitState": notification.submit_state,
        "providerMessageSid": notification.provider_sid,
        "providerStatus": notification.provider_status or None,
        "errorCode": notification.error_code,
        "errorMessage": notification.error_message or None,
        "contactNumberId": notification.contact_number_id,
        "reference": notification.reference,
        "body": None if notification.content_redacted_at else notification.body,
        "contentDisposedAt": _iso(notification.content_redacted_at),
        "scheduledFor": _iso(notification.scheduled_for),
        "submittedAt": _iso(notification.submitted_at),
        "sentAt": _iso(notification.sent_at),
        "lastCheckedAt": _iso(notification.last_checked_at),
        "cancellationPending": notification.cancel_requested,
        "resendOf": notification.resend_of_id,
        "createdAt": _iso(notification.created_at),
    }


def order_data(order, notifications):
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
        "notifications": [notification_data(n) for n in notifications],
    }


def _order_notifications(order):
    notifications = list(order.sms_notifications.select_related("contact_number"))
    services.refresh(notifications)
    return notifications


# -- Contact numbers --------------------------------------------------------------

@api_endpoint(["GET", "POST"])
def contact_numbers(request):
    if request.method == "GET":
        numbers = ContactNumber.objects.filter(user=request.user, deleted_at__isnull=True)
        return JsonResponse({"contactNumbers": [contact_number_data(c) for c in numbers]})
    data = _json_body(request)
    raw = data.get("phoneNumber")
    country = data.get("countryCode")
    if not isinstance(raw, str):
        raise services.ServiceError(400, "phoneNumber is required.")
    if country is not None and (not isinstance(country, str) or len(country) != 2):
        raise services.ServiceError(400, "countryCode must be a two-letter ISO country code.")
    contact, created = services.register_contact_number(request.user, raw, country)
    return JsonResponse(contact_number_data(contact), status=201 if created else 200)


@api_endpoint(["DELETE"])
def contact_number_detail(request, contact_number_id):
    contact = get_object_or_404(
        ContactNumber, pk=contact_number_id, user=request.user, deleted_at__isnull=True
    )
    services.delete_contact_number(contact)
    return JsonResponse({"contactNumberId": contact.pk, "deleted": True})


# -- Orders ------------------------------------------------------------------------

@api_endpoint(["POST"])
def orders(request):
    data = _json_body(request)
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines or len(raw_lines) > MAX_ORDER_LINES:
        raise services.ServiceError(
            400, "lines must be a non-empty list of {productId, quantity} (at most %s)." % MAX_ORDER_LINES)
    lines = []
    for item in raw_lines:
        if not isinstance(item, dict):
            raise services.ServiceError(400, "Each line must be an object with productId and quantity.")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise services.ServiceError(400, "productId must be an integer.")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise services.ServiceError(400, "quantity must be an integer from 1 to %s." % MAX_QUANTITY)
        lines.append((product_id, quantity))
    order = services.place_order(request.user, lines, request=request)
    return JsonResponse(order_data(order, order.sms_notifications.all()), status=201)


@api_endpoint(["POST"], staff_only=True)
def order_dispatch(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    services.dispatch_order(order)
    return JsonResponse(order_data(order, order.sms_notifications.all()))


@api_endpoint(["POST"], staff_only=True)
def order_cancel(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    followups, _notice = services.cancel_order(order)
    body = order_data(order, order.sms_notifications.all())
    body["followUpsCalledOff"] = [
        {"notificationId": f.pk, "outcome": f.outcome, "providerStatus": f.provider_status or None,
         "cancellationPending": f.cancel_requested}
        for f in followups
    ]
    return JsonResponse(body)


@api_endpoint(["GET"])
def my_orders(request):
    orders_qs = Order.objects.filter(user=request.user).order_by("-date_placed").prefetch_related("lines")
    return JsonResponse({"orders": [order_data(o, _order_notifications(o)) for o in orders_qs]})


@api_endpoint(["GET"])
def order_notifications(request, order_id):
    # Shoppers see their own orders only; staff may look at any order, since
    # the operator endpoints act on the notificationIds listed here.
    qs = Order.objects.all() if request.user.is_staff else Order.objects.filter(user=request.user)
    order = get_object_or_404(qs, pk=order_id)
    notifications = _order_notifications(order)
    return JsonResponse({
        "orderId": order.pk,
        "orderStatus": order.status,
        "notifications": [notification_data(n) for n in notifications],
    })


# -- Operator notification actions ------------------------------------------------------

@api_endpoint(["POST"], staff_only=True)
def notification_resend(request, notification_id):
    notification = get_object_or_404(Notification, pk=notification_id)
    data = _json_body(request)
    key = request.headers.get("Idempotency-Key") or data.get("idempotencyKey")
    if not isinstance(key, str) or not key.strip() or len(key) > MAX_IDEMPOTENCY_KEY:
        raise services.ServiceError(
            400, "An idempotency key (Idempotency-Key header or idempotencyKey field, up to %s characters) "
                 "is required." % MAX_IDEMPOTENCY_KEY)
    resend_request, replayed = services.resend(notification, key.strip(), request.user)
    result = resend_request.result
    body = {
        "notificationId": result.pk if result else None,
        "resendOf": notification.pk,
        "idempotencyKey": resend_request.key,
        "replayed": replayed,
        "state": resend_request.state,
        "notification": notification_data(result) if result else None,
    }
    if resend_request.state == resend_request.STATE_DONE:
        status = 200 if replayed else 201
    elif resend_request.state == resend_request.STATE_FAILED:
        body["error"] = "The message could not be handed to the messaging provider."
        status = 502
    else:
        # Sending, or it may have reached the provider: never re-sent
        # automatically; the stored state is reported as-is.
        status = 202
    return JsonResponse(body, status=status)


@api_endpoint(["DELETE"], staff_only=True)
def notification_content(request, notification_id):
    notification = get_object_or_404(Notification, pk=notification_id)
    notification = services.redact_content(notification)
    return JsonResponse(notification_data(notification))


def _parse_instant(value, name):
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        raise services.ServiceError(400, "%s must be an ISO-8601 date-time." % name)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


@api_endpoint(["GET"], staff_only=True)
def notification_reconciliation(request):
    start = _parse_instant(request.GET.get("from"), "from")
    end = _parse_instant(request.GET.get("to"), "to")
    if start > end:
        raise services.ServiceError(400, "from must not be after to.")
    try:
        report = services.reconcile(start, end)
    except gw.ProviderError as e:
        raise services.ServiceError(e.http_status, str(e))

    matched = []
    mismatches = 0
    for row in report["matched"]:
        n, m = row["notification"], row["message"]
        app_status = n.provider_status or None
        agrees = app_status == m.status
        mismatches += 0 if agrees else 1
        matched.append({
            "providerMessageSid": m.sid,
            "notificationId": n.pk,
            "orderId": n.order_id,
            "kind": n.kind,
            "providerStatus": m.status,
            "appRecordedStatus": app_status,
            "statusesAgree": agrees,
            "outcome": m.outcome,
            "errorCode": m.error_code,
            "sentAt": _iso(m.date_sent),
        })
        if not agrees:
            services._apply_provider_message(n, m)
    return JsonResponse({
        "from": start.isoformat(),
        "to": end.isoformat(),
        "summary": {
            "providerMessages": report["provider_count"],
            "matched": len(matched),
            "statusMismatches": mismatches,
            "providerOnly": len(report["provider_only"]),
            "appOnly": len(report["app_only"]),
            "unresolved": len(report["unresolved"]),
        },
        "matched": matched,
        "providerOnly": [
            {"providerMessageSid": r["message"].sid, "providerStatus": r["message"].status,
             "direction": r["message"].direction, "sentAt": _iso(r["message"].date_sent)}
            for r in report["provider_only"]
        ],
        "appOnly": [notification_data(r["notification"]) for r in report["app_only"]],
        "unresolved": [notification_data(n) for n in report["unresolved"]],
    })
