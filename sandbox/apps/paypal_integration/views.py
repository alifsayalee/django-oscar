"""
JSON API for orders, payments and saved cards.

Callers authenticate with Django's session login (the same session the
storefront uses; ``POST /api/login`` is a JSON front door onto it) and send the
CSRF token as ``X-CSRFToken`` on unsafe methods. Views manage their own
transactions: a payment claim has to be committed before PayPal is called,
which the sandbox's ATOMIC_REQUESTS would otherwise prevent.
"""
import functools
import json
import logging
from decimal import Decimal

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from . import services
from .gateway import ProviderError, format_amount
from .models import PayPalPayment, ProviderWrite
from .safe_write import AmountMismatch, OutcomeUnknown

log = logging.getLogger(__name__)

# The one place an outcome becomes the caller's HTTP status.
_OUTCOME_STATUS = {
    ProviderWrite.DONE: 200,
    ProviderWrite.PENDING: 202,
    ProviderWrite.SENDING: 202,
    ProviderWrite.FAILED: 409,
    ProviderWrite.NEEDS_REVIEW: 409,
}


def outcome_status(outcome, created=False):
    status = _OUTCOME_STATUS.get(outcome, 504)       # unknown: may have happened
    return 201 if created and status == 200 else status


def error(status, code, message, **extra):
    return JsonResponse({'error': code, 'message': message, **extra}, status=status)


def api(methods, staff=False):
    def decorate(view):
        @functools.wraps(view)
        @sensitive_post_parameters()
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return error(405, 'method_not_allowed', 'use %s' % ' or '.join(methods))
            if not request.user.is_authenticated:
                return error(401, 'authentication_required', 'log in first (POST /api/login)')
            if staff and not request.user.is_staff:
                return error(403, 'forbidden', 'this action is restricted to staff')
            if request.method in ('POST', 'PUT', 'PATCH'):
                try:
                    request.json = json.loads(request.body or b'{}')
                except ValueError:
                    return error(400, 'invalid_json', 'request body must be JSON')
                if not isinstance(request.json, dict):
                    return error(400, 'invalid_json', 'request body must be a JSON object')
            try:
                return view(request, *args, **kwargs)
            except services.DomainError as exc:
                return error(exc.http_status, exc.code, exc.message)
            except ProviderError as exc:
                return error(exc.http_status, 'paypal_error', exc.message,
                             outcomeUnknown=exc.outcome_unknown, issues=list(exc.issues))
            except OutcomeUnknown as exc:
                return error(504, 'outcome_unknown', (
                    'PayPal did not confirm the outcome. Nothing will be sent twice: repeat the same '
                    'request to re-check it.'), outcomeUnknown=True, **_write_ids(exc.record))
            except AmountMismatch as exc:
                return error(409, 'needs_review', 'PayPal processed a different amount than requested; '
                             'an operator must review it.', **_write_ids(exc.record))
        return transaction.non_atomic_requests(wrapper)
    return decorate


def _write_ids(record):
    ids = {'reference': str(record.public_id)}
    if record.kind == ProviderWrite.REFUND:
        ids['refundId'] = str(record.public_id)
    if record.order_id:
        ids['orderId'] = record.order.number
    return ids


def _idempotency_key(request):
    return (request.headers.get('Idempotency-Key') or str(request.json.get('idempotencyKey') or '')).strip()


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------

def _money(value, currency):
    return None if value is None else format_amount(Decimal(value), currency)


def payment_json(order):
    payment = PayPalPayment.objects.select_related('source').filter(source__order=order).first()
    if payment is None:
        return None
    source = payment.source
    cur = source.currency
    refunds = ProviderWrite.objects.filter(order=order, kind=ProviderWrite.REFUND).order_by('date_created')
    unresolved = ProviderWrite.objects.filter(order=order, outcome__in=services.UNSETTLED).order_by('date_created')
    refunded_or_reserved = sum((r.amount for r in refunds if r.outcome in services.RESERVING), 0)
    return {
        'state': payment.state,
        'currency': source.currency,
        'amountAuthorized': _money(source.amount_allocated, cur),
        'amountCaptured': _money(source.amount_debited, cur),
        'amountRefunded': _money(source.amount_refunded, cur),
        'amountRefundable': _money(max(payment.captured_amount - refunded_or_reserved, 0)
                                   if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED)
                                   else 0, cur),
        'paypalOrderId': payment.paypal_order_id or None,
        'card': {'brand': payment.card_brand, 'lastDigits': payment.card_last_digits}
        if payment.card_last_digits else None,
        'authorization': {
            'id': payment.authorization_id,
            'status': payment.authorization_status,
            'createdAt': payment.authorization_created_at.isoformat() if payment.authorization_created_at else None,
            'expiresAt': payment.authorization_expires_at.isoformat() if payment.authorization_expires_at else None,
        } if payment.authorization_id else None,
        'capture': {
            'id': payment.capture_id,
            'status': payment.capture_status,
            'amount': _money(payment.captured_amount, cur),
            'paypalFee': _money(payment.paypal_fee, cur),
            'netAmount': _money(payment.net_amount, cur),
        } if payment.capture_id else None,
        'refunds': [refund_json(r) for r in refunds],
        'unresolvedOperations': [
            {'kind': w.kind, 'outcome': w.outcome, 'reference': str(w.public_id)} for w in unresolved],
    }


