"""
JSON endpoints under /api/.

Callers authenticate with Django's session login (the sandbox's own login
page, or ``POST /api/auth/login``) and send the CSRF token in the
``X-CSRFToken`` header on unsafe methods, as for any Django form.
"""
import functools
import json
import logging
from decimal import Decimal

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from oscar.core.loading import get_model

from . import cards, fulfilment, ordering, payflow, reconciliation
from .errors import ApiProblem
from .models import PaymentState
from .money import parse_amount, to_wire

logger = logging.getLogger('apps.payments')
Order = get_model('order', 'Order')

MAX_BODY = 64 * 1024


def api(methods, *, staff=False, authenticated=True):
    """JSON in, JSON out; authentication, method and error handling in one place."""
    def decorator(view):
        @functools.wraps(view)
        @never_cache
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = JsonResponse({'error': 'method_not_allowed'}, status=405)
                response['Allow'] = ', '.join(methods)
                return response
            if authenticated and not request.user.is_authenticated:
                return JsonResponse({'error': 'not_authenticated',
                                     'message': 'Sign in first.'}, status=401)
            if staff and not request.user.is_staff:
                return JsonResponse({'error': 'forbidden',
                                     'message': 'Staff only.'}, status=403)
            try:
                body = _json_body(request)
                status, payload = view(request, body, *args, **kwargs)
            except ApiProblem as problem:
                return JsonResponse(problem.as_dict(), status=problem.status_code)
            return JsonResponse(payload, status=status, safe=not isinstance(payload, list))
        return wrapper
    return decorator


def _json_body(request) -> dict:
    if request.method in ('GET', 'DELETE', 'HEAD'):
        return {}
    if len(request.body) > MAX_BODY:
        raise ApiProblem(413, 'too_large', 'Request body too large.')
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ApiProblem(400, 'invalid_json', 'The request body must be JSON.') from None
    if not isinstance(body, dict):
        raise ApiProblem(400, 'invalid_json', 'The request body must be a JSON object.')
    return body


def _own_order(request, number):
    """The caller's order; someone else's order is indistinguishable from a missing one."""
    order = Order.objects.filter(number=number, user=request.user).first()
    return _with_payment(order)


def _any_order(number):
    return _with_payment(Order.objects.filter(number=number).first())


def _with_payment(order):
    if order is None or not hasattr(order, 'paypal_payment'):
        raise ApiProblem(404, 'order_not_found', 'No such order.')
    return order


# -- serialization -----------------------------------------------------------

def _money(value, currency):
    return to_wire(value, currency) if value is not None else None


def _time(value):
    return value.isoformat() if value else None


def payment_json(payment) -> dict:
    c = payment.currency
    refundable = Decimal('0')
    if payment.capture_id and payment.state == PaymentState.CAPTURED:
        refundable = max(payment.captured_amount - payment.refund_reserved, Decimal('0'))
    return {
        'state': payment.state,
        'amount': _money(payment.amount, c),
        'currency': c,
        'attempt': payment.attempt,
        'card': ({'brand': payment.card_brand, 'lastDigits': payment.card_last_digits}
                 if payment.card_last_digits else None),
        'paymentMethodId': str(payment.saved_card.public_id) if payment.saved_card_id else None,
        'paypal': {
            'orderId': payment.paypal_order_id or None,
            'orderStatus': payment.paypal_order_status or None,
            'authorization': {
                'id': payment.authorization_id,
                'status': payment.authorization_status,
                'authorizedAt': _time(payment.authorized_at),
                'expiresAt': _time(payment.authorization_expires_at),
            } if payment.authorization_id else None,
            'capture': {
                'id': payment.capture_id,
                'status': payment.capture_status,
                'amount': _money(payment.captured_amount, c),
                'paypalFee': _money(payment.paypal_fee, c),
                'netAmount': _money(payment.net_amount, c),
                'capturedAt': _time(payment.captured_at),
            } if payment.capture_id else None,
        },
        'refundedAmount': _money(payment.refunded_amount, c),
        'refundableAmount': _money(refundable, c),
        'refunds': [refund_json(r) for r in payment.refunds.order_by('created_at')],
        'lastError': payment.last_error or None,
    }


def refund_json(refund) -> dict:
    return {
        'refundId': str(refund.public_id),
        'amount': _money(refund.amount, refund.payment.currency),
        'state': refund.state,
        'paypalRefundId': refund.paypal_refund_id or None,
        'paypalStatus': refund.paypal_status or None,
        'idempotencyKey': refund.idempotency_key,
        'createdAt': _time(refund.created_at),
    }


