"""
JSON API for order SMS notifications, mounted under ``/api/``.

Callers authenticate with Django's session login (``POST /api/login``, or the
storefront's own login page) and send the ``csrftoken`` cookie back as the
``X-CSRFToken`` header on unsafe methods. Operator endpoints require
``is_staff``. Views that call the provider opt out of ``ATOMIC_REQUESTS`` so
that a write's claim is committed before the provider is asked to act.
"""
import functools
import json

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from oscar.core.loading import get_model

from . import services
from .models import ContactNumber, Notification, Outcome

Order = get_model('order', 'Order')


# -- plumbing ----------------------------------------------------------------------------------

def _error(status, message, **extra):
    return JsonResponse({'error': message, **extra}, status=status)


def api_view(methods, *, staff=False):
    """Method check, session authentication, staff check, JSON errors - in that order."""
    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return _error(405, 'Method not allowed.')
            if not request.user.is_authenticated:
                return _error(401, 'Authentication required.')
            if staff and not request.user.is_staff:
                return _error(403, 'This action is for operators only.')
            try:
                return view(request, *args, **kwargs)
            except services.ServiceError as e:
                return _error(e.status_code, e.message, **e.extra)
        return transaction.non_atomic_requests(wrapper)
    return decorator


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise services.ServiceError(400, 'The request body must be JSON.')
    if not isinstance(data, dict):
        raise services.ServiceError(400, 'The request body must be a JSON object.')
    return data


def _iso(value):
    return value.isoformat() if value else None


# An outcome becomes the caller's HTTP status here and nowhere else.
def status_for(outcome):
    match outcome:
        case Outcome.DONE:
            return 200
        case Outcome.PENDING | Outcome.SENDING:
            return 202          # accepted by the provider, not finished
        case Outcome.FAILED | Outcome.NEEDS_REVIEW:
            return 409
        case _:
            return 504          # may have happened: never reported as "not done"


def notification_json(notification):
    return {
        'notificationId': notification.pk,
        'orderId': notification.order_id,
        'kind': notification.kind,
        'outcome': notification.outcome,
        'providerStatus': notification.provider_status or None,
        'providerSid': notification.provider_sid or None,
        'providerErrorCode': notification.provider_error_code,
        'failureReason': notification.failure_reason or None,
        'scheduledFor': _iso(notification.scheduled_for),
        'sentAt': _iso(notification.provider_time),
        'lastCheckedAt': _iso(notification.last_checked_at),
        'body': notification.body or None,
        'contentDisposedAt': _iso(notification.content_disposed_at),
        'resendOf': notification.resend_of_id,
        'contactNumberId': notification.contact_number_id,
        'createdAt': _iso(notification.created_at),
    }


def _summary(notifications):
    counts: dict[str, int] = {}
    for notification in notifications:
        counts[notification.outcome] = counts.get(notification.outcome, 0) + 1
    return counts


def order_json(order, notifications=None):
    if notifications is None:
        notifications = list(order.sms_notifications.all())
    return {
        'orderId': order.pk,
        'number': order.number,
        'status': order.status,
        'totalInclTax': str(order.total_incl_tax),
        'currency': order.currency,
        'placedAt': _iso(order.date_placed),
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'linePriceInclTax': str(line.line_price_incl_tax),
        } for line in order.lines.all()],
        'notifications': [notification_json(n) for n in notifications],
        'notificationSummary': _summary(notifications),
    }


def contact_json(contact):
    return {
        'contactNumberId': contact.pk,
        'phoneNumber': contact.phone_number,
        'countryCode': contact.country_code or None,
        'createdAt': _iso(contact.created_at),
    }


def action_json(action):
    return {
        'notificationId': action.notification_id,
        'action': action.kind,
        'outcome': action.outcome,
        'providerStatus': action.provider_status or None,
        'detail': action.detail or None,
    }


# -- session ---------------------------------------------------------------------------------

@ensure_csrf_cookie
@require_http_methods(['GET'])
def csrf(request):
    """Sets the ``csrftoken`` cookie; send it back as ``X-CSRFToken``."""
    return JsonResponse({'authenticated': request.user.is_authenticated})


@require_http_methods(['POST'])
def session_login(request):
    try:
        data = _json_body(request)
    except services.ServiceError as e:
        return _error(e.status_code, e.message)
    user = authenticate(request, username=data.get('username') or data.get('email'),
                        password=data.get('password'))
    if user is None:
        return _error(401, 'Invalid credentials.')
    login(request, user)
    return JsonResponse({'userId': user.pk, 'isStaff': user.is_staff})


@require_http_methods(['POST'])
def session_logout(request):
    logout(request)
    return JsonResponse({'authenticated': False})


