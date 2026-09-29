"""
JSON endpoints under /api/.

Callers authenticate with Django's session login (the sandbox's own), and
CSRF protection stays on: send the ``csrftoken`` cookie value back in an
``X-CSRFToken`` header on every POST/DELETE.

Card numbers and security codes pass through these views to PayPal only: they
are never stored, never logged and never echoed back.
"""
import json
import logging
import re
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_http_methods
from oscar.core.loading import get_model

from . import services
from .client import PayPalNotConfigured
from .gateway import CardDetails, ProviderError
from .models import OrderPayment
from .reconciliation import reconcile
from .services import ApiProblem, answer_status

logger = logging.getLogger('apps.paypal_payments')

Order = get_model('order', 'Order')

MAX_ORDER_LINES = 50
MAX_QUANTITY = 999
MAX_RECONCILIATION_DAYS = 3 * 366   # PayPal lists at most the previous three years


def _error(status, code, message, **extra):
    body = {'error': {'code': code, 'message': message}}
    body.update(extra)
    return JsonResponse(body, status=status)


def api_view(methods, *, staff=False):
    """Session-authenticated JSON view; each claim commits before PayPal is called."""
    def decorator(func):
        @transaction.non_atomic_requests
        @require_http_methods(methods)
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return _error(401, 'not_authenticated', 'Sign in first (POST /api/session).')
            if staff and not request.user.is_staff:
                return _error(403, 'forbidden', 'This action is restricted to staff.')
            try:
                return func(request, *args, **kwargs)
            except ApiProblem as problem:
                return _error(problem.status, problem.code, problem.message, **problem.extra)
            except ProviderError as error:
                return _error(error.status_code, error.code, error.message, outcomeUnknown=error.outcome_unknown)
            except PayPalNotConfigured:
                logger.error('PayPal is not configured')
                return _error(503, 'paypal_not_configured', 'Payments are not configured on this site.')
            except Exception:
                logger.exception('Unexpected error in %s', func.__name__)
                return _error(500, 'internal_error', 'Something went wrong; the request may be repeated safely.')
        return wrapper
    return decorator


def _outcome_response(outcome):
    status = answer_status(outcome.outcome, created=outcome.created, error=outcome.error)
    body = dict(outcome.body)
    body['outcome'] = outcome.outcome
    if outcome.error is not None and status >= 400:
        body['error'] = {'code': outcome.error.code, 'message': outcome.error.message,
                         'outcomeUnknown': outcome.error.outcome_unknown}
        if outcome.error.debug_id:
            body['error']['paypalDebugId'] = outcome.error.debug_id
    return JsonResponse(body, status=status)


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ApiProblem(400, 'invalid_json', 'The request body must be JSON.')
    if not isinstance(data, dict):
        raise ApiProblem(400, 'invalid_json', 'The request body must be a JSON object.')
    return data


@sensitive_variables('raw', 'number', 'security_code')
def _parse_card(raw):
    if not isinstance(raw, dict):
        raise ApiProblem(400, 'invalid_card', '"card" must be an object.')
    number = re.sub(r'[\s-]', '', str(raw.get('number', '')))
    if not re.fullmatch(r'\d{12,19}', number):
        raise ApiProblem(400, 'invalid_card_number', 'card.number must be 12 to 19 digits.')
    expiry = str(raw.get('expiry', ''))
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', expiry):
        raise ApiProblem(400, 'invalid_card_expiry', 'card.expiry must be YYYY-MM.')
    security_code = str(raw.get('securityCode', ''))
    if not re.fullmatch(r'\d{3,4}', security_code):
        raise ApiProblem(400, 'invalid_card_security_code', 'card.securityCode must be 3 or 4 digits.')
    name = raw.get('name', '')
    if not isinstance(name, str) or len(name) > 300:
        raise ApiProblem(400, 'invalid_card_name', 'card.name must be a string of at most 300 characters.')
    address = raw.get('billingAddress')
    billing = None
    if address is not None:
        if not isinstance(address, dict):
            raise ApiProblem(400, 'invalid_billing_address', 'card.billingAddress must be an object.')
        country = str(address.get('countryCode', '')).upper()
        if not re.fullmatch(r'[A-Z]{2}', country):
            raise ApiProblem(400, 'invalid_billing_address', 'card.billingAddress.countryCode must be ISO 3166-1.')
        billing = {'country_code': country}
        for wire, key, limit in (('addressLine1', 'address_line_1', 300), ('addressLine2', 'address_line_2', 300),
                                 ('adminArea2', 'admin_area_2', 120), ('adminArea1', 'admin_area_1', 300),
                                 ('postalCode', 'postal_code', 60)):
            value = address.get(wire)
            if value is not None:
                if not isinstance(value, str) or len(value) > limit:
                    raise ApiProblem(400, 'invalid_billing_address', 'card.billingAddress.%s is invalid.' % wire)
                billing[key] = value
    return CardDetails(number=number, expiry=expiry, security_code=security_code, name=name.strip(),
                       billing_address=billing)


