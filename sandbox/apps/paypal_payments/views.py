"""
JSON API views. Callers authenticate with the sandbox's Django session login
and send the CSRF token (``X-CSRFToken`` header) on unsafe methods.
"""
import json
import logging
import re
from datetime import date

from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.csrf import csrf_failure as django_csrf_failure
from django.views.decorators.debug import sensitive_variables
from oscar.apps.payment.bankcards import luhn

from . import services
from .gateway import BillingAddress, CardInput
from .serializers import order_to_dict, payment_method_to_dict, refund_to_dict
from .services import InvalidRequest, ServiceError

logger = logging.getLogger(__name__)

EXPIRY_RE = re.compile(r'^(\d{4})-(0[1-9]|1[0-2])$')
DIGITS_RE = re.compile(r'^\d+$')


def error_response(status, code, message, **details):
    body = {'error': {'code': code, 'message': message}}
    body['error'].update(details)
    return JsonResponse(body, status=status)


def api_view(methods, *, staff_only=False):
    """Build a view from a {method: handler} map, with JSON auth errors and
    ServiceError mapping.

    Views run outside ATOMIC_REQUESTS: they call PayPal, and the payment
    in-flight lease must be committed (visible to a concurrent double-click)
    before that call, not when the request ends.
    """
    @transaction.non_atomic_requests
    def view(request, *args, **kwargs):
        handler = methods.get(request.method)
        if handler is None:
            response = error_response(405, 'method_not_allowed',
                                      'Method %s not allowed' % request.method)
            response['Allow'] = ', '.join(methods)
            return response
        if not request.user.is_authenticated:
            return error_response(
                401, 'not_authenticated',
                'Sign in first (session login at /en-gb/accounts/login/).')
        if staff_only and not request.user.is_staff:
            return error_response(403, 'forbidden', 'This action is restricted to staff.')
        try:
            return handler(request, *args, **kwargs)
        except ServiceError as exc:
            return error_response(exc.status, exc.code, exc.message, **exc.details)
    return view


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise InvalidRequest('Request body must be JSON', code='invalid_json') from None
    if not isinstance(data, dict):
        raise InvalidRequest('Request body must be a JSON object', code='invalid_json')
    return data


def _text(data, key, *, max_length=255, required=False):
    value = data.get(key, '')
    if value is None:
        value = ''
    if not isinstance(value, str) or len(value) > max_length:
        raise InvalidRequest('%s must be a string of at most %d characters' % (key, max_length),
                             code='invalid_card')
    if required and not value.strip():
        raise InvalidRequest('%s is required' % key, code='invalid_card')
    return value.strip()


@sensitive_variables('data', 'number', 'security_code')
def parse_card(data):
    """Validate a card object from a request. The result is used for one PayPal
    call and never stored."""
    if not isinstance(data, dict):
        raise InvalidRequest('"card" must be an object', code='invalid_card')
    number = re.sub(r'[\s-]', '', _text(data, 'number', max_length=32, required=True))
    if not DIGITS_RE.match(number) or not 12 <= len(number) <= 19 or not luhn(number):
        raise InvalidRequest('card number is not valid', code='invalid_card')
    expiry = _text(data, 'expiry', max_length=7, required=True)
    match = EXPIRY_RE.match(expiry)
    today = date.today()
    if not match or (int(match.group(1)), int(match.group(2))) < (today.year, today.month):
        raise InvalidRequest('expiry must be a future month as YYYY-MM', code='invalid_card')
    security_code = _text(data, 'securityCode', max_length=4)
    if security_code and not (DIGITS_RE.match(security_code) and len(security_code) in (3, 4)):
        raise InvalidRequest('securityCode must be 3 or 4 digits', code='invalid_card')
    address = None
    raw_address = data.get('billingAddress')
    if raw_address is not None:
        if not isinstance(raw_address, dict):
            raise InvalidRequest('billingAddress must be an object', code='invalid_card')
        country = _text(raw_address, 'countryCode', max_length=2, required=True).upper()
        if not re.match(r'^[A-Z]{2}$', country):
            raise InvalidRequest('billingAddress.countryCode must be a 2-letter code',
                                 code='invalid_card')
        address = BillingAddress(
            country_code=country,
            address_line_1=_text(raw_address, 'addressLine1', max_length=300),
            address_line_2=_text(raw_address, 'addressLine2', max_length=300),
            admin_area_2=_text(raw_address, 'adminArea2', max_length=120),
            admin_area_1=_text(raw_address, 'adminArea1', max_length=300),
            postal_code=_text(raw_address, 'postalCode', max_length=60))
    return CardInput(number=number, expiry=expiry, security_code=security_code,
                     name=_text(data, 'name', max_length=300), billing_address=address)


