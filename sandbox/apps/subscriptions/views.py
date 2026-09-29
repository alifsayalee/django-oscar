"""
JSON endpoints for Maxio-billed subscriptions.

Callers authenticate with the sandbox's normal Django session login; POSTs are
CSRF-protected by the site's middleware (the GET endpoints set the cookie).
Views run outside ATOMIC_REQUESTS because each claim must commit before the
Maxio call it guards.
"""

import json
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import services
from .errors import BillingError

logger = logging.getLogger("apps.subscriptions")

View = Callable[..., HttpResponse]


def error_response(status: int, code: str, message: str, *, details: list[str] | None = None,
                   retryable: bool = False) -> JsonResponse:
    return JsonResponse(
        {"error": {"code": code, "message": message, "details": details or [], "retryable": retryable}},
        status=status,
    )


def billing_api(view: View) -> View:
    """Session auth (401 JSON, no redirect) plus one error boundary for every endpoint."""

    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return error_response(401, "not_authenticated", "Log in to use the subscriptions API.")
        try:
            return view(request, *args, **kwargs)
        except BillingError as err:
            return error_response(
                err.status_code, err.code, err.message, details=err.details,
                retryable=err.outcome_unknown or err.status_code in (409, 502, 503, 504),
            )
        except ImproperlyConfigured:
            logger.exception("Maxio billing is misconfigured")
            return error_response(503, "billing_not_configured", "Subscription billing is not available.")

    return transaction.non_atomic_requests(wrapper)


@require_GET
@ensure_csrf_cookie
@billing_api
def subscription_plans(request: HttpRequest) -> HttpResponse:
    plans = services.list_plans()
    return JsonResponse({"plans": [plan.to_json() for plan in plans]})


@require_POST
@billing_api
def billing_customer(request: HttpRequest) -> HttpResponse:
    customer = services.ensure_customer(request.user)
    return JsonResponse({
        "customerId": customer.maxio_customer_id,
        "reference": customer.reference,
    })


def _plan_handle(request: HttpRequest) -> str | None:
    if request.content_type == "application/json":
        try:
            payload = json.loads(request.body or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise BillingError(400, "invalid_json", "The request body is not valid JSON.") from None
        value = payload.get("planHandle") if isinstance(payload, dict) else None
    else:
        value = request.POST.get("planHandle")
    return value.strip() if isinstance(value, str) and value.strip() else None


@require_POST
@billing_api
def subscriptions(request: HttpRequest) -> HttpResponse:
    plan_handle = _plan_handle(request)
    if plan_handle is None:
        return error_response(400, "plan_handle_required", "planHandle is required.")
    row, created = services.subscribe(request.user, plan_handle)
    body = services.subscription_to_json(row)
    body["created"] = created
    return JsonResponse(body, status=201 if created else 200)


@require_GET
@ensure_csrf_cookie
@billing_api
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    rows, source = services.my_subscriptions(request.user)
    return JsonResponse({
        "subscriptions": [services.subscription_to_json(row) for row in rows],
        "source": source,
    })


@require_GET
@billing_api
def subscription_detail(request: HttpRequest, subscription_id: int) -> HttpResponse:
    row = services.get_subscription(request.user, subscription_id)
    return JsonResponse(services.subscription_to_json(row))
