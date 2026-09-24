"""
JSON API for subscription billing, authenticated by Django's session login.

Views are deliberately *not* wrapped in the request transaction
(``ATOMIC_REQUESTS``): a write's claim must be committed before Maxio is
called and must survive an error raised afterwards.
"""

import json
import logging
from functools import wraps
from typing import Any, Callable

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from . import services
from .errors import BillingError
from .maxio import BillingNotConfigured
from .models import MaxioWrite

logger = logging.getLogger("apps.subscriptions")

View = Callable[..., HttpResponse]


def _error(status: int, code: str, message: str) -> JsonResponse:
    return JsonResponse({"error": code, "message": message, "outcomeUnknown": False}, status=status)


def api_view(login_required: bool = False) -> Callable[[View], View]:
    def decorator(view: View) -> View:
        @wraps(view)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if login_required and not request.user.is_authenticated:
                return _error(401, "not_authenticated", "Log in first (POST /api/session).")
            try:
                return view(request, *args, **kwargs)
            except BillingNotConfigured:
                logger.error("Maxio billing is not configured")
                return _error(503, "billing_not_configured", "Subscription billing is not available.")
            except BillingError as exc:
                return JsonResponse(exc.as_dict(), status=exc.status_code)

        return transaction.non_atomic_requests(wrapped)

    return decorator


def _json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise BillingError(400, "invalid_json", "Request body must be a JSON object.") from None
    if not isinstance(data, dict):
        raise BillingError(400, "invalid_json", "Request body must be a JSON object.")
    return data


def _me(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {"authenticated": False, "user": None}
    return {
        "authenticated": True,
        "user": {"id": user.pk, "username": user.get_username(), "email": getattr(user, "email", "")},
    }


# --- session -----------------------------------------------------------------------

@ensure_csrf_cookie
@require_http_methods(["GET", "POST", "DELETE"])
@api_view()
def session(request: HttpRequest) -> HttpResponse:
    """
    GET    - who is logged in; sets the ``csrftoken`` cookie (send it back as ``X-CSRFToken``).
    POST   - log in: ``{"username": ..., "password": ...}`` (an email works as username).
    DELETE - log out.
    """
    if request.method == "POST":
        data = _json_body(request)
        username = data.get("username") or data.get("email")
        password = data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise BillingError(400, "credentials_required", "username and password are required.")
        user = authenticate(request, username=username, password=password)
        if user is None or not user.is_active:
            raise BillingError(401, "invalid_credentials", "Invalid username or password.")
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
    return JsonResponse({**_me(request), "csrfToken": get_token(request)})


# --- plans ---------------------------------------------------------------------------

@require_GET
@api_view()
def subscription_plans(request: HttpRequest) -> HttpResponse:
    return JsonResponse({"plans": services.list_plans()})


# --- subscriptions -------------------------------------------------------------------

SUBSCRIBE_STATUS = {
    MaxioWrite.DONE: 201,  # the only success
    MaxioWrite.PENDING: 202,  # accepted, not in effect yet
    MaxioWrite.UNKNOWN: 202,
    MaxioWrite.FAILED: 409,
    MaxioWrite.NEEDS_REVIEW: 409,
}


@require_http_methods(["POST"])
@api_view(login_required=True)
def subscriptions(request: HttpRequest) -> HttpResponse:
    """Subscribe the logged-in user to ``{"planHandle": ...}``. Safe to repeat."""
    plan_handle = _json_body(request).get("planHandle")
    if not isinstance(plan_handle, str) or not plan_handle.strip():
        raise BillingError(400, "plan_handle_required", "planHandle is required.")
    result = services.subscribe(request.user, plan_handle.strip())  # type: ignore[arg-type]
    record = result.record
    body: dict[str, Any] = {
        **(result.subscription or {}),
        "subscriptionId": record.provider_id,
        "reference": record.reference,
        "outcome": record.outcome,
        "state": (result.subscription or {}).get("state") or record.provider_state,
        "planHandle": (result.subscription or {}).get("planHandle") or record.plan_handle,
    }
    if record.outcome == MaxioWrite.FAILED:
        body["error"] = "subscription_not_active"
        body["message"] = "The subscription was created but is not active."
    elif record.outcome == MaxioWrite.NEEDS_REVIEW:
        body["error"] = "needs_review"
        body["message"] = "The billing provider created a subscription that differs from the request."
    return JsonResponse(body, status=SUBSCRIBE_STATUS.get(record.outcome, 202))


@require_GET
@api_view(login_required=True)
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    return JsonResponse(services.my_subscriptions(request.user))  # type: ignore[arg-type]


@require_GET
@api_view(login_required=True)
def subscription_detail(request: HttpRequest, subscription_id: int) -> HttpResponse:
    return JsonResponse(services.get_my_subscription(request.user, subscription_id))  # type: ignore[arg-type]
