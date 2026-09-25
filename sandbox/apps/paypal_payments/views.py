"""
JSON endpoints. Callers authenticate with Django's session login (the
sandbox's own); every endpoint acts on the request user, and fulfil, cancel
and reconciliation are restricted to staff. Unsafe methods keep Django's CSRF
protection, as any cookie-authenticated endpoint must.
"""
import functools
import json
import logging

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from . import cards, orders, payments, reconciliation
from .errors import PaymentAPIError
from .models import Outcome, PayPalOperation, PayPalPayment

logger = logging.getLogger('apps.paypal_payments')

MAX_KEY_LENGTH = 200


def api(*, staff: bool = False, authenticated: bool = True):
    """Session auth, staff gate, JSON errors - the same for every endpoint."""
    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if authenticated and not request.user.is_authenticated:
                return error(401, 'not_authenticated', 'Sign in first.')
            if staff and not request.user.is_staff:
                return error(403, 'forbidden', 'Staff only.')
            try:
                return view(request, *args, **kwargs)
            except PaymentAPIError as e:
                if e.status >= 500:
                    logger.warning('%s %s -> %s %s', request.method, request.path, e.status, e.code)
                return JsonResponse(e.as_dict(), status=e.status)
            except ImproperlyConfigured as e:
                logger.error('PayPal integration misconfigured: %s', e)
                return error(503, 'not_configured', 'Payments are not configured on this site.')
        return wrapper
    return decorator


def error(status: int, code: str, message: str) -> JsonResponse:
    return JsonResponse({'error': {'code': code, 'message': message}}, status=status)


def body_of(request) -> object:
    if not request.body:
        return {}
    try:
        return json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise PaymentAPIError(400, 'invalid_json', 'The request body must be JSON.')


def idempotency_key(request) -> str:
    key = request.headers.get('Idempotency-Key', '').strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        raise PaymentAPIError(
            400, 'idempotency_key_required',
            'Send an Idempotency-Key header (1-%d characters) so a repeated request is recognised.'
            % MAX_KEY_LENGTH)
    return key


# ------------------------------------------------------------ serialization

def _dec(value) -> str | None:
    return None if value is None else str(value)


def serialize_refund(op: PayPalOperation) -> dict:
    return {
        'refundId': str(op.public_id),
        'paypalRefundId': op.provider_id or None,
        'amount': _dec(op.amount),
        'currency': op.currency,
        'outcome': op.outcome,
        'paypalStatus': op.provider_status or None,
        'createdAt': op.claimed_at.isoformat(),
        'paypalTime': op.provider_time.isoformat() if op.provider_time else None,
    }


def serialize_payment(payment: PayPalPayment) -> dict:
    refunds = payment.operations.filter(kind=PayPalOperation.Kind.REFUND).order_by('seq')
    return {
        'state': payment.state,
        'amount': _dec(payment.amount),
        'currency': payment.currency,
        'paypalOrderId': payment.paypal_order_id or None,
        'authorization': {
            'id': payment.authorization_id,
            'status': payment.authorization_status or None,
            'authorizedAt': payment.authorized_at.isoformat() if payment.authorized_at else None,
            'expiresAt': (payment.authorization_expires_at.isoformat()
                          if payment.authorization_expires_at else None),
            'reauthorizations': payment.reauthorization_count,
        } if payment.authorization_id else None,
        'capture': {
            'id': payment.capture_id,
            'status': payment.capture_status or None,
            'amount': _dec(payment.captured_amount),
            'paypalFee': _dec(payment.paypal_fee),
            'netAmount': _dec(payment.net_amount),
            'capturedAt': payment.captured_at.isoformat() if payment.captured_at else None,
        } if payment.capture_id else None,
        'refundedAmount': _dec(payment.refunded_amount),
        'refundableAmount': _dec(payment.captured_amount - payment.refund_reserved),
        'refunds': [serialize_refund(op) for op in refunds],
        'lastError': payment.last_error or None,
    }


