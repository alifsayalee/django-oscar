"""HTTP endpoints for the Maxio subscriptions capability.

Plain Django JSON views authenticated by the sandbox's own session login. Each capability
is a separate endpoint under /api/. CSRF protection stays enabled (session-cookie auth), so
the POST endpoint expects an ``X-CSRFToken`` header.
"""

import json

from django.db import transaction
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View

from . import services
from .exceptions import MaxioError


def _require_login(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)
    return None


def _error_response(exc):
    return JsonResponse(
        {"error": str(exc), "outcomeUnknown": getattr(exc, "outcome_unknown", False)},
        status=exc.status_code,
    )


class PlansView(View):
    """GET /api/subscription-plans -- list the available plans."""

    def get(self, request):
        denied = _require_login(request)
        if denied is not None:
            return denied
        try:
            plans = services.list_plans()
        except MaxioError as exc:
            return _error_response(exc)
        return JsonResponse({"plans": plans})


@method_decorator(transaction.non_atomic_requests, name="dispatch")
class SubscribeView(View):
    """POST /api/subscriptions -- subscribe the caller to a plan.

    Runs outside the request-level atomic transaction so the durable claim is committed
    before the provider call (the durability the idempotency guarantee rests on).
    """

    def post(self, request):
        denied = _require_login(request)
        if denied is not None:
            return denied
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Request body must be valid JSON."}, status=400)
        if not isinstance(payload, dict):
            return JsonResponse({"error": "Request body must be a JSON object."}, status=400)
        plan_handle = (payload.get("planHandle") or "").strip()
        if not plan_handle:
            return JsonResponse({"error": "planHandle is required."}, status=400)
        try:
            result = services.subscribe(request.user, plan_handle)
        except MaxioError as exc:
            return _error_response(exc)
        status = 200 if result.get("idempotent") else 201
        return JsonResponse(result, status=status)


class MySubscriptionsView(View):
    """GET /api/my-subscriptions -- the caller's subscriptions."""

    def get(self, request):
        denied = _require_login(request)
        if denied is not None:
            return denied
        try:
            subscriptions = services.list_my_subscriptions(request.user)
        except MaxioError as exc:
            return _error_response(exc)
        return JsonResponse({"subscriptions": subscriptions})
