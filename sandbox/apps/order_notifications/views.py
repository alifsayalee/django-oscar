"""
JSON API for SMS order notifications.

Callers authenticate with Django's session login; operator actions require
``is_staff``. Order and messaging work are separate steps: a message that
cannot be sent never fails the order operation that triggered it.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import partial, wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie

from . import orders, provider, reconciliation, services
from .models import ContactNumber, Notification

logger = logging.getLogger(__name__)

MAX_ORDER_LINES = 50
MAX_QUANTITY = 100
MAX_RECONCILIATION_RANGE = timedelta(days=366)
_NUMBER_INPUT = re.compile(r'^[0-9+()\-. ]{4,32}$')


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def error(status: int, message: str, **extra) -> JsonResponse:
    return JsonResponse({'error': message, **extra}, status=status)


class BadRequest(Exception):
    pass


def api(methods: tuple[str, ...], *, staff: bool = False, auth: bool = True):
    """Method, authentication and operator checks, JSON errors, and no request-wide transaction.

    Views manage their own transactions so a claim is committed before Twilio is called.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return error(405, 'Method not allowed.', allowed=list(methods))
            if auth and not request.user.is_authenticated:
                return error(401, 'Sign in first (POST /api/auth/login).')
            if staff and not request.user.is_staff:
                return error(403, 'This action is for operators only.')
            try:
                return view(request, *args, **kwargs)
            except BadRequest as e:
                return error(400, str(e))
            except services.Conflict as e:
                return error(409, str(e))
            except provider.ProviderError as e:
                return error(e.status_code, e.message, outcomeUnknown=e.outcome_unknown)
        return transaction.non_atomic_requests(wrapper)
    return decorator


def body_json(request) -> dict:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as e:
        raise BadRequest('Request body must be JSON.') from e
    if not isinstance(data, dict):
        raise BadRequest('Request body must be a JSON object.')
    return data


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def contact_json(contact: ContactNumber) -> dict:
    return {
        'contactNumberId': contact.pk,
        'phoneNumber': contact.phone_number,
        'countryCode': contact.country_code,
        'createdAt': iso(contact.created_at),
    }


def notification_json(n: Notification) -> dict:
    return {
        'notificationId': n.pk,
        'orderId': n.order_id,
        'kind': n.kind,
        'to': reconciliation.mask(n.contact_number.phone_number),
        'body': n.body if n.content_state == Notification.CONTENT_RETAINED else None,
        'outcome': n.outcome,
        'status': n.provider_status or None,
        'providerSid': n.provider_sid,
        'errorCode': n.provider_error_code,
        'errorMessage': n.provider_error_message or None,
        'claimedAt': iso(n.claimed_at),
        'scheduledFor': iso(n.scheduled_for),
        'providerTime': iso(n.provider_time),
        'lastCheckedAt': iso(n.last_checked_at),
        'cancelState': n.cancel_state,
        'contentState': n.content_state,
        'resendOf': n.resend_of_id,
    }


def order_json(order, notifications: list[Notification]) -> dict:
    return {
        'orderId': order.pk,
        'number': order.number,
        'status': order.status,
        'currency': order.currency,
        'totalInclTax': str(order.total_incl_tax),
        'placedAt': iso(order.date_placed),
        'lines': [
            {'productId': line.product_id, 'title': line.title, 'quantity': line.quantity}
            for line in order.lines.all()
        ],
        'notifications': [notification_json(n) for n in notifications],
    }


def _notifications_for(order_ids) -> dict[int, list[Notification]]:
    grouped: dict[int, list[Notification]] = {}
    for n in Notification.objects.filter(order_id__in=order_ids).select_related(
            'contact_number', 'order'):
        grouped.setdefault(n.order_id, []).append(n)
    return grouped


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

@ensure_csrf_cookie
@api(('GET',), auth=False)
def session(request):
    user = request.user
    return JsonResponse({
        'authenticated': user.is_authenticated,
        'username': user.get_username() if user.is_authenticated else None,
        'isStaff': bool(user.is_authenticated and user.is_staff),
        'csrfToken': get_token(request),
    })


