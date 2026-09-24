"""
JSON endpoints for orders, PayPal payments and saved cards.

Callers authenticate with the sandbox's Django session login; CSRF protection
stays on, so a non-browser client fetches a token from ``/api/auth/csrf`` and
sends it back in the ``X-CSRFToken`` header.
"""
import functools
import json
import logging
import re
from datetime import date, timezone as dt_timezone

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from oscar.core.loading import get_model

from . import gateway, money, services
from .models import PaypalPayment

logger = logging.getLogger(__name__)

Order = get_model('order', 'Order')

MAX_IDEMPOTENCY_KEY_LENGTH = 255


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

class BadRequest(Exception):
    def __init__(self, message, code='invalid_request', status_code=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


def _error(status_code, code, message, **extra):
    return JsonResponse({'error': {'code': code, 'message': message}, **extra}, status=status_code)


def api_view(methods, *, staff=False, anonymous=False):
    """Method routing, session authentication, and one error boundary for every endpoint."""
    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = _error(405, 'method_not_allowed', f'{request.method} is not allowed here.')
                response['Allow'] = ', '.join(methods)
                return response
            if not anonymous and not request.user.is_authenticated:
                return _error(401, 'not_authenticated', 'Sign in first (POST /api/auth/login).')
            if staff and not request.user.is_staff:
                return _error(403, 'forbidden', 'This action is restricted to staff.')
            try:
                return view(request, *args, **kwargs)
            except BadRequest as e:
                return _error(e.status_code, e.code, e.message)
            except services.ApiProblem as e:
                extra = {}
                if e.payment is not None:
                    extra['order'] = order_json(e.payment.order, e.payment)
                if e.refund is not None:
                    extra['refund'] = refund_json(e.refund)
                return _error(e.status_code, e.code, e.message, **extra)
            except gateway.ProviderError as e:
                return _error(e.status_code, e.code or 'paypal_error', e.message)
            except ImproperlyConfigured as e:
                logger.error('Payments are not configured: %s', e)
                return _error(503, 'payments_not_configured', 'Payments are not configured on this server.')
        return wrapper
    return decorator


def _json_body(request):
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest('The request body must be JSON.') from None
    if not isinstance(body, dict):
        raise BadRequest('The request body must be a JSON object.')
    return body


def _string(body, key, *, required=True, max_length=255):
    value = body.get(key)
    if value is None or value == '':
        if required:
            raise BadRequest(f'"{key}" is required.')
        return ''
    if not isinstance(value, str) or len(value) > max_length:
        raise BadRequest(f'"{key}" must be a string of at most {max_length} characters.')
    return value.strip()


# ---------------------------------------------------------------------------
# Card input — card numbers and security codes are never logged or stored
# ---------------------------------------------------------------------------

def _luhn_ok(number):
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@sensitive_variables('raw', 'number', 'security_code')
def _card_input(raw):
    if not isinstance(raw, dict):
        raise BadRequest('"card" must be an object.', code='invalid_card')
    number = raw.get('number')
    if not isinstance(number, str):
        raise BadRequest('"card.number" is required.', code='invalid_card')
    number = re.sub(r'[\s-]', '', number)
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise BadRequest('"card.number" is not a valid card number.', code='invalid_card')
    expiry = raw.get('expiry')
    match = re.fullmatch(r'(\d{4})-(\d{2})', expiry) if isinstance(expiry, str) else None
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise BadRequest('"card.expiry" must be YYYY-MM.', code='invalid_card')
    today = date.today()
    if (int(match.group(1)), int(match.group(2))) < (today.year, today.month):
        raise BadRequest('The card has expired.', code='invalid_card')
    security_code = raw.get('securityCode')
    if not isinstance(security_code, str) or not re.fullmatch(r'\d{3,4}', security_code):
        raise BadRequest('"card.securityCode" must be 3 or 4 digits.', code='invalid_card')
    name = _string(raw, 'name', required=False, max_length=300)
    address = None
    raw_address = raw.get('billingAddress')
    if raw_address is not None:
        if not isinstance(raw_address, dict):
            raise BadRequest('"card.billingAddress" must be an object.', code='invalid_card')
        country = _string(raw_address, 'countryCode', max_length=2).upper()
        if len(country) != 2 or not country.isalpha():
            raise BadRequest('"card.billingAddress.countryCode" must be a two-letter country code.',
                             code='invalid_card')
        address = gateway.BillingAddress(
            country_code=country,
            address_line_1=_string(raw_address, 'addressLine1', required=False, max_length=300),
            address_line_2=_string(raw_address, 'addressLine2', required=False, max_length=300),
            admin_area_2=_string(raw_address, 'adminArea2', required=False, max_length=120),
            admin_area_1=_string(raw_address, 'adminArea1', required=False, max_length=300),
            postal_code=_string(raw_address, 'postalCode', required=False, max_length=60),
        )
    return gateway.CardInput(number=number, expiry=expiry, security_code=security_code,
                             name=name, billing_address=address)


# ---------------------------------------------------------------------------
# Representations
# ---------------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if value else None


def refund_json(refund):
    return {
        'refundId': str(refund.public_id),
        'status': refund.status,
        'amount': services._fmt(refund.amount, refund.currency),
        'currency': refund.currency,
        'idempotencyKey': refund.idempotency_key,
        'paypalRefundId': refund.paypal_refund_id or None,
        'paypalStatus': refund.paypal_status or None,
        'createdAt': _iso(refund.date_created),
        'error': ({'code': refund.last_error_code, 'message': refund.last_error}
                  if refund.last_error else None),
    }


def payment_json(payment):
    cur = payment.currency
    refunds = list(payment.refunds.all())
    refunded = sum((r.amount for r in refunds if r.status == r.COMPLETED), money.quantum(cur) * 0)
    reserved = sum((r.amount for r in refunds if r.status in r.RESERVING_STATUSES), money.quantum(cur) * 0)
    refundable = None
    if payment.status in services.REFUNDABLE_STATUSES and payment.captured_amount is not None:
        refundable = services._fmt(payment.captured_amount - reserved, cur)
    return {
        'status': payment.status,
        'amount': services._fmt(payment.amount, cur),
        'currency': cur,
        'card': ({'brand': payment.card_brand or None, 'lastDigits': payment.card_last_digits}
                 if payment.card_last_digits else None),
        'paymentMethodId': str(payment.saved_card.public_id) if payment.saved_card_id else None,
        'authorization': ({
            'id': payment.authorization_id,
            'status': payment.authorization_status or None,
            'amount': services._fmt(payment.authorized_amount, cur),
            'createdAt': _iso(payment.authorization_created_at),
            'expiresAt': _iso(payment.authorization_expires_at),
            'reauthorizedAt': _iso(payment.reauthorized_at),
            'originalAuthorizationId': payment.original_authorization_id or None,
        } if payment.authorization_id else None),
        'capture': ({
            'id': payment.capture_id,
            'status': payment.capture_status or None,
            'statusReason': payment.capture_status_reason or None,
            'amount': services._fmt(payment.captured_amount, cur),
            'paypalFee': services._fmt(payment.paypal_fee, cur),
            'netAmount': services._fmt(payment.net_amount, cur),
            'capturedAt': _iso(payment.captured_at),
        } if payment.capture_id else None),
        'refunds': [refund_json(r) for r in refunds],
        'refundedAmount': services._fmt(refunded, cur),
        'refundableAmount': refundable,
        'paypalOrderId': payment.paypal_order_id or None,
        'voidedAt': _iso(payment.voided_at),
        'error': ({'code': payment.last_error_code, 'message': payment.last_error}
                  if payment.last_error else None),
    }


def order_json(order, payment):
    return {
        'orderId': str(order.number),
        'status': order.status,
        'total': str(order.total_incl_tax),
        'currency': payment.currency if payment else order.currency,
        'placedAt': _iso(order.date_placed),
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'unitPrice': str(line.unit_price_incl_tax) if line.unit_price_incl_tax is not None else None,
            'linePrice': str(line.line_price_incl_tax),
        } for line in order.lines.all()],
        'payment': payment_json(payment) if payment else None,
    }


