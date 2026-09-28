"""
JSON API for SMS order notifications, mounted under ``/api/``.

Callers authenticate with Django's own session login (the sandbox's login page, or ``POST /api/session``) and send
the CSRF token on unsafe methods, as with any Django form. Operator actions require ``is_staff``; everything else
acts only on the caller's own data - another shopper's records answer 404, exactly like records that do not exist.
"""
import functools
import json
import logging

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from oscar.core.loading import get_model

from . import services
from .models import ContactNumber, Notification, ProviderAction
from .outcomes import answer_status
from .provider import NotConfigured, ProviderError
from .safe_write import OutcomeUnknown

logger = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")


# ---------------------------------------------------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------------------------------------------------

def error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def api(methods, staff=False, anonymous=False):
    """Method dispatch, JSON errors instead of redirects, and one translation of every failure kind."""

    def decorate(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = error(405, "Method not allowed.")
                response["Allow"] = ", ".join(methods)
                return response
            if not anonymous and not request.user.is_authenticated:
                return error(401, "Authentication required: log in with a session first.")
            if staff and not request.user.is_staff:
                return error(403, "This action is restricted to staff operators.")
            return translate_failures(view, request, *args, **kwargs)
        return wrapper
    return decorate


def translate_failures(view, request, *args, **kwargs):
    try:
        return view(request, *args, **kwargs)
    except services.ServiceError as e:
        return error(e.status_code, e.message, **e.extra)
    except OutcomeUnknown:
        return error(504, "The SMS provider may have acted; the outcome is not known yet. Repeat the request to "
                          "check.", outcomeUnknown=True)
    except ProviderError as e:
        return error(e.status_code, e.message, outcomeUnknown=e.outcome_unknown)
    except NotConfigured:
        return error(503, "SMS messaging is not configured on this server.")


def json_body(request):
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise services.ServiceError(400, "The request body must be JSON.")
    if not isinstance(body, dict):
        raise services.ServiceError(400, "The request body must be a JSON object.")
    return body


def own_order_or_404(request, order_id):
    """The order, if the caller may see it (its owner, or staff). Anyone else gets the same 404 as a missing order."""
    order = Order.objects.filter(pk=order_id).first()
    if order is None or not (request.user.is_staff or order.user_id == request.user.pk):
        raise services.ServiceError(404, "Order not found.")
    return order


def iso(value):
    return value.isoformat() if value else None


def mask(number):
    """Show enough of a number to recognise it, never the whole thing."""
    return (number[:3] + "•" * max(len(number) - 5, 0) + number[-2:]) if number else None


# ---------------------------------------------------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------------------------------------------------

def contact_json(contact):
    return {
        "contactNumberId": contact.pk,
        "phoneNumber": contact.phone_number,
        "countryCode": contact.country_code or None,
        "createdAt": iso(contact.created_at),
    }


def action_json(action):
    if action is None:
        return None
    return {
        "action": action.action,
        "outcome": action.outcome,
        "providerStatus": action.provider_status or None,
        "detail": action.detail or None,
        "at": iso(action.provider_time or action.updated_at),
    }


def notification_json(notification):
    actions = {a.action: a for a in notification.actions.all()}
    return {
        "notificationId": notification.pk,
        "orderId": notification.order_id,
        "kind": notification.kind,
        "resendOf": notification.resend_of_id,
        "outcome": notification.outcome,
        "detail": notification.skip_reason or notification.detail or None,
        "to": mask(notification.to_number),
        "providerSid": notification.provider_sid or None,
        "providerStatus": notification.provider_status or None,
        "providerErrorCode": notification.provider_error_code,
        "scheduledFor": iso(notification.send_at),
        "sentAt": iso(notification.provider_sent_at),
        "lastCheckedAt": iso(notification.last_checked_at),
        "body": notification.body if notification.content_disposed_at is None else None,
        "contentDisposedAt": iso(notification.content_disposed_at),
        "callOff": action_json(actions.get(ProviderAction.CANCEL)),
        "redaction": action_json(actions.get(ProviderAction.REDACT)),
        "createdAt": iso(notification.created_at),
    }


def order_json(order, *, with_notifications=True):
    data = {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "placedAt": iso(order.date_placed),
        "lines": [
            {"productId": line.product_id, "title": line.title, "quantity": line.quantity}
            for line in order.lines.all()
        ],
    }
    if with_notifications:
        data["notifications"] = [notification_json(n) for n in order.sms_notifications.all()]
    return data


def call_off_json(call_off):
    return {
        "notificationId": call_off.notification.pk if call_off.notification else None,
        "outcome": call_off.outcome,
        "detail": call_off.detail or None,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------------------------------------------------

@ensure_csrf_cookie
@api(["GET", "POST", "DELETE"], anonymous=True)
def session(request):
    """GET: who am I, plus a CSRF token. POST {username|email, password}: Django session login. DELETE: log out."""
    if request.method == "POST":
        body = json_body(request)
        user = authenticate(
            request, username=body.get("username") or body.get("email"), password=body.get("password")
        )
        if user is None:
            return error(400, "Invalid credentials.")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
    user = request.user
    return JsonResponse({
        "authenticated": user.is_authenticated,
        "userId": user.pk if user.is_authenticated else None,
        "isStaff": bool(user.is_authenticated and user.is_staff),
        "csrfToken": get_token(request),
    })


# ---------------------------------------------------------------------------------------------------------------------
# Flow 1: contact numbers
# ---------------------------------------------------------------------------------------------------------------------

@api(["GET", "POST"])
def contact_numbers(request):
    if request.method == "GET":
        numbers = ContactNumber.objects.filter(user=request.user)
        return JsonResponse({"contactNumbers": [contact_json(c) for c in numbers]})
    body = json_body(request)
    raw = body.get("phoneNumber")
    if not isinstance(raw, str):
        raise services.ServiceError(400, "phoneNumber (string) is required.")
    contact, created = services.register_contact_number(request.user, raw)
    logger.info("contact number %s registered for user %s", contact.pk, request.user.pk)
    return JsonResponse(contact_json(contact), status=201 if created else 200)


@api(["DELETE"])
def contact_number_detail(request, contact_number_id):
    contact = ContactNumber.objects.filter(pk=contact_number_id, user=request.user).first()
    if contact is None:
        return error(404, "Contact number not found.")
    result = services.remove_contact_number(contact)
    logger.info("contact number %s removed for user %s", contact_number_id, request.user.pk)
    return JsonResponse(
        {
            "contactNumberId": contact_number_id,
            "removed": True,
            "outcome": result.outcome,
            "followUpCallOffs": [call_off_json(c) for c in result.call_offs],
        },
        status=answer_status(result.outcome),
    )


# ---------------------------------------------------------------------------------------------------------------------
# Flow 2: orders
# ---------------------------------------------------------------------------------------------------------------------

@api(["POST"])
def orders(request):
    body = json_body(request)
    order = services.place_order(request.user, body.get("lines"))
    notification = _never_fails(services.notify_order_placed, order)
    data = order_json(order, with_notifications=False)
    data["notifications"] = [notification_json(notification)] if notification else []
    return JsonResponse(data, status=201)


@api(["POST"], staff=True)
def order_dispatch(request, order_id):
    order = own_order_or_404(request, order_id)
    result = services.dispatch_order_state(order)
    sent = _never_fails(services.dispatch_notifications, order) or []
    return JsonResponse({**order_json(result, with_notifications=False),
                         "notifications": [notification_json(n) for n in sent]})


@api(["POST"], staff=True)
def order_cancel(request, order_id):
    order = own_order_or_404(request, order_id)
    result = services.cancel_order_state(order)
    outcome = _never_fails(services.cancel_notifications, order) or {}
    call_off = outcome.get("follow_up_call_off")
    return JsonResponse({
        **order_json(result, with_notifications=False),
        "notifications": [notification_json(n) for n in outcome.get("notifications", [])],
        "followUpCallOff": call_off_json(call_off) if call_off else None,
    })


@api(["GET"])
def my_orders(request):
    user_orders = Order.objects.filter(user=request.user).order_by("-date_placed")
    for order in user_orders:
        _never_fails(services.refresh_order, order)
    return JsonResponse({"orders": [order_json(o) for o in user_orders]})


@api(["GET"])
def order_notifications(request, order_id):
    order = own_order_or_404(request, order_id)
    _never_fails(services.refresh_order, order)
    return JsonResponse({
        "orderId": order.pk,
        "status": order.status,
        "notifications": [notification_json(n) for n in order.sms_notifications.all()],
    })


def _never_fails(step, *args):
    """A messaging step must never fail the order operation around it. Its records say what happened."""
    try:
        return step(*args)
    except Exception as exc:  # deliberately broad: the order operation has already succeeded
        logger.error("SMS step %s failed with %s", step.__name__, type(exc).__name__)
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Flow 3: operator actions
# ---------------------------------------------------------------------------------------------------------------------

def notification_or_404(notification_id):
    notification = Notification.objects.select_related("order").filter(pk=notification_id).first()
    if notification is None:
        raise services.ServiceError(404, "Notification not found.")
    return notification


@api(["POST"], staff=True)
def notification_resend(request, notification_id):
    original = notification_or_404(notification_id)
    body = json_body(request)
    key = request.headers.get("Idempotency-Key") or body.get("idempotencyKey")
    if not isinstance(key, str):
        raise services.ServiceError(400, "An Idempotency-Key header (or idempotencyKey field) is required.")
    produced = services.resend(original, key)
    payload = notification_json(produced)
    return JsonResponse({**payload, "resendOf": original.pk}, status=answer_status(produced.outcome))


@api(["DELETE"], staff=True)
def notification_content(request, notification_id):
    notification = notification_or_404(notification_id)
    result = services.dispose_content(notification)
    notification.refresh_from_db()
    outcome = result.outcome if isinstance(result, ProviderAction) else "done"
    return JsonResponse(
        {"notificationId": notification.pk, "outcome": outcome, "notification": notification_json(notification)},
        status=answer_status(outcome),
    )


def _parse_instant(name, value):
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        raise services.ServiceError(400, "'%s' must be an ISO-8601 date-time." % name)
    if parsed.tzinfo is None:
        raise services.ServiceError(400, "'%s' must carry a UTC offset (e.g. 2026-09-28T00:00:00Z)." % name)
    return parsed


@api(["GET"], staff=True)
def reconciliation(request):
    start = _parse_instant("from", request.GET.get("from"))
    end = _parse_instant("to", request.GET.get("to"))
    return JsonResponse(services.reconcile(start, end))