@api(('POST',), auth=False)
def session_login(request):
    data = body_json(request)
    username, password = data.get('username'), data.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        raise BadRequest('"username" and "password" are required.')
    user = authenticate(request, username=username, password=password)
    if user is None:
        return error(401, 'Invalid credentials.')
    login(request, user)
    return JsonResponse({'username': user.get_username(), 'isStaff': user.is_staff,
                         'csrfToken': get_token(request)})


@api(('POST',), auth=False)
def session_logout(request):
    logout(request)
    return JsonResponse({'authenticated': False})


# --------------------------------------------------------------------------
# Flow 1 — contact numbers
# --------------------------------------------------------------------------

@api(('GET', 'POST'))
def contact_numbers(request):
    if request.method == 'GET':
        return JsonResponse({'contactNumbers': [
            contact_json(c) for c in ContactNumber.objects.active().filter(user=request.user)]})

    raw = body_json(request).get('phoneNumber')
    if not isinstance(raw, str) or not _NUMBER_INPUT.match(raw.strip()):
        raise BadRequest('"phoneNumber" must be a phone number, ideally in E.164 form (+15551234567).')
    try:
        canonical = provider.lookup_number(raw.strip())
    except provider.NumberNotUsable:
        return error(422, 'The SMS provider does not recognise this as a usable phone number.')
    with transaction.atomic():
        existing = ContactNumber.objects.active().filter(
            user=request.user, phone_number=canonical.phone_number).first()
        if existing is not None:
            return JsonResponse(contact_json(existing), status=200)
        contact = ContactNumber.objects.create(
            user=request.user, phone_number=canonical.phone_number,
            country_code=canonical.country_code)
    logger.info('contact number #%s registered for user #%s', contact.pk, request.user.pk)
    return JsonResponse(contact_json(contact), status=201)


@api(('DELETE',))
def contact_number_detail(request, contact_number_id: int):
    contact = ContactNumber.objects.active().filter(
        pk=contact_number_id, user=request.user).first()
    if contact is None:
        return error(404, 'No such contact number.')
    ContactNumber.objects.filter(pk=contact.pk, removed_at__isnull=True).update(
        removed_at=timezone.now())
    # Anything still scheduled to this number must not go out.
    services.call_off_scheduled_to(contact)
    logger.info('contact number #%s removed', contact.pk)
    return HttpResponse(status=204)


# --------------------------------------------------------------------------
# Flow 2 — orders
# --------------------------------------------------------------------------

