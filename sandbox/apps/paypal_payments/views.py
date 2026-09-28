"""
JSON API under /api/. Callers authenticate with Django's session login; the caller's identity is
always ``request.user``. Fulfil, cancel, refunds and reconciliation are operator (``is_staff``)
actions; everything else is scoped to the caller's own data.
"""
import json
import logging
import re
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_http_methods

from . import services
from .gateway import ProviderError
from .models import PayPalPayment, ProviderWrite
from .provider import BillingAddress, CardDetails
from .services import ServiceError

logger = logging.getLogger('apps.paypal_payments')

MAX_BODY = 64 * 1024
MAX_RECONCILIATION_RANGE_DAYS = 366


# --------------------------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------------------------

def error(status, code, message, **extra):
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def answer(outcome, body, *, created=False):
    """The ONE place a write's outcome becomes the caller's HTTP status. Success is `done` only."""
    body = {**body, 'outcome': outcome}
    if outcome == ProviderWrite.DONE:
        return JsonResponse(body, status=201 if created else 200)
    if outcome in (ProviderWrite.PENDING, ProviderWrite.SENDING):
        return JsonResponse(body, status=202)     # accepted, not done
    if outcome in (ProviderWrite.FAILED, ProviderWrite.NEEDS_REVIEW):
        return JsonResponse(body, status=409)
    return JsonResponse({**body, 'outcomeUnknown': True}, status=504)   # may have happened


def api_view(methods, *, staff=False):
    """JSON errors (never redirects/HTML), auth, and no request-wide transaction: the PayPal
    flows commit their claims before calling PayPal."""
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return error(401, 'not_authenticated', 'Log in first (POST /api/session).')
            if staff and not request.user.is_staff:
                return error(403, 'forbidden', 'This action is restricted to staff operators.')
            try:
                return view(request, *args, **kwargs)
            except ServiceError as exc:
                return JsonResponse({'error': exc.as_dict()}, status=exc.status_code)
            except ProviderError as exc:
                return JsonResponse({'error': exc.as_dict()}, status=exc.status_code)
            except BadRequest as exc:
                return error(400, 'invalid_request', str(exc))
        return transaction.non_atomic_requests(require_http_methods(methods)(wrapped))
    return decorator


class BadRequest(Exception):
    pass


def read_json(request):
    if len(request.body) > MAX_BODY:
        raise BadRequest('Request body too large.')
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest('Request body must be JSON.')
    if not isinstance(data, dict):
        raise BadRequest('Request body must be a JSON object.')
    return data


def idempotency_key(request, data=None, required=False):
    key = request.headers.get('Idempotency-Key') or (data or {}).get('idempotencyKey')
    if key is None:
        if required:
            raise BadRequest('An Idempotency-Key header (or "idempotencyKey" field) is required.')
        return None
    key = str(key).strip()
    if not key or len(key) > 128:
        raise BadRequest('Idempotency-Key must be 1-128 characters.')
    return key


