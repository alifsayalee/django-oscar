"""
JSON API for order SMS notifications.

Authentication is Django's session login (the sandbox's own). Views that call
the messaging provider opt out of ATOMIC_REQUESTS so each claim row commits
before the provider is called and survives a failure after it.
"""

import json
import logging
from datetime import timedelta
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from oscar.core.loading import get_model

from . import services
from .models import ContactNumber, Notification
from .provider import ProviderError
from .services import ServiceError

logger = logging.getLogger("apps.order_notifications")

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")

MAX_LINES = 50
MAX_QUANTITY = 99
MAX_RECONCILIATION_RANGE = timedelta(days=93)


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def error(status, message, code=None, **extra):
    payload = {"error": {"message": message}}
    if code:
        payload["error"]["code"] = code
    payload["error"].update(extra)
    return JsonResponse(payload, status=status)


def api_login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error(401, "Authentication required.", "not_authenticated")
        return view(request, *args, **kwargs)
    return wrapper


def staff_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error(401, "Authentication required.", "not_authenticated")
        if not request.user.is_staff:
            return error(403, "This action is restricted to staff.", "forbidden")
        return view(request, *args, **kwargs)
    return wrapper


def handles_service_errors(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ServiceError as e:
            return error(e.http_status, e.message, e.code, **e.extra)
        except ProviderError as e:
            return error(e.http_status, "Messaging provider error.", "provider_unavailable",
                         outcomeUnknown=e.outcome_unknown)
    return wrapper


def read_json(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as e:
        raise ServiceError(400, "Request body must be JSON.", code="invalid_json") from e
    if not isinstance(data, dict):
        raise ServiceError(400, "Request body must be a JSON object.", code="invalid_json")
    return data


def _iso(value):
    return value.isoformat() if value else None


def serialize_contact(number):
    return {
        "contactNumberId": number.pk,
        "phoneNumber": number.e164,  # the owner's own number, shown only to them
        "countryCode": number.country_code or None,
        "createdAt": _iso(number.created_at),
    }


def serialize_notification(n, stale=frozenset()):
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "status": n.status,
        "providerStatus": n.provider_status or None,
        "providerSid": n.provider_sid,
        "errorCode": n.error_code,
        "errorDetail": n.error_detail or None,
        "to": n.contact_number.masked() if n.contact_number else None,
        "body": None if n.content_disposed_at else n.body,
        "contentDisposed": n.content_disposed_at is not None,
        "contentDisposedAt": _iso(n.content_disposed_at),
        "scheduledFor": _iso(n.scheduled_for),
        "sentAt": _iso(n.provider_date_sent),
        "resendOf": n.resend_of_id,
        "createdAt": _iso(n.created_at),
        "lastCheckedAt": _iso(n.last_checked_at),
        "stale": n.pk in stale,
    }


def serialize_order(order, notifications=None, stale=frozenset()):
    data = {
        "orderId": order.pk,
        "number": str(order.number),
        "status": order.status,
        "currency": order.currency,
        "totalInclTax": str(order.total_incl_tax),
        "placedAt": _iso(order.date_placed),
        "lines": [
            {"productId": line.product_id, "title": line.title, "quantity": line.quantity}
            for line in order.lines.all()
        ],
    }
    if notifications is not None:
        data["notifications"] = [serialize_notification(n, stale) for n in notifications]
    return data


def _notifications_for(order):
    return list(order.sms_notifications.select_related("contact_number").order_by("created_at", "id"))


def get_or_404(queryset, **lookup):
    """JSON 404 (the site's HTML 404 page is not an API answer)."""
    obj = queryset.filter(**lookup).first()
    if obj is None:
        raise ServiceError(404, "Not found.", code="not_found")
    return obj


def _order_for(request, order_id):
    """Staff see any order; a shopper sees only their own (others are 404)."""
    qs = Order.objects.all() if request.user.is_staff else Order.objects.filter(user=request.user)
    return get_or_404(qs, pk=order_id)


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

@require_GET
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({"csrfToken": get_token(request)})


@require_http_methods(["GET", "POST", "DELETE"])
@handles_service_errors
def session(request):
    if request.method == "POST":
        data = read_json(request)
        user = authenticate(request, username=data.get("username") or data.get("email"),
                            password=data.get("password"))
        if user is None:
            return error(401, "Invalid credentials.", "invalid_credentials")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
        return JsonResponse({"authenticated": False})
    if not request.user.is_authenticated:
        return JsonResponse({"authenticated": False})
    return JsonResponse({"authenticated": True, "userId": request.user.pk, "isStaff": request.user.is_staff,
                         "csrfToken": get_token(request)})


# --------------------------------------------------------------------------
# Flow 1 - contact numbers
# --------------------------------------------------------------------------

@transaction.non_atomic_requests
@require_http_methods(["GET", "POST"])
@api_login_required
@handles_service_errors
def contact_numbers(request):
    if request.method == "GET":
        numbers = ContactNumber.objects.filter(user=request.user)
        return JsonResponse({"contactNumbers": [serialize_contact(n) for n in numbers]})
    data = read_json(request)
    raw = data.get("phoneNumber")
    if not isinstance(raw, str):
        return error(400, "phoneNumber (string) is required.", "invalid_request")
    number, created = services.register_contact_number(request.user, raw)
    body = serialize_contact(number)
    body["created"] = created
    return JsonResponse(body, status=201 if created else 200)


@transaction.non_atomic_requests
@require_http_methods(["DELETE"])
@api_login_required
@handles_service_errors
def contact_number_detail(request, contact_number_id):
    number = get_or_404(ContactNumber.objects.all(), pk=contact_number_id, user=request.user)
    services.delete_contact_number(number)
    return HttpResponse(status=204)


# --------------------------------------------------------------------------
# Flow 2 - orders
# --------------------------------------------------------------------------

def _parse_items(data):
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_LINES:
        raise ServiceError(400, f"items must be a non-empty list of at most {MAX_LINES} entries.",
                           code="invalid_request")
    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise ServiceError(400, "Each item must be an object.", code="invalid_request")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise ServiceError(400, "productId must be a positive integer.", code="invalid_request")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise ServiceError(400, f"quantity must be an integer between 1 and {MAX_QUANTITY}.",
                               code="invalid_request")
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    products = Product.objects.in_bulk(list(quantities))
    missing = [pid for pid in quantities if pid not in products or not products[pid].is_public]
    if missing:
        raise ServiceError(404, "Unknown catalogue item(s).", code="unknown_product", extra={"productIds": missing})
    parents = [pid for pid in quantities if products[pid].is_parent]
    if parents:
        raise ServiceError(400, "Parent products cannot be bought directly; order a variant.",
                           code="parent_product", extra={"productIds": parents})
    return [(products[pid], qty) for pid, qty in quantities.items()]


@transaction.non_atomic_requests
@require_POST
@api_login_required
@handles_service_errors
def orders(request):
    items = _parse_items(read_json(request))
    order, notification = services.place_order(request, request.user, items)
    body = serialize_order(order, [notification] if notification else [])
    body["notified"] = notification is not None
    return JsonResponse(body, status=201)


@transaction.non_atomic_requests
@require_GET
@api_login_required
@handles_service_errors
def my_orders(request):
    user_orders = list(Order.objects.filter(user=request.user).order_by("-date_placed", "-id")
                       .prefetch_related("lines"))
    all_notifications = []
    per_order = {}
    for order in user_orders:
        per_order[order.pk] = _notifications_for(order)
        all_notifications.extend(per_order[order.pk])
    stale = services.refresh_notifications(all_notifications)
    return JsonResponse({"orders": [serialize_order(o, per_order[o.pk], stale) for o in user_orders]})


@transaction.non_atomic_requests
@require_GET
@api_login_required
@handles_service_errors
def order_notifications(request, order_id):
    order = _order_for(request, order_id)
    notifications = _notifications_for(order)
    stale = services.refresh_notifications(notifications)
    return JsonResponse({"orderId": order.pk, "orderStatus": order.status,
                         "notifications": [serialize_notification(n, stale) for n in notifications]})


def _transition_response(result):
    body = serialize_order(result.order, _notifications_for(result.order))
    body["changed"] = result.changed
    body["followupCancellations"] = result.followup_cancellations
    return JsonResponse(body)


@transaction.non_atomic_requests
@require_POST
@staff_required
@handles_service_errors
def dispatch_order(request, order_id):
    order = get_or_404(Order.objects.all(), pk=order_id)
    return _transition_response(services.dispatch_order(order, request.user))


@transaction.non_atomic_requests
@require_POST
@staff_required
@handles_service_errors
def cancel_order(request, order_id):
    order = get_or_404(Order.objects.all(), pk=order_id)
    return _transition_response(services.cancel_order(order, request.user))


# --------------------------------------------------------------------------
# Flow 3 - operator actions
# --------------------------------------------------------------------------

@transaction.non_atomic_requests
@require_POST
@staff_required
@handles_service_errors
def resend_notification(request, notification_id):
    source = get_or_404(Notification.objects.select_related("order", "contact_number"),
                               pk=notification_id)
    data = read_json(request)
    key = data.get("idempotencyKey") or request.headers.get("Idempotency-Key")
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        return error(400, "idempotencyKey (1-128 chars) is required.", "invalid_request")
    notification, created = services.resend(source, key.strip())
    body = serialize_notification(notification)
    body["replayed"] = not created
    return JsonResponse(body, status=201 if created else 200)


@transaction.non_atomic_requests
@require_http_methods(["DELETE"])
@staff_required
@handles_service_errors
def notification_content(request, notification_id):
    notification = get_or_404(Notification.objects.select_related("contact_number"), pk=notification_id)
    notification = services.dispose_content(notification)
    return JsonResponse(serialize_notification(notification))


def _parse_instant(name, value):
    if not value:
        raise ServiceError(400, f"'{name}' is required (ISO-8601 date-time with offset).", code="invalid_request")
    parsed = parse_datetime(value)
    if parsed is None and " " in value:  # an unencoded '+' in the query string arrives as a space
        parsed = parse_datetime(value.replace(" ", "+"))
    if parsed is None or parsed.tzinfo is None:
        raise ServiceError(400, f"'{name}' must be an ISO-8601 date-time with a UTC offset, e.g. "
                                "2026-09-23T00:00:00Z.", code="invalid_request")
    return parsed


@transaction.non_atomic_requests
@require_GET
@staff_required
@handles_service_errors
def reconciliation(request):
    start = _parse_instant("from", request.GET.get("from"))
    end = _parse_instant("to", request.GET.get("to"))
    if end <= start:
        return error(400, "'to' must be after 'from'.", "invalid_request")
    if end - start > MAX_RECONCILIATION_RANGE:
        return error(400, f"The range may span at most {MAX_RECONCILIATION_RANGE.days} days.", "invalid_request")
    return JsonResponse(services.reconcile(start, end))
