"""
JSON API for subscription billing.

Callers authenticate with the sandbox's own Django session login; the caller's identity is always
``request.user``. Unsafe methods need the CSRF token (``X-CSRFToken`` header), as for any other
session-authenticated request on the site.
"""

import json
import logging
from functools import wraps

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import maxio, services

logger = logging.getLogger(__name__)


def _error(status_code, message, details=None, **extra):
    body = {'error': message}
    if details:
        body['details'] = details
    body.update(extra)
    return JsonResponse(body, status=status_code)


def api_view(view):
    """Session auth as JSON (401, not a login redirect), no caching, and one error boundary.

    Views run outside ATOMIC_REQUESTS so the record of a Maxio write is committed before the
    write is sent, and survives if the request fails afterwards.
    """
    @never_cache
    @transaction.non_atomic_requests
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error(401, 'Authentication required.')
        try:
            return view(request, *args, **kwargs)
        except services.RequestInvalid as exc:
            return _error(exc.status_code, exc.message, exc.details)
        except services.OutcomeUnknown as exc:
            return _error(
                exc.status_code, exc.message, subscriptionId=None, reference=exc.reference,
                status=maxio.UNKNOWN, outcomeUnknown=True)
        except maxio.ProviderError as exc:
            return _error(
                exc.status_code, exc.message, exc.details if isinstance(exc, maxio.ProviderRejected) else None,
                outcomeUnknown=exc.outcome_unknown)
    return wrapper


def _subscription_status_code(subscription, created):
    """201/200 only when the provider says the subscription is in effect; else 202 (accepted)."""
    if maxio.status_from_provider(subscription.state) != maxio.DONE:
        return 202
    return 201 if created else 200


def _attempt_payload(attempt):
    return {
        'reference': attempt.reference,
        'planHandle': attempt.plan_handle,
        'status': attempt.status,
        'subscriptionId': attempt.maxio_subscription_id,
        'requestedAt': attempt.date_created.isoformat(),
    }


@require_GET
@api_view
def subscription_plans(request):
    return JsonResponse({'plans': services.list_plans()})


@require_POST
@api_view
def billing_customer(request):
    customer = services.ensure_customer(request.user)
    return JsonResponse({
        'customerId': customer.maxio_customer_id,
        'reference': customer.reference,
        'status': customer.status,
    })


@require_POST
@api_view
def subscriptions(request):
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return _error(400, 'The request body must be JSON.')
    plan_handle = payload.get('planHandle') if isinstance(payload, dict) else None
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return _error(400, 'planHandle is required.')
    result = services.subscribe(request.user, plan_handle.strip())
    body = maxio.subscription_payload(result.subscription)
    body['created'] = result.created
    return JsonResponse(body, status=_subscription_status_code(result.subscription, result.created))


@require_GET
@api_view
def subscription_detail(request, subscription_id):
    subscription = services.get_subscription(request.user, subscription_id)
    if subscription is None:
        return _error(404, 'Subscription not found.')
    return JsonResponse(maxio.subscription_payload(subscription))


@require_GET
@api_view
def my_subscriptions(request):
    subscriptions_, unresolved = services.list_my_subscriptions(request.user)
    return JsonResponse({
        'subscriptions': [maxio.subscription_payload(s) for s in subscriptions_],
        'unresolvedRequests': [_attempt_payload(a) for a in unresolved],
    })
