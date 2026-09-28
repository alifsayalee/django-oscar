"""
JSON API for PayPal payments and saved cards.

Callers authenticate with the sandbox's Django session login; unsafe methods
need the CSRF token (cookie ``csrftoken`` -> header ``X-CSRFToken``), which
``GET /api/session`` sets.

Views opt out of ATOMIC_REQUESTS: each PayPal write's claim must be committed
before PayPal is called, so a crash mid-call still leaves a record to check.
"""
import json
import logging
import re
from datetime import datetime, timezone as dt_timezone
from functools import wraps

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_model
from paypal.models import Address

from . import services
from .gateway import CardInput, ProviderError
from .safe_write import OutcomeUnknown, WriteRefused

Order = get_model('order', 'Order')

log = logging.getLogger('apps.paypal_payments')


def error(status, code, message, **extra):
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def answer(result, body, done_status=200):
    """The one place a write's outcome becomes the HTTP answer. Only ``done`` is a success."""
    match result.outcome:
        case 'done':
            status = done_status
        case 'pending' | 'sending':
            status = 202  # accepted, not done yet
        case 'failed':
            status = result.failed_status
        case 'needs_review':
            status = 409
        case _:
            status = 504  # may have happened: never "not done"
    code = result.code or result.outcome
    body = {'outcome': result.outcome, 'code': code, 'detail': result.detail or None, **body}
    if status >= 400:
        body['error'] = {'code': code, 'message': result.detail or code,
                         'outcomeUnknown': result.outcome == 'unknown'}
    return JsonResponse(body, status=status)


def api(methods, staff=False):
    """Session auth, method check, JSON errors, and no request-wide transaction."""
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                return error(405, 'method_not_allowed', 'Use %s.' % ' or '.join(methods))
            if not request.user.is_authenticated:
                return error(401, 'not_authenticated', 'Sign in first (session login).')
            if staff and not request.user.is_staff:
                return error(403, 'forbidden', 'This action is for staff operators only.')
            try:
                return view(request, *args, **kwargs)
            except services.ApiProblem as e:
                return error(e.status_code, e.code, e.message, **e.extra)
            except WriteRefused as e:
                return error(e.error.status_code, e.error.code, e.error.message)
            except ProviderError as e:
                return error(e.status_code, e.code, e.message, outcomeUnknown=e.outcome_unknown)
            except OutcomeUnknown as e:
                return error(504, 'outcome_unknown',
                             'PayPal did not give a readable answer; the operation may have happened. '
                             'Repeat the same request to re-check it - it will not be performed twice.',
                             outcomeUnknown=True, reference=e.record.ref)
            except ImproperlyConfigured as e:
                log.error('PayPal is not configured: %s', e)
                return error(503, 'payments_not_configured', 'Payments are not configured on this server.')
        return transaction.non_atomic_requests(wrapped)
    return decorator


def read_json(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise services.ApiProblem(400, 'invalid_json', 'The request body must be JSON.') from None
    if not isinstance(data, dict):
        raise services.ApiProblem(400, 'invalid_json', 'The request body must be a JSON object.')
    return data


def luhn_ok(number):
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


@sensitive_variables('raw', 'number', 'cvc')
def parse_card(raw):
    """Validate card input. The result lives in memory only; it is never stored or logged."""
    if not isinstance(raw, dict):
        raise services.ApiProblem(400, 'invalid_card', 'card must be an object.')
    number = re.sub(r'[\s-]', '', str(raw.get('number', '')))
    expiry = str(raw.get('expiry', '')).strip()
    cvc = str(raw.get('securityCode', raw.get('cvc', ''))).strip()
    name = str(raw.get('name', '')).strip()
    if not re.fullmatch(r'\d{12,19}', number) or not luhn_ok(number):
        raise services.ApiProblem(400, 'invalid_card', 'card.number is not a valid card number.')
    match = re.fullmatch(r'(\d{4})-(\d{2})', expiry)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise services.ApiProblem(400, 'invalid_card', 'card.expiry must be YYYY-MM.')
    now = datetime.now(dt_timezone.utc)
    if (int(match.group(1)), int(match.group(2))) < (now.year, now.month):
        raise services.ApiProblem(400, 'invalid_card', 'The card has expired.')
    if not re.fullmatch(r'\d{3,4}', cvc):
        raise services.ApiProblem(400, 'invalid_card', 'card.securityCode must be 3 or 4 digits.')
    if not name or len(name) > 300:
        raise services.ApiProblem(400, 'invalid_card', 'card.name is required.')
    return CardInput(number=number, expiry=expiry, security_code=cvc, name=name,
                     billing_address=parse_address(raw.get('billingAddress')))


def parse_address(raw):
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict) or not re.fullmatch(r'[A-Za-z]{2}', str(raw.get('countryCode', ''))):
        raise services.ApiProblem(400, 'invalid_address', 'billingAddress.countryCode (2 letters) is required.')
    fields = {'address_line_1': 'line1', 'address_line_2': 'line2', 'admin_area_2': 'city',
              'admin_area_1': 'state', 'postal_code': 'postalCode'}
    values = {k: str(raw[v])[:300] for k, v in fields.items() if raw.get(v)}
    return Address(country_code=str(raw['countryCode']).upper(), **values)


