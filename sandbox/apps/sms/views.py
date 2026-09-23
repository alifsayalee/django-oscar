"""HTTP API for SMS order notifications.

Plain Django JSON views, authenticated by the sandbox's own session login (`request.user`).
Operator actions (dispatch, cancel, resend, content disposal, reconciliation) require `is_staff`;
every other endpoint acts only on the caller's own data. Destination numbers are never returned
in a response body (a shopper's own numbers on the contact-numbers endpoint aside) and never logged.
"""
import functools
import json

from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from oscar.apps.order.models import Order

from . import services
from .models import ContactNumber, Notification
from .provider import ProviderError


# --- helpers -------------------------------------------------------------------------------

def _error(status, message):
    return JsonResponse({"error": message}, status=status)


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("request body must be valid JSON")
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    return data


def api_view(*, methods, staff=False):
    """Wrap a view: enforce method, session auth, optional is_staff, and map service errors."""

    def decorator(func):
        @csrf_exempt
        @functools.wraps(func)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return _error(405, "method not allowed")
            if not request.user.is_authenticated:
                return _error(401, "authentication required")
            if staff and not request.user.is_staff:
                return _error(403, "operator privileges required")
            try:
                return func(request, *args, **kwargs)
            except ValueError as exc:
                return _error(400, str(exc))
            except services.ServiceError as exc:
                return _error(exc.status_code, exc.message)
            except ProviderError as exc:
                return _error(exc.status_code, str(exc))

        return wrapper

    return decorator


def _order_or_404(pk):
    try:
        return Order.objects.get(pk=pk)
    except Order.DoesNotExist:
        return None


# --- serializers ---------------------------------------------------------------------------

def _dt(value):
    return value.isoformat() if value else None


def _provider_status_str(message):
    from .provider import _val  # local import to avoid confusion with SDK sentinels

    status = _val(message.status)
    return str(status) if status is not None else None


def _notification_dict(notification, *, full=False):
    data = {
        "notificationId": notification.pk,
        "kind": notification.kind,
        "outcome": notification.outcome,
        "providerStatus": notification.provider_status or None,
        "createdAt": _dt(notification.created_at),
        "contentDisposed": notification.content_disposed,
    }
    if full:
        data.update(
            {
                "orderId": notification.order_id,
                "providerSid": notification.provider_sid or None,
                "outcomeUnknown": notification.outcome_unknown,
                "errorCode": notification.error_code,
                "errorMessage": notification.error_message or None,
                "providerDateSent": _dt(notification.provider_date_sent),
                "providerDateCreated": _dt(notification.provider_date_created),
                "sourceNotificationId": notification.source_notification_id,
            }
        )
    return data


def _order_dict(order):
    notifications = order.sms_notifications.all()
    return {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "datePlaced": _dt(order.date_placed),
        "currency": order.currency,
        "totalInclTax": str(order.total_incl_tax),
        "numLines": order.num_lines,
        "notifications": [_notification_dict(n) for n in notifications],
    }


# --- contact numbers -----------------------------------------------------------------------

@api_view(methods={"POST", "GET"})
def contact_numbers(request):
    if request.method == "POST":
        body = _json_body(request)
        number = body.get("number") or body.get("phoneNumber") or body.get("e164")
        contact = services.register_contact_number(request.user, number)
        return JsonResponse(
            {"contactNumberId": contact.pk, "e164": contact.e164}, status=201
        )
    numbers = services.list_contact_numbers(request.user)
    return JsonResponse(
        {
            "contactNumbers": [
                {"contactNumberId": c.pk, "e164": c.e164, "createdAt": _dt(c.created_at)}
                for c in numbers
            ]
        }
    )


@api_view(methods={"DELETE"})
def contact_number_detail(request, contact_id):
    removed = services.delete_contact_number(request.user, contact_id)
    if not removed:
        return _error(404, "contact number not found")
    return JsonResponse({"deleted": True})


# --- orders --------------------------------------------------------------------------------

@api_view(methods={"POST"})
def create_order(request):
    body = _json_body(request)
    items = body.get("items") or body.get("lines")
    if not isinstance(items, list):
        return _error(400, "items must be a list of {product_id, quantity}")
    order, notification = services.place_order(request.user, items)
    payload = _order_dict(order)
    payload["placedNotification"] = (
        _notification_dict(notification) if notification is not None else None
    )
    return JsonResponse(payload, status=201)


