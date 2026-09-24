"""
JSON endpoints under /api/. Callers authenticate with the sandbox's own Django
session login; the caller's identity is always ``request.user``.

Views run outside the sandbox's ATOMIC_REQUESTS transaction: a notification
claim must be committed before the provider is called, and a failed message
must never roll back the order it is about.
"""
import functools
import json
import logging
from datetime import timezone as dt_timezone

from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import services
from . import twilio_gateway as tg
from .models import Notification

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


def _error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def api_view(methods, staff=False):
    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return _error(401, "Authentication required: sign in through the sandbox's login page.")
            if staff and not request.user.is_staff:
                return _error(403, "This action is restricted to staff operators.")
            try:
                return view(request, *args, **kwargs)
            except services.ApiProblem as exc:
                return _error(exc.status_code, exc.message, **exc.extra)
            except tg.ProviderError as exc:
                return _error(exc.status_code, exc.message, outcomeUnknown=exc.outcome_unknown)
        return transaction.non_atomic_requests(require_http_methods(methods)(wrapper))
    return decorator


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise services.ApiProblem(400, "Request body must be JSON.")
    if not isinstance(data, dict):
        raise services.ApiProblem(400, "Request body must be a JSON object.")
    return data


def _iso(value):
    return value.isoformat() if value else None


def _parse_instant(value, name):
    parsed = parse_datetime(value or "")
    if parsed is None:
        raise services.ApiProblem(400, "'%s' must be an ISO-8601 date-time." % name)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _contact_json(contact):
    return {"contactNumberId": contact.pk, "phoneNumber": contact.phone_number,
            "countryCode": contact.country_code or None, "createdAt": _iso(contact.created_at)}


def _notification_json(n):
    if n is None:
        return None
    return {
        "notificationId": n.pk,
        "kind": n.kind,
        "orderId": n.order_id,
        "outcome": n.outcome,
        "providerSid": n.provider_sid,
        "providerStatus": n.provider_status or None,
        "errorCode": n.error_code,
        "errorMessage": n.error_message or None,
        "failureReason": n.failure_reason or None,
        "scheduledFor": _iso(n.scheduled_for),
        "sentAt": _iso(n.provider_date_sent),
        "createdAt": _iso(n.claimed_at),
        "lastCheckedAt": _iso(n.last_checked_at),
        "resendOf": n.resend_of_id,
        "cancellation": n.cancel_outcome or None,
        "text": n.text or None,
        "contentDisposed": n.content_disposed_at is not None,
        "contentDisposedAt": _iso(n.content_disposed_at),
    }


def _order_json(order, notifications=None):
    data = {"orderId": order.pk, "number": order.number, "status": order.status,
            "total": str(order.total_incl_tax), "currency": order.currency,
            "placedAt": _iso(order.date_placed)}
    if notifications is not None:
        data["notifications"] = [_notification_json(n) for n in notifications]
    return data


# --------------------------------------------------------------------------
# Flow 1 — contact numbers
# --------------------------------------------------------------------------


@api_view(["GET", "POST"])
def contact_numbers(request):
    if request.method == "GET":
        return JsonResponse({"contactNumbers": [_contact_json(c) for c in services.active_numbers(request.user)]})
    data = _json_body(request)
    contact, created = services.register_number(request.user, data.get("phoneNumber"), data.get("countryCode"))
    return JsonResponse(_contact_json(contact), status=201 if created else 200)


@api_view(["DELETE"])
def contact_number_detail(request, contact_number_id):
    services.remove_number(request.user, contact_number_id)
    return HttpResponse(status=204)


# --------------------------------------------------------------------------
# Flow 2 — orders and their messages
# --------------------------------------------------------------------------


@api_view(["POST"])
def orders(request):
    data = _json_body(request)
    order, notification = services.place_order(request.user, request, data.get("lines"))
    return JsonResponse({**_order_json(order), "notification": _notification_json(notification)}, status=201)


@api_view(["POST"], staff=True)
def dispatch_order(request, order_id):
    order, dispatched, followup = services.dispatch_order(order_id)
    return JsonResponse({**_order_json(order), "notification": _notification_json(dispatched),
                         "followUp": _notification_json(followup)})