def card_json(card):
    return {
        'paymentMethodId': str(card.public_id),
        'brand': card.brand or None,
        'lastDigits': card.last_digits,
        'expiry': card.expiry or None,
        'createdAt': _iso(card.date_created),
    }


def _payment_response(result):
    body = order_json(result.payment.order, result.payment)
    return JsonResponse(body, status=result.status_code)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@ensure_csrf_cookie
@api_view(['GET'], anonymous=True)
def csrf(request):
    return JsonResponse({'csrfToken': get_token(request)})


@sensitive_post_parameters()
@sensitive_variables('body', 'password')
@api_view(['POST'], anonymous=True)
def session_login(request):
    body = _json_body(request)
    username = _string(body, 'username', required=False) or _string(body, 'email', required=False)
    password = body.get('password')
    if not username or not isinstance(password, str):
        raise BadRequest('"username" (or "email") and "password" are required.')
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return _error(401, 'invalid_credentials', 'Invalid username or password.')
    login(request, user)
    return JsonResponse({'userId': user.pk, 'username': user.get_username(), 'isStaff': user.is_staff,
                         'csrfToken': get_token(request)})


@api_view(['POST'], anonymous=True)
def session_logout(request):
    logout(request)
    return JsonResponse({'signedOut': True})


# ---------------------------------------------------------------------------
# Orders and payments
# ---------------------------------------------------------------------------

