"""HTTP endpoints for Maxio subscription billing.

Plain Django JSON views, session-authenticated the way the sandbox authenticates
every other page (``request.user``). Each capability is a separate, separately
invocable endpoint:

- ``GET  /api/subscription-plans``  -> the available plans (each carries ``planHandle``)
- ``POST /api/subscriptions``       -> subscribe; ``subscriptionId`` is top-level
- ``GET  /api/my-subscriptions``    -> the caller's subscriptions

State-changing POSTs keep Django's CSRF protection (no exemption); a session caller
sends the ``csrftoken`` cookie's value in the ``X-CSRFToken`` header.
"""

from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

from . import services
from .errors import MaxioServiceError


def _json(data: Any, status: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status, json_dumps_params={"indent": 2})


_View = Callable[..., HttpResponse]


def _api_endpoint(*, methods: tuple[str, ...]) -> Callable[[_View], _View]:
    """Wrap a view with session auth, method routing, and error translation.

    Unauthenticated callers get a 401 JSON body (not a login redirect), so the API
    is drivable on its own. Every :class:`MaxioServiceError` maps to its own HTTP
    status; ``outcome_unknown`` is surfaced so a caller can tell "definitely failed"
    from "may have taken effect".
    """

    def decorator(view: _View) -> _View:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if not request.user.is_authenticated:
                return _json({"error": "authentication required"}, status=401)
            if request.method not in methods:
                return _json(
                    {"error": f"method {request.method} not allowed"}, status=405
                )
            try:
                return view(request, *args, **kwargs)
            except MaxioServiceError as exc:
                payload: dict[str, Any] = {"error": exc.message}
                if exc.outcome_unknown:
                    payload["outcomeUnknown"] = True
                return _json(payload, status=exc.status_code)

        return wrapper

    return decorator


@_api_endpoint(methods=("GET",))
def subscription_plans(request: HttpRequest) -> HttpResponse:
    """List the subscription plans available to subscribe to."""
    return _json({"plans": services.list_plans()})


@_api_endpoint(methods=("POST",))
def subscriptions(request: HttpRequest) -> HttpResponse:
    """Subscribe the caller to a plan. Returns ``subscriptionId`` at the top level."""
    plan_handle = None
    if request.body:
        try:
            data = json.loads(request.body)
        except ValueError:
            return _json({"error": "request body is not valid JSON"}, status=400)
        if not isinstance(data, dict):
            return _json({"error": "request body must be a JSON object"}, status=400)
        plan_handle = data.get("planHandle") or data.get("plan_handle")

    subscription, created = services.subscribe(request.user, plan_handle)
    return _json(
        {
            "subscriptionId": subscription["subscriptionId"],
            "created": created,
            "subscription": subscription,
        },
        status=201 if created else 200,
    )


@_api_endpoint(methods=("GET",))
def my_subscriptions(request: HttpRequest) -> HttpResponse:
    """List the caller's subscriptions, read back from Maxio."""
    return _json({"subscriptions": services.list_my_subscriptions(request.user)})
