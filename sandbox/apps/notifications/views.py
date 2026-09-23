"""HTTP API for the SMS notification flows.

Plain Django views + JsonResponse (the sandbox ships no DRF). Callers authenticate with Django's own
session login; the JSON endpoints are CSRF-exempt so the API is drivable by a tool holding the
session cookie. Shopper endpoints act only on the caller's own data; operator endpoints require
``is_staff``.
"""

from __future__ import annotations

import functools
import json

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware, make_aware
from django.views.decorators.csrf import csrf_exempt

from . import services
from . import twilio_gateway as tw
from .models import ContactNumber, OrderNotification
from .serializers import serialize_contact_number, serialize_notification, serialize_order

Order = services.Order


# --- helpers ---------------------------------------------------------------------------------------

def _json(data, status=200):
    return JsonResponse(data, status=status)


def _error(message, status=400):
    return JsonResponse({"error": message}, status=status)


def _body(request):
    if not request.body:
        return {}
    try:
        parsed = json.loads(request.body)
    except (ValueError, TypeError):
        raise ValueError("Body must be valid JSON")
    if not isinstance(parsed, dict):
        raise ValueError("Body must be a JSON object")
    return parsed


def require_methods(*methods):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                return _error("Method not allowed", status=405)
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def require_auth(view):
    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error("Authentication required", status=401)
        return view(request, *args, **kwargs)
    return wrapped


def require_staff(view):
    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error("Authentication required", status=401)
        if not request.user.is_staff:
            return _error("Operator privileges required", status=403)
        return view(request, *args, **kwargs)
    return wrapped


def _get_order(order_number, user):
    """Fetch an order by its number, scoped: staff see any, a shopper sees only their own."""
    qs = Order.objects.filter(number=order_number)
    if not user.is_staff:
        qs = qs.filter(user=user)
    return qs.first()


# --- auth (Django session login, exposed as JSON for drivability) ----------------------------------

@csrf_exempt
@require_methods("POST")
def auth_login(request):
    try:
        data = _body(request)
    except ValueError as e:
        return _error(str(e))
    user = authenticate(
        request, username=data.get("username"), password=data.get("password")
    )
    if user is None:
        return _error("Invalid credentials", status=401)
    login(request, user)
    return _json({"username": user.get_username(), "isStaff": user.is_staff})


@csrf_exempt
@require_methods("POST")
def auth_logout(request):
    logout(request)
    return _json({"ok": True})


# --- flow 1: contact numbers -----------------------------------------------------------------------

@csrf_exempt
@require_methods("GET", "POST")
@require_auth
def contact_numbers(request):
    if request.method == "GET":
        rows = ContactNumber.objects.filter(user=request.user)
        return _json({"contactNumbers": [serialize_contact_number(c) for c in rows]})

    # POST: register a number after the provider confirms it is a usable destination.
    try:
        data = _body(request)
    except ValueError as e:
        return _error(str(e))
    number = (data.get("number") or "").strip()
    if not number:
        return _error("A 'number' is required")
    try:
        canonical = tw.validate_and_canonicalize(number)
    except tw.NotAUsableDestination:
        return _error("That number is not a usable destination", status=400)
    except tw.TwilioGatewayError:
        # Any other lookup failure (our credentials, provider outage, rate limit) is ours, not the
        # caller's: answer 502 rather than passing a provider status through.
        return _error("Could not validate the number with the provider", status=502)
    cn, _created = ContactNumber.objects.get_or_create(
        user=request.user, canonical_number=canonical
    )
    return _json({"contactNumberId": cn.id, **serialize_contact_number(cn)}, status=201)


@csrf_exempt
@require_methods("DELETE")
@require_auth
def contact_number_detail(request, pk):
    cn = ContactNumber.objects.filter(pk=pk, user=request.user).first()
    if cn is None:
        return _error("Contact number not found", status=404)
    number = cn.canonical_number
    cn.delete()
    # Nothing may be sent to it again: call off any still-pending follow-ups aimed at that number.
    pending = OrderNotification.objects.filter(
        user=request.user, is_followup=True, canceled=False, to_number=number
    ).exclude(provider_sid="")
    for row in pending:
        if row.provider_status and row.provider_status not in services.FOLLOWUP_PENDING_STATUSES:
            continue
        try:
            result = tw.cancel_scheduled(row.provider_sid)
            row.provider_status = result.provider_status or row.provider_status
            row.outcome = result.outcome
            row.canceled = True
            row.save()
        except tw.TwilioGatewayError:
            pass
    return _json({"ok": True})


# --- flow 2: orders --------------------------------------------------------------------------------

@csrf_exempt
@require_methods("POST")
@require_auth
def orders(request):
    try:
        data = _body(request)
    except ValueError as e:
        return _error(str(e))
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return _error("'items' must be a non-empty list of {productId, quantity}")
    try:
        order = services.place_order(request.user, items)
    except services.OrderError as e:
        return _error(str(e), status=e.status_code)
    return _json({"orderId": order.number, **serialize_order(order)}, status=201)


