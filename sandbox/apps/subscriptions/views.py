"""HTTP endpoints for the Maxio subscription capability.

Plain Django JSON views (no extra dependencies). Callers are authenticated exactly as the rest of
the sandbox authenticates them — Django's session login — and the caller's identity is taken from
``request.user``. Each capability has its own route and is separately invocable.

CSRF: the state-changing ``POST /api/subscriptions`` is protected by Django's CSRF middleware, as
befits a session-authenticated endpoint. ``GET /api/subscription-plans`` sets the CSRF cookie
(``ensure_csrf_cookie``) so a caller driving the API can obtain the token to send back on the POST.
"""

import json

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import ensure_csrf_cookie

from .exceptions import BillingError
from . import services


def _require_login(request):
    """Return a 401 JsonResponse if the request is not authenticated, else None."""
    if not request.user.is_authenticated:
        return JsonResponse(
            {'error': 'authentication_required',
             'detail': 'You must be logged in to use the subscription API.'},
            status=401,
        )
    return None


def _billing_error_response(exc):
    return JsonResponse(
        {'error': exc.__class__.__name__, 'detail': str(exc)},
        status=exc.status_code,
    )


@method_decorator(ensure_csrf_cookie, name='dispatch')
class SubscriptionPlansView(View):
    """GET /api/subscription-plans — list the available plans."""

    def get(self, request):
        unauthorized = _require_login(request)
        if unauthorized is not None:
            return unauthorized
        try:
            plans = services.list_plans()
        except BillingError as exc:
            return _billing_error_response(exc)
        return JsonResponse({'plans': plans})


class SubscriptionsView(View):
    """POST /api/subscriptions — subscribe the caller to a plan."""

    def post(self, request):
        unauthorized = _require_login(request)
        if unauthorized is not None:
            return unauthorized

        try:
            payload = json.loads(request.body or b'{}')
        except (ValueError, TypeError):
            return JsonResponse(
                {'error': 'invalid_request', 'detail': 'Request body must be valid JSON.'},
                status=400,
            )
        if not isinstance(payload, dict):
            return JsonResponse(
                {'error': 'invalid_request', 'detail': 'Request body must be a JSON object.'},
                status=400,
            )

        # planHandle is optional; default to the Pro plan (the default subscribe target).
        plan_handle = payload.get('planHandle') or 'eshop-pro'

        try:
            summary, created = services.subscribe(request.user, plan_handle)
        except BillingError as exc:
            return _billing_error_response(exc)

        body = dict(summary)
        body['created'] = created
        # subscriptionId is guaranteed present as a top-level field of the response body.
        return JsonResponse(body, status=201 if created else 200)


class MySubscriptionsView(View):
    """GET /api/my-subscriptions — the caller's subscriptions."""

    def get(self, request):
        unauthorized = _require_login(request)
        if unauthorized is not None:
            return unauthorized
        try:
            subscriptions = services.list_my_subscriptions(request.user)
        except BillingError as exc:
            return _billing_error_response(exc)
        return JsonResponse({'subscriptions': subscriptions})
