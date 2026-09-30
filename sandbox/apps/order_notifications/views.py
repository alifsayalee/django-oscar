"""
JSON API for order SMS notifications.

Callers authenticate with Django's session login (the sandbox's own login, or
``POST /api/session/login``); CSRF protection applies to every unsafe method.
Operator actions require ``is_staff``; everything else acts only on the
caller's own data.
"""
import json
import logging
from datetime import timedelta, timezone as dt_timezone
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from oscar.core.loading import get_class, get_model

from . import services
from .models import ContactNumber, Notification
from .services import ServiceError

logger = logging.getLogger(__name__)

Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
Selector = get_class('partner.strategy', 'Selector')

MAX_RECONCILIATION_RANGE = timedelta(days=366)


def _error(status, message, **extra):
    return JsonResponse({'error': message, **extra}, status=status)


def api_view(staff_only=False):
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return _error(401, 'Authentication required.')
            if staff_only and not request.user.is_staff:
                return _error(403, 'Staff only.')
            try:
                return view(request, *args, **kwargs)
            except ServiceError as e:
                return _error(e.status_code, e.message, **e.extra)
        return wrapper
    return decorator


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ServiceError(400, 'Request body must be JSON.')
    if not isinstance(data, dict):
        raise ServiceError(400, 'Request body must be a JSON object.')
    return data


def _iso(value):
    return value.isoformat() if value else None


def serialize_contact(contact):
    return {
        'contactNumberId': contact.pk,
        'phoneNumber': contact.phone_number,
        'createdAt': _iso(contact.created_at),
    }


def serialize_notification(n):
    return {
        'notificationId': n.pk,
        'orderId': n.order_id,
        'kind': n.kind,
        'content': None if n.content_disposed_at else n.body,
        'contentDisposedAt': _iso(n.content_disposed_at),
        'contactNumberId': n.contact_number_id,
        'sendState': n.send_state,
        'providerSid': n.provider_sid,
        'providerStatus': n.provider_status or None,
        'errorCode': n.error_code,
        'errorMessage': n.error_message or None,
        'scheduledFor': _iso(n.send_at),
        'dateSent': _iso(n.provider_date_sent),
        'lastCheckedAt': _iso(n.last_checked_at),
        'cancelNotConfirmed': n.cancel_failed,
        'resendOf': n.resend_of_id,
        'createdAt': _iso(n.created_at),
    }


def serialize_order(order, notifications=None):
    data = {
        'orderId': order.pk,
        'number': str(order.number),
        'status': order.status,
        'currency': order.currency,
        'totalInclTax': str(order.total_incl_tax),
        'datePlaced': _iso(order.date_placed),
        'lines': [
            {'productId': line.product_id, 'title': line.title, 'quantity': line.quantity}
            for line in order.lines.all()
        ],
    }
    if notifications is not None:
        data['notifications'] = [serialize_notification(n) for n in notifications]
    return data


def _order_notifications(order):
    notifications = list(order.sms_notifications.all())
    services.refresh_statuses(notifications)
    return notifications


# --- Session -----------------------------------------------------------------

@require_GET
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({'csrfToken': get_token(request)})


@require_POST
def session_login(request):
    try:
        data = _json_body(request)
    except ServiceError as e:
        return _error(e.status_code, e.message)
    username, password = data.get('username'), data.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        return _error(400, 'username and password are required.')
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return _error(401, 'Invalid credentials.')
    login(request, user)
    return JsonResponse({'userId': user.pk, 'isStaff': user.is_staff, 'csrfToken': get_token(request)})


@require_POST
def session_logout(request):
    logout(request)
    return JsonResponse({'loggedOut': True})


# --- Catalogue (so an order can be placed through the API alone) -------------

@require_GET
@api_view()
def products(request):
    strategy = Selector().strategy(request=request, user=request.user)
    items = Product.objects.browsable().filter(is_public=True).order_by('pk')[:200]
    result = []
    for product in items:
        for candidate in ([product] if not product.is_parent else list(product.children.filter(is_public=True))):
            info = strategy.fetch_for_product(candidate)
            if not info.availability.is_available_to_buy:
                continue
            result.append({
                'productId': candidate.pk,
                'title': candidate.get_title(),
                'upc': candidate.upc or None,
                'price': str(info.price.incl_tax) if info.price.exists and info.price.is_tax_known else None,
                'currency': info.price.currency if info.price.exists else None,
            })
    return JsonResponse({'products': result})


