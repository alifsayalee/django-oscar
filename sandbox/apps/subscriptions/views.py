"""HTTP endpoints for the Maxio subscription capability.

All routes authenticate the way the sandbox already does — Django's session login — and take the
caller's identity from ``request.user``.  Responses are JSON.  Each capability is its own route so
every action stays separately invocable.

CSRF note: the POST route is ``csrf_exempt`` because it is a JSON API endpoint driven by a client
(curl / a SPA sending the session cookie), not an HTML form.  Authentication is still enforced —
an anonymous caller gets 401.
"""

import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import services
from .exceptions import ProviderError

log = logging.getLogger(__name__)


def _require_login(request):
    """Return a 401 JsonResponse if the caller is not authenticated, else ``None``."""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "authentication_required", "detail": "Log in to use this endpoint."},
            status=401,
        )
    return None


def _provider_error_response(e):
    """Map a translated ``ProviderError`` to a JSON response with its boundary status."""
    log.warning(
        "Maxio provider error: %s (status=%s outcome_unknown=%s)",
        e, e.status_code, e.outcome_unknown,
    )
    body = {"error": "provider_error", "detail": e.detail or str(e)}
    if e.outcome_unknown:
        # The write may have taken effect at Maxio; the caller must reconcile before retrying.
        body["outcomeUnknown"] = True
    return JsonResponse(body, status=e.status_code)


@require_GET
def subscription_plans(request):
    """GET /api/subscription-plans — list available plans, each carrying its ``planHandle``."""
    unauth = _require_login(request)
    if unauth:
        return unauth
    try:
        plans = services.list_plans()
    except ProviderError as e:
        return _provider_error_response(e)
    return JsonResponse({"plans": plans}, status=200)


@csrf_exempt
@require_POST
def create_subscription(request):
    """POST /api/subscriptions — subscribe the authenticated user to a plan.

    Body (JSON, optional): ``{"planHandle": "eshop-pro"}``.  Defaults to ``eshop-pro``.
    Returns ``subscriptionId`` as a top-level field.
    """
    unauth = _require_login(request)
    if unauth:
        return unauth

    plan_handle = None
    if request.body:
        try:
            payload = json.loads(request.body)
        except (ValueError, TypeError):
            return JsonResponse(
                {"error": "invalid_json", "detail": "Request body must be valid JSON."},
                status=400,
            )
        if not isinstance(payload, dict):
            return JsonResponse(
                {"error": "invalid_body", "detail": "Request body must be a JSON object."},
                status=400,
            )
        plan_handle = payload.get("planHandle") or payload.get("plan_handle")

    try:
        claim, subscription = services.subscribe(request.user, plan_handle)
    except ProviderError as e:
        return _provider_error_response(e)

    if subscription is None:
        # Claimed by a concurrent request and not yet confirmed — accepted, not done.
        return JsonResponse(
            {
                "subscriptionId": claim.subscription_id,
                "status": claim.status,
                "reference": claim.reference,
                "detail": "Subscription request is in progress.",
            },
            status=202,
        )

    detail = services.serialize_subscription(subscription, claim=claim)
    status_code = 201 if claim.status in ("done", "pending") else 200
    return JsonResponse(
        {"subscriptionId": detail["subscriptionId"], "subscription": detail},
        status=status_code,
    )


@require_GET
def my_subscriptions(request):
    """GET /api/my-subscriptions — list the authenticated user's subscriptions."""
    unauth = _require_login(request)
    if unauth:
        return unauth
    try:
        subs = services.list_my_subscriptions(request.user)
    except ProviderError as e:
        return _provider_error_response(e)
    return JsonResponse({"subscriptions": subs}, status=200)
