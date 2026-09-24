"""
JSON endpoints under /api/. Callers authenticate with the site's normal
Django session login; POST/DELETE need the CSRF token (see GET /api/session).
"""
import functools
import json
import logging
import re
import uuid
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views import csrf as django_csrf
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from oscar.core.loading import get_model

from . import gateway, services
from .errors import ApiProblem
from .models import OrderPayment, Outcome, SavedCard

logger = logging.getLogger(__name__)

Order = get_model('order', 'Order')

IDEMPOTENCY_KEY_RE = re.compile(r'^[A-Za-z0-9._:\-]{1,255}$')


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def api(methods, staff=False):
    """
    JSON endpoint: allowed methods, session auth (401), staff-only (403), and
    this app's errors turned into JSON. Views run outside ATOMIC_REQUESTS so
    each PayPal claim is committed before PayPal is called.
    """
    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return _error(405, 'method_not_allowed', 'Use %s.' % ' or '.join(methods))
            if not request.user.is_authenticated:
                return _error(401, 'not_authenticated', 'Sign in first (Django session login).')
            if staff and not request.user.is_staff:
                return _error(403, 'forbidden', 'Only staff can do this.')
            try:
                return view(request, *args, **kwargs)
            except ApiProblem as problem:
                return JsonResponse(problem.as_dict(), status=problem.status)
            except ImproperlyConfigured:
                logger.exception('PayPal integration is misconfigured')
                return _error(503, 'not_configured', 'PayPal payments are not configured on this site.')
        return never_cache(transaction.non_atomic_requests(wrapper))
    return decorator


def _error(status, code, message, **extra):
    body = {'code': code, 'message': message}
    body.update(extra)
    return JsonResponse({'error': body}, status=status)


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ApiProblem(400, 'invalid_json', 'The request body must be a JSON object.') from None
    if not isinstance(data, dict):
        raise ApiProblem(400, 'invalid_json', 'The request body must be a JSON object.')
    return data


def _idempotency_key(request, data, required):
    key = request.headers.get('Idempotency-Key') or data.get('idempotencyKey')
    if not key:
        if required:
            raise ApiProblem(400, 'idempotency_key_required',
                             'Send an Idempotency-Key header (or idempotencyKey field) with every refund.')
        return None
    key = str(key)
    if not IDEMPOTENCY_KEY_RE.match(key):
        raise ApiProblem(400, 'invalid_idempotency_key',
                         'The idempotency key must be 1-255 characters of letters, digits, . _ : -')
    return key


def _decimal(value, name):
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ApiProblem(422, 'invalid_amount', '%r must be a decimal amount such as "10.00".' % name) from None
    if not amount.is_finite():
        raise ApiProblem(422, 'invalid_amount', '%r must be a decimal amount.' % name)
    return amount


def _money(value, cur):
    return None if value is None else gateway.format_amount(value, cur)


@sensitive_variables('data', 'number', 'code')
def _parse_card(data):
    """Card details from the request, validated; never echoed back or logged."""
    if not isinstance(data, dict):
        raise ApiProblem(422, 'invalid_card', '"card" must be an object.')
    number = re.sub(r'[\s-]', '', str(data.get('number') or ''))
    if not re.fullmatch(r'\d{12,19}', number):
        raise ApiProblem(422, 'invalid_card', 'card.number must be 12-19 digits.')
    expiry = str(data.get('expiry') or '').strip()
    short = re.fullmatch(r'(\d{2})/(\d{2})', expiry)
    if short:
        expiry = '20%s-%s' % (short.group(2), short.group(1))
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', expiry):
        raise ApiProblem(422, 'invalid_card', 'card.expiry must be YYYY-MM (or MM/YY).')
    code = str(data.get('securityCode') or '').strip()
    if not re.fullmatch(r'\d{3,4}', code):
        raise ApiProblem(422, 'invalid_card', 'card.securityCode must be 3 or 4 digits.')
    name = str(data.get('name') or '').strip()[:300]
    address = data.get('billingAddress')
    billing = None
    if address is not None:
        if not isinstance(address, dict):
            raise ApiProblem(422, 'invalid_card', 'card.billingAddress must be an object.')
        country = str(address.get('countryCode') or '').strip().upper()
        if not re.fullmatch(r'[A-Z]{2}', country):
            raise ApiProblem(422, 'invalid_card', 'card.billingAddress.countryCode must be a 2-letter ISO code.')
        billing = {
            'address_line_1': str(address.get('addressLine1') or '')[:300],
            'address_line_2': str(address.get('addressLine2') or '')[:300],
            'admin_area_2': str(address.get('city') or '')[:120],
            'admin_area_1': str(address.get('state') or '')[:300],
            'postal_code': str(address.get('postalCode') or '')[:60],
            'country_code': country,
        }
    return gateway.CardDetails(number=number, expiry=expiry, security_code=code, name=name,
                               billing_address=billing)


def _own_order(request, order_id):
    order = Order.objects.filter(number=order_id, user=request.user).first()
    if order is None:
        raise ApiProblem(404, 'order_not_found', 'No such order.')
    return order


