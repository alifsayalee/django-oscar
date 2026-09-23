"""
JSON API for subscription billing.

Callers authenticate with the sandbox's own session login; state-changing
requests need the CSRF token (``csrftoken`` cookie, sent back as ``X-CSRFToken``).
"""
import json
import logging
from decimal import Decimal
from functools import wraps

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from . import services
from .gateway import BillingError, SubscriptionView
from .models import BillingCustomer, SubscriptionEnrollment

logger = logging.getLogger('apps.maxio_billing')

# Our statuses -> HTTP status for a subscribe that produced this outcome
_CREATED_STATUS = {
    SubscriptionEnrollment.ACTIVE: 201,
    SubscriptionEnrollment.FAILED: 502,     # Maxio answered, but the signup failed
}


def _error(status, code, message, **extra):
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def api_view(view):
    """Session auth (401 JSON, not a login redirect) and one boundary for billing failures."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error(401, 'not_authenticated', 'Log in to use the billing API.')
        return _handle_failures(view, request, *args, **kwargs)
    # Claims must commit before the provider call, not with the request.
    return transaction.non_atomic_requests(wrapper)


def _handle_failures(view, request, *args, **kwargs):
    try:
        return view(request, *args, **kwargs)
    except services.InProgress as e:
        return _error(409, 'in_progress',
                      f'A {e.what} request for this account is already being processed; retry shortly.',
                      reference=e.reference or None)
    except services.OutcomeUnknown as e:
        return _error(504, 'outcome_unknown',
                      f'The billing provider did not confirm the {e.what}. Repeat the same request to '
                      'reconcile; it will not create a duplicate.', reference=e.reference)
    except services.CustomerDetailsMissing as e:
        return _error(400, 'customer_details_missing', str(e))
    except BillingError as e:
        code = 'provider_rejected' if e.status_code in (400, 404, 409, 422) else 'provider_unavailable'
        return _error(e.status_code, code, e.message, details=e.details)
    except ImproperlyConfigured:
        logger.exception('Maxio billing misconfigured')
        return _error(503, 'billing_not_configured', 'Subscription billing is not configured.')


def _json_body(request):
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _money(cents):
    return None if cents is None else str((Decimal(cents) / 100).quantize(Decimal('0.01')))


def _iso(value):
    return value.isoformat() if value is not None else None


def _plan_json(plan):
    return {
        'planHandle': plan.handle,
        'name': plan.name,
        'description': plan.description,
        'priceInCents': plan.price_in_cents,
        'price': _money(plan.price_in_cents),
        'interval': plan.interval,
        'intervalUnit': plan.interval_unit,
    }


def _subscription_json(view: SubscriptionView, row=None):
    return {
        'subscriptionId': view.id,
        'reference': view.reference,
        'status': row.status if row is not None else view.status,
        'state': view.state,
        'planHandle': view.plan_handle,
        'planName': view.plan_name,
        'priceInCents': view.price_in_cents,
        'price': _money(view.price_in_cents),
        'currency': view.currency,
        'interval': view.interval,
        'intervalUnit': view.interval_unit,
        'nextBillingAt': _iso(view.next_billing_at),
        'currentPeriodEndsAt': _iso(view.current_period_ends_at),
        'activatedAt': _iso(view.activated_at),
        'createdAt': _iso(view.created_at),
        'canceledAt': _iso(view.canceled_at),
        'paymentCollectionMethod': view.payment_collection_method,
    }


def _enrollment_json(row: SubscriptionEnrollment):
    """A request we hold locally, from our last snapshot of Maxio (or no answer yet)."""
    return {
        'subscriptionId': row.maxio_subscription_id,
        'reference': row.reference,
        'status': row.status,
        'state': row.state or None,
        'planHandle': row.plan_handle,
        'planName': row.plan_name or None,
        'priceInCents': row.price_in_cents,
        'price': _money(row.price_in_cents),
        'currency': row.currency or None,
        'nextBillingAt': _iso(row.next_billing_at),
        'requestedAt': _iso(row.created),
        'failureReason': row.failure_reason or None,
    }


def _customer_json(row):
    if row is None:
        return {'status': 'none', 'maxioCustomerId': None, 'reference': None}
    return {'status': row.status, 'maxioCustomerId': row.maxio_customer_id, 'reference': row.reference}


# ---------------------------------------------------------------------------

@require_GET
@ensure_csrf_cookie
@transaction.non_atomic_requests
def subscription_plans(request):
    """Plans of the configured product family. Public catalogue data."""
    try:
        catalog = services.list_plans()
    except BillingError as e:
        return _error(e.status_code, 'provider_unavailable', e.message)
    except ImproperlyConfigured:
        logger.exception('Maxio billing misconfigured')
        return _error(503, 'billing_not_configured', 'Subscription billing is not configured.')
    return JsonResponse({
        'plans': [_plan_json(plan) for plan in catalog.plans],
        'truncated': catalog.truncated,
    })


@require_http_methods(['GET', 'POST'])
@ensure_csrf_cookie
@api_view
def billing_customer(request):
    """GET: the caller's Maxio customer link. POST: ensure it exists (idempotent)."""
    if request.method == 'GET':
        return JsonResponse({'customer': _customer_json(services.get_customer(request.user))})
    body = _json_body(request)
    if body is None:
        return _error(400, 'invalid_json', 'Request body must be a JSON object.')
    already = BillingCustomer.objects.filter(user=request.user, status=BillingCustomer.LINKED).exists()
    row = services.ensure_customer(
        request.user, first_name=str(body.get('firstName') or ''), last_name=str(body.get('lastName') or ''))
    return JsonResponse({'customer': _customer_json(row)}, status=200 if already else 201)


@require_http_methods(['POST'])
@api_view
def subscriptions(request):
    """Subscribe the caller to ``planHandle``. Repeating the request never subscribes twice."""
    body = _json_body(request)
    if body is None:
        return _error(400, 'invalid_json', 'Request body must be a JSON object.')
    plan_handle = body.get('planHandle')
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return _error(400, 'plan_handle_required', 'planHandle is required.')
    try:
        result = services.subscribe(
            request.user, plan_handle.strip(),
            first_name=str(body.get('firstName') or ''), last_name=str(body.get('lastName') or ''))
    except services.UnknownPlan:
        return _error(422, 'unknown_plan', f'No subscription plan with handle {plan_handle!r}.')

    row = result.enrollment
    payload = (_subscription_json(result.subscription, row) if result.subscription is not None
               else _enrollment_json(row))
    payload['subscriptionId'] = row.maxio_subscription_id
    payload['status'] = row.status
    payload['existing'] = result.existing
    if result.existing:
        http_status = 200
    else:
        # Success only when Maxio says the subscription is live; anything else is "accepted".
        http_status = _CREATED_STATUS.get(row.status, 202)
    return JsonResponse(payload, status=http_status)


@require_GET
@api_view
def subscription_detail(request, subscription_id):
    found = services.read_subscription(request.user, subscription_id)
    if found is None:
        return _error(404, 'not_found', 'No such subscription on your account.')
    view, row = found
    return JsonResponse(_subscription_json(view, row))


@require_GET
@ensure_csrf_cookie
@api_view
def my_subscriptions(request):
    """The caller's subscriptions as Maxio has them, plus requests still being reconciled."""
    result = services.my_subscriptions(request.user)
    return JsonResponse({
        'customer': _customer_json(services.get_customer(request.user)),
        'subscriptions': [_subscription_json(view, row) for view, row in result.subscriptions],
        'unsettledRequests': [_enrollment_json(row) for row in result.unsettled],
    })
