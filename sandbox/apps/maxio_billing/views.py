"""
JSON API for subscription billing. Callers authenticate with Django's session
login (the same backends as the storefront); identity is always request.user.
"""
import functools
import json
import logging
from collections.abc import Callable
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from maxio_advanced_billing import MaxioAdvancedBillingClient

from . import services
from .client import get_client
from .errors import BillingError, BillingNotConfigured
from .models import BillingWrite
from .safe_write import WriteResult

logger = logging.getLogger(__name__)

View = Callable[..., HttpResponse]


def error_response(err: BillingError) -> JsonResponse:
    body: dict[str, Any] = {'error': {'code': err.code, 'message': err.message}}
    if err.details:
        body['error']['details'] = err.details
    if err.outcome_unknown:
        body['error']['outcomeUnknown'] = True
    return JsonResponse(body, status=err.status_code)


def api_view(methods: list[str], *, login_required: bool = True) -> Callable[[View], View]:
    """JSON errors, session auth, and no request-wide transaction (claims must commit first)."""
    def decorate(view: View) -> View:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if login_required and not request.user.is_authenticated:
                return error_response(BillingError(401, 'Log in first.', code='not_authenticated'))
            try:
                return view(request, *args, **kwargs)
            except BillingError as err:
                if err.status_code >= 500:
                    logger.warning('billing API %s %s -> %s %s', request.method, request.path,
                                   err.status_code, err.code, exc_info=err.__cause__ is not None)
                return error_response(err)
        return transaction.non_atomic_requests(require_http_methods(methods)(wrapper))
    return decorate


def billing_client() -> MaxioAdvancedBillingClient:
    try:
        return get_client()
    except ImproperlyConfigured as e:
        logger.error('Maxio billing is not configured: %s', e)
        raise BillingNotConfigured() from e


def json_body(request: HttpRequest) -> dict[str, Any]:
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        raise BillingError(400, 'The request body must be JSON.', code='invalid_json') from None
    if not isinstance(body, dict):
        raise BillingError(400, 'The request body must be a JSON object.', code='invalid_json')
    return body


def answer(step: str, write: WriteResult, payload: dict[str, Any]) -> JsonResponse:
    """The one place a write's outcome becomes the caller's answer. Success comes from done alone."""
    outcome = write.record.outcome
    body = {'step': step, 'outcome': outcome, 'reference': write.record.reference, **payload}
    match outcome:
        case BillingWrite.DONE:
            status = 201 if write.sent_now else 200
        case BillingWrite.PENDING | BillingWrite.SENDING:
            status = 202  # accepted / in flight - not done
        case BillingWrite.FAILED | BillingWrite.NEEDS_REVIEW:
            status = 409
            body['detail'] = write.record.detail
        case _:
            status = 504  # may have happened: never "not created"
            body['outcomeUnknown'] = True
    return JsonResponse(body, status=status)


# --- session ----------------------------------------------------------------

def _session_state(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    return {
        'authenticated': user.is_authenticated,
        'user': {'id': user.pk, 'email': getattr(user, 'email', '')}
        if user.is_authenticated else None,
        'csrfToken': get_token(request),
    }


@ensure_csrf_cookie
@api_view(['GET', 'POST', 'DELETE'], login_required=False)
def session(request: HttpRequest) -> HttpResponse:
    if request.method == 'GET':
        return JsonResponse(_session_state(request))
    if request.method == 'DELETE':
        logout(request)
        return JsonResponse(_session_state(request))
    body = json_body(request)
    username = body.get('email') or body.get('username')
    password = body.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        raise BillingError(400, 'Send "email" and "password".', code='invalid_request')
    user = authenticate(request, username=username, password=password)
    if user is None:
        raise BillingError(401, 'Invalid credentials.', code='invalid_credentials')
    login(request, user)
    return JsonResponse(_session_state(request))


# --- billing ----------------------------------------------------------------

@api_view(['GET'])
def subscription_plans(request: HttpRequest) -> HttpResponse:
    plans = services.list_plans(billing_client())
    return JsonResponse({'plans': [p.to_json() for p in plans]})


@api_view(['POST'])
def billing_customer(request: HttpRequest) -> HttpResponse:
    write = services.ensure_customer(billing_client(), request.user)
    return answer('customer', write, {'customerId': _int_or_none(write.record.provider_id)})


@api_view(['POST'])
def subscriptions(request: HttpRequest) -> HttpResponse:
    body = json_body(request)
    plan_handle = body.get('planHandle')
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        raise BillingError(400, 'Send "planHandle" (see GET /api/subscription-plans).',
                           code='invalid_request')
    key = request.headers.get('Idempotency-Key') or None
    if key is not None and len(key) > 255:
        raise BillingError(400, 'Idempotency-Key is too long.', code='invalid_request')
    result = services.subscribe(billing_client(), request.user, plan_handle.strip(), key)
    record = result.write.record
    if result.step == 'customer':
        payload: dict[str, Any] = {'subscriptionId': None,
                                   'customerId': _int_or_none(record.provider_id)}
    else:
        payload = {'subscriptionId': _int_or_none(record.provider_id),
                   'planHandle': record.plan_handle,
                   'subscription': record.snapshot or None}
    return answer(result.step, result.write, payload)


@api_view(['GET'])
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    return JsonResponse(services.my_subscriptions(billing_client(), request.user))


@api_view(['GET'])
def subscription_detail(request: HttpRequest, subscription_id: int) -> HttpResponse:
    return JsonResponse(
        services.get_subscription(billing_client(), request.user, subscription_id))


def _int_or_none(value: str) -> int | None:
    return int(value) if value else None
