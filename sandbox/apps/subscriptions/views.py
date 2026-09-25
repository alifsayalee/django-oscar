"""
JSON API for subscription billing. Callers authenticate with the sandbox's
normal Django session login; identity is always ``request.user``.

The views are non-atomic on purpose: a write's claim row must be committed
before Maxio is called (the sandbox runs with ATOMIC_REQUESTS).
"""
import functools
import json
import logging
from collections.abc import Callable
from typing import Any

from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_http_methods

from . import services
from .errors import BillingError, InvalidRequest
from .models import Outcome

logger = logging.getLogger('apps.subscriptions')

View = Callable[..., JsonResponse]


def _error(exc: BillingError) -> JsonResponse:
    body: dict[str, Any] = {'code': exc.code, 'message': exc.message}
    if exc.details:
        body['details'] = exc.details
    if exc.outcome_unknown:
        body['outcomeUnknown'] = True
    reference = getattr(exc, 'reference', None)
    if reference:
        body['reference'] = reference
    return JsonResponse({'error': body}, status=exc.status_code)


def api_view(*methods: str) -> Callable[[View], View]:
    """Session-authenticated JSON endpoint with the app's error boundary."""
    def decorator(view: View) -> View:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
            if not request.user.is_authenticated:
                return JsonResponse(
                    {'error': {'code': 'not_authenticated', 'message': 'Log in to use this endpoint.'}},
                    status=401)
            try:
                return view(request, *args, **kwargs)
            except BillingError as exc:
                return _error(exc)
        guarded: View = require_http_methods(list(methods))(wrapper)
        non_atomic: View = transaction.non_atomic_requests(guarded)
        return non_atomic
    return decorator


def _json_body(request: HttpRequest) -> dict[str, Any]:
    if request.content_type == 'application/json':
        try:
            data: object = json.loads(request.body or b'{}')
        except ValueError:
            raise InvalidRequest('Request body is not valid JSON.') from None
        if not isinstance(data, dict):
            raise InvalidRequest('Request body must be a JSON object.')
        return data
    form: dict[str, Any] = request.POST.dict()
    return form


@api_view('GET')
def subscription_plans(request: HttpRequest) -> JsonResponse:
    return JsonResponse({'plans': services.list_plans()})


@api_view('POST')
def billing_customer(request: HttpRequest) -> JsonResponse:
    record, created = services.ensure_customer(request.user)
    return JsonResponse(services.customer_dict(record), status=201 if created else 200)


@api_view('POST')
def subscriptions(request: HttpRequest) -> JsonResponse:
    plan_handle = _json_body(request).get('planHandle')
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        raise InvalidRequest('planHandle is required.')
    body, outcome, created = services.subscribe(request.user, plan_handle.strip())
    if outcome == Outcome.DONE:
        status = 201 if created else 200
    elif outcome == Outcome.FAILED:
        status = 422
        body['message'] = 'Maxio reports this subscription as not in effect.'
    else:
        status = 202          # pending, unknown or still being sent: not confirmed yet
    return JsonResponse(body, status=status)


@api_view('GET')
def subscription_detail(request: HttpRequest, subscription_id: int) -> JsonResponse:
    return JsonResponse(services.get_subscription(request.user, subscription_id))


@api_view('GET')
def my_subscriptions(request: HttpRequest) -> JsonResponse:
    return JsonResponse(services.my_subscriptions(request.user))
