"""
JSON endpoints for subscription billing, authenticated by the sandbox's
Django session login.

The write endpoints are ``non_atomic_requests``: the claim row each write
takes must be committed *before* the Maxio call, so that a concurrent request
sees it and an error after the call cannot roll it back.
"""
import json
import logging
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, cast

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from . import service
from .errors import InvalidRequest, ProviderError, provider_call
from .models import Outcome

logger = logging.getLogger('apps.subscriptions')

if TYPE_CHECKING:
    from django.contrib.auth.models import User

View = Callable[..., HttpResponse]


def api_view(methods: list[str], *, login_required: bool = True) -> Callable[[View], View]:
    def decorator(view: View) -> View:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if login_required and not request.user.is_authenticated:
                return JsonResponse(
                    {'error': 'not_authenticated', 'message': 'Log in first.'}, status=401)
            try:
                with provider_call():   # any SDK failure a write re-raised as-is
                    return view(request, *args, **kwargs)
            except ProviderError as e:
                if e.status_code >= 500:
                    logger.warning('subscriptions API %s %s -> %s %s', request.method,
                                   request.path, e.status_code, e.code, exc_info=e.__cause__)
                return JsonResponse(e.as_dict(), status=e.status_code)
        return transaction.non_atomic_requests(require_http_methods(methods)(wrapper))
    return decorator


def _user(request: HttpRequest) -> 'User':
    # api_view has already rejected anonymous callers.
    return cast('User', request.user)


def _json_body(request: HttpRequest) -> dict[str, Any]:
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        raise InvalidRequest(400, 'invalid_json', 'The request body must be JSON.') from None
    if not isinstance(body, dict):
        raise InvalidRequest(400, 'invalid_json', 'The request body must be a JSON object.')
    return body


def _session_payload(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    return {
        'authenticated': user.is_authenticated,
        'user': ({'id': user.pk, 'email': getattr(user, 'email', None)}
                 if user.is_authenticated else None),
        'csrfToken': get_token(request),
    }


@ensure_csrf_cookie
@api_view(['GET', 'POST', 'DELETE'], login_required=False)
def session(request: HttpRequest) -> HttpResponse:
    """GET: who am I + CSRF token. POST: Django session login. DELETE: logout."""
    if request.method == 'POST':
        body = _json_body(request)
        user = authenticate(request, username=body.get('username') or body.get('email'),
                            password=body.get('password'))
        if user is None:
            return JsonResponse(
                {'error': 'invalid_credentials', 'message': 'Invalid credentials.'}, status=400)
        login(request, user)
    elif request.method == 'DELETE':
        logout(request)
    return JsonResponse(_session_payload(request))


@api_view(['GET'])
def subscription_plans(request: HttpRequest) -> HttpResponse:
    return JsonResponse({'plans': [plan.as_dict() for plan in service.list_plans()]})


@api_view(['POST'])
def maxio_customer(request: HttpRequest) -> HttpResponse:
    """Ensure (idempotently) the caller's Maxio customer."""
    result = service.ensure_customer(_user(request))
    record = result.record
    body = {'customerId': record.maxio_customer_id, 'reference': record.reference,
            'outcome': 'in_progress' if result.in_flight else record.outcome}
    return JsonResponse(body, status=200 if record.outcome == Outcome.DONE else 202)


@api_view(['POST'])
def subscriptions(request: HttpRequest) -> HttpResponse:
    body = _json_body(request)
    plan_handle = body.get('planHandle')
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        raise InvalidRequest(400, 'plan_handle_required', 'Send {"planHandle": "<handle>"}.')

    result = service.subscribe(_user(request), plan_handle.strip())
    customer = {'customerId': result.customer.maxio_customer_id}
    if result.enrollment is None:
        return JsonResponse({'subscriptionId': None, 'outcome': 'in_progress',
                             'customer': customer}, status=202)

    payload = service.enrollment_as_dict(result.enrollment)
    payload['customer'] = customer
    if result.in_flight:
        payload['outcome'] = 'in_progress'
        return JsonResponse(payload, status=202)
    outcome = result.enrollment.outcome
    if outcome == Outcome.DONE:            # the only path to "subscribed"
        return JsonResponse(payload, status=201)
    if outcome == Outcome.FAILED:
        payload['error'] = 'subscription_failed'
        return JsonResponse(payload, status=409)
    return JsonResponse(payload, status=202)   # pending, needs_review, unknown


@api_view(['GET'])
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    return JsonResponse(service.my_subscriptions(_user(request)))


@api_view(['GET'])
def subscription_detail(request: HttpRequest, subscription_id: int) -> HttpResponse:
    return JsonResponse(service.get_subscription(_user(request), subscription_id))