@csrf_exempt
@require_methods("POST")
@require_staff
def dispatch_order(request, order_number):
    order = _get_order(order_number, request.user)
    if order is None:
        return _error("Order not found", status=404)
    changed = services.dispatch_order(order, request.user)
    order.refresh_from_db()
    return _json({"orderId": order.number, "status": order.status, "dispatched": changed})


@csrf_exempt
@require_methods("POST")
@require_staff
def cancel_order(request, order_number):
    order = _get_order(order_number, request.user)
    if order is None:
        return _error("Order not found", status=404)
    changed = services.cancel_order(order, request.user)
    order.refresh_from_db()
    return _json({"orderId": order.number, "status": order.status, "cancelled": changed})


@csrf_exempt
@require_methods("GET")
@require_auth
def my_orders(request):
    rows = Order.objects.filter(user=request.user).order_by("-date_placed")
    out = []
    for order in rows:
        notes = list(order.sms_notifications.all())
        out.append(serialize_order(order, notes))
    return _json({"orders": out})


@csrf_exempt
@require_methods("GET")
@require_auth
def order_notifications(request, order_number):
    order = _get_order(order_number, request.user)
    if order is None:
        return _error("Order not found", status=404)
    rows = list(order.sms_notifications.all())
    # Refresh non-terminal delivery outcomes from the provider so the operator sees where each got to.
    refreshed = [services.refresh_status(r) for r in rows]
    return _json({"orderId": order.number,
                  "notifications": [serialize_notification(r) for r in refreshed]})


# --- flow 3: operator actions ----------------------------------------------------------------------

@csrf_exempt
@require_methods("POST")
@require_staff
def resend_notification(request, pk):
    original = OrderNotification.objects.filter(pk=pk).first()
    if original is None:
        return _error("Notification not found", status=404)
    try:
        data = _body(request)
    except ValueError as e:
        return _error(str(e))
    key = (data.get("idempotencyKey") or "").strip()
    if not key:
        return _error("An 'idempotencyKey' is required")
    try:
        row = services.resend(original, key, request.user)
    except services.OrderError as e:
        return _error(str(e), status=e.status_code)
    return _json({"notificationId": row.id, **serialize_notification(row)}, status=201)


@csrf_exempt
@require_methods("DELETE")
@require_staff
def notification_content(request, pk):
    row = OrderNotification.objects.filter(pk=pk).first()
    if row is None:
        return _error("Notification not found", status=404)
    if not row.provider_sid:
        # Nothing was sent to the provider; just mark it disposed locally.
        row.content_redacted = True
        row.body = ""
        row.save(update_fields=["content_redacted", "body", "updated_at"])
        return _json({"notificationId": row.id, "contentRedacted": True})
    try:
        services.dispose_content(row)
    except tw.TwilioGatewayError:
        return _error("Could not dispose of the content at the provider", status=502)
    return _json({"notificationId": row.id, "contentRedacted": True})


@csrf_exempt
@require_methods("GET")
@require_staff
def reconciliation(request):
    from_raw = request.GET.get("from")
    to_raw = request.GET.get("to")
    if not from_raw or not to_raw:
        return _error("'from' and 'to' ISO-8601 date-times are required")
    from_dt = parse_datetime(from_raw)
    to_dt = parse_datetime(to_raw)
    if from_dt is None or to_dt is None:
        return _error("'from' and 'to' must be ISO-8601 date-times")
    if not is_aware(from_dt):
        from_dt = make_aware(from_dt)
    if not is_aware(to_dt):
        to_dt = make_aware(to_dt)
    if from_dt >= to_dt:
        return _error("'from' must be before 'to'")
    try:
        report = services.reconcile(from_dt, to_dt)
    except tw.TwilioGatewayError:
        return _error("Could not read the provider's records", status=502)

    def prov(m):
        return {"providerSid": m.sid, "to": m.to, "providerStatus": m.provider_status,
                "dateSent": m.date_sent.isoformat() if m.date_sent else None,
                "errorCode": m.error_code}

    return _json({
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "fromNumber": tw.from_number(),
        "truncated": report["truncated"],
        "matched": [
            {"notification": serialize_notification(row), "provider": prov(m)}
            for row, m in report["matched"]
        ],
        "localOnly": [serialize_notification(r) for r in report["local_only"]],
        "providerOnly": [prov(m) for m in report["provider_only"]],
        "unsettled": [serialize_notification(r) for r in report["unsettled"]],
        "counts": {
            "matched": len(report["matched"]),
            "localOnly": len(report["local_only"]),
            "providerOnly": len(report["provider_only"]),
            "unsettled": len(report["unsettled"]),
        },
    })
