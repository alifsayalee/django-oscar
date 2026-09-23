"""
JSON endpoints for Maxio subscription billing. Callers authenticate with the sandbox's own Django
session login; the caller's identity is always ``request.user``.
"""
import json
import logging
from functools import wraps
from typing import Any, Callable

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import services
from .maxio import ProviderError

logger = logging.getLogger(__name__)

View = Callable[..., JsonResponse]


def _error(status: int, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({'error': message, **extra}, status=status)


def api_view(view: View) -> View:
    """
    Session auth (401 JSON rather than a login redirect) and one translation of failures.

    Opted out of ATOMIC_REQUESTS: a claim row must be committed before Maxio is called, and must
    never be rolled back after Maxio acted, or a real subscription would be left with no local record.
    """
    @transaction.non_atomic_requests
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        if not request.user.is_authenticated:
            return _error(401, 'Authentication required: sign in through the sandbox login first.')
        try:
            return view(request, *args, **kwargs)
        except services.BillingError as exc:
            return _error(exc.status_code, exc.message, **exc.extra)
        except ProviderError as exc:
            extra: dict[str, Any] = {'outcomeUnknown': exc.outcome_unknown}
            if exc.details:
                extra['details'] = exc.details
            return _error(exc.status_code, exc.message, **extra)
        except ImproperlyConfigured:
            logger.exception('Maxio billing is not configured')
            return _error(503, 'Subscription billing is not configured on this site.')
    return wrapper


@require_GET
@api_view
def subscription_plans(request: HttpRequest) -> JsonResponse:
    result = services.list_plans()
    return JsonResponse({
        'productFamily': result.product_family,
        'plans': [plan.as_json() for plan in result.plans],
        'truncated': result.truncated,
    })


@require_POST
@api_view
def subscriptions(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return _error(400, 'Request body must be JSON.')
    plan_handle = payload.get('planHandle') if isinstance(payload, dict) else None
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return _error(400, 'planHandle is required.')

    result = services.subscribe(request.user, plan_handle.strip())
    row = result.request
    body: dict[str, Any] = {
        'subscriptionId': row.maxio_subscription_id,
        'status': row.status,
        'reference': row.reference,
        'created': result.created,
    }
    if result.subscription is not None:
        body['subscription'] = services.subscription_json(result.subscription)
    if result.notes:
        body['notes'] = result.notes

    if row.status == row.ACTIVE:
        status = 201 if result.created else 200
    elif row.status == row.FAILED:
        body['error'] = 'The billing provider could not create the subscription.'
        status = 502
    else:
        status = 202          # pending, attention, needs_review, in progress: accepted, not confirmed
    return JsonResponse(body, status=status)


@require_GET
@api_view
def subscription_detail(request: HttpRequest, subscription_id: int) -> JsonResponse:
    sub = services.get_subscription(request.user, subscription_id)
    return JsonResponse(services.subscription_json(sub))


@require_GET
@api_view
def my_subscriptions(request: HttpRequest) -> JsonResponse:
    subs, unconfirmed = services.my_subscriptions(request.user)
    return JsonResponse({'subscriptions': subs, 'unconfirmedRequests': unconfirmed})
