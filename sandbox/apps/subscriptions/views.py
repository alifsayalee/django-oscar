"""
JSON API for recurring subscriptions.

Callers authenticate with Django's own session login (``/api/session`` or
the storefront login page) and send the CSRF token on unsafe methods, like
every other form on the site. The caller's identity is always
``request.user``.

Views are ``non_atomic_requests``: a claim must be committed before Maxio is
called, not at the end of the request.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from . import services
from .errors import BillingError, OutcomeUnknown
from .models import Outcome

logger = logging.getLogger('apps.subscriptions')

View = Callable[..., HttpResponse]


def error_response(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def api_view(view: View) -> View:
    """Session auth required; BillingError and misconfiguration become JSON errors."""
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return error_response(401, 'not_authenticated', 'Log in first (POST /api/session).')
        try:
            return view(request, *args, **kwargs)
        except OutcomeUnknown as e:
            return error_response(e.status_code, e.code, e.message, outcomeUnknown=True,
                                  reference=e.reference, subscriptionId=None)
        except BillingError as e:
            return error_response(e.status_code, e.code, e.message, outcomeUnknown=e.outcome_unknown,
                                  details=e.details)
        except ImproperlyConfigured as e:
            logger.error('Maxio integration is not configured: %s', e)
            return error_response(503, 'billing_not_configured', 'Subscription billing is not configured.')
    return transaction.non_atomic_requests(wrapper)


def _json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise BillingError(400, 'invalid_json', 'The request body must be JSON.') from None
    if not isinstance(data, dict):
        raise BillingError(400, 'invalid_json', 'The request body must be a JSON object.')
    return data


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def _user_json(request: HttpRequest) -> dict[str, Any] | None:
    user = request.user
    if not user.is_authenticated:
        return None
    return {'id': user.pk, 'username': user.get_username(), 'email': getattr(user, 'email', '')}


@ensure_csrf_cookie
@require_http_methods(['GET', 'POST', 'DELETE'])
def session(request: HttpRequest) -> HttpResponse:
    """GET: CSRF token + current user. POST: log in. DELETE: log out."""
    if request.method == 'POST':
        try:
            data = _json_body(request)
        except BillingError as e:
            return error_response(e.status_code, e.code, e.message)
        identifier = str(data.get('username') or data.get('email') or '')
        password = str(data.get('password') or '')
        # Oscar's EmailBackend takes the email as the username
        user = authenticate(request, username=identifier, password=password)
        if user is None:
            return error_response(400, 'invalid_credentials', 'Invalid username/email or password.')
        login(request, user)
    elif request.method == 'DELETE':
        logout(request)
    return JsonResponse({'user': _user_json(request), 'csrfToken': get_token(request)})


# ---------------------------------------------------------------------------
# Plans, customer, subscriptions
# ---------------------------------------------------------------------------

@require_GET
@api_view
def subscription_plans(request: HttpRequest) -> HttpResponse:
    return JsonResponse({'plans': [plan.as_json() for plan in services.list_plans()]})


@require_http_methods(['GET', 'POST'])
@api_view
def billing_customer(request: HttpRequest) -> HttpResponse:
    """GET: the caller's Maxio customer link. POST: make sure it exists (idempotent)."""
    if request.method == 'GET':
        return JsonResponse(services.customer_json(services.get_customer(request.user)))
    existed = services.get_customer(request.user)
    already_done = existed is not None and existed.outcome == Outcome.DONE
    row = services.ensure_customer(request.user)
    return JsonResponse(services.customer_json(row), status=200 if already_done else 201)


@require_http_methods(['POST'])
@api_view
def subscriptions(request: HttpRequest) -> HttpResponse:
    """
    Subscribe the caller to a plan: ``{"planHandle": "..."}``.

    An optional ``Idempotency-Key`` header makes retries explicit; without it
    a repeat while a subscription to that plan is live answers from it.
    """
    data = _json_body(request)
    plan_handle = data.get('planHandle')
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        return error_response(400, 'plan_handle_required', 'planHandle is required.')
    idempotency_key = request.headers.get('Idempotency-Key', '').strip()[:255]

    enrollment, created = services.subscribe(request.user, plan_handle.strip(), idempotency_key)
    body = services.enrollment_json(enrollment)
    if enrollment.outcome == Outcome.DONE:
        return JsonResponse(body, status=201 if created else 200)
    if enrollment.outcome == Outcome.PENDING:
        return JsonResponse(body, status=202)
    if enrollment.outcome == Outcome.NEEDS_REVIEW:
        return JsonResponse({**body, 'error': {
            'code': 'subscription_needs_review',
            'message': 'Maxio created a subscription that differs from the request; support will review it.',
        }}, status=409)
    if enrollment.outcome == Outcome.FAILED:
        return JsonResponse({**body, 'error': {
            'code': 'subscription_not_active',
            'message': f'The subscription is not active (state: {enrollment.provider_state or "unknown"}).',
        }}, status=422)
    # sending / unknown: not confirmed either way
    return JsonResponse({**body, 'error': {
        'code': 'billing_outcome_unknown',
        'message': 'Maxio did not confirm the outcome yet; it is being reconciled.',
    }}, status=504)


@require_GET
@api_view
def subscription_detail(request: HttpRequest, subscription_id: int) -> HttpResponse:
    subscription = services.get_subscription(request.user, subscription_id)
    return JsonResponse(services.subscription_json(subscription))


@require_GET
@api_view
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    subscriptions, unresolved = services.my_subscriptions(request.user)
    return JsonResponse({
        'customer': services.customer_json(services.get_customer(request.user)),
        'subscriptions': [services.subscription_json(s) for s in subscriptions],
        'unconfirmed': [services.enrollment_json(row) for row in unresolved],
    })
