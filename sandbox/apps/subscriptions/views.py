import json
import logging
from functools import wraps
from typing import Any, Callable

import httpx
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET, require_POST
from maxio_advanced_billing.core import ApiError

from . import maxio, services
from .errors import ProviderError, translate
from .models import BillingClaim
from .safe_write import AmountMismatch, OutcomeUnknown

logger = logging.getLogger(__name__)

View = Callable[..., HttpResponse]

# The HTTP status each outcome is answered with. Only "done" is a success;
# anything not listed (unknown) may have happened and answers 504.
OUTCOME_STATUS = {
    BillingClaim.DONE: 200,
    BillingClaim.PENDING: 202,
    BillingClaim.SENDING: 202,
    BillingClaim.FAILED: 409,
    BillingClaim.NEEDS_REVIEW: 409,
}

OUTCOME_MESSAGES = {
    BillingClaim.DONE: 'Subscribed.',
    BillingClaim.PENDING: 'Accepted by the billing provider, not active yet.',
    BillingClaim.SENDING: 'This request is already in progress.',
    BillingClaim.FAILED: 'The billing provider did not subscribe you.',
    BillingClaim.NEEDS_REVIEW: 'The subscription was created, but not as requested; it needs review.',
    BillingClaim.UNKNOWN: 'The billing provider has not confirmed the outcome yet. Repeat the request to check.',
}


def error_response(http_status: int, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({'error': message, **extra}, status=http_status)


def answer(claim: BillingClaim) -> JsonResponse:
    """The one place a write's outcome becomes the caller's answer."""
    outcome = claim.outcome
    body: dict[str, Any] = {
        'status': outcome,
        'message': OUTCOME_MESSAGES.get(outcome, OUTCOME_MESSAGES[BillingClaim.UNKNOWN]),
        'subscriptionId': None,
        'reference': claim.reference,
        'outcomeUnknown': outcome not in OUTCOME_STATUS,
    }
    if claim.kind == BillingClaim.SUBSCRIPTION:
        body.update(claim.snapshot)
        body['status'] = outcome  # the write's outcome, not the listing's
        if body['subscriptionId'] is None and claim.provider_id:
            body['subscriptionId'] = int(claim.provider_id)
    else:
        # The request stopped at step 1: the shopper's Maxio customer.
        body['step'] = 'customer'
    return JsonResponse(body, status=OUTCOME_STATUS.get(outcome, 504))


def api_login_required(view: View) -> View:
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return error_response(401, 'Authentication required: sign in with your site account first.')
        return view(request, *args, **kwargs)
    return wrapper


def provider_boundary(view: View) -> View:
    """Turns every billing-provider failure into a JSON answer."""
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            return view(request, *args, **kwargs)
        except OutcomeUnknown as e:
            return error_response(
                504, OUTCOME_MESSAGES[BillingClaim.UNKNOWN], status=BillingClaim.UNKNOWN,
                outcomeUnknown=True, reference=e.claim.reference, subscriptionId=None)
        except AmountMismatch as e:
            return answer(e.claim)
        except ImproperlyConfigured:
            logger.exception('Maxio billing is not configured')
            return error_response(503, 'Subscription billing is not configured on this site.')
        except (ProviderError, ApiError, httpx.RequestError, ValueError) as e:
            error = translate(e)
            return error_response(error.status_code, error.message, details=error.details,
                                  outcomeUnknown=error.outcome_unknown)
    return wrapper


@require_GET
@provider_boundary
def subscription_plans(request: HttpRequest) -> HttpResponse:
    plans = services.list_plans(maxio.get_client())
    return JsonResponse([plan.as_json() for plan in plans], safe=False)


# Each claim step commits on its own: the request-wide transaction
# (ATOMIC_REQUESTS) would roll a claim back with the response that reports it.
@transaction.non_atomic_requests
@require_POST
@api_login_required
@provider_boundary
def create_subscription(request: HttpRequest) -> HttpResponse:
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return error_response(400, 'The request body must be JSON.')
    plan_handle = payload.get('planHandle') if isinstance(payload, dict) else None
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return error_response(400, 'planHandle is required.')
    user = request.user
    if not getattr(user, 'email', ''):
        return error_response(400, 'Your account needs an email address before you can subscribe.')

    client = maxio.get_client()
    plan = services.find_plan(client, plan_handle.strip())
    if plan is None:
        return error_response(404, 'There is no plan %r.' % plan_handle)
    claim = services.subscribe(
        client, user, plan, idempotency_key=request.headers.get('Idempotency-Key', '').strip())
    return answer(claim)


@require_GET
@api_login_required
@provider_boundary
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    return JsonResponse(services.my_subscriptions(maxio.get_client(), request.user))