def _idempotency_key(request, data, *, required):
    key = request.headers.get('Idempotency-Key') or data.get('idempotencyKey')
    if key is None:
        if required:
            raise ApiProblem(400, 'idempotency_key_required',
                             'Send an Idempotency-Key header (or "idempotencyKey"); repeat it to retry safely.')
        return None
    key = str(key)
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', key):
        raise ApiProblem(400, 'invalid_idempotency_key',
                         'Idempotency keys are 1-128 characters of letters, digits and . _ : -')
    return key


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@transaction.non_atomic_requests
@require_http_methods(['GET', 'POST', 'DELETE'])
@sensitive_variables('data', 'password')
def session(request):
    if request.method == 'POST':
        try:
            data = _json_body(request)
        except ApiProblem as problem:
            return _error(problem.status, problem.code, problem.message)
        password = data.get('password')
        user = authenticate(request, username=data.get('username'), password=password)
        if user is None:
            return _error(401, 'invalid_credentials', 'Unknown username or wrong password.')
        login(request, user)
    elif request.method == 'DELETE':
        logout(request)
    user = request.user
    return JsonResponse({
        'authenticated': user.is_authenticated,
        'username': user.get_username() if user.is_authenticated else None,
        'isStaff': bool(user.is_authenticated and user.is_staff),
        'csrfToken': get_token(request),
    })


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@api_view(['POST'])
def orders(request):
    data = _json_body(request)
    raw_items = data.get('items')
    if not isinstance(raw_items, list) or not raw_items:
        raise ApiProblem(400, 'invalid_items', '"items" must be a non-empty list of {productId, quantity}.')
    if len(raw_items) > MAX_ORDER_LINES:
        raise ApiProblem(400, 'invalid_items', 'At most %d items per order.' % MAX_ORDER_LINES)
    quantities: dict[int, int] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            raise ApiProblem(400, 'invalid_items', 'Each item must be an object.')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise ApiProblem(400, 'invalid_items', 'productId must be a positive integer.')
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise ApiProblem(400, 'invalid_items', 'quantity must be an integer from 1 to %d.' % MAX_QUANTITY)
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    payment = services.place_order(request, list(quantities.items()))
    body = services.payment_view(payment)
    return JsonResponse(body, status=201)


@api_view(['POST'])
@sensitive_variables('data', 'card')
def pay(request, order_id):
    data = _json_body(request)
    has_card, has_saved = 'card' in data, 'paymentMethodId' in data
    if has_card == has_saved:
        raise ApiProblem(400, 'invalid_payment_source',
                         'Send either "card" (one-off card details) or "paymentMethodId" (a saved card).')
    if has_saved:
        method_id = data['paymentMethodId']
        if not isinstance(method_id, int) or isinstance(method_id, bool):
            raise ApiProblem(400, 'invalid_payment_source', 'paymentMethodId must be an integer.')
        outcome = services.pay(request.user, order_id, payment_method_id=method_id)
    else:
        outcome = services.pay(request.user, order_id, card=_parse_card(data['card']))
    return _outcome_response(outcome)


@api_view(['POST'], staff=True)
def fulfil(request, order_id):
    return _outcome_response(services.fulfil(order_id))


@api_view(['POST'], staff=True)
def cancel(request, order_id):
    return _outcome_response(services.cancel(order_id))


@api_view(['POST'])
def refunds(request, order_id):
    data = _json_body(request)
    key = _idempotency_key(request, data, required=True)
    amount = None
    if data.get('amount') is not None:
        try:
            amount = Decimal(str(data['amount']))
        except (InvalidOperation, ValueError):
            raise ApiProblem(400, 'invalid_amount', 'amount must be a decimal string such as "12.50".')
        exponent = amount.as_tuple().exponent
        if not amount.is_finite() or amount <= 0 or not isinstance(exponent, int) or exponent < -2:
            raise ApiProblem(400, 'invalid_amount', 'amount must be positive with at most two decimal places.')
    return _outcome_response(services.refund(request.user, order_id, key, amount))


@api_view(['GET'])
def my_orders(request):
    results = []
    for order in Order.objects.filter(user=request.user).order_by('-date_placed', '-pk'):
        try:
            payment = order.paypal_payment
        except OrderPayment.DoesNotExist:
            results.append({'orderId': order.number, 'orderStatus': order.status,
                            'placedAt': order.date_placed.isoformat(),
                            'total': str(order.total_incl_tax), 'currency': order.currency,
                            'payment': None})
            continue
        results.append(services.payment_view(payment))
    return JsonResponse({'orders': results})


def _parse_moment(value, name):
    moment = parse_datetime(value or '')
    if moment is None:
        raise ApiProblem(400, 'invalid_%s' % name, '"%s" must be an ISO-8601 date-time.' % name)
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, dt_timezone.utc)
    return moment


@api_view(['GET'], staff=True)
def reconciliation(request):
    start = _parse_moment(request.GET.get('from'), 'from')
    end = _parse_moment(request.GET.get('to'), 'to')
    if end <= start:
        raise ApiProblem(400, 'invalid_range', '"to" must be after "from".')
    if (end - start).days > MAX_RECONCILIATION_DAYS:
        raise ApiProblem(400, 'invalid_range', 'The range may span at most three years.')
    return JsonResponse(reconcile(start, end))


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@sensitive_variables('data', 'card')
def payment_methods(request):
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': services.list_cards(request.user)})
    data = _json_body(request)
    key = _idempotency_key(request, data, required=False)
    if 'card' not in data:
        raise ApiProblem(400, 'invalid_card', 'Send the card to save as "card".')
    card = _parse_card(data['card'])
    return _outcome_response(services.save_card(request.user, card, key))


@api_view(['DELETE'])
def payment_method(request, payment_method_id):
    return _outcome_response(services.delete_card(request.user, payment_method_id))