@api_view(["POST"], staff=True)
def cancel_order(request, order_id):
    order, cancelled, followup = services.cancel_order(order_id)
    return JsonResponse({**_order_json(order), "notification": _notification_json(cancelled),
                         "followUp": _notification_json(followup)})


#: Provider re-reads per listing request, so a long order history cannot turn one GET into hundreds of calls.
MAX_REFRESHES_PER_REQUEST = 20


def _refreshed(notifications):
    budget = MAX_REFRESHES_PER_REQUEST
    for n in notifications:
        if budget <= 0:
            break
        if n.outcome not in services.FINAL_OUTCOMES or n.kind == Notification.FOLLOWUP:
            services.refresh(n)
            budget -= 1
    return notifications


@api_view(["GET"])
def my_orders(request):
    user_orders = list(Order.objects.filter(user=request.user).order_by("-date_placed", "-id"))
    notes = list(Notification.objects.filter(order__in=user_orders).select_related("order", "contact_number"))
    _refreshed(notes)
    by_order: dict[int, list[Notification]] = {}
    for n in notes:
        by_order.setdefault(n.order_id, []).append(n)
    return JsonResponse({"orders": [_order_json(o, by_order.get(o.pk, [])) for o in user_orders]})


@api_view(["GET"])
def order_notifications(request, order_id):
    order = Order.objects.filter(pk=order_id, user=request.user).first()
    if order is None:
        return _error(404, "Order not found.")
    notes = list(order.sms_notifications.select_related("order", "contact_number"))
    _refreshed(notes)
    return JsonResponse({"orderId": order.pk, "notifications": [_notification_json(n) for n in notes]})


# --------------------------------------------------------------------------
# Flow 3 — operator actions
# --------------------------------------------------------------------------


@api_view(["POST"], staff=True)
def resend_notification(request, notification_id):
    notification, repeated = services.resend(notification_id, request.headers.get("Idempotency-Key", "").strip())
    body = _notification_json(notification)
    body["repeatedRequest"] = repeated
    if notification.outcome == Notification.FAILED and not notification.provider_sid:
        return JsonResponse({**body, "error": "The message could not be sent."}, status=502)
    return JsonResponse(body, status=200 if repeated else 201)


@api_view(["DELETE"], staff=True)
def notification_content(request, notification_id):
    notification = services.dispose_content(notification_id)
    return JsonResponse(_notification_json(notification))


def _report_entry(n=None, view=None):
    entry: dict[str, object] = {}
    if n is not None:
        entry.update(notificationId=n.pk, orderId=n.order_id, kind=n.kind, localOutcome=n.outcome,
                     localProviderStatus=n.provider_status or None, createdAt=_iso(n.claimed_at))
    if view is not None:
        entry.update(providerSid=view.sid, providerStatus=view.status, providerOutcome=view.outcome,
                     providerTime=_iso(view.provider_time), errorCode=view.error_code)
    elif n is not None:
        entry.update(providerSid=n.provider_sid, providerTime=_iso(n.provider_time))
    return entry


@api_view(["GET"], staff=True)
def reconciliation(request):
    start = _parse_instant(request.GET.get("from"), "from")
    end = _parse_instant(request.GET.get("to"), "to")
    report = services.reconcile(start, end)
    matched = [dict(_report_entry(n, v), statusAgrees=(n.provider_status == v.status)) for n, v in report["matched"]]
    return JsonResponse({
        "from": _iso(start),
        "to": _iso(end),
        "sendingNumber": services.get_gateway().config.from_number,
        "summary": {
            "matched": len(matched),
            "providerOnly": len(report["providerOnly"]),
            "localOnly": len(report["localOnly"]),
            "unsettled": len(report["unsettled"]),
            "notSent": len(report["notSent"]),
            "inboundRecordsIgnored": report["inboundIgnored"],
        },
        "matched": matched,
        "providerOnly": [_report_entry(view=v) for v in report["providerOnly"]],
        "localOnly": [_report_entry(n) for n in report["localOnly"]],
        "unsettled": [_report_entry(n) for n in report["unsettled"]],
        "notSent": [dict(_report_entry(n), failureReason=n.failure_reason or None) for n in report["notSent"]],
    })

