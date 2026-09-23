"""
JSON API for order SMS notifications.

Callers authenticate with Django's session login (the sandbox's own); unsafe
methods need the CSRF token (GET /api/csrf) in an ``X-CSRFToken`` header.
Views are non-atomic on purpose: the sandbox runs ATOMIC_REQUESTS, and a claim
row must be committed before Twilio is called, not rolled back afterwards.
"""
import json
import logging
import re
from datetime import timezone as dt_timezone
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import services
from . import twilio_gateway as gateway
from .models import ContactNumber, Notification

logger = logging.getLogger("apps.order_sms")

Order = get_model("order", "Order")

_PHONE_INPUT = re.compile(r"^[+0-9 ().-]{3,32}$")
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")
MAX_LINES = 50
MAX_QUANTITY = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def provider_error(exc):
    return error(exc.http_status, exc.message, outcomeUnknown=exc.outcome_unknown)


def api_view(*methods, staff=False):
    """Session auth (401), optional staff check (403), JSON errors, no request-wide transaction."""

    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return error(401, "Authentication required.")
            if staff and not request.user.is_staff:
                return error(403, "Only staff may do this.")
            return view(request, *args, **kwargs)

        return transaction.non_atomic_requests(require_http_methods(list(methods))(wrapped))

    return decorate


def read_json(request):
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Request body must be JSON.")
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


def iso(value):
    return value.isoformat() if value else None


def contact_json(contact):
    return {
        "contactNumberId": contact.pk,
        "phoneNumber": contact.phone_number,
        "countryCode": contact.country_code or None,
        "createdAt": iso(contact.created_at),
    }


def notification_json(n, stale=frozenset()):
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "status": n.status,
        "outcome": n.outcome,
        "providerMessageSid": n.provider_sid,
        "contactNumberId": n.contact_id,
        "body": None if n.content_redacted_at else n.body,
        "contentDisposed": n.content_redacted_at is not None,
        "contentDisposedAt": iso(n.content_redacted_at),
        "errorCode": n.error_code,
        "errorMessage": n.error_message or None,
        "scheduledFor": iso(n.send_at),
        "providerDateCreated": iso(n.provider_date_created),
        "providerDateSent": iso(n.provider_date_sent),
        "cancelRequested": n.cancel_requested_at is not None,
        "resendOf": n.resend_of_id,
        "createdAt": iso(n.created_at),
        "lastCheckedWithProviderAt": iso(n.last_synced_at),
        "stale": n.pk in stale,
    }


def order_json(order, notifications=None, stale=frozenset()):
    data = {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "datePlaced": iso(order.date_placed),
        "lines": [
            {"productId": line.product_id, "title": line.title, "quantity": line.quantity}
            for line in order.lines.all()
        ],
    }
    if notifications is not None:
        data["notifications"] = [notification_json(n, stale) for n in notifications]
    return data


def own_order(request, order_id):
    return Order.objects.filter(pk=order_id, user=request.user).first()


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@ensure_csrf_cookie
@require_http_methods(["GET"])
def csrf(request):
    return JsonResponse({"csrfToken": get_token(request)})


@transaction.non_atomic_requests
@require_http_methods(["GET", "POST", "DELETE"])
def session(request):
    if request.method == "POST":
        try:
            payload = read_json(request)
        except ValueError as exc:
            return error(400, str(exc))
        user = authenticate(
            request,
            username=str(payload.get("username", "")),
            password=str(payload.get("password", "")),
        )
        if user is None:
            return error(401, "Invalid credentials.")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
        return JsonResponse({"authenticated": False})
    if not request.user.is_authenticated:
        return JsonResponse({"authenticated": False})
    return JsonResponse(
        {
            "authenticated": True,
            "userId": request.user.pk,
            "username": request.user.get_username(),
            "isStaff": request.user.is_staff,
            "csrfToken": get_token(request),
        }
    )