def _order_response(order_id, status=200):
    order = services.Order._default_manager.select_related('paypal_payment').get(pk=order_id)
    return JsonResponse(order_to_dict(order), status=status)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

def _create_order(request):
    data = _json_body(request)
    order = services.place_order(request.user, data.get('items'), request=request)
    return _order_response(order.pk, status=201)


orders = api_view({'POST': _create_order})


@sensitive_variables('data', 'card')
def _pay(request, order_id):
    data = _json_body(request)
    card = parse_card(data['card']) if data.get('card') is not None else None
    payment_method_id = data.get('paymentMethodId')
    if payment_method_id is not None and (not isinstance(payment_method_id, int)
                                          or isinstance(payment_method_id, bool)):
        raise InvalidRequest('paymentMethodId must be an integer', code='invalid_payment_source')
    services.pay(request.user, order_id, card=card, payment_method_id=payment_method_id)
    return _order_response(order_id)


pay = api_view({'POST': _pay})


def _fulfil(request, order_id):
    services.fulfil(request.user, order_id)
    return _order_response(order_id)


fulfil = api_view({'POST': _fulfil}, staff_only=True)


def _cancel(request, order_id):
    services.cancel(request.user, order_id)
    return _order_response(order_id)


cancel = api_view({'POST': _cancel}, staff_only=True)


def _create_refund(request, order_id):
    data = _json_body(request)
    key = request.headers.get('Idempotency-Key') or data.get('idempotencyKey') or ''
    if not isinstance(key, str):
        raise InvalidRequest('idempotencyKey must be a string', code='idempotency_key_required')
    note = data.get('note') or ''
    if not isinstance(note, str):
        raise InvalidRequest('note must be a string', code='invalid_note')
    refund, created = services.refund(
        request.user, order_id, idempotency_key=key.strip(),
        amount=services.parse_amount(data.get('amount')), note=note)
    body = refund_to_dict(refund)
    order = services.Order._default_manager.select_related('paypal_payment').get(pk=order_id)
    body['order'] = order_to_dict(order)
    return JsonResponse(body, status=201 if created else 200)


def _list_refunds(request, order_id):
    order = services.get_order(request.user, order_id, allow_staff=True)
    payment = services.payment_for(order)
    return JsonResponse({'refunds': [refund_to_dict(r) for r in payment.refunds.all()]})


refunds = api_view({'POST': _create_refund, 'GET': _list_refunds})


def _my_orders(request):
    qs = (services.Order._default_manager.filter(user=request.user)
          .select_related('paypal_payment').prefetch_related('lines')
          .order_by('-date_placed'))
    return JsonResponse({'orders': [order_to_dict(o) for o in qs]})


my_orders = api_view({'GET': _my_orders})


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------

def _list_payment_methods(request):
    return JsonResponse({'paymentMethods': [
        payment_method_to_dict(c) for c in services.list_cards(request.user)]})


@sensitive_variables('data', 'card')
def _create_payment_method(request):
    data = _json_body(request)
    card = parse_card(data.get('card'))
    key = request.headers.get('Idempotency-Key') or None
    bankcard = services.save_card(request.user, card, idempotency_key=key)
    return JsonResponse(payment_method_to_dict(bankcard), status=201)


payment_methods = api_view({'GET': _list_payment_methods, 'POST': _create_payment_method})


def _delete_payment_method(request, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return HttpResponse(status=204)


payment_method_detail = api_view({'DELETE': _delete_payment_method})


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def _reconciliation(request):
    raw_from, raw_to = request.GET.get('from'), request.GET.get('to')
    try:
        start = parse_datetime(raw_from or '')
        end = parse_datetime(raw_to or '')
    except ValueError:
        start = end = None
    if start is None or end is None:
        raise InvalidRequest('from and to are required ISO-8601 date-times with a UTC offset, '
                             'e.g. 2026-09-01T00:00:00Z', code='invalid_range')
    return JsonResponse(services.reconcile(start, end))


reconciliation = api_view({'GET': _reconciliation}, staff_only=True)


# --------------------------------------------------------------------------
# CSRF failures as JSON for the API
# --------------------------------------------------------------------------

def csrf_failure(request, reason=''):
    if request.path.startswith('/api/'):
        return error_response(403, 'csrf_failed',
                              'CSRF check failed: send the csrftoken cookie value in an '
                              'X-CSRFToken header (%s)' % reason)
    return django_csrf_failure(request, reason=reason)