@api_view(['POST'])
def orders(request):
    body = _json_body(request)
    items = body.get('items')
    if not isinstance(items, list) or not items:
        raise BadRequest('"items" must be a non-empty list of {"productId", "quantity"}.')
    if len(items) > services.MAX_ORDER_LINES:
        raise BadRequest(f'At most {services.MAX_ORDER_LINES} items per order.')
    parsed = []
    for item in items:
        if not isinstance(item, dict):
            raise BadRequest('Each item must be an object.')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            raise BadRequest('"productId" must be an integer.')
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise BadRequest('"quantity" must be a positive integer.')
        parsed.append({'productId': product_id, 'quantity': quantity})
    order, payment = services.place_order(request.user, parsed, request=request)
    return JsonResponse(order_json(order, payment), status=201)


@api_view(['GET'])
def order_detail(request, order_number):
    if request.user.is_staff:
        payment = services.payment_for_operator(order_number)
    else:
        payment = services.payment_for_owner(request.user, order_number)
    return JsonResponse(order_json(payment.order, payment))


@sensitive_post_parameters()
@sensitive_variables('body', 'card')
@api_view(['POST'])
def pay(request, order_number):
    body = _json_body(request)
    has_card = body.get('card') is not None
    payment_method_id = body.get('paymentMethodId')
    if has_card == (payment_method_id is not None):
        raise BadRequest('Send either "card" or "paymentMethodId", not both.')
    if payment_method_id is not None and not isinstance(payment_method_id, str):
        raise BadRequest('"paymentMethodId" must be a string.')
    card = _card_input(body['card']) if has_card else None
    result = services.pay(request.user, order_number, card=card, payment_method_id=payment_method_id)
    return _payment_response(result)


@api_view(['POST'], staff=True)
def fulfil(request, order_number):
    return _payment_response(services.fulfil(order_number))


@api_view(['POST'], staff=True)
def cancel(request, order_number):
    return _payment_response(services.cancel(order_number))


@api_view(['POST'])
def refunds(request, order_number):
    key = request.headers.get('Idempotency-Key', '').strip()
    if not key:
        raise BadRequest('The Idempotency-Key header is required.', code='idempotency_key_required')
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise BadRequest(f'Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters.')
    body = _json_body(request)
    payment = services.payment_for_owner(request.user, order_number)
    amount = None
    if body.get('amount') is not None:
        try:
            amount = money.parse_positive(body['amount'], payment.currency)
        except money.AmountError as e:
            raise BadRequest(str(e), code='invalid_amount') from None
    result = services.refund(request.user, order_number, idempotency_key=key, amount=amount)
    body = refund_json(result.refund)
    body['order'] = order_json(result.payment.order, result.payment)
    return JsonResponse(body, status=result.status_code)


@api_view(['GET'])
def my_orders(request):
    user_orders = (Order.objects.filter(user=request.user).order_by('-date_placed', '-pk')
                   .prefetch_related('lines'))
    payments = {p.order_id: p for p in PaypalPayment.objects.filter(order__in=user_orders)
                .select_related('saved_card').prefetch_related('refunds')}
    return JsonResponse({'orders': [order_json(o, payments.get(o.pk)) for o in user_orders]})


@api_view(['GET'], staff=True)
def reconciliation(request):
    start = _parse_instant(request.GET.get('from'), 'from')
    end = _parse_instant(request.GET.get('to'), 'to')
    return JsonResponse(services.reconcile(start, end))


def _parse_instant(value, name):
    if not value:
        raise BadRequest(f'"{name}" is required (ISO-8601 date-time).')
    parsed = parse_datetime(value.replace(' ', '+'))
    if parsed is None:
        raise BadRequest(f'"{name}" must be an ISO-8601 date-time.')
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

@sensitive_post_parameters()
@sensitive_variables('body', 'card')
@api_view(['GET', 'POST'])
def payment_methods(request):
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': [card_json(c) for c in services.list_cards(request.user)]})
    body = _json_body(request)
    card = _card_input(body.get('card'))
    saved = services.save_card(request.user, card)
    return JsonResponse(card_json(saved), status=201)


@api_view(['DELETE'])
def payment_method_detail(request, payment_method_id):
    card = services.remove_card(request.user, payment_method_id)
    body = {'paymentMethodId': str(card.public_id), 'removed': True,
            'paypalDeletionPending': card.paypal_delete_pending}
    return JsonResponse(body, status=202 if card.paypal_delete_pending else 200)
