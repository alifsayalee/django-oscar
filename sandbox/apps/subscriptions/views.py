"""
JSON API for subscription billing, under /api/.

Callers authenticate with the sandbox's own Django session login; identity is
always ``request.user``. POSTs are CSRF-protected like every other form on the
site. Views run outside ATOMIC_REQUESTS so that each claim row commits before
Maxio is called.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from apps.subscriptions import services
from apps.subscriptions.gateway import Plan, ProviderError, RemoteSubscription
from apps.subscriptions.maxio import MaxioConfigError
from apps.subscriptions.models import BillingCustomer, ClaimStatus, SubscriptionEnrollment

if TYPE_CHECKING:
    # Typing only; at runtime the user model is AUTH_USER_MODEL.
    from django.contrib.auth.models import User  # pylint: disable=imported-auth-user

logger = logging.getLogger("apps.subscriptions.views")

# Accepted but not confirmed: answered 202, never 200/201.
NOT_CONFIRMED = (
    ClaimStatus.SENDING,
    ClaimStatus.PENDING,
    ClaimStatus.UNKNOWN,
    ClaimStatus.NEEDS_REVIEW,
)


def _error(status: int, message: str, **extra: object) -> JsonResponse:
    return JsonResponse({"error": {"message": message, **extra}}, status=status)


View = Callable[..., HttpResponse]


def api_view(view: View) -> View:
    """Session auth, JSON errors, and no request-wide transaction."""

    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return _error(401, "Authentication required.")
        try:
            return view(request, *args, **kwargs)
        except services.InProgress as exc:
            return JsonResponse(
                {"status": ClaimStatus.SENDING, "message": exc.message}, status=202
            )
        except services.SubscriptionFlowError as exc:
            return _error(exc.status_code, exc.message, **exc.extra)
        except ProviderError as exc:
            body: dict[str, object] = {"outcomeUnknown": exc.outcome_unknown}
            if exc.details and exc.status_code in (400, 404, 409, 422):
                body["details"] = exc.details
            return _error(exc.status_code, exc.message, **body)
        except MaxioConfigError:
            logger.exception("maxio integration misconfigured")
            return _error(503, "Subscription billing is not configured.")

    return cast(View, transaction.non_atomic_requests(wrapper))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _money(cents: int | None) -> str | None:
    if cents is None:
        return None
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _plan_json(plan: Plan) -> dict[str, object]:
    return {
        "planHandle": plan.handle,
        "planId": plan.id,
        "name": plan.name,
        "description": plan.description,
        "priceInCents": plan.price_in_cents,
        "price": _money(plan.price_in_cents),
        "interval": plan.interval,
        "intervalUnit": plan.interval_unit,
        "trialPriceInCents": plan.trial_price_in_cents,
        "trialInterval": plan.trial_interval,
        "trialIntervalUnit": plan.trial_interval_unit,
        "setupFeeInCents": plan.initial_charge_in_cents,
        "requiresPaymentMethod": plan.requires_payment_method,
        "productFamily": plan.product_family_handle,
    }


def _subscription_json(sub: RemoteSubscription) -> dict[str, object]:
    return {
        "subscriptionId": sub.id,
        "planHandle": sub.product_handle,
        "planName": sub.product_name,
        "state": sub.state,
        "status": services.status_from_state(sub.state),
        "priceInCents": sub.price_in_cents,
        "price": _money(sub.price_in_cents),
        "currentBillingAmountInCents": sub.current_billing_amount_in_cents,
        "currency": sub.currency,
        "paymentCollectionMethod": sub.payment_collection_method,
        "interval": sub.interval,
        "intervalUnit": sub.interval_unit,
        "nextBillingAt": _iso(sub.next_billing_at),
        "currentPeriodEndsAt": _iso(sub.current_period_ends_at),
        "activatedAt": _iso(sub.activated_at),
        "createdAt": _iso(sub.created_at),
        "canceledAt": _iso(sub.canceled_at),
        "reference": sub.reference,
    }


def _enrollment_json(row: SubscriptionEnrollment) -> dict[str, object]:
    return {
        "subscriptionId": row.maxio_subscription_id,
        "planHandle": row.plan_handle,
        "status": row.status,
        "state": row.maxio_state or None,
        "reference": row.reference,
        "requestedAt": _iso(row.date_created),
    }


def _customer_json(row: BillingCustomer) -> dict[str, object]:
    return {
        "customerId": row.maxio_customer_id,
        "reference": row.reference,
        "status": row.status,
    }


@require_GET
@api_view
def subscription_plans(request: HttpRequest) -> HttpResponse:
    plans, truncated = services.list_plans()
    return JsonResponse(
        {"plans": [_plan_json(p) for p in plans], "truncated": truncated}
    )


@require_POST
@api_view
def billing_customer(request: HttpRequest) -> HttpResponse:
    row = services.ensure_customer(_user(request))
    return JsonResponse({"customer": _customer_json(row)})


def _user(request: HttpRequest) -> User:
    # api_view has already rejected anonymous callers.
    return cast("User", request.user)


def _plan_handle_from(request: HttpRequest) -> str:
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        raise services.SubscriptionFlowError(400, "Request body must be JSON.")
    if not isinstance(payload, dict):
        raise services.SubscriptionFlowError(400, "Request body must be a JSON object.")
    plan_handle = payload.get("planHandle")
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        raise services.SubscriptionFlowError(
            400, "planHandle is required.", field="planHandle"
        )
    return plan_handle.strip()


@require_POST
@api_view
def subscriptions(request: HttpRequest) -> HttpResponse:
    plan_handle = _plan_handle_from(request)
    try:
        result = services.subscribe(_user(request), plan_handle)
    except services.InProgress as exc:
        return JsonResponse(
            {"subscriptionId": None, "status": ClaimStatus.SENDING, "message": exc.message},
            status=202,
        )
    row = result.enrollment
    body: dict[str, object] = {
        "subscriptionId": row.maxio_subscription_id,
        "status": row.status,
        "created": result.created,
        "reference": row.reference,
        "subscription": _subscription_json(result.subscription)
        if result.subscription is not None
        else None,
    }
    if row.status == ClaimStatus.FAILED:
        # Maxio answered with an id and a refused state: not a subscription.
        body["error"] = {"message": "Billing provider could not create the subscription."}
        return JsonResponse(body, status=422)
    if result.subscription is None or row.status in NOT_CONFIRMED:
        # Accepted, not confirmed: in flight at another request, pending at
        # Maxio, or an outcome we are still reconciling.
        return JsonResponse(body, status=202)
    return JsonResponse(body, status=201 if result.created else 200)


@require_GET
@api_view
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    remote, unresolved = services.my_subscriptions(_user(request))
    return JsonResponse(
        {
            "subscriptions": [_subscription_json(s) for s in remote],
            "pendingEnrollments": [_enrollment_json(r) for r in unresolved],
        }
    )


@require_GET
@api_view
def subscription_detail(request: HttpRequest, subscription_id: int) -> HttpResponse:
    remote = services.get_subscription(_user(request), subscription_id)
    return JsonResponse({"subscriptionId": remote.id, "subscription": _subscription_json(remote)})