@api_view(methods={"GET"})
def my_orders(request):
    orders = (
        Order.objects.filter(user=request.user)
        .prefetch_related("sms_notifications")
        .order_by("-date_placed")
    )
    return JsonResponse({"orders": [_order_dict(o) for o in orders]})


@api_view(methods={"POST"}, staff=True)
def dispatch_order(request, order_id):
    order = _order_or_404(order_id)
    if order is None:
        return _error(404, "order not found")
    order, changed = services.dispatch_order(order)
    return JsonResponse({"orderId": order.pk, "status": order.status, "changed": changed})


@api_view(methods={"POST"}, staff=True)
def cancel_order(request, order_id):
    order = _order_or_404(order_id)
    if order is None:
        return _error(404, "order not found")
    order, changed = services.cancel_order(order)
    return JsonResponse({"orderId": order.pk, "status": order.status, "changed": changed})


@api_view(methods={"GET"})
def order_notifications(request, order_id):
    order = _order_or_404(order_id)
    if order is None:
        return _error(404, "order not found")
    # Operators see any order; a shopper sees only their own.
    if not request.user.is_staff and order.user_id != request.user.id:
        return _error(404, "order not found")
    notifications = order.sms_notifications.all()
    for notification in notifications:
        services.refresh_outcome(notification)
    return JsonResponse(
        {
            "orderId": order.pk,
            "notifications": [_notification_dict(n, full=True) for n in notifications],
        }
    )


# --- operator notification actions ---------------------------------------------------------

def _notification_or_404(pk):
    try:
        return Notification.objects.get(pk=pk)
    except Notification.DoesNotExist:
        return None


@api_view(methods={"POST"}, staff=True)
def resend_notification(request, notification_id):
    source = _notification_or_404(notification_id)
    if source is None:
        return _error(404, "notification not found")
    body = _json_body(request)
    key = body.get("idempotencyKey") or body.get("idempotency_key")
    notification, sent = services.resend_notification(source, key)
    return JsonResponse(
        {
            "notificationId": notification.pk,
            "outcome": notification.outcome,
            "sent": sent,
        },
        status=201 if sent else 200,
    )


@api_view(methods={"DELETE"}, staff=True)
def dispose_notification_content(request, notification_id):
    notification = _notification_or_404(notification_id)
    if notification is None:
        return _error(404, "notification not found")
    notification = services.dispose_content(notification)
    return JsonResponse(
        {
            "notificationId": notification.pk,
            "contentDisposed": notification.content_disposed,
            "outcome": notification.outcome,
        }
    )


@api_view(methods={"GET"}, staff=True)
def reconciliation(request):
    raw_from = request.GET.get("from")
    raw_to = request.GET.get("to")
    if not raw_from or not raw_to:
        return _error(400, "both 'from' and 'to' ISO-8601 date-times are required")
    start = parse_datetime(raw_from)
    end = parse_datetime(raw_to)
    if start is None or end is None:
        return _error(400, "'from' and 'to' must be ISO-8601 date-times")
    if timezone.is_naive(start):
        start = timezone.make_aware(start)
    if timezone.is_naive(end):
        end = timezone.make_aware(end)
    if end <= start:
        return _error(400, "'to' must be after 'from'")

    report = services.reconcile(start, end)
    return JsonResponse(
        {
            "fromNumber": report["from_number"],
            "window": {"from": _dt(start), "to": _dt(end)},
            "truncated": report["truncated"],
            "summary": {
                "matched": len(report["matched"]),
                "providerOnly": len(report["provider_only"]),
                "localOnly": len(report["local_only"]),
                "unsettled": len(report["unsettled"]),
            },
            "matched": [
                {
                    "notificationId": n.pk,
                    "sid": n.provider_sid,
                    "outcome": n.outcome,
                    "sentAt": _dt(sent),
                }
                for (n, _msg, sent) in report["matched"]
            ],
            "providerOnly": [
                {"sid": sid, "status": _provider_status_str(msg), "sentAt": _dt(sent)}
                for (sid, msg, sent) in report["provider_only"]
            ],
            "localOnly": [
                {"notificationId": n.pk, "sid": n.provider_sid, "outcome": n.outcome}
                for n in report["local_only"]
            ],
            "unsettled": [
                {"notificationId": n.pk, "kind": n.kind, "outcome": n.outcome}
                for n in report["unsettled"]
            ],
        }
    )
