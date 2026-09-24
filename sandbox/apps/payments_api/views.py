"""
JSON endpoints under /api/.

Callers authenticate with Django's session login (the sandbox's own
mechanism; ``POST /api/session`` is a JSON front door onto it) and CSRF
protection stays on for every unsafe method. Fulfil, cancel and
reconciliation are for staff; every other endpoint acts on the caller's own
orders and cards only.

These views opt out of ATOMIC_REQUESTS: the service layer commits each state
transition on its own, before and after the PayPal call, so a PayPal result
is never rolled back with an unrelated error.
"""

import functools
import json
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables

from . import gateway as gw
from . import services
from .services import ApiProblem

logger = logging.getLogger(__name__)

MAX_LINES = 100
MAX_QUANTITY = 1000


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _error(status, code, message, **extra):
    body = {'code': code, 'message': message}
    body.update({k: v for k, v in extra.items() if v is not None})
    return JsonResponse({'error': body}, status=status)


def api_view(methods, *, staff=False, anonymous=False):
    """JSON view: method check, authentication, staff check and error mapping."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = _error(405, 'METHOD_NOT_ALLOWED', 'Use %s.' % ', '.join(methods))
                response['Allow'] = ', '.join(methods)
                return response
            if not anonymous and not request.user.is_authenticated:
                return _error(401, 'NOT_AUTHENTICATED', 'Sign in first (POST /api/session).')
            if staff and not request.user.is_staff:
                return _error(403, 'STAFF_ONLY', 'This action is restricted to staff.')
            try:
                return view(request, *args, **kwargs)
            except ApiProblem as problem:
                return _error(problem.status, problem.code, problem.message, **problem.extra)
            except gw.PayPalError as exc:
                problem = services.problem_from_paypal(exc)
                return _error(problem.status, problem.code, problem.message, **problem.extra)

        return transaction.non_atomic_requests(wrapper)

    return decorator


@sensitive_variables('raw', 'payload')
def _json_body(request):
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body)
    except ValueError:
        raise ApiProblem(400, 'INVALID_JSON', 'The request body must be JSON.')
    if not isinstance(payload, dict):
        raise ApiProblem(400, 'INVALID_JSON', 'The request body must be a JSON object.')
    return payload


def _idempotency_key(request, payload, *, required):
    key = request.headers.get('Idempotency-Key') or payload.get('idempotencyKey')
    if key is None or key == '':
        if required:
            raise ApiProblem(400, 'IDEMPOTENCY_KEY_REQUIRED',
                             'Send an Idempotency-Key header (or "idempotencyKey") identifying this request.')
        return None
    key = str(key).strip()
    if not key or len(key) > 255:
        raise ApiProblem(400, 'INVALID_IDEMPOTENCY_KEY', 'The idempotency key must be 1-255 characters.')
    return key


_EXPIRY_PATTERNS = (
    re.compile(r'^(?P<year>\d{4})-(?P<month>\d{1,2})$'),
    re.compile(r'^(?P<month>\d{1,2})\s*/\s*(?P<year>\d{2}|\d{4})$'),
)
_ADDRESS_FIELDS = {
    'addressLine1': 'address_line_1',
    'addressLine2': 'address_line_2',
    'adminArea2': 'admin_area_2',
    'city': 'admin_area_2',
    'adminArea1': 'admin_area_1',
    'state': 'admin_area_1',
    'postalCode': 'postal_code',
    'countryCode': 'country_code',
}


@sensitive_variables('data', 'number', 'digits', 'security_code')
def _card_details(data):
    """Validate card input. The values are never stored or logged."""
    if not isinstance(data, dict):
        raise ApiProblem(400, 'INVALID_CARD', '"card" must be an object.')
    number = re.sub(r'[\s-]', '', str(data.get('number') or ''))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise ApiProblem(422, 'INVALID_CARD_NUMBER', 'The card number is not valid.', field='card.number')

    expiry = str(data.get('expiry') or '').strip()
    match = next((m for m in (p.match(expiry) for p in _EXPIRY_PATTERNS) if m), None)
    if match is None:
        raise ApiProblem(422, 'INVALID_CARD_EXPIRY', 'The expiry must look like YYYY-MM or MM/YY.',
                         field='card.expiry')
    year, month = int(match.group('year')), int(match.group('month'))
    year = year + 2000 if year < 100 else year
    today = date.today()
    if not 1 <= month <= 12 or (year, month) < (today.year, today.month):
        raise ApiProblem(422, 'CARD_EXPIRED', 'The card has expired or the expiry is invalid.', field='card.expiry')

    security_code = data.get('securityCode')
    if security_code is not None:
        security_code = str(security_code).strip()
        if not re.fullmatch(r'\d{3,4}', security_code):
            raise ApiProblem(422, 'INVALID_SECURITY_CODE', 'The security code must be 3 or 4 digits.',
                             field='card.securityCode')

    name = data.get('name')
    address = None
    raw_address = data.get('billingAddress')
    if raw_address is not None:
        if not isinstance(raw_address, dict):
            raise ApiProblem(400, 'INVALID_ADDRESS', '"card.billingAddress" must be an object.')
        address = {}
        for key, value in raw_address.items():
            if key in _ADDRESS_FIELDS and value not in (None, ''):
                address[_ADDRESS_FIELDS[key]] = str(value).strip()
        country = address.get('country_code', '')
        if not re.fullmatch(r'[A-Za-z]{2}', country):
            raise ApiProblem(422, 'INVALID_ADDRESS', 'The billing address needs a 2-letter countryCode.',
                             field='card.billingAddress.countryCode')
        address['country_code'] = country.upper()

    return gw.CardDetails(
        number=number,
        expiry='%04d-%02d' % (year, month),
        security_code=security_code or None,
        name=str(name).strip()[:300] if name else None,
        billing_address=address,
    )


def _luhn_ok(number):
    total = 0
    for index, char in enumerate(reversed(number)):
        digit = int(char)
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _positive_int(value, name):
    if isinstance(value, bool):
        value = None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiProblem(400, 'INVALID_%s' % name.upper(), '"%s" must be a positive integer.' % name)
    if number <= 0 or str(number) != str(value).strip():
        raise ApiProblem(400, 'INVALID_%s' % name.upper(), '"%s" must be a positive integer.' % name)
    return number


def _decimal_amount(value, currency):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ApiProblem(400, 'INVALID_AMOUNT', '"amount" must be a decimal number such as "12.50".')
    if not amount.is_finite() or amount <= 0:
        raise ApiProblem(422, 'INVALID_AMOUNT', '"amount" must be greater than zero.')
    places = min(gw.currency_exponent(currency), 2)
    if amount != amount.quantize(Decimal(1).scaleb(-places)):
        raise ApiProblem(422, 'INVALID_AMOUNT', '%s amounts have at most %d decimal places.' % (currency, places))
    return amount


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _money(value):
    return None if value is None else str(value)


def _iso(value):
    return value.isoformat() if value else None


def serialize_card(bankcard):
    return {
        'paymentMethodId': str(bankcard.pk),
        'type': 'card',
        'brand': bankcard.card_type,
        'lastDigits': bankcard.number[-4:],
        'expiry': bankcard.expiry_date.strftime('%Y-%m'),
        'description': '%s ending %s, expires %s' % (
            bankcard.card_type, bankcard.number[-4:], bankcard.expiry_date.strftime('%m/%Y')),
    }


def serialize_refund(refund):
    return {
        'refundId': str(refund.pk),
        'paypalRefundId': refund.paypal_refund_id or None,
        'amount': _money(refund.amount),
        'state': refund.state,
        'paypalStatus': refund.paypal_status or None,
        'paypalFee': _money(refund.paypal_fee),
        'netAmount': _money(refund.net_amount),
        'createdAt': _iso(refund.created_at),
        'error': {'code': refund.error_code, 'message': refund.error_message} if refund.error_code else None,
    }


def serialize_payment(order):
    payment = getattr(order, 'paypal_payment', None)
    if payment is None:
        return {'state': 'unpaid'}
    refunds = list(payment.refunds.order_by('pk'))
    refunded = sum((r.amount for r in refunds if r.state in ('completed', 'pending')), Decimal('0.00'))
    body = {
        'state': payment.state,
        'paypalOrderId': payment.paypal_order_id or None,
        'paymentMethod': {
            'savedPaymentMethodId': str(payment.saved_card_id) if payment.saved_card_id else None,
            'brand': payment.card_brand or None,
            'lastDigits': payment.card_last_digits or None,
        } if payment.card_last_digits or payment.saved_card_id else None,
        'authorization': {
            'id': payment.authorization_id,
            'status': payment.authorization_status or None,
            'amount': _money(payment.authorized_amount),
            'authorizedAt': _iso(payment.authorized_at),
            'expiresAt': _iso(payment.authorization_expires_at),
            'reauthorizedAt': _iso(payment.reauthorized_at),
            'originalAuthorizationId': payment.original_authorization_id or None,
        } if payment.authorization_id else None,
        'capture': {
            'id': payment.capture_id,
            'status': payment.capture_status or None,
            'amount': _money(payment.captured_amount),
            'paypalFee': _money(payment.paypal_fee),
            'netAmount': _money(payment.net_amount),
            'capturedAt': _iso(payment.captured_at),
        } if payment.capture_id else None,
        'refunds': [serialize_refund(r) for r in refunds],
        'refundedAmount': _money(refunded),
        'refundableAmount': _money(payment.refundable_amount()) if payment.capture_id else None,
        'lastError': {'code': payment.last_error_code, 'message': payment.last_error_message}
        if payment.last_error_code else None,
    }
    return body


def serialize_order(order):
    return {
        'orderId': str(order.number),
        'status': order.status,
        'currency': order.currency,
        'total': _money(order.total_incl_tax),
        'placedAt': _iso(order.date_placed),
        'lines': [
            {
                'productId': line.product_id,
                'title': line.title,
                'quantity': line.quantity,
                'unitPrice': _money(line.unit_price_incl_tax),
                'linePrice': _money(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        'payment': serialize_payment(order),
    }


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@ensure_csrf_cookie
@api_view(['GET'], anonymous=True)
def csrf(request):
    return JsonResponse({'csrfToken': get_token(request)})


@api_view(['GET', 'POST', 'DELETE'], anonymous=True)
@sensitive_variables('payload', 'password')
def session(request):
    if request.method == 'GET':
        if not request.user.is_authenticated:
            return JsonResponse({'authenticated': False})
        return JsonResponse(_user_json(request.user))
    if request.method == 'DELETE':
        logout(request)
        return JsonResponse({'authenticated': False})
    payload = _json_body(request)
    username = payload.get('username') or payload.get('email')
    password = payload.get('password')
    if not username or not password:
        raise ApiProblem(400, 'CREDENTIALS_REQUIRED', 'Send "username" (or "email") and "password".')
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        raise ApiProblem(401, 'INVALID_CREDENTIALS', 'Those credentials are not valid.')
    login(request, user)
    body = _user_json(user)
    body['csrfToken'] = get_token(request)  # login rotates the token
    return JsonResponse(body)


def _user_json(user):
    return {'authenticated': True, 'userId': user.pk, 'username': user.get_username(),
            'email': user.email, 'isStaff': user.is_staff}


# ---------------------------------------------------------------------------
# Orders and payments
# ---------------------------------------------------------------------------


@api_view(['POST'])
def orders(request):
    payload = _json_body(request)
    raw_lines = payload.get('lines')
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ApiProblem(400, 'LINES_REQUIRED', 'Send "lines": [{"productId": ..., "quantity": ...}, ...].')
    if len(raw_lines) > MAX_LINES:
        raise ApiProblem(400, 'TOO_MANY_LINES', 'At most %d lines per order.' % MAX_LINES)
    lines = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise ApiProblem(400, 'INVALID_LINE', 'Each line must be an object.')
        quantity = _positive_int(raw.get('quantity', 1), 'quantity')
        if quantity > MAX_QUANTITY:
            raise ApiProblem(422, 'QUANTITY_NOT_ALLOWED', 'At most %d of one item.' % MAX_QUANTITY)
        lines.append((_positive_int(raw.get('productId'), 'productId'), quantity))
    order = services.place_order(request.user, lines, request=request)
    return JsonResponse(serialize_order(order), status=201)


@api_view(['GET'])
def order_detail(request, order_id):
    return JsonResponse(serialize_order(services.get_order_for_user(request.user, order_id)))


@api_view(['POST'])
@sensitive_variables('payload', 'card')
def pay(request, order_id):
    payload = _json_body(request)
    has_card = payload.get('card') is not None
    method_id = payload.get('paymentMethodId')
    if has_card == (method_id is not None):
        raise ApiProblem(400, 'PAYMENT_SOURCE_REQUIRED',
                         'Send either "card" (card details) or "paymentMethodId" (a saved card), not both.')
    card = _card_details(payload['card']) if has_card else None
    order = services.pay_order(
        request.user, order_id, card=card,
        payment_method_id=_positive_int(method_id, 'paymentMethodId') if method_id is not None else None)
    return JsonResponse(serialize_order(order))


@api_view(['POST'], staff=True)
def fulfil(request, order_id):
    return JsonResponse(serialize_order(services.fulfil_order(order_id)))


@api_view(['POST'], staff=True)
def cancel(request, order_id):
    return JsonResponse(serialize_order(services.cancel_order(order_id)))


@api_view(['GET', 'POST'])
def refunds(request, order_id):
    if request.method == 'GET':
        order = services.get_order_for_user(request.user, order_id)
        return JsonResponse({'orderId': str(order.number), 'refunds': serialize_payment(order).get('refunds', [])})
    payload = _json_body(request)
    key = _idempotency_key(request, payload, required=True)
    order = services.get_order_for_user(request.user, order_id)
    amount = payload.get('amount')
    amount = _decimal_amount(amount, order.currency) if amount is not None else None
    refund, created = services.refund_order(request.user, order_id, amount=amount, idempotency_key=key)
    order.refresh_from_db()
    body = serialize_refund(refund)
    body['orderId'] = str(order.number)
    body['payment'] = serialize_payment(services.get_order_for_user(request.user, order_id))
    return JsonResponse(body, status=201 if created else 200)


@api_view(['GET'])
def my_orders(request):
    user_orders = (services.Order.objects.filter(user=request.user)
                   .select_related('paypal_payment').prefetch_related('lines').order_by('-date_placed'))
    return JsonResponse({'orders': [serialize_order(order) for order in user_orders]})


@api_view(['GET'], staff=True)
def reconciliation(request):
    start = services.parse_iso_datetime(request.GET.get('from'), 'from')
    end = services.parse_iso_datetime(request.GET.get('to'), 'to')
    start, end = services.validate_range(start, end)
    return JsonResponse(services.reconcile(start, end))


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


@api_view(['GET', 'POST'])
@sensitive_variables('payload', 'card')
def payment_methods(request):
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': [serialize_card(c) for c in services.list_saved_cards(request.user)]})
    payload = _json_body(request)
    card = _card_details(payload.get('card'))
    key = _idempotency_key(request, payload, required=False)
    bankcard, created = services.save_card(request.user, card, idempotency_key=key)
    return JsonResponse(serialize_card(bankcard), status=201 if created else 200)


@api_view(['GET', 'DELETE'])
def payment_method_detail(request, payment_method_id):
    if request.method == 'GET':
        card = services.list_saved_cards(request.user)
        match = next((c for c in card if c.pk == payment_method_id), None)
        if match is None:
            raise ApiProblem(404, 'PAYMENT_METHOD_NOT_FOUND', 'No such saved card.')
        return JsonResponse(serialize_card(match))
    services.delete_card(request.user, payment_method_id)
    return JsonResponse({'paymentMethodId': str(payment_method_id), 'deleted': True})
