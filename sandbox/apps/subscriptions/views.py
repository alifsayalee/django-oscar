"""
JSON endpoints for subscription billing. Callers authenticate with the
sandbox's normal Django session login; POSTs carry the CSRF token.
"""
import json
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from . import service
from .errors import InvalidRequest, OutcomeUnknown, ProviderError
from .models import MaxioWrite
from .outcomes import answer

logger = logging.getLogger(__name__)

MAX_IDEMPOTENCY_KEY_LENGTH = 255

View = Callable[..., HttpResponse]


def error_response(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def api_view(view: View) -> View:
    """Translate this app's failures into JSON errors, one way for every endpoint."""
    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            return view(request, *args, **kwargs)
        except InvalidRequest as e:
            return error_response(e.status_code, e.code, e.message)
        except ProviderError as e:
            return error_response(e.status_code, e.code, e.message,
                                  outcomeUnknown=e.outcome_unknown, details=e.details)
        except OutcomeUnknown as e:
            return error_response(
                answer(MaxioWrite.UNKNOWN), 'outcome_unknown',
                'The billing provider did not confirm the request. It may still complete; '
                'check /api/my-subscriptions or repeat the same request.',
                outcomeUnknown=True, reference=e.reference)
        except ImproperlyConfigured:
            logger.exception('Maxio billing is not configured')
            return error_response(503, 'billing_not_configured', 'Subscription billing is not configured.')
    return wrapped


def login_required_json(view: View) -> View:
    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return error_response(401, 'authentication_required', 'Sign in to continue.',
                                  loginUrl=reverse('customer:login'))
        return view(request, *args, **kwargs)
    return wrapped


@require_GET
@api_view
def subscription_plans(request: HttpRequest) -> HttpResponse:
    plans = service.list_plans()
    return JsonResponse({'plans': [plan.as_json() for plan in plans]})


# Not atomic: each write claim must be committed before Maxio is called, and
# its outcome recorded even if the request later fails.
@transaction.non_atomic_requests
@require_POST
@login_required_json
@api_view
def subscriptions(request: HttpRequest) -> HttpResponse:
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        raise InvalidRequest(400, 'invalid_json', 'The request body must be JSON.')
    if not isinstance(payload, dict):
        raise InvalidRequest(400, 'invalid_json', 'The request body must be a JSON object.')

    plan_handle = payload.get('planHandle')
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        raise InvalidRequest(400, 'plan_handle_required', 'planHandle is required.')

    idempotency_key = request.headers.get('Idempotency-Key') or None
    if idempotency_key is not None and len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidRequest(400, 'invalid_idempotency_key',
                             f'Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters.')

    result = service.subscribe(request.user, plan_handle.strip(), idempotency_key)
    body = service.subscription_json(result.subscription, outcome=result.outcome,
                                     record=result.record if result.stage == 'subscription' else None)
    body['stage'] = result.stage
    return JsonResponse(body, status=answer(result.outcome, created=True))


@transaction.non_atomic_requests
@require_GET
@login_required_json
@api_view
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    return JsonResponse({'subscriptions': service.my_subscriptions(request.user)})
