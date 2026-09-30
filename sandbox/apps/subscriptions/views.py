"""
JSON endpoints for subscription billing, under /api/.

Callers authenticate with the site's Django session login; identity is always
the session user. POST requests need the CSRF token like any other form post
on the site (cookie ``csrftoken``, header ``X-CSRFToken``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import services
from .maxio import MaxioError, get_client

logger = logging.getLogger(__name__)

View = Callable[..., HttpResponse]


def _error(status: int, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({"error": message, **extra}, status=status)


def _maxio_error(exc: MaxioError) -> JsonResponse:
    extra: dict[str, Any] = {}
    if exc.details:
        extra["details"] = exc.details
    if exc.outcome_unknown:
        extra["outcomeUnknown"] = True
    return _error(exc.status_code, exc.message, **extra)


def api_login_required(view: View) -> View:
    """Answer 401 JSON rather than redirecting an unauthenticated API caller to a login page."""
    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return _error(401, "Authentication required. Sign in through the site's login page.")
        return view(request, *args, **kwargs)
    return wrapped


# Claims must commit before Maxio is called, so these views manage their own
# transactions instead of running inside ATOMIC_REQUESTS.

@transaction.non_atomic_requests
@require_GET
@ensure_csrf_cookie
def subscription_plans(request: HttpRequest) -> HttpResponse:
    try:
        plans = services.list_plans(get_client())
    except MaxioError as exc:
        return _maxio_error(exc)
    return JsonResponse({"productFamily": settings.MAXIO_DEFAULT_PRODUCT_FAMILY, "plans": plans})


@transaction.non_atomic_requests
@require_POST
@api_login_required
def create_subscription(request: HttpRequest) -> HttpResponse:
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return _error(400, "The request body must be JSON.")
    if not isinstance(payload, dict):
        return _error(400, "The request body must be a JSON object.")
    plan_handle = payload.get("planHandle")
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return _error(400, "planHandle is required; pick one from GET /api/subscription-plans.")
    plan_handle = plan_handle.strip()

    try:
        result = services.subscribe(get_client(), request.user, plan_handle)
    except services.PlanNotFound:
        return _error(404, f"No subscription plan with handle {plan_handle!r}.")
    except services.InvalidCustomerDetails as exc:
        return _error(400, str(exc))
    except services.CustomerSetupInProgress:
        return _error(409, "Your billing account is still being set up; try again in a moment.")
    except MaxioError as exc:
        return _maxio_error(exc)

    record = result.record
    body = {
        "subscriptionId": record.maxio_subscription_id,
        "created": result.created,
        "status": record.status,
        "subscription": result.subscription,
    }
    if not result.created:
        if record.status == record.LIVE:
            # Double submit: the subscription already exists; answer with it.
            return JsonResponse(body, status=200)
        return JsonResponse({**body, "error": "A subscription to this plan is already being set up."},
                            status=409)
    if record.status == record.UNKNOWN:
        return JsonResponse({**body, "outcomeUnknown": True}, status=202)
    return JsonResponse(body, status=201)


@transaction.non_atomic_requests
@require_GET
@api_login_required
@ensure_csrf_cookie
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    try:
        subscriptions = services.my_subscriptions(get_client(), request.user)
    except MaxioError as exc:
        return _maxio_error(exc)
    return JsonResponse({"subscriptions": subscriptions})
