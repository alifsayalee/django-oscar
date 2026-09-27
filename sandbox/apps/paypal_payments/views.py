"""
JSON endpoints under /api/.

Callers authenticate with Django's session login (``/api/auth/login``) and
send the CSRF token back in ``X-CSRFToken`` on every unsafe request. Views
opt out of the sandbox's ATOMIC_REQUESTS: a PayPal write must see its claim
committed before the call and keep its record if the request later fails.
"""
from __future__ import annotations

import functools
import json
import logging
from typing import Any, Callable

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_post_parameters

from . import services
from .gateway import PaymentError

logger = logging.getLogger(__name__)

Result = tuple[int, 'dict[str, Any] | None']
Handler = Callable[..., Result]


def error_response(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({'error': {'code': code, 'message': message, **extra}}, status=status)


def api_view(methods: list[str], staff_only: bool = False,
             login_required: bool = True) -> Callable[[Handler], Callable[..., HttpResponse]]:
    """Method dispatch, JSON parsing, authentication, and the error boundary."""
    def decorator(func: Handler) -> Callable[..., HttpResponse]:
        @functools.wraps(func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if request.method not in methods:
                response = error_response(405, 'method_not_allowed', 'Use %s.' % ', '.join(methods))
                response['Allow'] = ', '.join(methods)
                return response
            if login_required and not request.user.is_authenticated:
                return error_response(401, 'not_authenticated', 'Log in first (POST /api/auth/login).')
            if staff_only and not getattr(request.user, 'is_staff', False):
                return error_response(403, 'forbidden', 'This action is restricted to staff operators.')
            data: Any = None
            if request.method in ('POST', 'PUT', 'PATCH') and request.body:
                try:
                    data = json.loads(request.body)
                except ValueError:
                    return error_response(400, 'invalid_json', 'The request body must be JSON.')
            try:
                result = func(request, data, *args, **kwargs)
            except PaymentError as exc:
                extra: dict[str, Any] = {}
                if exc.outcome_unknown:
                    extra['outcomeUnknown'] = True
                if exc.issues:
                    extra['paypalIssues'] = exc.issues
                return error_response(exc.status_code, exc.code, exc.message, **extra)
            except ImproperlyConfigured as exc:
                logger.error('PayPal is not configured: %s', exc)
                return error_response(503, 'paypal_not_configured', 'Payments are not configured on this site.')
            status, body = result
            if body is None:
                return HttpResponse(status=status)
            return JsonResponse(body, status=status)
        return transaction.non_atomic_requests(wrapper)
    return decorator


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@api_view(['GET'], login_required=False)
def csrf(request: HttpRequest, data: Any) -> Result:
    return 200, {'csrfToken': get_token(request)}


@sensitive_post_parameters()
@api_view(['POST'], login_required=False)
def login_view(request: HttpRequest, data: Any) -> Result:
    data = data or {}
    identifier = data.get('username') or data.get('email')
    password = data.get('password')
    if not isinstance(identifier, str) or not isinstance(password, str):
        return 400, {'error': {'code': 'invalid_request', 'message': 'Send "username" (or "email") and "password".'}}
    user = authenticate(request, username=identifier, password=password)
    if user is None:
        return 401, {'error': {'code': 'invalid_credentials', 'message': 'Invalid credentials.'}}
    login(request, user)
    return 200, {'userId': user.pk, 'username': user.get_username(), 'isStaff': getattr(user, 'is_staff', False),
                 'csrfToken': get_token(request)}


@sensitive_post_parameters()
@api_view(['POST'], login_required=False)
def register_view(request: HttpRequest, data: Any) -> Result:
    data = data or {}
    email, password = data.get('email'), data.get('password')
    if not isinstance(email, str) or '@' not in email or not isinstance(password, str):
        return 400, {'error': {'code': 'invalid_request', 'message': 'Send "email" and "password".'}}
    email = email.strip().lower()
    User = get_user_model()
    if User.objects.filter(email__iexact=email).exists():
        return 409, {'error': {'code': 'email_taken', 'message': 'That email is already registered.'}}
    candidate = User(username=email[:150], email=email)
    try:
        validate_password(password, candidate)
    except ValidationError as exc:
        return 400, {'error': {'code': 'weak_password', 'message': ' '.join(exc.messages)}}
    candidate.set_password(password)
    try:
        with transaction.atomic():
            candidate.save()
    except IntegrityError:
        return 409, {'error': {'code': 'email_taken', 'message': 'That email is already registered.'}}
    user = authenticate(request, username=email, password=password)
    if user is None:
        return 500, {'error': {'code': 'login_failed', 'message': 'Registered, but could not log in.'}}
    login(request, user)
    return 201, {'userId': user.pk, 'username': user.get_username(), 'isStaff': getattr(user, 'is_staff', False),
                 'csrfToken': get_token(request)}


@api_view(['POST'], login_required=False)
def logout_view(request: HttpRequest, data: Any) -> Result:
    logout(request)
    return 204, None


# ---------------------------------------------------------------------------
# Orders and payments
# ---------------------------------------------------------------------------

@api_view(['POST'])
def orders(request: HttpRequest, data: Any) -> Result:
    return services.place_order(request, data, request.headers.get('Idempotency-Key'))


@sensitive_post_parameters()
@api_view(['POST'])
def pay(request: HttpRequest, data: Any, order_id: str) -> Result:
    return services.pay(request.user, order_id, data)


@api_view(['POST'], staff_only=True)
def fulfil(request: HttpRequest, data: Any, order_id: str) -> Result:
    return services.fulfil(order_id)


@api_view(['POST'], staff_only=True)
def cancel(request: HttpRequest, data: Any, order_id: str) -> Result:
    return services.cancel(order_id)


@api_view(['POST'])
def refunds(request: HttpRequest, data: Any, order_id: str) -> Result:
    return services.refund(request.user, order_id, data, request.headers.get('Idempotency-Key'))


@api_view(['GET'])
def my_orders(request: HttpRequest, data: Any) -> Result:
    return services.my_orders(request.user)


@api_view(['GET'], staff_only=True)
def reconciliation(request: HttpRequest, data: Any) -> Result:
    bounds = []
    for name in ('from', 'to'):
        raw = request.GET.get(name, '')
        # A '+' offset arrives as a space when the caller did not URL-encode it.
        value = parse_datetime(raw.replace(' ', '+')) if raw else None
        if value is None or value.tzinfo is None:
            return 400, {'error': {'code': 'invalid_request', 'message':
                                   '"%s" must be an ISO-8601 date-time with a timezone, '
                                   'e.g. 2026-09-01T00:00:00Z.' % name}}
        bounds.append(value)
    return services.reconciliation(bounds[0], bounds[1])


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

@sensitive_post_parameters()
@api_view(['GET', 'POST'])
def payment_methods(request: HttpRequest, data: Any) -> Result:
    if request.method == 'GET':
        return services.list_cards(request.user)
    return services.save_card(request.user, data, request.headers.get('Idempotency-Key'))


@api_view(['DELETE'])
def payment_method(request: HttpRequest, data: Any, payment_method_id: str) -> Result:
    return services.delete_card(request.user, payment_method_id)
