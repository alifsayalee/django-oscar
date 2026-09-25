"""
JSON API for order SMS notifications.

Callers authenticate with Django's session login (``/api/session`` or any
other sandbox login). Operator actions require ``is_staff``; everything else
acts only on the caller's own data. Views run outside ``ATOMIC_REQUESTS`` so a
notification claim is committed before the provider is called.
"""
import functools
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from oscar.core.loading import get_model

from . import orders as order_ops
from .errors import InvalidRequest, NotFound, NotificationError
from .gateway import get_gateway
from .models import ContactNumber, SmsNotification
from .services import NotificationService, notification_json, resend_reference

logger = logging.getLogger('apps.sms_notifications')
Order = get_model('order', 'Order')

View = Callable[..., HttpResponse]
MAX_ORDERS_LISTED = 50


def error(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({'error': code, 'message': message, **extra}, status=status)


def api(methods: tuple[str, ...], *, staff: bool = False, login_required: bool = True) -> Callable[[View], View]:
    def decorate(view: View) -> View:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if request.method not in methods:
                response = error(405, 'method_not_allowed', 'Method not allowed.')
                response['Allow'] = ', '.join(methods)
                return response
            if login_required and not shopper(request).is_authenticated:
                return error(401, 'not_authenticated', 'Log in first (POST /api/session).')
            if staff and not shopper(request).is_staff:
                return error(403, 'forbidden', 'This action is restricted to staff operators.')
            try:
                return view(request, *args, **kwargs)
            except NotificationError as e:
                extra: dict[str, Any] = {'outcomeUnknown': e.outcome_unknown} if e.outcome_unknown else {}
                return error(e.status_code, e.code, e.message, **extra)
        return transaction.non_atomic_requests(wrapper)
    return decorate


def json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as e:
        raise InvalidRequest('Request body must be JSON.') from e
    if not isinstance(data, dict):
        raise InvalidRequest('Request body must be a JSON object.')
    return data


def shopper(request: HttpRequest) -> Any:
    """The authenticated caller (the ``api`` decorator has already rejected anonymous requests)."""
    return request.user


def service() -> NotificationService:
    return NotificationService()


# -- Session -------------------------------------------------------------------------------

@api(('GET', 'POST', 'DELETE'), login_required=False)
def session_view(request: HttpRequest) -> HttpResponse:
    if request.method == 'POST':
        data = json_body(request)
        identifier, password = data.get('username') or data.get('email'), data.get('password')
        if not isinstance(identifier, str) or not isinstance(password, str):
            raise InvalidRequest('"username" (or "email") and "password" are required.')
        user = authenticate(request, username=identifier, password=password) or authenticate(
            request, email=identifier, password=password)
        if user is None:
            return error(401, 'invalid_credentials', 'Invalid credentials.')
        login(request, user)
    elif request.method == 'DELETE':
        logout(request)
    current: Any = shopper(request)
    return JsonResponse({
        'authenticated': current.is_authenticated,
        'userId': current.pk if current.is_authenticated else None,
        'isStaff': bool(current.is_authenticated and current.is_staff),
        'csrfToken': get_token(request),
    })


# -- Contact numbers -----------------------------------------------------------------------

def contact_json(contact: ContactNumber) -> dict[str, Any]:
    return {'contactNumberId': contact.pk, 'phoneNumber': contact.phone_number,
            'countryCode': contact.country_code or None, 'createdAt': contact.date_created.isoformat()}


@api(('GET', 'POST'))
def contact_numbers(request: HttpRequest) -> HttpResponse:
    if request.method == 'GET':
        contacts = ContactNumber.objects.filter(user=shopper(request))
        return JsonResponse({'contactNumbers': [contact_json(c) for c in contacts]})
    raw = json_body(request).get('phoneNumber')
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 40:
        raise InvalidRequest('"phoneNumber" is required.')
    canonical, country = get_gateway().canonical_number(raw.strip())
    try:
        with transaction.atomic():
            contact = ContactNumber.objects.create(user=shopper(request), phone_number=canonical,
                                                   country_code=country)
        status = 201
    except IntegrityError:
        contact = ContactNumber.objects.get(user=shopper(request), phone_number=canonical)
        status = 200
    return JsonResponse(contact_json(contact), status=status)


@api(('DELETE',))
def contact_number_detail(request: HttpRequest, contact_id: int) -> HttpResponse:
    contact = ContactNumber.objects.filter(pk=contact_id, user=shopper(request)).first()
    if contact is None:
        raise NotFound('Contact number not found.')
    try:
        service().cancel_followups_to_contact(contact)
    except NotificationError as e:
        logger.warning('Could not call off queued messages for contact #%s: %s', contact.pk, e.message)
    contact.delete()        # notifications keep their history; contact_number becomes NULL
    return HttpResponse(status=204)


# -- Orders --------------------------------------------------------------------------------

def order_json(order: Any, notifications: list[SmsNotification], *, staff: bool = False) -> dict[str, Any]:
    return {
        'orderId': order.pk,
        'number': str(order.number),
        'status': order.status,
        'dispatched': order_ops.is_dispatched(order),
        'currency': order.currency,
        'totalInclTax': str(order.total_incl_tax),
        'datePlaced': order.date_placed.isoformat(),
        'lines': [{'productId': line.product_id, 'title': line.title, 'quantity': line.quantity,
                   'lineTotalInclTax': str(line.line_price_incl_tax)} for line in order.lines.all()],
        'notifications': [notification_json(n, staff=staff) for n in notifications],
    }


def notifications_for(order: Any) -> list[SmsNotification]:
    return list(SmsNotification.objects.filter(order=order).select_related('order'))


@api(('POST',))
def orders(request: HttpRequest) -> HttpResponse:
    items = order_ops.parse_items(json_body(request))
    order = order_ops.place_order(shopper(request), items)
    service().notify(order, SmsNotification.KIND_ORDER_PLACED)
    return JsonResponse(order_json(order, notifications_for(order)), status=201)


@api(('GET',))
def my_orders(request: HttpRequest) -> HttpResponse:
    user_orders = list(Order.objects.filter(user=shopper(request)).order_by('-date_placed')[:MAX_ORDERS_LISTED])
    records = list(SmsNotification.objects.filter(order__in=user_orders).select_related('order'))
    service().refresh(records)
    return JsonResponse({'orders': [order_json(o, notifications_for(o)) for o in user_orders]})


def visible_order(request: HttpRequest, order_id: int) -> Any:
    """Shoppers see only their own orders; staff operators may see any."""
    qs = Order.objects.filter(pk=order_id)
    if not shopper(request).is_staff:
        qs = qs.filter(user=shopper(request))
    order = qs.first()
    if order is None:
        raise NotFound('Order not found.')
    return order


@api(('GET',))
def order_notifications(request: HttpRequest, order_id: int) -> HttpResponse:
    order = visible_order(request, order_id)
    service().refresh(notifications_for(order))
    order.refresh_from_db()
    staff = bool(shopper(request).is_staff)
    return JsonResponse({'orderId': order.pk, 'orderNumber': order.number, 'orderStatus': order.status,
                         'notifications': [notification_json(n, staff=staff) for n in notifications_for(order)]})


@api(('POST',), staff=True)
def order_dispatch(request: HttpRequest, order_id: int) -> HttpResponse:
    order, _ = order_ops.dispatch_order(order_id, shopper(request))
    svc = service()
    svc.notify(order, SmsNotification.KIND_ORDER_DISPATCHED)
    svc.schedule_followup(order)
    order.refresh_from_db()
    return JsonResponse(order_json(order, notifications_for(order), staff=True))


@api(('POST',), staff=True)
def order_cancel(request: HttpRequest, order_id: int) -> HttpResponse:
    order, _ = order_ops.cancel_order(order_id, shopper(request))
    svc = service()
    # Call off the queued follow-up first: it is the message that must never go out.
    try:
        svc.cancel_followups(order)
    except NotificationError as e:
        logger.warning('Calling off follow-ups for order %s incomplete: %s', order.number, e.message)
    svc.notify(order, SmsNotification.KIND_ORDER_CANCELLED)
    order.refresh_from_db()
    return JsonResponse(order_json(order, notifications_for(order), staff=True))


# -- Notifications (operator) ----------------------------------------------------------------

def get_notification(notification_id: int) -> SmsNotification:
    record = SmsNotification.objects.select_related('order').filter(pk=notification_id).first()
    if record is None:
        raise NotFound('Notification not found.')
    return record


@api(('POST',), staff=True)
def notification_resend(request: HttpRequest, notification_id: int) -> HttpResponse:
    original = get_notification(notification_id)
    key = request.headers.get('Idempotency-Key') or json_body(request).get('idempotencyKey')
    if not isinstance(key, str):
        raise InvalidRequest('An idempotency key is required (Idempotency-Key header or "idempotencyKey").')
    key = key.strip()
    try:
        record, created = service().resend(original, key)
    except NotificationError as e:
        # The claim row is the message this resend produced: report it with the failure.
        produced = SmsNotification.objects.filter(reference=resend_reference(original, key)).first()
        if produced is None:
            raise
        return JsonResponse({**notification_json(produced, staff=True), 'error': e.code, 'message': e.message,
                             'outcomeUnknown': e.outcome_unknown}, status=e.status_code)
    return JsonResponse(notification_json(record, staff=True), status=201 if created else 200)


@api(('DELETE',), staff=True)
def notification_content(request: HttpRequest, notification_id: int) -> HttpResponse:
    record = service().dispose_content(get_notification(notification_id))
    return JsonResponse(notification_json(record, staff=True))


def parse_instant(value: str | None, name: str) -> datetime:
    parsed = parse_datetime(value) if value else None
    if parsed is None:
        raise InvalidRequest('"%s" must be an ISO-8601 date-time.' % name)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


@api(('GET',), staff=True)
def reconciliation(request: HttpRequest) -> HttpResponse:
    start = parse_instant(request.GET.get('from'), 'from')
    end = parse_instant(request.GET.get('to'), 'to')
    if end <= start:
        raise InvalidRequest('"to" must be after "from".')
    return JsonResponse(service().reconcile(start, end))