def _any_order(order_id):
    order = Order.objects.filter(number=order_id).first()
    if order is None:
        raise ApiProblem(404, 'order_not_found', 'No such order.')
    return order


def _payment_of(order):
    payment = OrderPayment.objects.select_related('order', 'saved_card').filter(order=order).first()
    if payment is None:
        raise ApiProblem(409, 'no_paypal_payment', 'This order was not placed through the payments API.')
    return payment


# --------------------------------------------------------------------------
# Representations
# --------------------------------------------------------------------------

def card_dict(card):
    return {
        'paymentMethodId': str(card.public_id),
        'brand': card.brand or None,
        'lastDigits': card.last_digits or None,
        'expiry': card.expiry or None,
        'name': card.name or None,
        'label': '%s ending in %s' % (card.brand.title() if card.brand else 'Card', card.last_digits),
        'createdAt': card.created_at.isoformat(),
    }


def refund_dict(refund):
    return {
        'refundId': str(refund.public_id),
        'status': refund.outcome,
        'amount': _money(refund.amount, refund.currency),
        'currency': refund.currency,
        'paypalRefundId': refund.paypal_refund_id or None,
        'paypalStatus': refund.provider_status or None,
        'paypalTime': refund.provider_time.isoformat() if refund.provider_time else None,
        'error': refund.error_message or None,
        'createdAt': refund.created_at.isoformat(),
    }


def payment_dict(payment):
    cur = payment.currency
    saved = payment.saved_card
    return {
        'state': payment.state,
        'amount': _money(payment.amount, cur),
        'currency': cur,
        'card': {
            'brand': payment.card_brand or None,
            'lastDigits': payment.card_last_digits or None,
            'paymentMethodId': str(saved.public_id) if saved else None,
        } if payment.card_last_digits or saved else None,
        'paypalOrderId': payment.paypal_order_id or None,
        'authorization': {
            'id': payment.authorization_id,
            'status': payment.authorization_status or None,
            'authorizedAt': payment.authorized_at.isoformat() if payment.authorized_at else None,
            'expiresAt': (payment.authorization_expires_at.isoformat()
                          if payment.authorization_expires_at else None),
        } if payment.authorization_id else None,
        'capture': {
            'id': payment.capture_id,
            'status': payment.capture_status or None,
            'capturedAmount': _money(payment.captured_amount, cur),
            'paypalFee': _money(payment.paypal_fee, cur),
            'netAmount': _money(payment.net_amount, cur),
            'capturedAt': payment.captured_at.isoformat() if payment.captured_at else None,
        } if payment.capture_id else None,
        'refundedAmount': _money(payment.refunded_amount, cur),
        'refundableAmount': _money(services.refundable_amount(payment), cur),
        'refunds': [refund_dict(r) for r in payment.refunds.all()],
        'void': {
            'status': payment.void_status or None,
            'voidedAt': payment.voided_at.isoformat() if payment.voided_at else None,
        } if payment.void_status else None,
        'lastError': payment.last_error or None,
    }


def order_dict(order):
    payment = OrderPayment.objects.select_related('saved_card').filter(order=order).first()
    return {
        'orderId': order.number,
        'status': order.status,
        'placedAt': order.date_placed.isoformat() if order.date_placed else None,
        'total': _money(order.total_incl_tax, order.currency),
        'currency': order.currency,
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'unitPrice': _money(line.unit_price_incl_tax, order.currency),
            'lineTotal': _money(line.line_price_incl_tax, order.currency),
        } for line in order.lines.all()],
        'payment': payment_dict(payment) if payment else None,
    }


_STATE_HTTP = {
    OrderPayment.AUTHORIZING: 202,
    OrderPayment.CAPTURING: 202,
    OrderPayment.VOIDING: 202,
}


def _payment_response(payment, done_status=200):
    payment.refresh_from_db()
    status = _STATE_HTTP.get(payment.state, done_status)
    return JsonResponse(order_dict(payment.order), status=status)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

def csrf_failure(request, reason=''):
    """CSRF_FAILURE_VIEW: JSON under /api/, Django's normal page everywhere else."""
    if request.path.startswith('/api/'):
        return _error(403, 'csrf_failed', 'CSRF check failed (%s). GET /api/session and send its csrfToken '
                      'in the X-CSRFToken header with the csrftoken cookie.' % reason)
    return django_csrf.csrf_failure(request, reason=reason)


@never_cache
def session(request):
    """GET: the CSRF token (also set as the csrftoken cookie) and who is signed in."""
    user = request.user
    return JsonResponse({
        'csrfToken': get_token(request),
        'authenticated': user.is_authenticated,
        'user': {'id': user.pk, 'email': user.email, 'isStaff': user.is_staff} if user.is_authenticated else None,
    })


@api(['POST'])
def orders(request):
    data = _json_body(request)
    raw_lines = data.get('lines') if 'lines' in data else data.get('items')
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ApiProblem(422, 'invalid_lines', '"lines" must be a non-empty list of {productId, quantity}.')
    lines = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise ApiProblem(422, 'invalid_lines', 'Each line must be an object.')
        product_id, quantity = raw.get('productId'), raw.get('quantity', 1)
        if isinstance(product_id, str) and product_id.isdigit():
            product_id = int(product_id)
        if not isinstance(product_id, int) or isinstance(product_id, bool) or \
                not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            raise ApiProblem(422, 'invalid_lines', 'productId must be an id and quantity a positive integer.')
        lines.append(services.RequestedLine(product_id, quantity))
    order = services.place_order(request.user, lines, request)
    return JsonResponse(order_dict(order), status=201)