def order_json(order) -> dict:
    payment = order.paypal_payment
    return {
        'orderId': str(order.number),
        'status': order.status,
        'total': _money(order.total_incl_tax, payment.currency),
        'currency': payment.currency,
        'placedAt': _time(order.date_placed),
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'unitPrice': _money(line.unit_price_incl_tax, payment.currency),
            'linePrice': _money(line.line_price_incl_tax, payment.currency),
        } for line in order.lines.all()],
        'payment': payment_json(payment),
    }


def card_json(card) -> dict:
    return {
        'paymentMethodId': str(card.public_id),
        'brand': card.brand,
        'lastDigits': card.last_digits,
        'expiry': card.expiry,
        'cardholderName': card.cardholder_name or None,
        'state': card.state,
        'createdAt': _time(card.created_at),
    }


# -- session -----------------------------------------------------------------

@ensure_csrf_cookie
@api(['GET'], authenticated=False)
def csrf(request, body):
    user = request.user
    return 200, {'csrfToken': get_token(request),
                 'user': {'id': user.pk, 'email': user.email, 'isStaff': user.is_staff}
                 if user.is_authenticated else None}


@sensitive_variables('body', 'password')
@sensitive_post_parameters()
@api(['POST'], authenticated=False)
def session_login(request, body):
    username = body.get('username') or body.get('email')
    password = body.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        raise ApiProblem(400, 'invalid_request', '"username" (or "email") and "password" are required.')
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        raise ApiProblem(401, 'invalid_credentials', 'Wrong username or password.')
    login(request, user)
    return 200, {'user': {'id': user.pk, 'email': user.email, 'isStaff': user.is_staff},
                 'csrfToken': get_token(request)}


@api(['POST'], authenticated=False)
def session_logout(request, body):
    logout(request)
    return 200, {'loggedOut': True}


# -- orders ------------------------------------------------------------------

@api(['POST'])
def orders(request, body):
    order = ordering.place_order(request.user, body.get('lines'), body.get('shippingAddress'),
                                 request=request)
    logger.info('Order %s placed by user %s via API', order.number, request.user.pk)
    return 201, order_json(order)


@api(['GET'])
def my_orders(request, body):
    placed = (Order.objects.filter(user=request.user, paypal_payment__isnull=False)
              .select_related('paypal_payment').prefetch_related('lines')
              .order_by('-date_placed'))
    return 200, {'orders': [order_json(o) for o in placed]}


@sensitive_variables('body')
@sensitive_post_parameters()
@api(['POST'])
def pay(request, body, number):
    order = _own_order(request, number)
    status, payment = payflow.pay(request.user, order, body)
    return status, {'orderId': order.number, 'orderStatus': _status(order),
                    'payment': payment_json(payment)}


@api(['POST'], staff=True)
def fulfil(request, body, number):
    order = _any_order(number)
    status, payment = fulfilment.fulfil(order)
    return status, {'orderId': order.number, 'orderStatus': _status(order),
                    'payment': payment_json(payment)}


@api(['POST'], staff=True)
def cancel(request, body, number):
    order = _any_order(number)
    status, payment = fulfilment.cancel(order)
    return status, {'orderId': order.number, 'orderStatus': _status(order),
                    'payment': payment_json(payment)}


@api(['POST'])
def refunds(request, body, number):
    order = _own_order(request, number)
    key = request.headers.get('Idempotency-Key') or body.get('idempotencyKey')
    raw_amount = body.get('amount')
    try:
        amount = None if raw_amount is None else parse_amount(raw_amount, order.paypal_payment.currency)
    except ValueError as e:
        raise ApiProblem(400, 'invalid_amount', str(e)) from None
    status, record = fulfilment.refund(request.user, order, amount, key)
    record.payment.refresh_from_db()
    return status, {'refundId': str(record.public_id), 'orderId': order.number,
                    'refund': refund_json(record), 'payment': payment_json(record.payment)}


@api(['GET'], staff=True)
def reconciliation_report(request, body):
    start, end = reconciliation.parse_range(request.GET.get('from'), request.GET.get('to'))
    return 200, reconciliation.reconcile(start, end)


def _status(order):
    order.refresh_from_db(fields=['status'])
    return order.status


# -- saved cards -------------------------------------------------------------

@sensitive_variables('body')
@sensitive_post_parameters()
@api(['GET', 'POST'])
def payment_methods(request, body):
    if request.method == 'GET':
        return 200, {'paymentMethods': [card_json(c) for c in cards.list_cards(request.user)]}
    status, card = cards.save_card(request.user, body, request.headers.get('Idempotency-Key'))
    return status, card_json(card)


@api(['DELETE'])
def payment_method(request, body, public_id):
    card = cards.delete_card(request.user, public_id)
    return 200, {'paymentMethodId': str(card.public_id), 'deleted': True}
