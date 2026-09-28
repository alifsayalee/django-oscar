"""
JSON endpoints under /api/.

Callers authenticate with Django's own session login (the sandbox's login
page, or ``POST /api/session``); CSRF protection stays on, so unsafe requests
send the ``X-CSRFToken`` header (``GET /api/csrf`` hands one out). Fulfil,
cancel and reconciliation are staff-only; everything else acts only on the
caller's own orders and cards -- anything else answers 404.
"""
import functools
import json
import logging
from datetime import timezone as dt_timezone
from typing import Any, Callable

import httpx
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, transaction
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from paypal.core import ApiError

from . import cards, payments, reconcile
from .errors import NEVER_SENT, ApiProblem, ProviderError, provider_error
from .models import ProviderWrite
from .orders import Order, order_body, order_for, place_order, refund_body
from .outcomes import answer
from .paypal_client import get_client

logger = logging.getLogger(__name__)

MAX_BODY = 64 * 1024


def _json(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    if len(request.body) > MAX_BODY:
        raise ApiProblem(413, 'body_too_large', 'Request body is too large.')
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ApiProblem(400, 'invalid_json', 'Request body must be JSON.') from None
    if not isinstance(payload, dict):
        raise ApiProblem(400, 'invalid_json', 'Request body must be a JSON object.')
    return payload


def _idempotency_key(request: HttpRequest, payload: dict[str, Any]) -> str | None:
    key = request.headers.get('Idempotency-Key') or payload.get('idempotencyKey')
    if key is None:
        return None
    key = str(key).strip()
    if not 1 <= len(key) <= 255:
        raise ApiProblem(400, 'invalid_idempotency_key', 'Idempotency-Key must be 1 to 255 characters.')
    return key


def api(methods: tuple[str, ...], *, staff: bool = False, login_required: bool = True) -> Callable:
    """JSON view: method check, auth, error boundary; no request-wide transaction
    (a claim must commit before PayPal is called)."""

    def decorate(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
            if request.method not in methods:
                response = JsonResponse({'error': {'code': 'method_not_allowed', 'message': 'Method not allowed.'}},
                                        status=405)
                response['Allow'] = ', '.join(methods)
                return response
            if login_required and not request.user.is_authenticated:
                return JsonResponse({'error': {'code': 'not_authenticated', 'message': 'Log in first.'}},
                                    status=401)
            if staff and not request.user.is_staff:
                return JsonResponse({'error': {'code': 'forbidden', 'message': 'Staff only.'}}, status=403)
            try:
                return view(request, *args, **kwargs)
            except ApiProblem as e:
                return JsonResponse(e.body(), status=e.status)
            except ImproperlyConfigured as e:
                logger.error('PayPal integration misconfigured: %s', e)
                return JsonResponse({'error': {'code': 'paypal_not_configured',
                                               'message': 'Payments are not configured on this server.'}},
                                    status=503)
            except ApiError as e:  # an unguarded read
                problem = provider_error(e.status_code, e.error)
                return JsonResponse(problem.body(), status=problem.status)
            except NEVER_SENT:
                return JsonResponse({'error': {'code': 'paypal_unreachable', 'outcomeUnknown': False,
                                               'message': 'PayPal could not be reached; nothing was sent.'}},
                                    status=502)
            except httpx.RequestError:
                return JsonResponse({'error': {'code': 'paypal_timeout', 'outcomeUnknown': True,
                                               'message': 'PayPal did not answer in time.'}}, status=504)
            except OperationalError:
                logger.exception('Database unavailable')
                return JsonResponse({'error': {'code': 'busy',
                                               'message': 'The server is busy; retry the same request.'}},
                                    status=503)

        return transaction.non_atomic_requests(wrapper)

    return decorate


# ---------------------------------------------------------------- session

@ensure_csrf_cookie
@api(('GET',), login_required=False)
def csrf(request: HttpRequest) -> JsonResponse:
    return JsonResponse({'csrfToken': get_token(request)})


def _user_body(user) -> dict[str, Any]:
    return {'id': user.pk, 'username': user.get_username(), 'email': user.email, 'isStaff': user.is_staff}


@sensitive_post_parameters('password')
@sensitive_variables('payload', 'password')
@api(('GET', 'POST', 'DELETE'), login_required=False)
def session(request: HttpRequest) -> JsonResponse:
    if request.method == 'GET':
        if not request.user.is_authenticated:
            return JsonResponse({'authenticated': False})
        return JsonResponse({'authenticated': True, 'user': _user_body(request.user)})
    if request.method == 'DELETE':
        logout(request)
        return JsonResponse({'authenticated': False})
    payload = _json(request)
    username = payload.get('username') or payload.get('email')
    password = payload.get('password')
    user = authenticate(request, username=username, password=password) if username and password else None
    if user is None:
        raise ApiProblem(401, 'invalid_credentials', 'Unknown user or wrong password.')
    login(request, user)
    return JsonResponse({'authenticated': True, 'user': _user_body(user), 'csrfToken': get_token(request)})


# ---------------------------------------------------------------- orders

@api(('POST',))
def orders(request: HttpRequest) -> JsonResponse:
    order = place_order(request.user, _json(request).get('items'))
    order = Order.objects.select_related('paypal_payment').get(pk=order.pk)
    return JsonResponse(order_body(order), status=201)


@api(('GET',))
def my_orders(request: HttpRequest) -> JsonResponse:
    placed = Order.objects.filter(user=request.user, paypal_payment__isnull=False) \
        .select_related('paypal_payment').prefetch_related('lines').order_by('-date_placed', '-pk')
    return JsonResponse({'orders': [order_body(o) for o in placed]})


@sensitive_variables('payload')
@api(('POST',))
def pay(request: HttpRequest, order_id: str) -> JsonResponse:
    order = order_for(request.user, order_id)
    payload = _json(request)
    try:
        outcome, __ = payments.authorize(request.user, order, payload)
    except ProviderError as e:
        if e.code == 'paypal_refused':  # PayPal refused this card for this amount
            raise ApiProblem(402, 'payment_declined', e.message, paypal=e.paypal) from e
        raise
    order.refresh_from_db()
    return answer(outcome, order_body(order), failed_status=402)


@api(('POST',), staff=True)
def fulfil(request: HttpRequest, order_id: str) -> JsonResponse:
    order = order_for(request.user, order_id, staff=True)
    outcome, __ = payments.fulfil(order)
    order.refresh_from_db()
    return answer(outcome, order_body(order))


@api(('POST',), staff=True)
def cancel(request: HttpRequest, order_id: str) -> JsonResponse:
    order = order_for(request.user, order_id, staff=True)
    outcome, __ = payments.cancel(order)
    order.refresh_from_db()
    return answer(outcome, order_body(order))


@api(('POST',))
def refunds(request: HttpRequest, order_id: str) -> JsonResponse:
    order = order_for(request.user, order_id)
    payload = _json(request)
    key = _idempotency_key(request, payload)
    if key is None:
        raise ApiProblem(400, 'idempotency_key_required',
                         'Send an Idempotency-Key header: repeating a request with the same key never refunds twice.')
    write: ProviderWrite = payments.refund(request.user, order, payload, key)
    order.refresh_from_db()
    return answer(write.outcome, {**refund_body(write), 'orderId': order.number, 'order': order_body(order)},
                  done_status=201)


# ---------------------------------------------------------------- saved cards

@sensitive_variables('payload')
@api(('GET', 'POST'))
def payment_methods(request: HttpRequest) -> JsonResponse:
    if request.method == 'GET':
        return JsonResponse({'paymentMethods': cards.list_cards(request.user)})
    payload = _json(request)
    outcome, card = cards.save_card(request.user, payload, _idempotency_key(request, payload))
    body = cards.card_body(card) if card is not None else {'paymentMethodId': None}
    return answer(outcome, body, done_status=201, failed_status=422)


@api(('DELETE',))
def payment_method(request: HttpRequest, payment_method_id: str) -> JsonResponse:
    deletion = cards.delete_card(request.user, payment_method_id)
    return JsonResponse({'paymentMethodId': int(payment_method_id), 'deleted': True,
                         'vaultDeletion': deletion})


# ---------------------------------------------------------------- operators

@api(('GET',), staff=True)
def reconciliation(request: HttpRequest) -> JsonResponse:
    bounds = []
    for name in ('from', 'to'):
        raw = request.GET.get(name, '')
        try:
            value = parse_datetime(raw)
        except ValueError:
            value = None
        if value is None:
            raise ApiProblem(422, 'invalid_range', '"%s" must be an ISO-8601 date-time.' % name)
        bounds.append(value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc))
    return JsonResponse(reconcile.report(get_client(), bounds[0], bounds[1]))
