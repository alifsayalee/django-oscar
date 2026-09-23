"""HTTP endpoints for the Maxio subscription capability.

Plain Django JSON views (the sandbox ships no DRF). Callers authenticate with the sandbox's
own Django session login; identity is taken from ``request.user``. Each capability is its
own separately-invocable view:

    GET  /api/subscription-plans   -> browse plans
    POST /api/subscriptions        -> subscribe (idempotent)
    GET  /api/my-subscriptions     -> read the caller's subscriptions
"""

import json
import logging
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from .exceptions import SubscriptionError
from . import services

logger = logging.getLogger("maxio.subscriptions")


def _require_login(request: HttpRequest) -> Optional[JsonResponse]:
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "Authentication required."}, status=401
        )
    return None


def _error_response(exc: SubscriptionError) -> JsonResponse:
    return JsonResponse({"error": exc.message}, status=exc.status_code)


@require_GET
def subscription_plans(request: HttpRequest) -> HttpResponse:
    """List the plans a caller can subscribe to."""
    unauthorized = _require_login(request)
    if unauthorized:
        return unauthorized
    try:
        plans = services.list_plans()
    except SubscriptionError as exc:
        return _error_response(exc)
    except Exception:  # pragma: no cover - unexpected
        logger.exception("Unexpected error listing plans")
        return JsonResponse({"error": "Internal error."}, status=500)
    return JsonResponse({"plans": plans})


@transaction.non_atomic_requests
@require_POST
def create_subscription(request: HttpRequest) -> HttpResponse:
    """Subscribe the authenticated caller to a plan (idempotent).

    ``non_atomic_requests`` (outermost, so Django's request handler sees the flag) means the
    durable claim row commits *before* the provider call, rather than being held open in the
    request's transaction. Because ATOMIC_REQUESTS is on globally, this view opts out and
    relies on autocommit plus the explicit ``transaction.atomic()`` block in ``subscribe``.
    """
    unauthorized = _require_login(request)
    if unauthorized:
        return unauthorized

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
        if not isinstance(body, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Request body must be a JSON object."}, status=400)

    plan_handle = body.get("planHandle") or getattr(
        settings, "MAXIO_DEFAULT_PLAN_HANDLE", "eshop-pro"
    )
    if not isinstance(plan_handle, str) or not plan_handle:
        return JsonResponse({"error": "planHandle must be a non-empty string."}, status=400)

    try:
        payload, created = services.subscribe(request.user, plan_handle)
    except SubscriptionError as exc:
        return _error_response(exc)
    except Exception:  # pragma: no cover - unexpected
        logger.exception("Unexpected error creating subscription")
        return JsonResponse({"error": "Internal error."}, status=500)

    return JsonResponse(payload, status=201 if created else 200)


@require_GET
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    """Return the authenticated caller's subscriptions from Maxio."""
    unauthorized = _require_login(request)
    if unauthorized:
        return unauthorized
    try:
        subscriptions = services.list_my_subscriptions(request.user)
    except SubscriptionError as exc:
        return _error_response(exc)
    except Exception:  # pragma: no cover - unexpected
        logger.exception("Unexpected error listing subscriptions")
        return JsonResponse({"error": "Internal error."}, status=500)
    return JsonResponse({"subscriptions": subscriptions})
