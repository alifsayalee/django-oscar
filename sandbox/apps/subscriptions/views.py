"""HTTP endpoints for Maxio subscription billing, mounted under ``/api/``.

Callers are authenticated exactly as the rest of the sandbox authenticates
them — Django's own session login — and the caller's identity is taken from the
authenticated ``request.user``. Each capability is a separate endpoint; there is
no do-everything route.

Responses are JSON. The subscribe endpoint is a JSON API entry point driven by a
session cookie, so it is CSRF-exempt; it still requires an authenticated user.
"""

import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .serializers import serialize_plan, serialize_subscription
from .service import MaxioServiceError, MaxioSubscriptionService

logger = logging.getLogger('apps.subscriptions')


def _require_login(request):
    """Return a 401 JsonResponse when the request is not authenticated, else None."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse(
            {'error': 'authentication_required',
             'message': 'You must be signed in to use the subscription API.'},
            status=401,
        )
    return None


def _service_error_response(exc):
    payload = {'error': 'maxio_error', 'message': exc.message}
    if exc.detail:
        payload['detail'] = exc.detail
    return JsonResponse(payload, status=exc.http_status)


@require_http_methods(['GET'])
def subscription_plans(request):
    """GET /api/subscription-plans — the plans on offer.

    Each entry carries its own ``planHandle``.
    """
    unauthorized = _require_login(request)
    if unauthorized is not None:
        return unauthorized

    service = MaxioSubscriptionService()
    try:
        plans = service.list_plans()
    except MaxioServiceError as exc:
        return _service_error_response(exc)

    return JsonResponse({'plans': [serialize_plan(p) for p in plans]})


@csrf_exempt
@require_http_methods(['POST'])
def create_subscription(request):
    """POST /api/subscriptions — subscribe the caller to a plan.

    Body (JSON or form): ``{"planHandle": "<handle>"}``. When ``planHandle`` is
    omitted, the configured default plan is used. Ensures a Maxio customer exists
    for the caller (idempotent) and enrolls them (idempotent per plan).

    Returns ``subscriptionId`` as a top-level field of the response body.
    """
    unauthorized = _require_login(request)
    if unauthorized is not None:
        return unauthorized

    plan_handle = _read_plan_handle(request)
    if not plan_handle:
        return JsonResponse(
            {'error': 'invalid_request',
             'message': 'No planHandle was provided and no default plan is configured.'},
            status=400,
        )

    service = MaxioSubscriptionService()
    try:
        subscription, created = service.subscribe(request.user, plan_handle)
    except MaxioServiceError as exc:
        return _service_error_response(exc)

    body = serialize_subscription(subscription)
    # subscriptionId is promoted to a top-level field so the flow can be driven
    # end to end by a caller.
    response = {
        'subscriptionId': body['subscriptionId'],
        'created': created,
        'subscription': body,
    }
    return JsonResponse(response, status=201 if created else 200)


@require_http_methods(['GET'])
def my_subscriptions(request):
    """GET /api/my-subscriptions — the caller's subscriptions."""
    unauthorized = _require_login(request)
    if unauthorized is not None:
        return unauthorized

    service = MaxioSubscriptionService()
    try:
        subscriptions = service.list_my_subscriptions(request.user)
    except MaxioServiceError as exc:
        return _service_error_response(exc)

    return JsonResponse(
        {'subscriptions': [serialize_subscription(s) for s in subscriptions]}
    )


def _read_plan_handle(request):
    """Extract planHandle from a JSON body or form data, falling back to default."""
    plan_handle = None
    content_type = (request.content_type or '').lower()
    if 'application/json' in content_type and request.body:
        try:
            data = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict):
            plan_handle = data.get('planHandle') or data.get('plan_handle')
    if not plan_handle:
        plan_handle = request.POST.get('planHandle') or request.POST.get('plan_handle')
    if not plan_handle:
        plan_handle = getattr(settings, 'MAXIO_DEFAULT_PLAN_HANDLE', '') or None
    return plan_handle
