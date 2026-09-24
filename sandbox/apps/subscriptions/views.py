"""
JSON endpoints for subscription billing.

Callers authenticate with the sandbox's Django session login; identity is
always the authenticated request user. POST keeps Django's CSRF protection:
send the ``csrftoken`` cookie value back as the ``X-CSRFToken`` header.
"""
import json
import logging
from functools import wraps

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import billing
from .errors import BillingError, BillingNotConfigured
from .models import MaxioWriteClaim

logger = logging.getLogger('apps.subscriptions.maxio')


def _error(status, code, message, **extra):
    return JsonResponse(dict({'error': code, 'message': message}, **extra), status=status)


def billing_endpoint(view):
    """
    Converts billing failures into JSON answers and keeps the view out of the
    per-request transaction (ATOMIC_REQUESTS): a claim must be committed
    before Maxio is called so a racing request sees it.
    """
    @transaction.non_atomic_requests
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ImproperlyConfigured as exc:
            logger.error('Maxio billing is not configured: %s', exc)
            err = BillingNotConfigured()
            return _error(err.status_code, err.code, err.message)
        except BillingError as err:
            extra = {'outcomeUnknown': err.outcome_unknown}
            if err.details:
                extra['details'] = err.details
            if request.method == 'POST':
                # The response contract carries subscriptionId on every POST answer.
                extra['subscriptionId'] = None
            return _error(err.status_code, err.code, err.message, **extra)
    return wrapper


def login_required_json(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error(401, 'authentication_required', 'Sign in to use this endpoint.')
        return view(request, *args, **kwargs)
    return wrapper


@require_GET
@billing_endpoint
def subscription_plans(request):
    plans = billing.list_plans()
    return JsonResponse({'plans': [plan.as_json() for plan in plans]})


def _subscription_body(record, plan, replayed):
    return {
        'subscriptionId': int(record.provider_id) if record.provider_id else None,
        'outcome': record.outcome,
        'replayed': replayed,
        'reference': record.reference,
        'planHandle': plan.handle,
        'planName': record.plan_name or plan.name,
        'state': record.provider_state or None,
        'priceInCents': record.price_in_cents,
        'price': billing.format_price(record.price_in_cents),
        'currency': record.currency or None,
        'nextBillingAt': record.next_billing_at.isoformat() if record.next_billing_at else None,
    }


# What each recorded outcome answers. Success (201/200) comes only from DONE.
_NOT_DONE_STATUS = {
    MaxioWriteClaim.SENDING: 202,
    MaxioWriteClaim.PENDING: 202,
    MaxioWriteClaim.UNKNOWN: 202,
    MaxioWriteClaim.FAILED: 409,
    MaxioWriteClaim.NEEDS_REVIEW: 502,
}


@require_POST
@billing_endpoint
@login_required_json
def create_subscription(request):
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return _error(400, 'invalid_json', 'Request body must be JSON.', subscriptionId=None)
    plan_handle = payload.get('planHandle') if isinstance(payload, dict) else None
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return _error(400, 'plan_handle_required', 'planHandle is required.', subscriptionId=None)

    result = billing.subscribe(request.user, plan_handle.strip())

    if result.subscription is None:
        # The customer step has not completed; nothing was subscribed yet.
        customer = result.customer.record
        outcome = 'in_progress' if customer.outcome == MaxioWriteClaim.SENDING else customer.outcome
        return JsonResponse({
            'subscriptionId': None,
            'outcome': outcome,
            'step': 'customer',
            'message': 'Your billing account is still being set up; retry the same request shortly.',
        }, status=_NOT_DONE_STATUS.get(customer.outcome, 502))

    record = result.subscription.record
    body = _subscription_body(record, result.plan, result.subscription.replayed)
    body['customerId'] = int(result.customer.record.provider_id)
    if record.outcome == MaxioWriteClaim.DONE:
        return JsonResponse(body, status=200 if result.subscription.replayed else 201)
    if record.outcome == MaxioWriteClaim.SENDING:
        body['outcome'] = 'in_progress'
    return JsonResponse(body, status=_NOT_DONE_STATUS.get(record.outcome, 502))


@require_GET
@billing_endpoint
@login_required_json
def my_subscriptions(request):
    return JsonResponse(billing.my_subscriptions(request.user))
