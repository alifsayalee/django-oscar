"""
JSON API for subscriptions. Callers authenticate with the sandbox's own session
login; the acting user is always ``request.user``.

These views make network calls to Maxio, so they opt out of ATOMIC_REQUESTS:
no database transaction is held open across a provider call.
"""
import functools
import json
import logging
from decimal import Decimal

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import services
from .exceptions import BillingError

logger = logging.getLogger(__name__)


def _error(status, code, message, details=(), outcome_unknown=False):
    response = JsonResponse({'error': {
        'code': code,
        'message': message,
        'details': list(details),
        'outcomeUnknown': outcome_unknown,
    }}, status=status)
    if status in (409, 503):
        response['Retry-After'] = '5'
    return response


def api_view(view):
    """Session-authenticated JSON endpoint; BillingErrors become JSON errors."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error(401, 'not_authenticated', "Log in to use the subscription API.")
        try:
            return view(request, *args, **kwargs)
        except BillingError as exc:
            return _error(exc.status_code, exc.code, exc.message, exc.details, exc.outcome_unknown)
    return transaction.non_atomic_requests(wrapper)


def _amount(cents):
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal('0.01')))


def _iso(value):
    return value.isoformat() if value is not None else None


def _plan_data(plan):
    return {
        'planHandle': plan.handle,
        'name': plan.name,
        'description': plan.description,
        'priceInCents': plan.price_in_cents,
        'price': _amount(plan.price_in_cents),
        'interval': plan.interval,
        'intervalUnit': plan.interval_unit,
    }


def _subscription_data(subscription):
    return {
        'subscriptionId': subscription.id,
        'reference': subscription.reference,
        'state': subscription.state,
        'planHandle': subscription.plan_handle,
        'planName': subscription.plan_name,
        'priceInCents': subscription.price_in_cents,
        'price': _amount(subscription.price_in_cents),
        'currency': subscription.currency,
        'interval': subscription.interval,
        'intervalUnit': subscription.interval_unit,
        'nextBillingAt': _iso(subscription.next_billing_at),
        'currentPeriodEndsAt': _iso(subscription.current_period_ends_at),
        'createdAt': _iso(subscription.created_at),
    }


@require_GET
@api_view
def subscription_plans(request):
    return JsonResponse({'plans': [_plan_data(plan) for plan in services.list_plans()]})


@require_POST
@api_view
def billing_customer(request):
    customer, created = services.ensure_customer(request.user)
    return JsonResponse({
        'customerId': customer.id,
        'reference': customer.reference,
        'created': created,
    }, status=201 if created else 200)


@require_POST
@api_view
def subscriptions(request):
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return _error(400, 'invalid_json', "The request body must be a JSON object.")
    plan_handle = payload.get('planHandle') if isinstance(payload, dict) else None
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return _error(400, 'invalid_request', "planHandle is required.")

    subscription, created = services.subscribe(request.user, plan_handle.strip())
    data = _subscription_data(subscription)
    data['created'] = created
    return JsonResponse(data, status=201 if created else 200)


@require_GET
@api_view
def my_subscriptions(request):
    return JsonResponse({
        'subscriptions': [_subscription_data(s) for s in services.my_subscriptions(request.user)],
    })


@require_GET
@api_view
def my_subscription(request, subscription_id):
    return JsonResponse(_subscription_data(services.my_subscription(request.user, subscription_id)))