def parse_amount(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise BadRequest('amount must be a decimal string such as "12.50".')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise BadRequest('amount must be a decimal string such as "12.50".')
    if not amount.is_finite():
        raise BadRequest('amount must be a finite number.')
    return amount


_EXPIRY = re.compile(r'^(\d{4})-(\d{2})$')


def _luhn(number):
    total = 0
    for i, digit in enumerate(reversed(number)):
        d = int(digit)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@sensitive_variables('data', 'number', 'cvc')
def parse_card(data):
    """Card details from the request body — validated, kept in memory only."""
    if not isinstance(data, dict):
        raise BadRequest('"card" must be an object.')
    number = re.sub(r'[\s-]', '', str(data.get('number', '')))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn(number):
        raise BadRequest('card.number is not a valid card number.')
    expiry = str(data.get('expiry', ''))
    match = _EXPIRY.match(expiry)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise BadRequest('card.expiry must be YYYY-MM.')
    now = timezone.now()
    if (int(match.group(1)), int(match.group(2))) < (now.year, now.month):
        raise BadRequest('card.expiry is in the past.')
    cvc = str(data.get('securityCode', data.get('cvc', '')))
    if not cvc.isdigit() or not 3 <= len(cvc) <= 4:
        raise BadRequest('card.securityCode must be 3 or 4 digits.')
    name = data.get('name')
    if name is not None and (not isinstance(name, str) or len(name) > 300):
        raise BadRequest('card.name must be a string.')
    address = None
    raw_address = data.get('billingAddress')
    if raw_address is not None:
        if not isinstance(raw_address, dict):
            raise BadRequest('card.billingAddress must be an object.')
        country = str(raw_address.get('countryCode', '')).upper()
        if not re.match(r'^[A-Z]{2}$', country):
            raise BadRequest('card.billingAddress.countryCode must be a 2-letter ISO country code.')

        def field(name):
            value = raw_address.get(name)
            return str(value)[:300] if value not in (None, '') else None
        address = BillingAddress(
            country_code=country, address_line_1=field('addressLine1'), address_line_2=field('addressLine2'),
            admin_area_2=field('city'), admin_area_1=field('state'), postal_code=field('postalCode'))
    return CardDetails(number=number, expiry=expiry, security_code=cvc, name=name or None,
                       billing_address=address)


def parse_datetime_param(request, name):
    raw = request.GET.get(name)
    if not raw:
        raise BadRequest('Query parameter "%s" (ISO-8601 date-time) is required.' % name)
    value = parse_datetime(raw.replace(' ', '+'))
    if value is None:
        raise BadRequest('"%s" must be an ISO-8601 date-time, e.g. 2026-09-01T00:00:00Z.' % name)
    if timezone.is_naive(value):
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


# --------------------------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------------------------

def _money(value):
    return None if value is None else str(value)


def _iso(value):
    return value.isoformat() if value else None


def payment_dict(payment):
    if payment is None:
        return None
    refundable = (payment.captured_amount - payment.refund_reserved) if payment.captured_amount else None
    honor_ends = payment.authorized_at + services.HONOR_PERIOD if payment.authorized_at else None
    return {
        'state': payment.state,
        'amount': _money(payment.amount),
        'currency': payment.currency,
        'card': ({'brand': payment.card_brand, 'lastDigits': payment.card_last_digits,
                  'savedPaymentMethodId': str(payment.saved_card.public_id) if payment.saved_card_id else None}
                 if payment.card_last_digits else None),
        'paypalOrderId': payment.paypal_order_id or None,
        'authorization': ({
            'id': payment.authorization_id, 'status': payment.authorization_status,
            'authorizedAt': _iso(payment.authorized_at), 'honorPeriodEndsAt': _iso(honor_ends),
            'expiresAt': _iso(payment.authorization_expires_at),
            'reauthorizations': payment.reauthorization_count,
        } if payment.authorization_id else None),
        'capture': ({
            'id': payment.capture_id, 'status': payment.capture_status,
            'amount': _money(payment.captured_amount), 'paypalFee': _money(payment.paypal_fee),
            'netAmount': _money(payment.net_amount), 'capturedAt': _iso(payment.captured_at),
        } if payment.capture_id else None),
        'refunded': _money(payment.refunded_amount),
        'refundable': _money(refundable),
        'refunds': [r.as_dict() for r in payment.refunds.all()],
        'voidedAt': _iso(payment.voided_at),
        'lastError': payment.last_error or None,
    }


def order_dict(order):
    payment = PayPalPayment.objects.filter(order=order).select_related('saved_card').first()
    return {
        'orderId': str(order.number),
        'status': order.status,
        'total': _money(order.total_incl_tax),
        'currency': order.currency,
        'placedAt': _iso(order.date_placed),
        'lines': [{
            'productId': line.product_id, 'title': line.title, 'quantity': line.quantity,
            'unitPrice': _money(line.unit_price_incl_tax), 'linePrice': _money(line.line_price_incl_tax),
        } for line in order.lines.all()],
        'payment': payment_dict(payment),
    }


def write_dict(record):
    if record is None:
        return None
    return {'ref': record.ref, 'outcome': record.outcome, 'paypalId': record.provider_id or None,
            'paypalStatus': record.provider_status or None, 'error': record.error or None}


# --------------------------------------------------------------------------------------------
# session (Django's own session login)
# --------------------------------------------------------------------------------------------

@ensure_csrf_cookie
@require_http_methods(['GET'])
def csrf(request):
    return JsonResponse({'csrfToken': get_token(request)})


@sensitive_variables('data', 'password')
@require_http_methods(['POST', 'DELETE', 'GET'])
def session(request):
    if request.method == 'GET':
        if not request.user.is_authenticated:
            return error(401, 'not_authenticated', 'Not logged in.')
        return JsonResponse({'user': _user_dict(request.user)})
    if request.method == 'DELETE':
        logout(request)
        return JsonResponse({'loggedOut': True})
    try:
        data = read_json(request)
    except BadRequest as exc:
        return error(400, 'invalid_request', str(exc))
    username, password = data.get('username') or data.get('email'), data.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        return error(400, 'invalid_request', '"username" (or "email") and "password" are required.')
    user = authenticate(request, username=username, password=password)
    if user is None and '@' in username:
        user = authenticate(request, email=username, password=password)
    if user is None or not user.is_active:
        return error(401, 'invalid_credentials', 'Invalid credentials.')
    login(request, user)
    return JsonResponse({'user': _user_dict(user), 'csrfToken': get_token(request)})


def _user_dict(user):
    return {'id': user.pk, 'username': user.get_username(), 'email': user.email, 'isStaff': user.is_staff}


# --------------------------------------------------------------------------------------------
# orders
# --------------------------------------------------------------------------------------------

@api_view(['POST'])
def orders(request):
    data = read_json(request)
    order, created = services.place_order(request.user, data.get('items'), request=request,
                                          idempotency_key=idempotency_key(request))
    body = order_dict(order)
    return JsonResponse(body, status=201 if created else 200)


@api_view(['GET'])
def my_orders(request):
    qs = request.user.orders.order_by('-date_placed').prefetch_related('lines')
    return JsonResponse({'orders': [order_dict(o) for o in qs]})


@api_view(['GET'])
def order_detail(request, order_id):
    order = (services.get_order_for_operator(order_id) if request.user.is_staff
             else services.get_order_for_shopper(request.user, order_id))
    return JsonResponse(order_dict(order))


@sensitive_variables('data', 'card')
@api_view(['POST'])
def pay(request, order_id):
    data = read_json(request)
    has_card, has_saved = data.get('card') is not None, data.get('paymentMethodId') is not None
    if has_card == has_saved:
        raise BadRequest('Send exactly one of "card" (card details) or "paymentMethodId" (a saved card).')
    card = parse_card(data['card']) if has_card else None
    saved_id = None
    if has_saved:
        try:
            saved_id = uuid.UUID(str(data['paymentMethodId']))
        except ValueError:
            return error(404, 'payment_method_not_found', 'Saved card not found.')
    record, payment = services.pay_order(request.user, order_id, card=card, saved_card_id=saved_id)
    order = payment.order
    outcome = record.outcome if record is not None else ProviderWrite.DONE
    return answer(outcome, {'orderId': order.number, 'order': order_dict(order), 'write': write_dict(record)})


@api_view(['POST'], staff=True)
def fulfil(request, order_id):
    record, payment = services.fulfil_order(order_id)
    outcome = record.outcome if record is not None else ProviderWrite.DONE
    return answer(outcome, {'orderId': payment.order.number, 'order': order_dict(payment.order),
                            'write': write_dict(record)})


@api_view(['POST'], staff=True)
def cancel(request, order_id):
    outcome, record, payment = services.cancel_order(order_id)
    return answer(outcome, {'orderId': payment.order.number, 'order': order_dict(payment.order),
                            'write': write_dict(record)})


@api_view(['POST'], staff=True)
def refunds(request, order_id):
    data = read_json(request)
    key = idempotency_key(request, data, required=True)
    reason = data.get('reason') or ''
    if not isinstance(reason, str):
        raise BadRequest('reason must be a string.')
    record, refund, payment = services.refund_order(
        order_id, key, amount=parse_amount(data.get('amount')), reason=reason, requested_by=request.user)
    return answer(record.outcome, {
        'refundId': str(refund.public_id), 'refund': refund.as_dict(), 'orderId': payment.order.number,
        'order': order_dict(payment.order), 'write': write_dict(record)}, created=True)


# --------------------------------------------------------------------------------------------
# saved cards
# --------------------------------------------------------------------------------------------

@sensitive_variables('data', 'card')
@api_view(['GET', 'POST'])
def payment_methods(request):
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': [c.as_dict() for c in services.list_cards(request.user)]})
    data = read_json(request)
    card = parse_card(data.get('card') if 'card' in data else data)
    record, saved = services.save_card(request.user, card, idempotency_key(request))
    body = {'write': write_dict(record)}
    if saved is not None:
        body.update(saved.as_dict())
    return answer(record.outcome, body, created=True)


@api_view(['DELETE'])
def payment_method_detail(request, payment_method_id):
    try:
        public_id = uuid.UUID(str(payment_method_id))
    except ValueError:
        return error(404, 'payment_method_not_found', 'Saved card not found.')
    outcome, card = services.delete_card(request.user, public_id)
    return answer(outcome, {'paymentMethodId': str(card.public_id), 'state': card.state})


# --------------------------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------------------------

@api_view(['GET'], staff=True)
def reconciliation(request):
    start, end = parse_datetime_param(request, 'from'), parse_datetime_param(request, 'to')
    if end <= start:
        raise BadRequest('"to" must be after "from".')
    if (end - start).days > MAX_RECONCILIATION_RANGE_DAYS:
        raise BadRequest('The range may span at most %d days.' % MAX_RECONCILIATION_RANGE_DAYS)
    end = min(end, datetime.now(dt_timezone.utc))
    if end <= start:
        raise BadRequest('"from" is in the future.')
    return JsonResponse(services.reconcile(start, end))