# -- contact numbers ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
def contact_numbers(request):
    if request.method == 'GET':
        contacts = ContactNumber.objects.filter(user=request.user)
        return JsonResponse({'contactNumbers': [contact_json(c) for c in contacts]})
    data = _json_body(request)
    contact, created = services.register_contact_number(request.user, data.get('phoneNumber'))
    return JsonResponse(contact_json(contact), status=201 if created else 200)


@api_view(['DELETE'])
def contact_number_detail(request, contact_number_id):
    call_offs = services.delete_contact_number(request.user, contact_number_id)
    return JsonResponse({
        'contactNumberId': contact_number_id,
        'deleted': True,
        'scheduledMessagesCalledOff': [action_json(a) for a in call_offs],
    })


# -- orders ------------------------------------------------------------------------------------

@api_view(['POST'])
def orders(request):
    data = _json_body(request)
    order, notification = services.place_order(request.user, data.get('items'))
    body = order_json(order)
    body['notification'] = notification_json(notification) if notification else None
    return JsonResponse(body, status=201)


@api_view(['POST'], staff=True)
def dispatch_order(request, order_id):
    order, dispatched, follow_up = services.dispatch_order(order_id)
    body = order_json(order)
    body['dispatchNotification'] = notification_json(dispatched) if dispatched else None
    body['followUpNotification'] = notification_json(follow_up) if follow_up else None
    return JsonResponse(body)


@api_view(['POST'], staff=True)
def cancel_order(request, order_id):
    order, cancelled, call_offs = services.cancel_order(order_id)
    body = order_json(order)
    body['cancellationNotification'] = notification_json(cancelled) if cancelled else None
    body['followUpCallOffs'] = [action_json(a) for a in call_offs]
    return JsonResponse(body)


@api_view(['GET'])
def my_orders(request):
    result = []
    for order in Order.objects.filter(user=request.user).order_by('-date_placed')[:50]:
        notifications = services.refresh_many(list(order.sms_notifications.all()))
        result.append(order_json(order, notifications))
    return JsonResponse({'orders': result})


@api_view(['GET'])
def order_notifications(request, order_id):
    order = Order.objects.filter(pk=order_id).first()
    # Operators may look at any order; a shopper only at their own (and learns nothing of others').
    if order is None or (order.user_id != request.user.pk and not request.user.is_staff):
        return _error(404, 'No such order.')
    notifications = services.refresh_many(list(order.sms_notifications.all()))
    return JsonResponse({
        'orderId': order.pk,
        'orderStatus': order.status,
        'notifications': [notification_json(n) for n in notifications],
    })


# -- operator actions --------------------------------------------------------------------------

@api_view(['POST'], staff=True)
def resend_notification(request, notification_id):
    data = _json_body(request)
    key = request.headers.get('Idempotency-Key') or data.get('idempotencyKey')
    notification = services.resend(notification_id, key)
    body = notification_json(notification)
    return JsonResponse(body, status=status_for(notification.outcome))


@api_view(['DELETE'], staff=True)
def notification_content(request, notification_id):
    notification, outcome = services.dispose_content(notification_id)
    notification = Notification.objects.get(pk=notification.pk)
    body = notification_json(notification)
    body['contentDisposal'] = outcome
    return JsonResponse(body, status=status_for(outcome))


def _parse_when(value, name):
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        raise services.ServiceError(400, '"%s" must be an ISO-8601 date-time.' % name)
    if parsed.tzinfo is None:
        raise services.ServiceError(400, '"%s" must carry a UTC offset (e.g. Z).' % name)
    return parsed


def _mask(number):
    return '...%s' % number[-2:] if number else None


def _provider_json(record):
    return {
        'providerSid': record.sid,
        'providerStatus': record.status,
        'sentAt': _iso(record.date_sent),
        'to': _mask(record.to),
        'errorCode': record.error_code,
    }


@api_view(['GET'], staff=True)
def reconciliation(request):
    start = _parse_when(request.GET.get('from'), 'from')
    end = _parse_when(request.GET.get('to'), 'to')
    report = services.reconcile(start, end)
    matched = [{
        'notificationId': n.pk,
        'orderId': n.order_id,
        'kind': n.kind,
        'outcome': n.outcome,
        'appStatus': n.provider_status or None,
        **_provider_json(record),
    } for n, record in report['matched']]
    return JsonResponse({
        'from': start.isoformat(),
        'to': end.isoformat(),
        'fromNumber': _mask(services.get_gateway().from_number),
        'counts': {
            'provider': len(report['provider_records']),
            'matched': len(matched),
            'appOnly': len(report['local_only']),
            'providerOnly': len(report['provider_only']),
            'unsettled': len(report['unsettled']),
            'neverSent': len(report['never_sent']),
        },
        'matched': matched,
        'appOnly': [notification_json(n) for n in report['local_only']],
        'providerOnly': [_provider_json(r) for r in report['provider_only']],
        'unsettled': [notification_json(n) for n in report['unsettled']],
        'neverSent': [notification_json(n) for n in report['never_sent']],
        'statusUpdated': report['status_changed'],
    })