def idempotency_key(request, data):
    return request.headers.get('Idempotency-Key') or data.get('idempotencyKey') or None


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

@ensure_csrf_cookie
def session(request):
    """Who the caller is; also sets the CSRF cookie needed for POST/DELETE."""
    user = request.user
    return JsonResponse({
        'authenticated': user.is_authenticated,
        'userId': user.pk if user.is_authenticated else None,
        'email': user.email if user.is_authenticated else None,
        'isStaff': bool(user.is_authenticated and user.is_staff),
    })


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@api(['POST'])
def orders(request):
    data = read_json(request)
    items = data.get('items')
    if not isinstance(items, list):
        raise services.ApiProblem(400, 'invalid_items', 'items must be a list of {productId, quantity}.')
    order = services.place_order(request.user, items, request)
    return JsonResponse(services.order_json(order), status=201)


@api(['POST'])
@sensitive_variables('data', 'card')
def pay(request, order_id):
    data = read_json(request)
    method_id = data.get('paymentMethodId')
    if method_id is not None and data.get('card') is not None:
        raise services.ApiProblem(400, 'ambiguous_payment_source', 'Send card details or a paymentMethodId, not both.')
    card = parse_card(data['card']) if data.get('card') is not None else None
    if method_id is not None:
        try:
            method_id = int(method_id)
        except (TypeError, ValueError):
            raise services.ApiProblem(400, 'invalid_payment_method', 'paymentMethodId must be an integer.') from None
    result = services.pay(request.user, order_id, card=card, payment_method_id=method_id)
    return answer(result, {'orderId': order_id, 'payment': services.payment_json(result.payment)})


@api(['POST'], staff=True)
def fulfil(request, order_id):
    result = services.fulfil(order_id)
    return answer(result, {'orderId': order_id, 'payment': services.payment_json(result.payment)})


@api(['POST'], staff=True)
def cancel(request, order_id):
    result = services.cancel(order_id)
    return answer(result, {'orderId': order_id, 'payment': services.payment_json(result.payment)})


@api(['POST'])
def refunds(request, order_id):
    # The order's own shopper, or a staff operator handling a return.
    services.get_payment_for(request.user, order_id, staff=request.user.is_staff)
    data = read_json(request)
    result = services.refund(order_id, idempotency_key=idempotency_key(request, data), amount_raw=data.get('amount'))
    body = {'orderId': order_id, 'refundId': result.refund.pk if result.refund else None,
            'refund': services.refund_json(result.refund) if result.refund else None,
            'payment': services.payment_json(result.payment)}
    return answer(result, body, done_status=201)


@api(['GET'])
def my_orders(request):
    qs = Order.objects.filter(user=request.user).order_by('-date_placed').prefetch_related('lines')
    return JsonResponse({'orders': [services.order_json(o) for o in qs]})


@api(['GET'], staff=True)
def reconciliation(request):
    start = parse_iso(request.GET.get('from'), 'from')
    end = parse_iso(request.GET.get('to'), 'to')
    return JsonResponse(services.reconcile(start, end))


def parse_iso(value, name):
    parsed = parse_datetime(value or '')
    if parsed is None:
        raise services.ApiProblem(400, 'invalid_range', '"%s" must be an ISO-8601 date-time.' % name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

@api(['GET', 'POST'])
@sensitive_variables('data', 'card')
def payment_methods(request):
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': [services.card_json(c) for c in services.list_cards(request.user)]})
    data = read_json(request)
    card = parse_card(data.get('card'))
    result = services.save_card(request.user, card, idempotency_key(request, data))
    body = services.card_json(result.card) if result.card else {'paymentMethodId': None}
    return answer(result, body, done_status=201)


@api(['DELETE'])
def payment_method(request, payment_method_id):
    result = services.delete_card(request.user, payment_method_id)
    return answer(result, {'paymentMethodId': payment_method_id})