# ---------------------------------------------------------------------------
# Flow 1 - contact numbers
# ---------------------------------------------------------------------------


@api_view("GET", "POST")
def contact_numbers(request):
    if request.method == "GET":
        contacts = ContactNumber.objects.filter(user=request.user)
        return JsonResponse({"contactNumbers": [contact_json(c) for c in contacts]})
    try:
        payload = read_json(request)
    except ValueError as exc:
        return error(400, str(exc))
    raw = payload.get("phoneNumber")
    country = payload.get("countryCode")
    if not isinstance(raw, str) or not _PHONE_INPUT.match(raw.strip()):
        return error(400, "phoneNumber must be a phone number.")
    if country is not None and (not isinstance(country, str) or not _COUNTRY.match(country)):
        return error(400, "countryCode must be a two-letter ISO country code.")
    try:
        contact, created = services.register_contact(
            request.user, raw.strip(), country.upper() if country else None
        )
    except gateway.ProviderRejected:
        return error(422, "Twilio does not recognise this as a usable phone number.")
    except gateway.ProviderError as exc:
        return provider_error(exc)
    return JsonResponse(contact_json(contact), status=201 if created else 200)


@api_view("DELETE")
def contact_number_detail(request, contact_number_id):
    contact = ContactNumber.objects.filter(pk=contact_number_id, user=request.user).first()
    if contact is None:
        return error(404, "No such contact number.")
    services.delete_contact(contact)
    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Flow 2 - orders
# ---------------------------------------------------------------------------


def _parse_lines(payload):
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ValueError("lines must be a non-empty list of {productId, quantity}.")
    if len(lines) > MAX_LINES:
        raise ValueError(f"At most {MAX_LINES} lines per order.")
    parsed = []
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError("Each line must be an object with productId and quantity.")
        product_id, quantity = line.get("productId"), line.get("quantity", 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise ValueError("productId must be a positive integer.")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise ValueError(f"quantity must be an integer between 1 and {MAX_QUANTITY}.")
        parsed.append((product_id, quantity))
    return parsed


@api_view("POST")
def orders(request):
    try:
        lines = _parse_lines(read_json(request))
    except ValueError as exc:
        return error(400, str(exc))
    try:
        order, notification = services.place_order(request.user, lines)
    except services.OrderRequestInvalid as exc:
        return error(422, str(exc))
    body = order_json(order, [notification] if notification else [])
    body["notified"] = notification is not None
    return JsonResponse(body, status=201)


def _operator_transition(request, order_id, action):
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        return error(404, "No such order.")
    try:
        performed = action(order)
    except services.ActionNotAllowed as exc:
        return error(409, str(exc), status=order.status)
    notifications = list(order.sms_notifications.all())
    body = order_json(order, notifications)
    body["alreadyApplied"] = not performed
    return JsonResponse(body)


@api_view("POST", staff=True)
def order_dispatch(request, order_id):
    return _operator_transition(request, order_id, services.dispatch_order)


@api_view("POST", staff=True)
def order_cancel(request, order_id):
    return _operator_transition(request, order_id, services.cancel_order)


@api_view("GET")
def my_orders(request):
    orders = list(
        Order.objects.filter(user=request.user).order_by("-date_placed").prefetch_related("lines")[:100]
    )
    notifications = list(Notification.objects.filter(order__in=orders).select_related("contact"))
    stale = services.sync_many(notifications)
    by_order = {}
    for n in notifications:
        by_order.setdefault(n.order_id, []).append(n)
    return JsonResponse({"orders": [order_json(o, by_order.get(o.pk, []), stale) for o in orders]})


@api_view("GET")
def order_notifications(request, order_id):
    order = own_order(request, order_id)
    if order is None:
        return error(404, "No such order.")
    notifications = list(order.sms_notifications.select_related("contact"))
    stale = services.sync_many(notifications)
    return JsonResponse(
        {"orderId": order.pk, "notifications": [notification_json(n, stale) for n in notifications]}
    )


# ---------------------------------------------------------------------------
# Flow 3 - operator actions
# ---------------------------------------------------------------------------


@api_view("POST", staff=True)
def notification_resend(request, notification_id):
    try:
        payload = read_json(request)
    except ValueError as exc:
        return error(400, str(exc))
    key = request.headers.get("Idempotency-Key") or payload.get("idempotencyKey")
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        return error(400, "An idempotency key (Idempotency-Key header or idempotencyKey) is required.")
    source = Notification.objects.select_related("order", "contact").filter(pk=notification_id).first()
    if source is None:
        return error(404, "No such notification.")
    try:
        notification, created = services.resend(source, key.strip())
    except services.ActionNotAllowed as exc:
        return error(409, str(exc))
    body = notification_json(notification)
    body["replayed"] = not created
    return JsonResponse(body, status=201 if created else 200)


@api_view("DELETE", staff=True)
def notification_content(request, notification_id):
    notification = Notification.objects.select_related("contact").filter(pk=notification_id).first()
    if notification is None:
        return error(404, "No such notification.")
    try:
        notification = services.dispose_content(notification)
    except services.ActionNotAllowed as exc:
        return error(409, str(exc))
    except gateway.ProviderError as exc:
        return provider_error(exc)
    return JsonResponse(notification_json(notification))


def _parse_instant(value, name):
    parsed = parse_datetime(value or "")
    if parsed is None:
        raise ValueError(f"{name} must be an ISO-8601 date-time.")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


def _masked(number):
    return f"{number[:2]}{'*' * max(len(number) - 4, 0)}{number[-2:]}" if number else None


def _provider_entry(snap):
    if snap is None:
        return None
    return {
        "sid": snap.sid,
        "status": snap.status,
        "outcome": snap.outcome,
        "direction": snap.direction,
        "to": _masked(snap.to_number),
        "dateCreated": iso(snap.date_created),
        "dateSent": iso(snap.date_sent),
        "errorCode": snap.error_code,
    }


def _local_entry(n):
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "status": n.status,
        "outcome": n.outcome,
        "providerMessageSid": n.provider_sid,
        "createdAt": iso(n.created_at),
    }


