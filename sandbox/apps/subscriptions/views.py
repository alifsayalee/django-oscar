"""
JSON API for subscription billing, authenticated with Django's session login.

The views opt out of ATOMIC_REQUESTS: a claim must be committed *before* the call to Maxio and must
survive whatever happens after it, so the billing module runs its own short transactions.
"""
import json
import logging
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from . import billing
from .models import MaxioClaim

logger = logging.getLogger(__name__)


def error_response(status: int, code: str, message: str, **details: Any) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message, **details}}, status=status)


def parse_json(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise billing.BillingError(400, "invalid_json", "The request body is not valid JSON.") from None
    if not isinstance(data, dict):
        raise billing.BillingError(400, "invalid_json", "The request body must be a JSON object.")
    return data


@method_decorator([never_cache, transaction.non_atomic_requests], name="dispatch")
class ApiView(View):
    """Base: JSON errors, session-authenticated by default."""

    login_required = True

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if self.login_required and not request.user.is_authenticated:
            return error_response(401, "not_authenticated", "Sign in first (POST /api/session).")
        try:
            return super().dispatch(request, *args, **kwargs)
        except billing.BillingError as e:
            if e.status_code >= 500:
                logger.warning("Billing error %s (%s) for user %s", e.code, e.status_code, request.user.pk)
            return error_response(e.status_code, e.code, e.message, outcomeUnknown=e.outcome_unknown, **e.details)

    def http_method_not_allowed(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        response = error_response(405, "method_not_allowed", "Method not allowed.")
        response["Allow"] = ", ".join(m.upper() for m in self.http_method_names if hasattr(self, m))
        return response


def _user_dict(user: Any) -> dict[str, Any]:
    return {"id": user.pk, "username": user.get_username(), "email": user.email}


@method_decorator(ensure_csrf_cookie, name="dispatch")
class SessionView(ApiView):
    """
    GET    -> who is signed in, and a CSRF cookie for the POSTs that follow
    POST   -> sign in with {"username" or "email", "password"} (Django session login, sandbox backends)
    DELETE -> sign out
    """

    login_required = False

    def get(self, request: HttpRequest) -> HttpResponse:
        user = request.user
        return JsonResponse({
            "authenticated": user.is_authenticated,
            "user": _user_dict(user) if user.is_authenticated else None,
            "csrfToken": get_token(request),
        })

    def post(self, request: HttpRequest) -> HttpResponse:
        data = parse_json(request)
        identifier = data.get("username") or data.get("email")
        password = data.get("password")
        if not isinstance(identifier, str) or not isinstance(password, str):
            return error_response(400, "invalid_credentials_payload", "Send username (or email) and password.")
        # Oscar's EmailBackend takes the email as "username"; ModelBackend takes the username.
        user = authenticate(request, username=identifier, password=password)
        if user is None:
            return error_response(401, "invalid_credentials", "Invalid username/email or password.")
        login(request, user)
        return JsonResponse({"authenticated": True, "user": _user_dict(user), "csrfToken": get_token(request)})

    def delete(self, request: HttpRequest) -> HttpResponse:
        logout(request)
        return HttpResponse(status=204)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class SubscriptionPlansView(ApiView):
    def get(self, request: HttpRequest) -> HttpResponse:
        plans = billing.list_plans()
        return JsonResponse({"plans": [billing.plan_to_dict(p) for p in plans]})


class BillingCustomerView(ApiView):
    """POST -> ensure the signed-in user has a Maxio customer (idempotent). GET -> the recorded link."""

    def get(self, request: HttpRequest) -> HttpResponse:
        claim = billing.customer_claim(request.user)
        if claim is None:
            return error_response(404, "no_billing_customer", "No billing customer yet.")
        return JsonResponse({"customerId": claim.provider_id, "reference": claim.reference})

    def post(self, request: HttpRequest) -> HttpResponse:
        claim = billing.ensure_customer(request.user)
        return JsonResponse({"customerId": claim.provider_id, "reference": claim.reference})


class SubscriptionsView(ApiView):
    """
    POST {"planHandle": "..."} -> subscribe the signed-in user.

    Optional ``Idempotency-Key`` header: the same key is the same request; a new key is a deliberate
    additional subscription. Without one, a user holds at most one live subscription per plan.

    201 created and in effect · 200 repeat of a request that is in effect · 202 accepted but not in
    effect yet (pending / in progress / unknown) · 422 rejected or ended · 504 outcome unknown.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        data = parse_json(request)
        plan_handle = data.get("planHandle")
        if not isinstance(plan_handle, str) or not plan_handle.strip():
            return error_response(400, "plan_handle_required", "planHandle is required.")
        key = request.headers.get("Idempotency-Key")
        result = billing.subscribe(request.user, plan_handle.strip(), key)
        claim = result.claim

        body: dict[str, Any] = {
            "subscriptionId": claim.provider_id,
            "reference": claim.reference,
            "outcome": claim.outcome,
            "repeat": not result.created,
        }
        if result.subscription is not None:
            body["subscription"] = billing.subscription_to_dict(result.subscription)

        if claim.outcome == MaxioClaim.DONE:          # the ONLY path to success
            return JsonResponse(body, status=201 if result.created else 200)
        if claim.outcome == MaxioClaim.FAILED:
            return JsonResponse({**body, "error": {"code": "subscription_failed",
                                                   "message": "The subscription is not in effect."}}, status=422)
        if claim.outcome == MaxioClaim.NEEDS_REVIEW:
            return JsonResponse({**body, "error": {"code": "needs_review",
                                                   "message": "This subscription request needs review."}},
                                status=502)
        # pending, unknown, and a request still in flight: accepted, not in effect yet.
        return JsonResponse(body, status=202)


class MySubscriptionsView(ApiView):
    def get(self, request: HttpRequest) -> HttpResponse:
        subscriptions = billing.list_my_subscriptions(request.user)
        return JsonResponse({
            "subscriptions": [billing.subscription_to_dict(s) for s in subscriptions],
            "unsettledRequests": [billing.claim_to_dict(c) for c in billing.unsettled_requests(request.user)],
        })
