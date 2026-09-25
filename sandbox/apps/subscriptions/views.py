"""
JSON API for recurring subscriptions billed through Maxio Advanced Billing.

Callers authenticate with the sandbox's normal Django session login; the
caller's identity is always ``request.user``. Views that write claims opt out
of ATOMIC_REQUESTS so each claim is committed before Maxio is called.
"""
import json
import logging
from collections.abc import Callable
from datetime import timezone as dt_timezone
from functools import wraps
from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import (
    require_GET, require_http_methods, require_POST)

from . import billing
from .maxio_client import product_family
from .models import BillingCustomer, Outcome, SubscriptionClaim

logger = logging.getLogger(__name__)

View = Callable[..., HttpResponse]


def error_response(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def api_view(view: View) -> View:
    """Session-authenticated JSON endpoint: 401 for anonymous callers, BillingError -> JSON."""
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return error_response(401, 'not_authenticated', 'Log in to use this endpoint.')
        try:
            return view(request, *args, **kwargs)
        except billing.BillingError as exc:
            extra: dict[str, Any] = {'outcomeUnknown': exc.outcome_unknown}
            if exc.details:
                extra['details'] = exc.details
            if isinstance(exc, billing.OutcomeUnknown):
                extra['reference'] = exc.reference
            return error_response(exc.status_code, exc.code, exc.message, **extra)
    return wrapper


def _user(request: HttpRequest) -> Any:
    """The authenticated caller (``api_view`` has already rejected anonymous requests)."""
    return request.user


def _json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise billing.BillingError(400, 'invalid_json', 'Request body must be JSON.') from None
    if not isinstance(body, dict):
        raise billing.BillingError(400, 'invalid_json', 'Request body must be a JSON object.')
    return body


def _iso(value: Any) -> str | None:
    return value.astimezone(dt_timezone.utc).isoformat() if value is not None else None


def _price(cents: int | None, currency: str | None = None) -> dict[str, Any]:
    return {'amountInCents': cents, 'amount': billing.format_cents(cents), 'currency': currency or None}


def _plan_json(plan: billing.Plan, currency: str | None) -> dict[str, Any]:
    return {
        'planHandle': plan.handle,
        'name': plan.name,
        'description': plan.description,
        'price': _price(plan.price_in_cents, currency),
        'interval': plan.interval,
        'intervalUnit': plan.interval_unit,
        'trialPriceInCents': plan.trial_price_in_cents,
        'initialChargeInCents': plan.initial_charge_in_cents,
        'requiresPaymentMethod': plan.requires_payment_method,
    }


def _customer_json(customer: BillingCustomer | None) -> dict[str, Any] | None:
    if customer is None:
        return None
    return {
        'customerId': customer.maxio_customer_id,
        'reference': customer.reference,
        'outcome': customer.outcome,
        'createdAt': _iso(customer.provider_time),
    }


def _claim_json(claim: SubscriptionClaim) -> dict[str, Any]:
    return {
        'subscriptionId': claim.maxio_subscription_id,
        'reference': claim.reference,
        'outcome': claim.outcome,
        'state': claim.state or None,
        'planHandle': claim.plan_handle,
        'planName': claim.plan_name or None,
        'price': _price(claim.price_in_cents, claim.currency),
        'nextBillingAt': _iso(claim.next_billing_at),
        'updatedAt': _iso(claim.provider_time),
    }


def _subscription_json(subscription: Any, claim: SubscriptionClaim | None) -> dict[str, Any]:
    u = billing.unset_to_none
    product = u(subscription.product)
    state = u(subscription.state)
    interval_unit = u(product.interval_unit) if product is not None else None
    return {
        'subscriptionId': u(subscription.id),
        'reference': u(subscription.reference),
        'outcome': billing.status_from_provider(state),
        'state': str(state) if state is not None else None,
        'planHandle': u(product.handle) if product is not None else None,
        'planName': u(product.name) if product is not None else None,
        'price': _price(u(subscription.product_price_in_cents), u(subscription.currency)),
        'currentBillingAmount': _price(u(subscription.current_billing_amount_in_cents),
                                       u(subscription.currency)),
        'interval': u(product.interval) if product is not None else None,
        'intervalUnit': str(interval_unit) if interval_unit is not None else None,
        'nextBillingAt': _iso(u(subscription.next_assessment_at) or u(subscription.current_period_ends_at)),
        'currentPeriodEndsAt': _iso(u(subscription.current_period_ends_at)),
        'activatedAt': _iso(u(subscription.activated_at)),
        'createdAt': _iso(u(subscription.created_at)),
        'managedByThisSite': claim is not None,
    }


@require_GET
@api_view
def subscription_plans(request: HttpRequest) -> HttpResponse:
    plans = billing.list_plans()
    currency = billing.site_settings().currency
    return JsonResponse({
        'productFamily': product_family(),
        'plans': [_plan_json(p, currency) for p in plans],
    })


@transaction.non_atomic_requests
@require_http_methods(['GET', 'POST'])
@api_view
def billing_customer(request: HttpRequest) -> HttpResponse:
    if request.method == 'GET':
        customer = BillingCustomer.objects.filter(user=_user(request)).first()
        if customer is None:
            return error_response(404, 'no_billing_customer', 'No billing account yet.')
        return JsonResponse(_customer_json(customer) or {})
    customer, created = billing.ensure_customer(_user(request))
    return JsonResponse(_customer_json(customer) or {}, status=201 if created else 200)


# The HTTP status for each outcome of a subscribe. Success (201/200) comes only from "done".
SUBSCRIBE_STATUS: dict[str, int] = {
    Outcome.PENDING: 202,
    Outcome.UNKNOWN: 202,
    Outcome.SENDING: 202,
    Outcome.FAILED: 409,
    Outcome.NEEDS_REVIEW: 409,
}


@transaction.non_atomic_requests
@require_POST
@api_view
def subscriptions(request: HttpRequest) -> HttpResponse:
    body = _json_body(request)
    plan_handle = body.get('planHandle')
    if not isinstance(plan_handle, str) or not plan_handle:
        return error_response(400, 'plan_handle_required', 'planHandle is required.')

    result, plan, customer = billing.subscribe(_user(request), plan_handle)
    if result.claim is None:
        return JsonResponse({
            'subscriptionId': None, 'outcome': Outcome.SENDING, 'planHandle': plan.handle,
            'message': 'This subscription is already being created; repeat the request to check it.',
        }, status=202)

    claim: SubscriptionClaim = result.claim
    payload = _claim_json(claim)
    payload['customerId'] = customer.maxio_customer_id
    payload['interval'] = plan.interval
    payload['intervalUnit'] = plan.interval_unit
    payload['repeat'] = not result.created
    if claim.outcome == Outcome.DONE:
        status = 201 if result.created else 200
    else:
        status = SUBSCRIBE_STATUS.get(claim.outcome, 202)
        messages: dict[str, str] = {
            Outcome.PENDING: 'Maxio accepted the subscription but it is not active yet.',
            Outcome.UNKNOWN: 'Maxio has not confirmed this subscription yet.',
            Outcome.SENDING: 'This subscription is already being created; repeat the request to check it.',
            Outcome.FAILED: 'The subscription did not go through.',
            Outcome.NEEDS_REVIEW: 'The subscription needs review; please contact support.',
        }
        payload['message'] = messages.get(claim.outcome, 'Maxio has not confirmed this subscription yet.')
    return JsonResponse(payload, status=status)


@transaction.non_atomic_requests
@require_GET
@api_view
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    mine = billing.my_subscriptions(_user(request))
    entries = [
        _subscription_json(s, mine.claims_by_subscription.get(billing.unset_to_none(s.id) or 0))
        for s in mine.subscriptions
    ]
    return JsonResponse({
        'customer': _customer_json(mine.customer),
        'subscriptions': entries,
        'unconfirmed': [_claim_json(c) for c in mine.unsettled],
    })
