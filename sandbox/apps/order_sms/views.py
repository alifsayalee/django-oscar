"""
JSON API for order SMS notifications.

Callers authenticate with Django's own session login (``POST /api/session``); state-changing
requests carry the CSRF token from ``GET /api/csrf`` in an ``X-CSRFToken`` header. Operator actions
require ``is_staff``. Views manage their own transactions (``ATOMIC_REQUESTS`` is on for the site):
a notification claim must be committed before the provider is called, and must survive whatever
happens after.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone as dt_timezone
from functools import wraps
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import gateway, notifications, orders, reconciliation
from .gateway import ProviderError
from .models import ContactNumber, Notification

log = logging.getLogger('apps.order_sms')

Order = get_model('order', 'Order')

View = Callable[..., HttpResponse]


def error(status: int, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({'error': message, **extra}, status=status)


def api_view(methods: list[str], *, staff: bool = False) -> Callable[[View], View]:
    """JSON view: method check, session authentication, optional staff check, own transactions."""
    def decorator(view: View) -> View:
        @wraps(view)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if not request.user.is_authenticated:
                return error(401, 'Authentication required.')
            if staff and not request.user.is_staff:
                return error(403, 'Operator (staff) access required.')
            try:
                return view(request, *args, **kwargs)
            except orders.OrderRequestError as exc:
                return error(exc.status_code, exc.message)
            except notifications.NotificationConflict as exc:
                return error(409, str(exc))
            except ProviderError as exc:
                return error(exc.status_code, exc.message, outcomeUnknown=exc.outcome_unknown)
        return transaction.non_atomic_requests(require_http_methods(methods)(wrapped))
    return decorator


def _uid(request: HttpRequest) -> int:
    """The authenticated caller's id - every shopper-scoped query filters on it."""
    uid = request.user.pk
    if not isinstance(uid, int):
        raise orders.OrderRequestError(401, 'Authentication required.')
    return uid


def json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise orders.OrderRequestError(400, 'Request body must be JSON.') from None
    if not isinstance(data, dict):
        raise orders.OrderRequestError(400, 'Request body must be a JSON object.')
    return data


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def notification_json(n: Notification) -> dict[str, Any]:
    return {
        'notificationId': n.pk,
        'orderId': n.order_id,
        'kind': n.kind,
        'outcome': n.outcome,
        'providerStatus': n.provider_status or None,
        'messageSid': n.message_sid,
        'errorCode': n.error_code,
        'errorDetail': n.error_detail or None,
        'to': n.contact.masked,
        'body': n.body or None,
        'scheduledFor': _iso(n.scheduled_for),
        'callOff': n.cancel_outcome or None,
        'contentDisposal': n.content_disposal or None,
        'contentDisposedAt': _iso(n.content_disposed_at),
        'resendOf': n.resend_of_id,
        'claimedAt': n.claimed_at.isoformat(),
        'providerDateSent': _iso(n.provider_date_sent),
        'lastCheckedAt': _iso(n.last_checked_at),
    }


def order_json(order: Any, notes: list[Notification]) -> dict[str, Any]:
    return {
        'orderId': order.pk,
        'number': str(order.number),
        'status': order.status,
        'currency': order.currency,
        'total': str(order.total_incl_tax),
        'placedAt': _iso(order.date_placed),
        'notifications': [notification_json(n) for n in notes],
    }


def _refreshed(qs: Any) -> list[Notification]:
    return [notifications.refresh(n) for n in qs.select_related('contact')]


# --------------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------------

@transaction.non_atomic_requests
@require_http_methods(['GET'])
def csrf(request: HttpRequest) -> HttpResponse:
    return JsonResponse({'csrfToken': get_token(request)})


@transaction.non_atomic_requests
@require_http_methods(['POST', 'DELETE'])
def session(request: HttpRequest) -> HttpResponse:
    if request.method == 'DELETE':
        logout(request)
        return HttpResponse(status=204)
    try:
        data = json_body(request)
    except orders.OrderRequestError as exc:
        return error(exc.status_code, exc.message)
    username, password = data.get('username'), data.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        return error(400, '"username" and "password" are required.')
    # Oscar's EmailBackend accepts an email address; ModelBackend a username.
    user = authenticate(request, username=username, password=password)
    if user is None:
        return error(401, 'Invalid credentials.')
    login(request, user)
    return JsonResponse({'userId': user.pk, 'isStaff': user.is_staff})


# --------------------------------------------------------------------------------------------
# Contact numbers
# --------------------------------------------------------------------------------------------

def contact_json(c: ContactNumber) -> dict[str, Any]:
    return {'contactNumberId': c.pk, 'phoneNumber': c.phone_number,
            'countryCode': c.country_code or None, 'createdAt': c.created_at.isoformat()}


@api_view(['GET', 'POST'])
def contact_numbers(request: HttpRequest) -> HttpResponse:
    if request.method == 'GET':
        mine = ContactNumber.objects.filter(user_id=_uid(request), deleted_at__isnull=True)
        return JsonResponse({'contactNumbers': [contact_json(c) for c in mine]})
    raw = json_body(request).get('phoneNumber')
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 32:
        return error(400, '"phoneNumber" is required.')
    # Rejected here, not when a message later fails: the provider decides what is usable, and
    # what is stored is its canonical form.
    number, country = gateway.lookup_number(raw.strip())
    try:
        with transaction.atomic():
            contact = ContactNumber.objects.create(
                user_id=_uid(request), phone_number=number, country_code=country)
    except IntegrityError:
        contact = ContactNumber.objects.get(
            user_id=_uid(request), phone_number=number, deleted_at__isnull=True)
        return JsonResponse(contact_json(contact), status=200)
    log.info('user %s registered contact number %s', request.user.pk, contact.pk)
    return JsonResponse(contact_json(contact), status=201)


