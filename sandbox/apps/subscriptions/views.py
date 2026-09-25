"""
JSON API for subscription billing. Callers authenticate with Django's session
login (``/api/session`` or the storefront's own login page); unsafe methods
need the CSRF token in the ``X-CSRFToken`` header, as everywhere in Django.
"""
import json
import logging
from functools import wraps
from typing import Any, Callable

from django.contrib.auth import authenticate, login
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.http import require_http_methods

from .maxio import BillingError
from .models import ProviderWrite
from .services import InProgress, OutcomeUnknown, list_plans, my_subscriptions, record_to_dict, subscribe

logger = logging.getLogger('apps.subscriptions')

View = Callable[..., HttpResponse]

# What a stored outcome answers to the caller. Only "done" is a success.
OUTCOME_STATUS = {
    ProviderWrite.DONE: 201,
    ProviderWrite.PENDING: 202,
    ProviderWrite.SENDING: 202,
    ProviderWrite.UNKNOWN: 202,
    ProviderWrite.FAILED: 409,
    ProviderWrite.NEEDS_REVIEW: 502,
}


def error(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse(dict({'error': code, 'message': message}, **extra), status=status)


def billing_api(view: View) -> View:
    """Map billing failures to JSON; never leak provider bodies or tracebacks."""
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            return view(request, *args, **kwargs)
        except InProgress:
            return JsonResponse({'subscriptionId': None, 'outcome': 'in_progress',
                                 'message': 'An identical request is already being processed.'}, status=202)
        except OutcomeUnknown as exc:
            logger.warning('Maxio outcome unknown for %s', exc.reference)
            return error(exc.status_code, exc.code, exc.message, subscriptionId=None, outcomeUnknown=True)
        except BillingError as exc:
            return error(exc.status_code, exc.code, exc.message, details=exc.details,
                         outcomeUnknown=exc.outcome_unknown)
        except ImproperlyConfigured as exc:
            logger.error('Maxio billing is not configured: %s', exc)
            return error(503, 'billing_not_configured', 'Subscription billing is not configured.')
    return wrapper


def login_required_json(view: View) -> View:
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return error(401, 'not_authenticated', 'Log in first (POST /api/session).')
        return view(request, *args, **kwargs)
    return wrapper


def _json_body(request: HttpRequest) -> dict[str, Any] | None:
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@require_http_methods(['GET', 'POST'])
def session_view(request: HttpRequest) -> HttpResponse:
    if request.method == 'POST':
        body = _json_body(request)
        if body is None or not isinstance(body.get('email'), str) or not isinstance(body.get('password'), str):
            return error(400, 'invalid_request', 'Send {"email": ..., "password": ...}.')
        candidate = authenticate(request, username=body['email'], password=body['password'])
        if candidate is None or not candidate.is_active:
            return error(401, 'invalid_credentials', 'Email or password is incorrect.')
        login(request, candidate)
    current = request.user
    return JsonResponse({
        'authenticated': current.is_authenticated,
        'email': getattr(current, 'email', None) if current.is_authenticated else None,
        # login() rotates the token, so always hand back the current one.
        'csrfToken': get_token(request),
    })


@require_http_methods(['GET'])
@billing_api
def plans_view(request: HttpRequest) -> HttpResponse:
    return JsonResponse({'plans': list_plans()})


@transaction.non_atomic_requests
@require_http_methods(['POST'])
@login_required_json
@billing_api
def subscriptions_view(request: HttpRequest) -> HttpResponse:
    body = _json_body(request)
    plan_handle = body.get('planHandle') if body is not None else None
    if not isinstance(plan_handle, str) or not plan_handle:
        return error(400, 'invalid_request', 'Send {"planHandle": "<handle from /api/subscription-plans>"}.')
    idempotency_key = request.headers.get('Idempotency-Key', '').strip() or 'default'
    if len(idempotency_key) > 255:
        return error(400, 'invalid_request', 'Idempotency-Key is too long.')

    result = subscribe(request.user, plan_handle, idempotency_key)
    record = result.record
    payload = record_to_dict(record)
    payload['plan'] = result.plan
    status = OUTCOME_STATUS.get(record.outcome, 202)
    if record.outcome == ProviderWrite.FAILED:
        payload.update(error='subscription_not_in_effect',
                       message='The billing provider reports this subscription is not in effect.')
    elif record.outcome == ProviderWrite.NEEDS_REVIEW:
        payload.update(error='needs_review', message='The subscription has been flagged for review.')
    return JsonResponse(payload, status=status)


@transaction.non_atomic_requests
@require_http_methods(['GET'])
@login_required_json
@billing_api
def my_subscriptions_view(request: HttpRequest) -> HttpResponse:
    return JsonResponse({'subscriptions': my_subscriptions(request.user)})