def refund_json(record):
    return {
        'refundId': str(record.public_id),
        'outcome': record.outcome,
        'amount': _money(record.amount, record.currency),
        'currency': record.currency,
        'paypalRefundId': record.provider_id or None,
        'paypalStatus': record.provider_status or None,
        'message': record.message or None,
        'createdAt': record.date_created.isoformat(),
    }


def order_json(order):
    return {
        'orderId': str(order.number),
        'status': order.status,
        'currency': order.currency,
        'total': _money(order.total_incl_tax, order.currency),
        'placedAt': order.date_placed.isoformat(),
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'unitPrice': _money(line.unit_price_incl_tax, order.currency),
            'linePrice': _money(line.line_price_incl_tax, order.currency),
        } for line in order.lines.all()],
        'payment': payment_json(order),
    }


def card_json(card):
    return {
        'paymentMethodId': card.pk,
        'brand': card.card_type,
        'lastDigits': card.number[-4:],
        'expiry': card.expiry_date.strftime('%Y-%m') if card.expiry_date.year < 9999 else None,
        'label': '%s ending %s' % (card.card_type, card.number[-4:]),
    }


def _outcome_response(record, body, created=False):
    body = {'outcome': record.outcome, **body}
    if record.message and record.outcome != ProviderWrite.DONE:
        body['message'] = record.message
    return JsonResponse(body, status=outcome_status(record.outcome, created))


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({'csrfToken': get_token(request)})


@sensitive_post_parameters()
@sensitive_variables('password', 'data')
def session_login(request):
    if request.method != 'POST':
        return error(405, 'method_not_allowed', 'use POST')
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return error(400, 'invalid_json', 'request body must be JSON')
    username, password = str(data.get('username') or data.get('email') or ''), str(data.get('password') or '')
    user = authenticate(request, username=username, password=password) or \
        authenticate(request, email=username, password=password)
    if user is None:
        return error(401, 'invalid_credentials', 'unknown user or wrong password')
    login(request, user)
    return JsonResponse({'userId': user.pk, 'isStaff': user.is_staff, 'csrfToken': get_token(request)})


def session_logout(request):
    if request.method != 'POST':
        return error(405, 'method_not_allowed', 'use POST')
    logout(request)
    return JsonResponse({'loggedOut': True})


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

@api(['POST'])
def orders(request):
    order = services.place_order(request.user, request.json.get('lines'), request)
    return JsonResponse(order_json(order), status=201)


@api(['GET'])
def my_orders(request):
    qs = services.Order.objects.filter(user=request.user).order_by('-date_placed').prefetch_related('lines')
    return JsonResponse({'orders': [order_json(o) for o in qs[:200]]})


@api(['POST'])
def pay(request, order_id):
    record = services.pay(request.user, order_id, request.json)
    order = services.order_for_shopper(request.user, order_id)
    return _outcome_response(record, {'orderId': order.number, 'status': order.status,
                                      'payment': payment_json(order)})


@api(['POST'], staff=True)
def fulfil(request, order_id):
    record = services.fulfil(order_id)
    order = services.order_for_operator(order_id)
    return _outcome_response(record, {'orderId': order.number, 'status': order.status,
                                      'payment': payment_json(order)})


@api(['POST'], staff=True)
def cancel(request, order_id):
    record, order = services.cancel(order_id)
    body = {'orderId': order.number, 'status': order.status, 'payment': payment_json(order)}
    if record is None:                               # nothing was held at PayPal
        return JsonResponse({'outcome': ProviderWrite.DONE, **body})
    return _outcome_response(record, body)


@api(['POST'])
def refunds(request, order_id):
    record = services.refund(request.user, order_id, request.json, _idempotency_key(request))
    order = services.order_for_shopper(request.user, order_id)
    return _outcome_response(record, {**refund_json(record), 'orderId': order.number,
                                      'payment': payment_json(order)}, created=True)


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------

@api(['GET', 'POST'])
def payment_methods(request):
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': [card_json(c) for c in services.saved_cards(request.user)]})
    record = services.save_card(request.user, request.json, _idempotency_key(request))
    body = {}
    if record.bankcard_id:
        card = services.Bankcard.objects.filter(pk=record.bankcard_id, user=request.user).first()
        if card is not None:
            body = card_json(card)
    return _outcome_response(record, body, created=True)


@api(['DELETE'])
def payment_method(request, payment_method_id):
    record = services.delete_card(request.user, payment_method_id)
    if record.outcome == ProviderWrite.DONE:
        return HttpResponse(status=204)
    return _outcome_response(record, {'paymentMethodId': payment_method_id})


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

@api(['GET'], staff=True)
def reconciliation(request):
    start = services.parse_datetime(request.GET.get('from'), 'from')
    end = services.parse_datetime(request.GET.get('to'), 'to')
    return JsonResponse(services.reconcile(start, end))
