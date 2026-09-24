"""
JSON API for SMS order notifications.

Callers authenticate with the sandbox's own Django session login; the caller's
identity is always ``request.user``. Views run outside the sandbox's
ATOMIC_REQUESTS transaction so that a message's claim and each order change
are committed before the provider is called.
"""
import json
import logging
from datetime import timezone as dt_timezone
from functools import wraps

from django.db import transaction
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime

from . import services
from .errors import ProviderError
from .models import Notification
from .services import ApiProblem

logger = logging.getLogger(__name__)


def _error(status, code, message, **extra):
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def api_view(methods, staff_only=False):
    """Method check, session authentication, staff gate and error mapping."""
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = _error(405, 'method_not_allowed', 'Method not allowed.')
                response['Allow'] = ', '.join(methods)
                return response
            if not request.user.is_authenticated:
                return _error(401, 'not_authenticated', 'Log in first.')
            if staff_only and not request.user.is_staff:
                return _error(403, 'forbidden', 'This action is for staff only.')
            try:
                return func(request, *args, **kwargs)
            except ApiProblem as e:
                return _error(e.status_code, e.code, e.message)
            except ProviderError as e:
                return _error(e.status_code, e.code, e.message, outcomeUnknown=e.outcome_unknown)
        return transaction.non_atomic_requests(wrapper)
    return decorator


def _payload(request):
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        raise ApiProblem(400, 'invalid_json', 'The request body must be JSON.')


def _contact_json(contact):
    return {'contactNumberId': contact.pk, 'phoneNumber': contact.phone_number,
            'countryCode': contact.country_code or None, 'createdAt': contact.created_at.isoformat()}


# ---------------------------------------------------------------------------
# Shopper endpoints
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
def contact_numbers(request):
    if request.method == 'GET':
        return JsonResponse({'contactNumbers': [
            _contact_json(c) for c in services.active_contacts(request.user)]})
    payload = _payload(request)
    raw = payload.get('phoneNumber') if isinstance(payload, dict) else None
    contact, created = services.register_contact(request.user, raw)
    return JsonResponse(_contact_json(contact), status=201 if created else 200)


@api_view(['DELETE'])
def contact_number_detail(request, contact_number_id):
    return JsonResponse(services.delete_contact(request.user, contact_number_id))


@api_view(['POST'])
def orders(request):
    order, notifications = services.place_order(request, _payload(request))
    return JsonResponse(services.order_json(order, notifications), status=201)


@api_view(['GET'])
def my_orders(request):
    result = []
    refreshed = True
    for order in services.Order.objects.filter(user=request.user).order_by('-date_placed', '-pk'):
        notifications = list(order.sms_notifications.select_related('contact'))
        refreshed = services.refresh_unsettled(notifications) and refreshed
        result.append(services.order_json(order, notifications))
    return JsonResponse({'orders': result, 'providerStatusCurrent': refreshed})


@api_view(['GET'])
def order_notifications(request, order_id):
    order = services.Order.objects.filter(pk=order_id, user=request.user).first()
    if order is None:
        return _error(404, 'not_found', 'No such order.')
    notifications = list(order.sms_notifications.select_related('contact'))
    refreshed = services.refresh_unsettled(notifications)
    return JsonResponse({
        'orderId': order.pk,
        'orderStatus': order.status,
        'providerStatusCurrent': refreshed,
        'notifications': [services.notification_json(n) for n in notifications],
    })


# ---------------------------------------------------------------------------
# Operator endpoints
# ---------------------------------------------------------------------------

@api_view(['POST'], staff_only=True)
def dispatch_order(request, order_id):
    order, notifications = services.dispatch_order(order_id)
    return JsonResponse({
        'orderId': order.pk, 'status': order.status,
        'notifications': [services.notification_json(n) for n in notifications],
    })


@api_view(['POST'], staff_only=True)
def cancel_order(request, order_id):
    order, called_off, notifications = services.cancel_order(order_id)
    return JsonResponse({
        'orderId': order.pk, 'status': order.status,
        'followupsCalledOff': called_off,
        'allFollowupsCalledOff': all(
            c['cancelState'] == Notification.CANCEL_DONE for c in called_off),
        'notifications': [services.notification_json(n) for n in notifications],
    })


@api_view(['POST'], staff_only=True)
def resend_notification(request, notification_id):
    key = request.headers.get('Idempotency-Key')
    if key is None and request.body:
        payload = _payload(request)
        key = payload.get('idempotencyKey') if isinstance(payload, dict) else None
    notification, created = services.resend_notification(notification_id, key)
    body = services.notification_json(notification)
    body['replayed'] = not created
    return JsonResponse(body, status=201 if created else 200)


@api_view(['DELETE'], staff_only=True)
def notification_content(request, notification_id):
    notification = services.dispose_content(notification_id)
    return JsonResponse(services.notification_json(notification))


@api_view(['GET'], staff_only=True)
def reconciliation(request):
    start = _query_datetime(request, 'from')
    end = _query_datetime(request, 'to')
    if start >= end:
        return _error(400, 'invalid_range', '"from" must be before "to".')
    return JsonResponse(services.reconcile(start, end))


def _query_datetime(request, name):
    raw = request.GET.get(name)
    parsed = parse_datetime(raw) if raw else None
    if parsed is None:
        raise ApiProblem(400, 'invalid_range',
                         '"%s" must be an ISO-8601 date-time (encode "+" as %%2B).' % name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed
