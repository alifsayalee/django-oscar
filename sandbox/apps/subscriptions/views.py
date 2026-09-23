"""
JSON endpoints for subscription billing.

Callers authenticate with the sandbox's normal Django session login; the
billing identity is always ``request.user``. Unsafe methods keep Django's CSRF
protection (send the ``csrftoken`` cookie value as ``X-CSRFToken``).

The views opt out of the sandbox's ATOMIC_REQUESTS: a claim row must be
committed *before* the Maxio call it guards, so a concurrent request can see it
and a crash mid-call cannot roll it back and strand what Maxio created.
"""

import json
import logging
from functools import wraps

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import services
from .gateway import ProviderError, ProviderRejected, present
from .models import SubscriptionEnrollment

logger = logging.getLogger("apps.subscriptions")


def _error(status, code, message, **extra):
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status)


def api_view(view):
    """Session auth as JSON (401, not a login redirect) plus one error boundary."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if getattr(view, "login_required", True) and not request.user.is_authenticated:
            return _error(401, "not_authenticated", "Log in to use this endpoint.")
        try:
            return view(request, *args, **kwargs)
        except services.SubscriptionServiceError as exc:
            return _error(exc.status_code, exc.code, exc.message, **exc.extra)
        except ProviderRejected as exc:
            return _error(exc.status_code, "rejected_by_billing_provider", exc.message, details=exc.messages)
        except ProviderError as exc:
            logger.warning(
                "Maxio call failed: %s (provider status %s, outcome unknown %s)",
                type(exc).__name__, exc.provider_status, exc.outcome_unknown,
            )
            return _error(exc.status_code, "billing_provider_unavailable", exc.message)
        except ImproperlyConfigured:
            logger.exception("Maxio billing is not configured")
            return _error(503, "billing_not_configured", "Subscription billing is not available.")

    return wrapped


def public(view):
    view.login_required = False
    return view


def _iso(value):
    return value.isoformat() if value is not None else None


def plan_json(plan):
    return {
        "planHandle": plan.handle,
        "name": plan.name,
        "description": plan.description,
        "priceInCents": plan.price_in_cents,
        "price": services.format_cents(plan.price_in_cents),
        "interval": plan.interval,
        "intervalUnit": plan.interval_unit,
    }


def subscription_json(sub, enrollment=None):
    product = present(sub.product)
    state = present(sub.state)
    status = enrollment.status if enrollment is not None else services.status_from_provider(state)
    price = present(sub.product_price_in_cents)
    unit = present(product.interval_unit) if product is not None else None
    return {
        "subscriptionId": present(sub.id),
        "planHandle": present(product.handle) if product is not None else None,
        "planName": present(product.name) if product is not None else None,
        "priceInCents": price,
        "price": services.format_cents(price),
        "interval": present(product.interval) if product is not None else None,
        "intervalUnit": str(unit) if unit is not None else None,
        "state": str(state) if state is not None else None,
        "status": status,
        "nextBillingAt": _iso(present(sub.next_assessment_at)),
        "currentPeriodEndsAt": _iso(present(sub.current_period_ends_at)),
        "activatedAt": _iso(present(sub.activated_at)),
        "createdAt": _iso(present(sub.created_at)),
        "reference": present(sub.reference),
    }


def enrollment_json(enrollment):
    """A claim Maxio has not confirmed yet (or that we could not read back)."""
    return {
        "subscriptionId": enrollment.maxio_subscription_id,
        "planHandle": enrollment.plan_handle,
        "priceInCents": enrollment.expected_price_in_cents,
        "price": services.format_cents(enrollment.expected_price_in_cents),
        "state": enrollment.maxio_state or None,
        "status": enrollment.status,
        "reference": enrollment.reference,
    }


@transaction.non_atomic_requests
@require_GET
@api_view
@public
def subscription_plans(request):
    plans = services.list_plans()
    return JsonResponse({"plans": [plan_json(p) for p in plans]})


@transaction.non_atomic_requests
@require_POST
@api_view
def billing_customer(request):
    row = services.ensure_customer(request.user)
    return JsonResponse({"customerId": row.maxio_customer_id, "reference": row.reference, "status": row.status})


@transaction.non_atomic_requests
@require_POST
@api_view
def subscriptions(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return _error(400, "invalid_json", "Request body must be JSON.")
    plan_handle = payload.get("planHandle") if isinstance(payload, dict) else None
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return _error(400, "invalid_request", "planHandle is required.")

    result = services.subscribe(request.user, plan_handle.strip())
    enrollment = result.enrollment
    body = (
        subscription_json(result.subscription, enrollment)
        if result.subscription is not None
        else enrollment_json(enrollment)
    )
    if enrollment.status_detail:
        body["statusDetail"] = enrollment.status_detail

    # Success only from the done state; everything else is "accepted, not done".
    if enrollment.status == SubscriptionEnrollment.DONE:
        status = 201 if result.created else 200
    else:
        status = 202
    return JsonResponse(body, status=status)


@transaction.non_atomic_requests
@require_GET
@api_view
def subscription_detail(request, subscription_id):
    sub, enrollment = services.get_subscription(request.user, subscription_id)
    return JsonResponse(subscription_json(sub, enrollment))


@transaction.non_atomic_requests
@require_GET
@api_view
def my_subscriptions(request):
    live, unresolved = services.my_subscriptions(request.user)
    rows = {e.maxio_subscription_id: e for e in request.user.subscription_enrollments.all()
            if e.maxio_subscription_id}
    return JsonResponse({
        "subscriptions": [subscription_json(s, rows.get(present(s.id))) for s in live],
        "pending": [enrollment_json(e) for e in unresolved],
    })
