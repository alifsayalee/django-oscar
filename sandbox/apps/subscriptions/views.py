"""
JSON endpoints for subscription billing, authenticated by Django's session login.
"""

import functools
import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from . import services
from .maxio.client import MaxioConfigurationError
from .maxio.errors import ProviderError, ProviderRejected
from .provider import get_gateway

logger = logging.getLogger(__name__)


def error_response(status, code, message, **extra):
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def api_view(view):
    """Session auth + one place where every failure becomes a JSON answer."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error_response(401, 'not_authenticated', 'Log in to use this endpoint.')
        try:
            return view(request, *args, **kwargs)
        except services.ServiceError as exc:
            return error_response(exc.status_code, exc.code, exc.message)
        except ProviderRejected as exc:
            return error_response(exc.status_code, 'rejected_by_billing_provider', exc.message,
                                  details=exc.messages)
        except ProviderError as exc:
            logger.warning('Maxio call failed (%s, outcome_unknown=%s): %s',
                           type(exc).__name__, exc.outcome_unknown, exc.message)
            return error_response(exc.status_code, 'billing_provider_unavailable', exc.message,
                                  outcomeUnknown=exc.outcome_unknown)
        except MaxioConfigurationError:
            logger.exception('Maxio is not configured')
            return error_response(503, 'billing_not_configured', 'Subscription billing is not configured.')

    return wrapper


@require_GET
@ensure_csrf_cookie
@api_view
def subscription_plans(request):
    gateway = get_gateway()
    page = services.list_plans(gateway)
    return JsonResponse({
        'productFamily': gateway.product_family,
        'plans': [services.plan_payload(plan) for plan in page.plans],
        'truncated': page.truncated,
    })


# The claim row must be committed before Maxio is called, so these views manage
# their own transactions instead of running inside ATOMIC_REQUESTS.
@transaction.non_atomic_requests
@require_http_methods(['POST'])
@api_view
def subscriptions(request):
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        return error_response(400, 'invalid_json', 'Request body must be JSON.')
    plan_handle = body.get('planHandle') if isinstance(body, dict) else None
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return error_response(400, 'plan_handle_required', 'Provide "planHandle" from GET /api/subscription-plans.')
    result = services.subscribe(get_gateway(), request.user, plan_handle.strip())
    payload = services.subscription_payload(row=result.row)
    return JsonResponse({
        'subscriptionId': result.row.maxio_subscription_id,
        'status': result.row.status,
        'subscription': payload,
    }, status=result.http_status)


@transaction.non_atomic_requests
@require_GET
@ensure_csrf_cookie
@api_view
def my_subscriptions(request):
    return JsonResponse({'subscriptions': services.my_subscriptions(get_gateway(), request.user)})
