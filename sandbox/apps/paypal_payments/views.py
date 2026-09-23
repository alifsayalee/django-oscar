"""
JSON API for PayPal payments and saved cards.

Callers authenticate with Django's session login (the sandbox's own login page,
or ``POST /api/auth/login``) and send the CSRF token on unsafe requests.
Views opt out of the sandbox's ``ATOMIC_REQUESTS`` so that no PayPal call ever
runs inside a database transaction; the services open their own short ones.
"""
import functools
import json
import logging
import re
from datetime import timezone as dt_timezone

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_variables

from . import services
from .gateway import CardDetails, PaymentGatewayError
from .serializers import order_json, payment_method_json, refund_json

logger = logging.getLogger(__name__)

_EXPIRY = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
_ADDRESS_FIELDS = {
    'addressLine1': 'address_line_1', 'addressLine2': 'address_line_2', 'adminArea2': 'admin_area_2',
    'adminArea1': 'admin_area_1', 'postalCode': 'postal_code', 'countryCode': 'country_code',
}


class BadRequest(Exception):
    pass


def _error(status, code, message, **details):
    body = {'error': {'code': code, 'message': message}}
    if details:
        body['error']['details'] = details
    return JsonResponse(body, status=status)


def api(methods, staff=False, login_required=True):
    """Method dispatch, authentication, JSON parsing and error translation."""
    def decorator(view):
        @functools.wraps(view)
        @transaction.non_atomic_requests
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = _error(405, 'method_not_allowed', 'Use %s.' % ', '.join(methods))
                response['Allow'] = ', '.join(methods)
                return response
            if login_required and not request.user.is_authenticated:
                return _error(401, 'not_authenticated', 'Sign in first.')
            if staff and not request.user.is_staff:
                return _error(403, 'forbidden', 'This action is restricted to staff.')
            if request.method in ('POST', 'PUT', 'PATCH'):
                try:
                    request.json = json.loads(request.body or b'{}')
                except ValueError:
                    return _error(400, 'invalid_json', 'The request body must be JSON.')
                if not isinstance(request.json, dict):
                    return _error(400, 'invalid_json', 'The request body must be a JSON object.')
            try:
                return view(request, *args, **kwargs)
            except BadRequest as exc:
                return _error(400, 'invalid_request', str(exc))
            except services.ServiceError as exc:
                return _error(exc.http_status, exc.code, exc.message, **exc.details)
            except PaymentGatewayError as exc:
                details = {'outcomeUnknown': exc.outcome_unknown}
                if exc.issue:
                    details['paypalIssue'] = exc.issue
                if exc.debug_id:
                    details['paypalDebugId'] = exc.debug_id
                return _error(exc.http_status, exc.code, exc.message, **details)
            except ImproperlyConfigured as exc:
                logger.error('Payments are misconfigured: %s', exc)
                return _error(503, 'payments_not_configured', 'Payments are not configured on this site.')
        return wrapper
    return decorator


# --------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------

@sensitive_variables('data', 'number')
def _card(data):
    """Validate caller-supplied card details. They are passed to PayPal and dropped."""
    if not isinstance(data, dict):
        raise BadRequest('card must be an object.')
    number = re.sub(r'[\s-]', '', str(data.get('number', '')))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn(number):
        raise BadRequest('card.number is not a valid card number.')
    expiry = str(data.get('expiry', ''))
    if not _EXPIRY.match(expiry):
        raise BadRequest('card.expiry must be YYYY-MM.')
    security_code = data.get('securityCode')
    if security_code is not None and not re.fullmatch(r'\d{3,4}', str(security_code)):
        raise BadRequest('card.securityCode must be 3 or 4 digits.')
    name = data.get('name')
    address = data.get('billingAddress')
    billing = None
    if address is not None:
        if not isinstance(address, dict) or not address.get('countryCode'):
            raise BadRequest('card.billingAddress must be an object with at least countryCode.')
        billing = {model: str(address[key]) for key, model in _ADDRESS_FIELDS.items() if address.get(key)}
    return CardDetails(
        number=number,
        expiry=expiry,
        security_code=str(security_code) if security_code is not None else None,
        name=str(name)[:300] if name else None,
        billing_address=billing,
    )