@api_view(['DELETE'])
def contact_number_detail(request: HttpRequest, contact_number_id: int) -> HttpResponse:
    with transaction.atomic():
        updated = ContactNumber.objects.filter(
            pk=contact_number_id, user_id=_uid(request), deleted_at__isnull=True,
        ).update(deleted_at=timezone.now())
    if not updated:
        return error(404, 'Contact number not found.')
    # Nothing may be sent to it again - including a follow-up already queued with the provider.
    notifications.call_off_for_contact(ContactNumber.objects.get(pk=contact_number_id))
    log.info('user %s removed contact number %s', request.user.pk, contact_number_id)
    return HttpResponse(status=204)


# --------------------------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------------------------

@api_view(['POST'])
def place_order(request: HttpRequest) -> HttpResponse:
    data = json_body(request)
    lines = orders.parse_lines(data.get('lines'))
    order, notification = orders.place_order(request, lines, data.get('shippingAddress'))
    body = order_json(order, [notification] if notification else [])
    return JsonResponse(body, status=201)


def _order_for(request: HttpRequest, order_id: int) -> Any:
    qs = Order.objects.all() if request.user.is_staff else Order.objects.filter(user_id=_uid(request))
    order = qs.filter(pk=order_id).first()
    if order is None:
        raise orders.OrderRequestError(404, 'Order not found.')
    return order


@api_view(['POST'], staff=True)
def dispatch_order(request: HttpRequest, order_id: int) -> HttpResponse:
    order = _order_for(request, order_id)
    notes = orders.dispatch(order)
    order.refresh_from_db()
    return JsonResponse(order_json(order, notes))


@api_view(['POST'], staff=True)
def cancel_order(request: HttpRequest, order_id: int) -> HttpResponse:
    order = _order_for(request, order_id)
    notes = orders.cancel(order)
    order.refresh_from_db()
    return JsonResponse(order_json(order, notes))


@api_view(['GET'])
def my_orders(request: HttpRequest) -> HttpResponse:
    mine = Order.objects.filter(user_id=_uid(request)).order_by('-date_placed')
    return JsonResponse({'orders': [
        order_json(o, _refreshed(Notification.objects.filter(order=o))) for o in mine]})


@api_view(['GET'])
def order_notifications(request: HttpRequest, order_id: int) -> HttpResponse:
    order = _order_for(request, order_id)
    notes = _refreshed(Notification.objects.filter(order=order))
    return JsonResponse({'orderId': order.pk, 'notifications': [notification_json(n) for n in notes]})


# --------------------------------------------------------------------------------------------
# Operator actions
# --------------------------------------------------------------------------------------------

def _notification(notification_id: int) -> Notification:
    n = Notification.objects.select_related('contact', 'order').filter(pk=notification_id).first()
    if n is None:
        raise orders.OrderRequestError(404, 'Notification not found.')
    return n


@api_view(['POST'], staff=True)
def resend_notification(request: HttpRequest, notification_id: int) -> HttpResponse:
    key = request.headers.get('Idempotency-Key') or json_body(request).get('idempotencyKey')
    if not isinstance(key, str) or not 8 <= len(key) <= 200:
        return error(400, 'An idempotency key (8-200 chars) is required, as the "Idempotency-Key" '
                          'header or "idempotencyKey" in the body.')
    n, newly_sent = notifications.resend(_notification(notification_id), key)
    body = notification_json(n)
    body['replayed'] = not newly_sent
    if n.outcome == Notification.UNKNOWN:
        return JsonResponse({**body, 'outcomeUnknown': True,
                             'error': 'The provider did not confirm the send; it may have gone '
                                      'out. It is being checked - do not resend under a new key.'},
                            status=504)
    if n.outcome == Notification.FAILED:
        return JsonResponse({**body, 'error': 'The provider did not accept the message.'},
                            status=502)
    # Accepted by the provider; delivery is reported by the notifications endpoint.
    return JsonResponse(body, status=201 if newly_sent else 200)


@api_view(['DELETE'], staff=True)
def notification_content(request: HttpRequest, notification_id: int) -> HttpResponse:
    n = notifications.dispose_content(_notification(notification_id))
    body = notification_json(n)
    if n.content_disposal == Notification.DONE:
        return JsonResponse(body)
    if n.content_disposal == Notification.UNKNOWN:
        return JsonResponse({**body, 'outcomeUnknown': True,
                             'error': 'Could not confirm the text was erased at the provider; '
                                      'repeat the request.'}, status=504)
    return JsonResponse({**body, 'error': 'The provider did not erase the text; repeat the request '
                                          'once the message has finished sending.'}, status=502)


def _parse_instant(value: str | None, name: str) -> datetime:
    parsed = parse_datetime(value) if value else None
    if parsed is None:
        raise orders.OrderRequestError(400, f'"{name}" must be an ISO-8601 date-time.')
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


@api_view(['GET'], staff=True)
def notification_reconciliation(request: HttpRequest) -> HttpResponse:
    start = _parse_instant(request.GET.get('from'), 'from')
    end = _parse_instant(request.GET.get('to'), 'to')
    if end <= start:
        return error(400, '"to" must be after "from".')
    return JsonResponse(reconciliation.reconcile(start, end))