def serialize_order(payment: PayPalPayment) -> dict:
    order = payment.order
    return {
        'orderId': str(order.number),
        'status': order.status,
        'placedAt': order.date_placed.isoformat(),
        'currency': payment.currency,
        'total': _dec(payment.amount),
        'lines': [
            {
                'productId': line.product_id,
                'title': line.title,
                'quantity': line.quantity,
                'unitPrice': _dec(line.unit_price_incl_tax),
                'linePrice': _dec(line.line_price_incl_tax),
            } for line in order.lines.all()
        ],
        'payment': serialize_payment(payment),
    }


def _status_for(outcome: str) -> int:
    # Only the provider's "done" answers 200; everything else is accepted, not done.
    return 200 if outcome == Outcome.DONE else 202


# ------------------------------------------------------------ session

@require_http_methods(['GET'])
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({'csrfToken': get_token(request)})


@require_http_methods(['POST', 'DELETE'])
@sensitive_post_parameters()
@api(authenticated=False)
def session(request):
    if request.method == 'DELETE':
        logout(request)
        return HttpResponse(status=204)
    payload = body_of(request)
    if not isinstance(payload, dict):
        raise PaymentAPIError(400, 'invalid_request', 'Body must be a JSON object.')
    user = authenticate(request, username=payload.get('email') or payload.get('username'),
                        password=payload.get('password'))
    if user is None:
        return error(401, 'invalid_credentials', 'Unknown user or wrong password.')
    login(request, user)
    return JsonResponse({'userId': user.pk, 'email': user.email, 'isStaff': user.is_staff,
                         'csrfToken': get_token(request)})


# ------------------------------------------------------------ orders

@require_POST
@api()
def order_create(request):
    order = orders.place_order(request.user, body_of(request), request=request)
    return JsonResponse(serialize_order(order.paypal_payment), status=201)


@require_http_methods(['GET'])
@api()
def my_orders(request):
    qs = (PayPalPayment.objects.select_related('order')
          .filter(order__user=request.user).order_by('-order__date_placed'))
    return JsonResponse({'orders': [serialize_order(p) for p in qs]})


@require_POST
@api()
def order_pay(request, order_id):
    payment, outcome = payments.pay(request.user, order_id, body_of(request))
    return JsonResponse(serialize_order(payment), status=_status_for(outcome))


@require_POST
@api(staff=True)
def order_fulfil(request, order_id):
    payment, outcome = payments.fulfil(order_id)
    return JsonResponse(serialize_order(payment), status=_status_for(outcome))


@require_POST
@api(staff=True)
def order_cancel(request, order_id):
    payment, outcome = payments.cancel(order_id)
    return JsonResponse(serialize_order(payment), status=_status_for(outcome))


@require_POST
@api(staff=True)
def order_refunds(request, order_id):
    key = idempotency_key(request)
    payment, op = payments.refund(order_id, key, body_of(request))
    if op.outcome == Outcome.FAILED:
        return JsonResponse({'refundId': str(op.public_id), 'error': {
            'code': 'refund_failed', 'message': 'PayPal did not complete the refund: %s'
            % (op.detail or op.provider_status)}, 'refund': serialize_refund(op)}, status=422)
    body = {'refundId': str(op.public_id), 'refund': serialize_refund(op), 'order': serialize_order(payment)}
    return JsonResponse(body, status=201 if op.outcome == Outcome.DONE else 202)


# ------------------------------------------------------------ reconciliation

@require_http_methods(['GET'])
@api(staff=True)
def reconciliation_report(request):
    start, end = reconciliation.parse_range(request.GET.get('from'), request.GET.get('to'))
    return JsonResponse(reconciliation.report(start, end))


# ------------------------------------------------------------ saved cards

@require_http_methods(['GET', 'POST'])
@api()
def payment_methods(request):
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': cards.list_cards(request.user)})
    key = idempotency_key(request)
    card, created = cards.save_card(request.user, key, body_of(request))
    return JsonResponse(cards.serialize(card), status=201 if created else 200)


@require_http_methods(['DELETE'])
@api()
def payment_method_detail(request, payment_method_id):
    if cards.delete_card(request.user, payment_method_id):
        return HttpResponse(status=204)
    return JsonResponse({
        'paymentMethodId': payment_method_id,
        'removed': True,
        'message': 'The card is removed and can no longer pay; PayPal has not yet confirmed deleting it '
                   'from the vault. Repeat the DELETE to retry.',
    }, status=202)