# --- Flow 1: contact numbers -----------------------------------------------

@require_http_methods(['GET', 'POST'])
@api_view()
def contact_numbers(request):
    if request.method == 'GET':
        contacts = ContactNumber.objects.filter(user=request.user)
        return JsonResponse({'contactNumbers': [serialize_contact(c) for c in contacts]})
    data = _json_body(request)
    contact, created = services.register_contact_number(
        request.user, data.get('phoneNumber'), data.get('countryCode'))
    return JsonResponse(serialize_contact(contact), status=201 if created else 200)


@require_http_methods(['DELETE'])
@api_view()
def contact_number_detail(request, contact_number_id):
    services.remove_contact_number(request.user, contact_number_id)
    return HttpResponse(status=204)


# --- Flow 2: orders ------------------------------------------------------------

@require_POST
@api_view()
def orders(request):
    data = _json_body(request)
    order = services.place_order(request.user, request, data.get('lines'))
    return JsonResponse(serialize_order(order, _order_notifications(order)), status=201)


@require_POST
@api_view(staff_only=True)
def order_dispatch(request, order_id):
    order = services.dispatch_order(order_id)
    return JsonResponse(serialize_order(order, _order_notifications(order)))


@require_POST
@api_view(staff_only=True)
def order_cancel(request, order_id):
    order = services.cancel_order(order_id)
    return JsonResponse(serialize_order(order, _order_notifications(order)))


@require_GET
@api_view()
def my_orders(request):
    user_orders = list(Order.objects.filter(user=request.user).order_by('-date_placed')[:50])
    notifications = list(Notification.objects.filter(order__in=user_orders))
    services.refresh_statuses(notifications)
    by_order = {}
    for n in notifications:
        by_order.setdefault(n.order_id, []).append(n)
    return JsonResponse({'orders': [serialize_order(o, by_order.get(o.pk, [])) for o in user_orders]})


@require_GET
@api_view()
def order_notifications(request, order_id):
    order = Order.objects.filter(pk=order_id, user=request.user).first()
    if order is None:
        return _error(404, 'Order not found.')
    notifications = _order_notifications(order)
    return JsonResponse({
        'orderId': order.pk,
        'status': order.status,
        'notifications': [serialize_notification(n) for n in notifications],
    })


# --- Flow 3: operator actions ------------------------------------------------

@require_POST
@api_view(staff_only=True)
def notification_resend(request, notification_id):
    data = _json_body(request)
    key = request.headers.get('Idempotency-Key') or data.get('idempotencyKey')
    notification, created = services.resend_notification(notification_id, key)
    body = serialize_notification(notification)
    body['replayed'] = not created
    return JsonResponse(body, status=201 if created else 200)


@require_http_methods(['DELETE'])
@api_view(staff_only=True)
def notification_content(request, notification_id):
    notification = services.dispose_content(notification_id)
    return JsonResponse(serialize_notification(notification))


def _parse_instant(value, name):
    if not value:
        raise ServiceError(400, '%s is required (ISO-8601 date-time).' % name)
    # An unencoded "+01:00" offset arrives as " 01:00" in a query string.
    parsed = parse_datetime(value.strip().replace(' ', '+'))
    if parsed is None:
        raise ServiceError(400, '%s must be an ISO-8601 date-time.' % name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


@require_GET
@api_view(staff_only=True)
def reconciliation(request):
    start = _parse_instant(request.GET.get('from'), 'from')
    end = _parse_instant(request.GET.get('to'), 'to')
    if start > end:
        return _error(400, '"from" must not be after "to".')
    if end - start > MAX_RECONCILIATION_RANGE:
        return _error(400, 'The range may cover at most 366 days.')
    return JsonResponse(services.reconcile(start, end))