@api_view("GET", staff=True)
def reconciliation(request):
    try:
        start = _parse_instant(request.GET.get("from"), "from")
        end = _parse_instant(request.GET.get("to"), "to")
    except ValueError as exc:
        return error(400, str(exc))
    if end <= start:
        return error(400, "to must be after from.")
    try:
        report = services.reconcile(start, end)
    except gateway.ProviderError as exc:
        return provider_error(exc)
    matched = [
        {
            **_local_entry(n),
            "provider": _provider_entry(snap),
            "statusBefore": previous,
            "statusChanged": previous != snap.status,
        }
        for n, snap, previous in report.matched
    ]
    return JsonResponse(
        {
            "from": iso(report.start),
            "to": iso(report.end),
            "fromNumber": _masked(gateway._setting("TWILIO_FROM_NUMBER")),
            "complete": not report.truncated and not report.unverified,
            "truncated": report.truncated,
            "providerPagesRead": report.pages,
            "summary": {
                "matched": len(report.matched),
                "providerOnly": len(report.provider_only),
                "appOnly": len(report.app_only),
                "outOfWindow": len(report.out_of_window),
                "unverified": len(report.unverified),
                "unsettled": len(report.unsettled),
                "inboundExcluded": report.inbound_excluded,
            },
            "matched": matched,
            "providerOnly": [_provider_entry(snap) for _, snap in report.provider_only],
            "appOnly": [_local_entry(n) for n, _ in report.app_only],
            "outOfWindow": [
                {**_local_entry(n), "provider": _provider_entry(snap)} for n, snap in report.out_of_window
            ],
            "unverified": [_local_entry(n) for n, _ in report.unverified],
            "unsettled": [_local_entry(n) for n in report.unsettled],
        }
    )
