"""HTTP JSON endpoints for Maxio subscription billing.

All endpoints authenticate with the sandbox's own Django session login and take
the caller's identity from ``request.user``. Each capability is a separate,
independently invocable endpoint:

* ``GET  /api/subscription-plans``  -> available plans (each carries ``planHandle``)
* ``POST /api/subscriptions``       -> subscribe; returns top-level ``subscriptionId``
* ``GET  /api/my-subscriptions``    -> the caller's subscriptions
"""

from __future__ import annotations

import json
import logging

from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .services import MaxioService, SubscriptionError

logger = logging.getLogger('sandbox.subscriptions')

#: The default plan to subscribe to when the caller does not name one.
DEFAULT_PLAN_HANDLE = 'eshop-pro'


def _require_login(request):
    """Return a 401 JsonResponse if the caller is not authenticated, else None."""
    if not request.user.is_authenticated:
        return JsonResponse(
            {'error': 'Authentication required. Log in to the sandbox first.'},
            status=401,
        )
    return None


def _error_response(exc):
    payload = {'error': exc.message}
    if exc.detail:
        payload['detail'] = exc.detail
    return JsonResponse(payload, status=exc.http_status)


def _service():
    return MaxioService()


@require_http_methods(['GET'])
def subscription_plans(request):
    """GET /api/subscription-plans — list the available subscription plans."""
    denied = _require_login(request)
    if denied is not None:
        return denied
    try:
        plans = _service().list_plans()
    except ImproperlyConfigured as exc:
        logger.error('Maxio not configured: %s', exc)
        return JsonResponse({'error': 'Maxio billing is not configured.'}, status=503)
    except SubscriptionError as exc:
        return _error_response(exc)
    return JsonResponse({'plans': plans, 'count': len(plans)})


@csrf_exempt
@require_http_methods(['POST'])
def create_subscription(request):
    """POST /api/subscriptions — subscribe the caller to a plan.

    Body (JSON or form): ``planHandle`` (optional; defaults to the Pro plan).
    Returns ``subscriptionId`` as a top-level field.
    """
    denied = _require_login(request)
    if denied is not None:
        return denied

    plan_handle = _read_plan_handle(request)
    try:
        result = _service().subscribe(request.user, plan_handle)
    except ImproperlyConfigured as exc:
        logger.error('Maxio not configured: %s', exc)
        return JsonResponse({'error': 'Maxio billing is not configured.'}, status=503)
    except SubscriptionError as exc:
        return _error_response(exc)

    status = 200 if result.get('reused') else 201
    body = {'subscriptionId': result.get('subscriptionId'), 'subscription': result}
    return JsonResponse(body, status=status)


@require_http_methods(['GET'])
def my_subscriptions(request):
    """GET /api/my-subscriptions — list the caller's subscriptions."""
    denied = _require_login(request)
    if denied is not None:
        return denied
    try:
        subscriptions = _service().list_my_subscriptions(request.user)
    except ImproperlyConfigured as exc:
        logger.error('Maxio not configured: %s', exc)
        return JsonResponse({'error': 'Maxio billing is not configured.'}, status=503)
    except SubscriptionError as exc:
        return _error_response(exc)
    return JsonResponse(
        {'subscriptions': subscriptions, 'count': len(subscriptions)}
    )


def _read_plan_handle(request):
    """Extract ``planHandle`` from a JSON or form body, defaulting to the Pro plan."""
    handle = None
    content_type = (request.content_type or '').lower()
    if 'application/json' in content_type:
        raw = request.body.decode('utf-8').strip() if request.body else ''
        if raw:
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                data = {}
            if isinstance(data, dict):
                handle = data.get('planHandle') or data.get('plan_handle')
    if not handle:
        handle = request.POST.get('planHandle') or request.POST.get('plan_handle')
    return handle or DEFAULT_PLAN_HANDLE