def _parse_lines(data: dict) -> list[orders.LineRequest]:
    lines = data.get('lines')
    if not isinstance(lines, list) or not lines or len(lines) > MAX_ORDER_LINES:
        raise BadRequest(f'"lines" must be a list of 1-{MAX_ORDER_LINES} items.')
    parsed = []
    for line in lines:
        if not isinstance(line, dict):
            raise BadRequest('Each line must be an object {"productId", "quantity"}.')
        product_id, quantity = line.get('productId'), line.get('quantity', 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise BadRequest('"productId" must be an integer catalogue item id.')
        if not isinstance(quantity, int) or isinstance(quantity, bool) \
                or not 1 <= quantity <= MAX_QUANTITY:
            raise BadRequest(f'"quantity" must be an integer from 1 to {MAX_QUANTITY}.')
        parsed.append(orders.LineRequest(product_id=product_id, quantity=quantity))
    return parsed


@api(('POST',))
def order_create(request):
    lines = _parse_lines(body_json(request))
    try:
        order = orders.place_order(request.user, lines, request=request)
    except orders.OrderRequestInvalid as e:
        return error(422, str(e))
    notification = services.contained(
        'order placed', lambda: services.notify(order, Notification.KIND_ORDER_PLACED))
    return JsonResponse({
        'orderId': order.pk,
        'number': order.number,
        'status': order.status,
        'notification': notification_json(notification) if notification else None,
    }, status=201)


def _operator_transition(order_id: int, new_status: str):
    try:
        return orders.transition(order_id, new_status), None
    except orders.Order.DoesNotExist:
        return None, error(404, 'No such order.')
    except orders.TransitionRefused as e:
        return None, error(409, str(e))


@api(('POST',), staff=True)
def order_dispatch(request, order_id: int):
    order, failure = _operator_transition(order_id, orders.STATUS_DISPATCHED)
    if failure:
        return failure
    dispatched = services.contained(
        'order dispatched', lambda: services.notify(order, Notification.KIND_ORDER_DISPATCHED))
    send_at = timezone.now() + timedelta(hours=settings.SMS_FOLLOWUP_DELAY_HOURS)
    followup = services.contained('delivery follow-up', lambda: services.notify(
        order, Notification.KIND_DELIVERY_FOLLOWUP, send_at=send_at))
    if followup is not None:
        # A cancel that ran while the follow-up was being scheduled could not call it off.
        order.refresh_from_db(fields=['status'])
        if order.status == orders.STATUS_CANCELLED:
            followup = services.contained('call off', partial(services.call_off, followup)) or followup
    return JsonResponse({
        'orderId': order.pk,
        'status': order.status,
        'notifications': [notification_json(n) for n in (dispatched, followup) if n],
    })


@api(('POST',), staff=True)
def order_cancel(request, order_id: int):
    order, failure = _operator_transition(order_id, orders.STATUS_CANCELLED)
    if failure:
        return failure
    # First make sure nothing about the delivery goes out, then tell the shopper.
    services.contained('call off follow-ups', lambda: services.call_off_followups(order))
    cancelled = services.contained(
        'order cancelled', lambda: services.notify(order, Notification.KIND_ORDER_CANCELLED))
    notifications = list(order.sms_notifications.select_related('contact_number').all())
    return JsonResponse({
        'orderId': order.pk,
        'status': order.status,
        'notification': notification_json(cancelled) if cancelled else None,
        'notifications': [notification_json(n) for n in notifications],
    })


@api(('GET',))
def my_orders(request):
    user_orders = list(orders.Order.objects.filter(user=request.user)
                       .prefetch_related('lines').order_by('-date_placed'))
    grouped = _notifications_for([o.pk for o in user_orders])
    services.sweep([n for ns in grouped.values() for n in ns])
    return JsonResponse({'orders': [order_json(o, grouped.get(o.pk, [])) for o in user_orders]})


@api(('GET',))
def order_notifications(request, order_id: int):
    order = orders.Order.objects.filter(pk=order_id).first()
    # Shoppers see only their own orders; operators may inspect any order.
    if order is None or (order.user_id != request.user.pk and not request.user.is_staff):
        return error(404, 'No such order.')
    notifications = _notifications_for([order.pk]).get(order.pk, [])
    services.sweep(notifications)
    return JsonResponse({'orderId': order.pk, 'status': order.status,
                         'notifications': [notification_json(n) for n in notifications]})


# --------------------------------------------------------------------------
# Flow 3 — operator actions
# --------------------------------------------------------------------------

def _notification(notification_id: int) -> Notification | None:
    return Notification.objects.select_related('contact_number', 'order').filter(
        pk=notification_id).first()


@api(('POST',), staff=True)
def notification_resend(request, notification_id: int):
    original = _notification(notification_id)
    if original is None:
        return error(404, 'No such notification.')
    key = request.headers.get('Idempotency-Key') or body_json(request).get('idempotencyKey')
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise BadRequest('An idempotency key is required (Idempotency-Key header or '
                         '"idempotencyKey"), at most 200 characters.')
    notification, created = services.resend(original, key.strip())
    return JsonResponse({**notification_json(notification), 'notificationId': notification.pk,
                         'replayed': not created},
                        status=201 if created else 200)


@api(('DELETE',), staff=True)
def notification_content(request, notification_id: int):
    notification = _notification(notification_id)
    if notification is None:
        return error(404, 'No such notification.')
    notification = services.dispose_content(notification)
    return JsonResponse(notification_json(notification))


def _parse_instant(name: str, value: str | None) -> datetime:
    if not value:
        raise BadRequest(f'"{name}" is required (ISO-8601 date-time).')
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00').replace(' ', '+'))
    except ValueError as e:
        raise BadRequest(f'"{name}" must be an ISO-8601 date-time.') from e
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt_timezone.utc)
    return moment


@api(('GET',), staff=True)
def notification_reconciliation(request):
    start = _parse_instant('from', request.GET.get('from'))
    end = _parse_instant('to', request.GET.get('to'))
    if end <= start:
        raise BadRequest('"to" must be after "from".')
    if end - start > MAX_RECONCILIATION_RANGE:
        raise BadRequest('The range may be at most 366 days.')
    return JsonResponse(reconciliation.reconcile(start, end))
