"""JSON HTTP endpoints for order SMS notifications.

Session-authenticated (the sandbox's own auth). Shopper-scoped endpoints act
only on the caller's own data; operator endpoints require ``is_staff``.

CSRF note: these endpoints are ``csrf_exempt`` so the JSON API is drivable with
just a session cookie (curl / scripts). Authentication and per-object ownership
are still enforced on every call. A production deployment would front them with
CSRF tokens or a token-auth scheme.
"""

import functools
import json

from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from oscar.apps.order.models import Order

from . import services
from .models import ContactNumber, OrderNotification
from .twilio_gateway import TwilioError


# --- helpers -------------------------------------------------------------

def _json(data, status=200):
    return JsonResponse(data, status=status)


def _error(message, status):
    return JsonResponse({"error": message}, status=status)


def login_required_json(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error("authentication required", 401)
        return view(request, *args, **kwargs)

    return wrapper


def staff_required_json(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error("authentication required", 401)
        if not request.user.is_staff:
            return _error("operator privileges required", 403)
        return view(request, *args, **kwargs)

    return wrapper


def _parse_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        raise ValueError("request body must be valid JSON")
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    return data


def _notification_dict(n):
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "providerSid": n.provider_sid or None,
        "status": n.status or None,
        "reached": n.reached,
        "resendEligible": n.resend_eligible,
        "errorCode": n.error_code,
        "errorMessage": n.error_message or None,
        "toNumber": n.to_number,
        "isScheduled": n.is_scheduled,
        "contentRedacted": n.content_redacted,
        "createdAt": n.created_at.isoformat(),
    }


def _order_dict(order, notifications):
    return {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "total": str(order.total_incl_tax),
        "datePlaced": order.date_placed.isoformat() if order.date_placed else None,
        "notifications": [_notification_dict(n) for n in notifications],
    }


# --- Flow 1: contact numbers --------------------------------------------

@csrf_exempt
@require_http_methods(["GET", "POST"])
@login_required_json
def contact_numbers(request):
    if request.method == "GET":
        numbers = ContactNumber.objects.filter(owner=request.user)
        return _json(
            {
                "contactNumbers": [
                    {
                        "contactNumberId": c.pk,
                        "number": c.e164,
                        "createdAt": c.created_at.isoformat(),
                    }
                    for c in numbers
                ]
            }
        )

    # POST — register a number for the signed-in shopper.
    try:
        data = _parse_body(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    raw_number = (data.get("number") or "").strip()
    if not raw_number:
        return _error("a 'number' is required", 400)

    from . import twilio_gateway

    try:
        canonical = twilio_gateway.canonicalize_number(raw_number)
    except TwilioError as exc:
        if exc.status_code == 404:
            return _error("that number is not a usable destination", 422)
        if exc.outcome_unknown:
            return _error("could not validate the number right now", 502)
        return _error("that number could not be validated", 422)

    contact, created = ContactNumber.objects.get_or_create(
        owner=request.user, e164=canonical
    )
    return _json(
        {
            "contactNumberId": contact.pk,
            "number": contact.e164,
            "created": created,
        },
        status=201 if created else 200,
    )


@csrf_exempt
@require_http_methods(["DELETE"])
@login_required_json
def contact_number_detail(request, contact_number_id):
    try:
        contact = ContactNumber.objects.get(
            pk=contact_number_id, owner=request.user
        )
    except ContactNumber.DoesNotExist:
        return _error("contact number not found", 404)
    contact.delete()
    return _json({"deleted": True})


# --- Flow 2: orders and their messages ----------------------------------

@csrf_exempt
@require_http_methods(["POST"])
@login_required_json
def orders(request):
    try:
        data = _parse_body(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return _error("'items' must be a non-empty list", 400)

    try:
        order = services.place_order(request.user, items)
    except services.OrderPlacementError as exc:
        return _error(str(exc), 400)

    # Best-effort: tell the shopper their order was placed.
    notif = services.notify_order_placed(order)
    return _json(
        {
            "orderId": order.pk,
            "number": order.number,
            "status": order.status,
            "notification": _notification_dict(notif) if notif else None,
        },
        status=201,
    )


@csrf_exempt
@require_http_methods(["POST"])
@staff_required_json
def dispatch_order(request, order_id):
    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        return _error("order not found", 404)
    try:
        result = services.dispatch_order(order)
    except ValueError as exc:
        # e.g. an invalid status transition
        return _error(str(exc), 409)
    dispatched = result["dispatched"]
    followup = result["followup"]
    return _json(
        {
            "orderId": order.pk,
            "status": order.status,
            "notification": _notification_dict(dispatched) if dispatched else None,
            "followup": _notification_dict(followup) if followup else None,
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
@staff_required_json
def cancel_order(request, order_id):
    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        return _error("order not found", 404)
    try:
        result = services.cancel_order(order)
    except ValueError as exc:
        return _error(str(exc), 409)
    notice = result["cancelled_notice"]
    return _json(
        {
            "orderId": order.pk,
            "status": order.status,
            "notification": _notification_dict(notice) if notice else None,
            "cancelledFollowups": [
                _notification_dict(n) for n in result["cancelled_followups"]
            ],
        }
    )


@csrf_exempt
@require_http_methods(["GET"])
@login_required_json
def my_orders(request):
    orders_qs = Order.objects.filter(user=request.user).order_by("-date_placed")
    payload = []
    for order in orders_qs:
        notifs = list(order.sms_notifications.all())
        for n in notifs:
            services.refresh_status(n)
        payload.append(_order_dict(order, order.sms_notifications.all()))
    return _json({"orders": payload})


@csrf_exempt
@require_http_methods(["GET"])
@login_required_json
def order_notifications(request, order_id):
    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        return _error("order not found", 404)
    # Shopper sees only their own order; operators may see any.
    if not request.user.is_staff and order.user_id != request.user.id:
        return _error("order not found", 404)

    notifs = list(order.sms_notifications.all())
    for n in notifs:
        services.refresh_status(n)
    return _json(
        {
            "orderId": order.pk,
            "notifications": [
                _notification_dict(n) for n in order.sms_notifications.all()
            ],
        }
    )


# --- Flow 3: operator / shopper actions on messages ---------------------

@csrf_exempt
@require_http_methods(["POST"])
@staff_required_json
def resend_notification(request, notification_id):
    try:
        notification = OrderNotification.objects.get(pk=notification_id)
    except OrderNotification.DoesNotExist:
        return _error("notification not found", 404)
    try:
        data = _parse_body(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    idempotency_key = (data.get("idempotencyKey") or "").strip()

    try:
        resend, created = services.resend_notification(notification, idempotency_key)
    except services.ResendError as exc:
        return _error(str(exc), exc.status)

    return _json(
        {
            "notificationId": resend.pk,
            "created": created,
            "status": resend.status or None,
            "providerSid": resend.provider_sid or None,
        },
        status=201 if created else 200,
    )


@csrf_exempt
@require_http_methods(["DELETE"])
@login_required_json
def notification_content(request, notification_id):
    try:
        notification = OrderNotification.objects.get(pk=notification_id)
    except OrderNotification.DoesNotExist:
        return _error("notification not found", 404)
    # A shopper may dispose of content about them (their own order); an operator
    # may act on any.
    if not request.user.is_staff and notification.order.user_id != request.user.id:
        return _error("notification not found", 404)

    try:
        services.dispose_content(notification)
    except services.ContentDisposalError as exc:
        return _error(str(exc), exc.status)

    return _json(
        {
            "notificationId": notification.pk,
            "contentRedacted": notification.content_redacted,
        }
    )


@csrf_exempt
@require_http_methods(["GET"])
@staff_required_json
def reconciliation(request):
    from_raw = request.GET.get("from")
    to_raw = request.GET.get("to")
    if not from_raw or not to_raw:
        return _error("'from' and 'to' query parameters are required", 400)

    date_from = parse_datetime(from_raw)
    date_to = parse_datetime(to_raw)
    if date_from is None or date_to is None:
        return _error("'from' and 'to' must be ISO-8601 date-times", 400)
    if timezone.is_naive(date_from):
        date_from = timezone.make_aware(date_from)
    if timezone.is_naive(date_to):
        date_to = timezone.make_aware(date_to)
    if date_from > date_to:
        return _error("'from' must not be after 'to'", 400)

    try:
        report = services.reconcile(date_from, date_to)
    except TwilioError as exc:
        return _error("could not reach the provider for reconciliation", 502)

    def _provider_entry(m):
        return {
            "providerSid": m.sid,
            "status": m.status,
            "to": m.to,
            "from": m.from_,
            "dateSent": m.date_sent.isoformat() if m.date_sent else None,
        }

    return _json(
        {
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "providerCount": len(report["provider_messages"]),
            "appCount": len(report["app_by_sid"]),
            "matched": report["matched"],
            "inProviderNotApp": [
                _provider_entry(report["provider_by_sid"][sid])
                for sid in report["only_provider"]
            ],
            "inAppNotProvider": [
                _notification_dict(report["app_by_sid"][sid])
                for sid in report["only_app"]
            ],
        }
    )
