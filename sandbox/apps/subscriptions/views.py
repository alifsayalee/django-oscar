"""HTTP API for Maxio subscription billing.

Endpoints (routed under ``/api/`` in ``sandbox/urls.py``):

* ``GET  /api/subscription-plans``  -- list plans; each carries ``planHandle``.
* ``POST /api/subscriptions``       -- subscribe; returns ``subscriptionId``.
* ``GET  /api/my-subscriptions``    -- the caller's subscriptions.

Callers are authenticated exactly as the rest of the sandbox authenticates
them -- Django's own session login -- and the acting user is taken from the
authenticated request. The endpoints are CSRF-exempt so a programmatic caller
holding a session cookie can drive the API directly.
"""

import json
from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import services
from .errors import MaxioError


def api_endpoint(view):
    """Enforce session auth and translate :class:`MaxioError` into JSON."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required.'}, status=401)
        try:
            return view(request, *args, **kwargs)
        except MaxioError as exc:
            return JsonResponse({'error': exc.message}, status=exc.status_code)

    return wrapper


def _read_plan_handle(request):
    """Read ``planHandle`` from a JSON or form body; fall back to the default."""
    plan_handle = None
    content_type = (request.content_type or '').lower()
    if 'application/json' in content_type:
        if request.body:
            try:
                data = json.loads(request.body.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                raise MaxioError(400, 'Request body is not valid JSON.')
            if not isinstance(data, dict):
                raise MaxioError(400, 'Request body must be a JSON object.')
            plan_handle = data.get('planHandle') or data.get('plan_handle')
    else:
        plan_handle = request.POST.get('planHandle') or request.POST.get('plan_handle')

    if not plan_handle:
        plan_handle = getattr(settings, 'MAXIO_DEFAULT_PLAN_HANDLE', '') or ''
    if not plan_handle:
        raise MaxioError(400, 'planHandle is required.')
    return plan_handle


@csrf_exempt
@require_http_methods(['GET'])
@api_endpoint
def subscription_plans(request):
    return JsonResponse({'plans': services.list_plans()})


@csrf_exempt
@require_http_methods(['POST'])
@api_endpoint
def create_subscription(request):
    plan_handle = _read_plan_handle(request)
    payload, status = services.subscribe(request.user, plan_handle)
    return JsonResponse(payload, status=status)


@csrf_exempt
@require_http_methods(['GET'])
@api_endpoint
def my_subscriptions(request):
    return JsonResponse({'subscriptions': services.list_my_subscriptions(request.user)})
