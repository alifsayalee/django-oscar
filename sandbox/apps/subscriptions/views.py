"""
JSON API for subscription billing. Callers authenticate with the sandbox's
normal Django session login; POSTs need the CSRF token like any other form.
"""
import json
import logging
from functools import wraps

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .maxio import MaxioError

logger = logging.getLogger('apps.subscriptions')


def _error(status, message, **extra):
    return JsonResponse(dict(error=message, **extra), status=status)


def api_view(view):
    """Session auth (401 JSON instead of a login redirect) and one error boundary.

    Views opt out of ATOMIC_REQUESTS: no database transaction is held open
    across a Maxio round trip; the few writes commit on their own.
    """
    @transaction.non_atomic_requests
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error(401, 'Authentication required. Log in to the site first.')
        try:
            return view(request, *args, **kwargs)
        except services.InvalidRequest as e:
            return _error(e.status_code, e.message)
        except services.SubscriptionConflict:
            return _error(409, 'A subscription to this plan is already being created. Retry shortly.')
        except MaxioError as e:
            return _error(e.status_code, e.message, outcomeUnknown=e.outcome_unknown)
    return wrapper


@require_GET
@ensure_csrf_cookie
@api_view
def subscription_plans(request):
    return JsonResponse({
        'productFamily': services.family_handle(),
        'plans': services.list_plans(),
    })


@require_http_methods(['GET', 'POST'])
@ensure_csrf_cookie
@api_view
def billing_customer(request):
    if request.method == 'POST':
        customer, created = services.ensure_customer(request.user)
        return JsonResponse({'customer': services.customer_to_dict(customer), 'created': created},
                            status=201 if created else 200)
    customer = services.find_customer(request.user)
    if customer is None:
        return _error(404, 'No billing customer exists for this account yet.')
    return JsonResponse({'customer': services.customer_to_dict(customer)})


def _plan_handle_from(request):
    if request.content_type == 'application/json':
        try:
            payload = json.loads(request.body or b'{}')
        except ValueError:
            raise services.InvalidRequest(400, 'Request body is not valid JSON.')
        if not isinstance(payload, dict):
            raise services.InvalidRequest(400, 'Request body must be a JSON object.')
        handle = payload.get('planHandle')
    else:
        handle = request.POST.get('planHandle')
    if not isinstance(handle, str) or not handle.strip():
        raise services.InvalidRequest(400, '"planHandle" is required.')
    return handle.strip()


@require_POST
@api_view
def subscriptions(request):
    result = services.subscribe(request.user, _plan_handle_from(request))
    data = services.subscription_to_dict(result.subscription)
    return JsonResponse({
        'subscriptionId': data['subscriptionId'],
        'created': result.created,
        'subscription': data,
    }, status=201 if result.created else 200)


@require_GET
@ensure_csrf_cookie
@api_view
def my_subscriptions(request):
    return JsonResponse({'subscriptions': services.list_my_subscriptions(request.user)})


@require_GET
@api_view
def subscription_detail(request, subscription_id):
    return JsonResponse({'subscription': services.get_my_subscription(request.user, subscription_id)})
