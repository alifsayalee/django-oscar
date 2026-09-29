"""
JSON endpoints under /api/. Callers authenticate with Django's session login
(the same session the storefront uses); unsafe methods need the CSRF token.
Fulfil, cancel and reconciliation are for staff (``is_staff``) only; every
other endpoint acts on the caller's own orders and cards.
"""
import functools
import json
import logging
import uuid
from datetime import datetime, timezone as dt_timezone

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from . import services
from .gateway import ProviderError
from .serializers import card_dict, order_dict, payment_dict, refund_dict
from .services import ServiceError
from .validation import parse_card, parse_idempotency_key

logger = logging.getLogger(__name__)


def _error(status, code, message, **extra):
    return JsonResponse({'error': dict(code=code, message=message, **extra)}, status=status)


def _provider_error(exc):
    extra = {'outcomeUnknown': True} if exc.outcome_unknown else {}
    if exc.issue:
        extra['paypalIssue'] = exc.issue
    if exc.debug_id:
        extra['paypalDebugId'] = exc.debug_id
    message = exc.message
    if exc.outcome_unknown:
        message += (' The outcome is unknown; repeat the same request to settle it '
                    '- it will not be applied twice.')
    return _error(exc.status_code, exc.code, message, **extra)


def _refusal(request, methods, staff, authenticated):
    if request.method not in methods:
        response = _error(405, 'method_not_allowed', 'Method not allowed.')
        response['Allow'] = ', '.join(methods)
        return response
    if (authenticated or staff) and not request.user.is_authenticated:
        return _error(401, 'not_authenticated', 'Sign in first.')
    if staff and not request.user.is_staff:
        return _error(403, 'forbidden', 'This action is for staff only.')
    return None


def api_view(methods, *, staff=False, authenticated=True):
    """Method dispatch, authentication, JSON errors and a transaction scope
    that lets each payment claim commit before PayPal is called."""

    def decorator(view):
        @functools.wraps(view)
        @never_cache
        def wrapper(request, *args, **kwargs):
            refusal = _refusal(request, methods, staff, authenticated)
            if refusal is not None:
                return refusal
            try:
                return view(request, *args, **kwargs)
            except ServiceError as exc:
                return _error(exc.status_code, exc.code, exc.message, **exc.details)
            except ProviderError as exc:
                return _provider_error(exc)
            except ImproperlyConfigured:
                logger.exception('PayPal is not configured')
                return _error(503, 'payments_not_configured', 'Payments are not configured.')
            except Exception:
                logger.exception('Unhandled error in %s', view.__name__)
                return _error(500, 'internal_error', 'Something went wrong.')

        return transaction.non_atomic_requests(wrapper)

    return decorator


@sensitive_variables('body')
def _json_body(request):
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ServiceError(400, 'invalid_json', 'The request body must be JSON.')
    if not isinstance(body, dict):
        raise ServiceError(400, 'invalid_json', 'The request body must be a JSON object.')
    return body


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@api_view(['GET'], authenticated=False)
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({'csrfToken': get_token(request)})


@api_view(['POST'], authenticated=False)
@sensitive_post_parameters()
@sensitive_variables('body', 'password')
def session_login(request):
    body = _json_body(request)
    username, password = body.get('username') or body.get('email'), body.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        raise ServiceError(422, 'invalid_credentials', 'Provide "username" (or "email") and "password".')
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return _error(401, 'invalid_credentials', 'Unknown user or wrong password.')
    login(request, user)
    return JsonResponse({'userId': user.pk, 'isStaff': user.is_staff,
                         'csrfToken': get_token(request)})


@api_view(['POST'], authenticated=False)
def session_logout(request):
    logout(request)
    return JsonResponse({}, status=200)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@api_view(['POST'])
def orders(request):
    quantities = services.parse_order_items(_json_body(request))
    payment = services.place_order(request, quantities)
    return JsonResponse(order_dict(payment.order, payment), status=201)


@api_view(['POST'])
@sensitive_post_parameters()
@sensitive_variables('body', 'card')
def pay(request, order_id):
    body = _json_body(request)
    has_card, method_id = 'card' in body, body.get('paymentMethodId')
    if has_card == (method_id is not None):
        raise ServiceError(422, 'invalid_payment_source',
                           'Send either "card" or "paymentMethodId", not both.')
    card = parse_card(body['card']) if has_card else None
    if method_id is not None and not isinstance(method_id, str):
        raise ServiceError(422, 'invalid_payment_source', '"paymentMethodId" must be a string.')
    payment = services.pay_order(request.user, order_id, card=card,
                                 payment_method_id=_uuid_or_404(method_id) if method_id else None)
    return JsonResponse(order_dict(payment.order, payment))


def _uuid_or_404(value):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ServiceError(404, 'payment_method_not_found', 'No such saved card.')


@api_view(['POST'], staff=True)
def fulfil(request, order_id):
    payment = services.fulfil_order(order_id)
    return JsonResponse(order_dict(payment.order, payment))


@api_view(['POST'], staff=True)
def cancel(request, order_id):
    payment = services.cancel_order(order_id)
    return JsonResponse(order_dict(payment.order, payment))


@api_view(['POST'])
def refunds(request, order_id):
    body = _json_body(request)
    key = parse_idempotency_key(request.headers.get('Idempotency-Key'), required=True)
    note = body.get('note') or ''
    if not isinstance(note, str) or len(note) > 255:
        raise ServiceError(422, 'invalid_note', '"note" must be text of at most 255 characters.')
    refund, created = services.refund_order(request.user, order_id, idempotency_key=key,
                                            amount=body.get('amount'), note=note)
    payment = refund.payment
    payment.refresh_from_db()
    return JsonResponse(dict(refund_dict(refund), payment=payment_dict(payment)),
                        status=201 if created else 200)


@api_view(['GET'])
def my_orders(request):
    return JsonResponse({'orders': [order_dict(o) for o in services.orders_for(request.user)]})


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@sensitive_post_parameters()
@sensitive_variables('body', 'card')
def payment_methods(request):
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': [
            card_dict(c) for c in services.list_saved_cards(request.user)]})
    body = _json_body(request)
    card = parse_card(body.get('card', body))
    key = parse_idempotency_key(request.headers.get('Idempotency-Key'), required=False)
    saved, created = services.save_card(request.user, card, idempotency_key=key)
    return JsonResponse(card_dict(saved), status=201 if created else 200)


@api_view(['DELETE'])
def payment_method(request, payment_method_id):
    services.delete_saved_card(request.user, payment_method_id)
    return JsonResponse({}, status=200)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _query_datetime(request, name):
    raw = request.GET.get(name)
    value = parse_datetime(raw) if raw else None
    if value is None:
        raise ServiceError(422, 'invalid_range',
                           '"%s" must be an ISO-8601 date-time, e.g. 2026-09-01T00:00:00Z.' % name)
    return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)


@api_view(['GET'], staff=True)
def reconciliation(request):
    start, end = _query_datetime(request, 'from'), _query_datetime(request, 'to')
    now = datetime.now(dt_timezone.utc)
    return JsonResponse(services.reconcile(start, min(end, now)))