def _luhn(number):
    total = 0
    for index, digit in enumerate(int(d) for d in reversed(number)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _moment(request, name):
    raw = request.GET.get(name)
    if not raw:
        raise BadRequest('"%s" is required (ISO-8601 date-time).' % name)
    parsed = parse_datetime(raw.replace(' ', '+'))  # an unencoded "+" arrives as a space
    if parsed is None:
        raise BadRequest('"%s" must be an ISO-8601 date-time.' % name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

@ensure_csrf_cookie
@api(['GET'], login_required=False)
def csrf(request):
    return JsonResponse({'csrfToken': get_token(request)})


@api(['POST'], login_required=False)
@sensitive_variables('password')
def session_login(request):
    username = request.json.get('username') or request.json.get('email')
    password = request.json.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        raise BadRequest('Send "username" (or "email") and "password".')
    user = authenticate(request, username=username, password=password)
    if user is None:
        return _error(401, 'invalid_credentials', 'Unknown user or wrong password.')
    login(request, user)
    return JsonResponse({'userId': user.pk, 'username': user.get_username(), 'isStaff': user.is_staff,
                         'csrfToken': get_token(request)})


@api(['POST'])
def session_logout(request):
    logout(request)
    return JsonResponse({}, status=200)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

@api(['POST'])
def orders(request):
    order = services.place_order(
        request.user, request.json.get('items'), request.json.get('shippingAddress'), request=request)
    return JsonResponse(order_json(order), status=201)


@api(['POST'])
@sensitive_variables('card', 'body')
def pay(request, order_id):
    body = request.json
    card = _card(body['card']) if body.get('card') is not None else None
    saved_card_id = body.get('paymentMethodId')
    if saved_card_id is not None and (isinstance(saved_card_id, bool) or not isinstance(saved_card_id, int)):
        raise BadRequest('paymentMethodId must be an integer.')
    order = services.pay(request.user, order_id, card=card, saved_card_id=saved_card_id)
    return JsonResponse(order_json(order))


@api(['POST'], staff=True)
def fulfil(request, order_id):
    return JsonResponse(order_json(services.fulfil(order_id)))


@api(['POST'], staff=True)
def cancel(request, order_id):
    return JsonResponse(order_json(services.cancel(order_id)))


@api(['POST'])
def refunds(request, order_id):
    key = request.headers.get('Idempotency-Key') or request.json.get('idempotencyKey')
    refund, created = services.refund(request.user, order_id, key, request.json.get('amount'))
    order = services.get_own_order(request.user, order_id)
    body = refund_json(refund)
    body['order'] = order_json(order)
    return JsonResponse(body, status=201 if created else 200)


@api(['GET'])
def my_orders(request):
    orders_ = services._order_queryset().filter(user=request.user).order_by('-date_placed')
    return JsonResponse({'orders': [order_json(o) for o in orders_]})


@api(['GET'], staff=True)
def reconciliation(request):
    report = services.reconcile(_moment(request, 'from'), _moment(request, 'to'))
    return JsonResponse(report)


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------

@api(['GET', 'POST'])
@sensitive_variables('card', 'body')
def payment_methods(request):
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': [
            payment_method_json(c) for c in services.saved_cards(request.user)]})
    body = request.json
    card = _card(body.get('card', body))
    bankcard = services.save_card(
        request.user, card, idempotency_key=request.headers.get('Idempotency-Key'))
    return JsonResponse(payment_method_json(bankcard), status=201)


@api(['DELETE'])
def payment_method(request, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return JsonResponse({}, status=200)