@sensitive_post_parameters()
@api(['POST'])
@sensitive_variables('data', 'card')
def pay(request, order_id):
    order = _own_order(request, order_id)
    payment = _payment_of(order)
    data = _json_body(request)
    method_id = data.get('paymentMethodId')
    if ('card' in data) == bool(method_id):
        raise ApiProblem(422, 'invalid_payment_source', 'Send either "card" or "paymentMethodId", not both.')
    card = saved = None
    if method_id:
        try:
            public_id = uuid.UUID(str(method_id))
        except ValueError:
            raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.') from None
        saved = SavedCard.objects.active().filter(user=request.user, public_id=public_id).first()
        if saved is None:
            raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')
    else:
        card = _parse_card(data['card'])
    payment = services.pay(payment, card=card, saved_card=saved)
    return _payment_response(payment)


@api(['POST'], staff=True)
def fulfil(request, order_id):
    payment = _payment_of(_any_order(order_id))
    try:
        payment = services.fulfil(payment)
    except services._Accepted as accepted:
        return JsonResponse(dict(order_dict(payment.order), message=accepted.message), status=202)
    return _payment_response(payment)


@api(['POST'], staff=True)
def cancel(request, order_id):
    payment = _payment_of(_any_order(order_id))
    try:
        payment = services.cancel(payment)
    except services._Accepted as accepted:
        return JsonResponse(dict(order_dict(payment.order), message=accepted.message), status=202)
    return _payment_response(payment)


@api(['GET', 'POST'])
def refunds(request, order_id):
    order = _own_order(request, order_id)
    payment = _payment_of(order)
    if request.method == 'GET':
        return JsonResponse({'orderId': order.number, 'refunds': [refund_dict(r) for r in payment.refunds.all()]})
    data = _json_body(request)
    key = _idempotency_key(request, data, required=True)
    amount = _decimal(data.get('amount'), 'amount')
    refund, created = services.refund(payment, key=key, amount=amount)
    payment.refresh_from_db()
    body = dict(refund_dict(refund),
                orderId=order.number,
                paymentState=payment.state,
                refundedAmount=_money(payment.refunded_amount, payment.currency),
                refundableAmount=_money(services.refundable_amount(payment), payment.currency))
    if refund.outcome == Outcome.DONE:
        status = 201 if created else 200
    elif refund.outcome == Outcome.FAILED:
        status = 402
    elif refund.outcome == Outcome.NEEDS_REVIEW:
        status = 409
    else:
        status = 202
    return JsonResponse(body, status=status)


@api(['GET'])
def my_orders(request):
    user_orders = Order.objects.filter(user=request.user).order_by('-date_placed').prefetch_related('lines')
    return JsonResponse({'orders': [order_dict(o) for o in user_orders]})


@sensitive_post_parameters()
@api(['GET', 'POST'])
@sensitive_variables('data', 'card')
def payment_methods(request):
    if request.method == 'GET':
        cards = SavedCard.objects.active().filter(user=request.user)
        return JsonResponse({'paymentMethods': [card_dict(c) for c in cards]})
    data = _json_body(request)
    card = _parse_card(data.get('card', data))
    key = _idempotency_key(request, data, required=False) or uuid.uuid4().hex
    saved, outcome, created = services.save_card(request.user, card, key)
    if saved is not None and saved.deleted_at is None:
        return JsonResponse(card_dict(saved), status=201 if created else 200)
    if saved is not None:
        raise ApiProblem(410, 'payment_method_deleted', 'That card was saved and has since been removed.')
    return JsonResponse({'paymentMethodId': None, 'status': outcome,
                         'message': 'PayPal has not confirmed the saved card yet; repeat the request with the '
                                    'same Idempotency-Key to re-check.'}, status=202)


@api(['DELETE'])
def payment_method(request, payment_method_id):
    card = SavedCard.objects.filter(user=request.user, public_id=payment_method_id).first()
    if card is None or (card.deleted_at is not None and card.provider_deleted):
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')
    confirmed = services.delete_card(card)
    if confirmed:
        return HttpResponse(status=204)
    return JsonResponse({'paymentMethodId': str(card.public_id), 'deleted': True,
                         'paypalDeletion': 'pending',
                         'message': 'Removed from your saved cards; PayPal has not confirmed deleting the token '
                                    'yet. Repeat the DELETE to retry.'}, status=202)


@api(['GET'], staff=True)
def reconciliation(request):
    start = services.parse_instant(request.GET.get('from', ''), 'from')
    end = services.parse_instant(request.GET.get('to', ''), 'to')
    if start >= end:
        raise ApiProblem(400, 'invalid_range', '"from" must be before "to".')
    return JsonResponse(services.reconcile(start, end))
