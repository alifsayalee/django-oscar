"""
JSON API for SMS order notifications, authenticated by the sandbox's own
Django session login. Operator actions require ``is_staff``; everything else
acts only on the caller's own data.

Views opt out of ATOMIC_REQUESTS: a provider-write claim must be committed
before the provider is called, not rolled back with the request.
"""
import json
from functools import wraps

from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from . import services
from .models import Notification
from .provider import ProviderError
from .services import ServiceError


def error(status, message, **extra):
    return JsonResponse({'error': message, **extra}, status=status)


def api_view(methods, *, staff=False):
    def decorator(view):
        @wraps(view)
        @transaction.non_atomic_requests
        @require_http_methods(methods)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return error(401, 'Authentication required.')
            if staff and not request.user.is_staff:
                return error(403, 'Staff only.')
            try:
                return view(request, *args, **kwargs)
            except ServiceError as exc:
                return error(exc.status_code, exc.message, **exc.extra)
            except ProviderError as exc:
                return error(exc.status_code, exc.message, outcomeUnknown=exc.outcome_unknown)
        return wrapper
    return decorator


def json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ServiceError(400, 'Request body must be JSON.') from exc
    if not isinstance(data, dict):
        raise ServiceError(400, 'Request body must be a JSON object.')
    return data


def answer_status(outcome):
    """The one place a write outcome becomes the HTTP status for the write the caller asked for."""
    match outcome:
        case 'done':
            return 200
        case 'pending' | 'sending':
            return 202
        case 'failed' | 'needs_review':
            return 409
        case _:
            return 504


def notification_for_staff(notification_id):
    notification = Notification.objects.select_related(
        'send_write', 'order', 'contact_number').filter(pk=notification_id).first()
    if notification is None:
        raise ServiceError(404, 'Notification not found.')
    return notification


# ---------------------------------------------------------------- contact numbers

@api_view(['GET', 'POST'])
def contact_numbers(request):
    if request.method == 'GET':
        return JsonResponse({'contactNumbers': [
            services.serialize_contact(c) for c in services.active_numbers(request.user)]})
    data = json_body(request)
    raw = data.get('phoneNumber')
    if not isinstance(raw, str):
        raise ServiceError(400, 'phoneNumber is required.')
    contact, created = services.register_contact_number(request.user, raw, data.get('countryCode'))
    return JsonResponse(services.serialize_contact(contact), status=201 if created else 200)


@api_view(['DELETE'])
def contact_number_detail(request, contact_number_id):
    services.remove_contact_number(request.user, contact_number_id)
    return HttpResponse(status=204)


# ---------------------------------------------------------------- orders

@api_view(['POST'])
def orders(request):
    data = json_body(request)
    order, notification = services.place_order(request.user, request, data.get('items'))
    body = services.serialize_order(order, [notification] if notification else [])
    body['notification'] = services.serialize_notification(notification) if notification else None
    return JsonResponse(body, status=201)


@api_view(['GET'])
def my_orders(request):
    user_orders = request.user.orders.order_by('-date_placed', '-id')[:50]
    return JsonResponse({'orders': [
        services.serialize_order(order, services.order_notifications(order))
        for order in user_orders]})


@api_view(['GET'])
def order_notifications(request, order_id):
    order = services.get_order_for(request.user, order_id, staff_ok=True)
    return JsonResponse({
        'orderId': order.pk,
        'notifications': [services.serialize_notification(n)
                          for n in services.order_notifications(order)]})


@api_view(['POST'], staff=True)
def dispatch_order(request, order_id):
    order, sent = services.dispatch_order(order_id)
    return JsonResponse({
        'orderId': order.pk,
        'status': order.status,
        'notifications': [services.serialize_notification(n) for n in sent],
    })


@api_view(['POST'], staff=True)
def cancel_order(request, order_id):
    order, cancelled, follow_ups = services.cancel_order(order_id)
    return JsonResponse({
        'orderId': order.pk,
        'status': order.status,
        'notification': services.serialize_notification(cancelled) if cancelled else None,
        'followUpCancellation': [
            {'notificationId': n.pk, 'outcome': outcome} for n, outcome in follow_ups],
    })


# ---------------------------------------------------------------- operator: notifications

@api_view(['POST'], staff=True)
def resend_notification(request, notification_id):
    data = json_body(request)
    key = request.headers.get('Idempotency-Key') or data.get('idempotencyKey')
    if not isinstance(key, str) or not 8 <= len(key) <= 255:
        raise ServiceError(400, 'An idempotency key of 8-255 characters is required '
                                '(Idempotency-Key header or idempotencyKey field).')
    original = notification_for_staff(notification_id)
    notification = services.resend(original, key)
    if notification is None:
        raise ServiceError(409, 'The re-send could not be recorded.')
    outcome = notification.send_write.outcome
    return JsonResponse({
        'notificationId': notification.pk,
        'resendOf': original.pk,
        'status': outcome,
        'notification': services.serialize_notification(notification),
    }, status=answer_status(outcome))


@api_view(['DELETE'], staff=True)
def notification_content(request, notification_id):
    notification = notification_for_staff(notification_id)
    write = services.redact(notification)
    notification.refresh_from_db()
    return JsonResponse({
        'notificationId': notification.pk,
        'status': write.outcome,
        'notification': services.serialize_notification(notification),
    }, status=answer_status(write.outcome))


@api_view(['GET'], staff=True)
def reconciliation(request):
    start = parse_datetime(request.GET.get('from', '') or '')
    end = parse_datetime(request.GET.get('to', '') or '')
    if start is None or end is None or start.tzinfo is None or end.tzinfo is None:
        raise ServiceError(400, 'from and to must be ISO-8601 date-times with a UTC offset.')
    if end <= start:
        raise ServiceError(400, 'to must be after from.')
    return JsonResponse(services.reconcile(start, end))
