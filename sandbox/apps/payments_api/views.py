"""JSON API views. Callers authenticate with Django's session login; identity is ``request.user``."""

import functools
import json
import logging

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import serializers, services
from .paypal_gateway.client import PayPalConfigurationError
from .paypal_gateway.errors import AmountMismatch, OutcomeUnknown, ProviderError
from .services import ApiProblem, Order

log = logging.getLogger("apps.payments_api")


def _error(status, code, message, **extra):
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status)


def api_view(methods, *, staff=False, login_required=True):
    """Session-authenticated JSON endpoint.

    - Opts out of ATOMIC_REQUESTS: a PayPal claim must be committed before the call to PayPal.
    - CSRF: a state-changing call must be ``Content-Type: application/json``, which a cross-site form
      cannot send without a CORS preflight (and this site allows no cross-origin requests).
    """

    def decorator(view):
        @csrf_exempt
        @transaction.non_atomic_requests
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return _error(405, "method_not_allowed", f"Use {' or '.join(methods)}.")
            if login_required and not request.user.is_authenticated:
                return _error(401, "not_authenticated", "Sign in first (POST /api/session).")
            if staff and not request.user.is_staff:
                return _error(403, "forbidden", "This is an operator action.")
            data = {}
            if request.method in ("POST", "PUT", "PATCH"):
                if request.content_type != "application/json":
                    return _error(415, "unsupported_media_type", "Send Content-Type: application/json.")
                try:
                    data = json.loads(request.body or b"{}")
                except ValueError:
                    return _error(400, "invalid_json", "The body is not valid JSON.")
                if not isinstance(data, dict):
                    return _error(400, "invalid_json", "The body must be a JSON object.")
            try:
                return view(request, data, *args, **kwargs)
            except ApiProblem as exc:
                return _error(exc.status, exc.code, exc.message, **exc.extra)
            except ProviderError as exc:
                extra: dict[str, object] = {"outcomeUnknown": exc.outcome_unknown}
                if exc.issues:
                    extra["paypalIssues"] = list(exc.issues)
                if exc.debug_id:
                    extra["paypalDebugId"] = exc.debug_id
                return _error(exc.status_code, exc.code, exc.message, **extra)
            except OutcomeUnknown:
                return _error(504, "outcome_unknown",
                              "PayPal may have processed this; it could not be confirmed. Repeat the same request "
                              "to settle it.", outcomeUnknown=True)
            except AmountMismatch as exc:
                return _error(409, "needs_review", "PayPal processed a different amount than requested; an operator "
                                                   "must review it.", paypalAmount=exc.amount, paypalCurrency=exc.currency)
            except PayPalConfigurationError as exc:
                log.error("PayPal is not configured: %s", exc)
                return _error(503, "paypal_not_configured", "Payments are not configured on this site.")
            except Exception:
                # Always JSON, never the debug page (which would echo request details).
                log.exception("unhandled error in %s", view.__name__)
                return _error(500, "internal_error", "Something went wrong on our side.")

        return wrapper

    return decorator


def _order_response(outcome):
    return JsonResponse(serializers.order_json(outcome.obj) | outcome.extra, status=outcome.http_status)


# --- session ------------------------------------------------------------------------------------------

@api_view(["POST", "DELETE", "GET"], login_required=False)
def session(request, data):
    if request.method == "GET":
        if not request.user.is_authenticated:
            return _error(401, "not_authenticated", "Not signed in.")
        return JsonResponse({"username": request.user.get_username(), "isStaff": request.user.is_staff})
    if request.method == "DELETE":
        logout(request)
        return HttpResponse(status=204)
    username, password = data.get("username"), data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return _error(400, "invalid_request", "username and password are required.")
    user = authenticate(request, username=username, password=password)
    if user is None:
        return _error(401, "invalid_credentials", "Wrong username or password.")
    login(request, user)
    return JsonResponse({"username": user.get_username(), "isStaff": user.is_staff})


# --- orders -------------------------------------------------------------------------------------------

@api_view(["POST"])
def orders(request, data):
    items, shipping = serializers.parse_order_request(data)
    order = services.place_order(request.user, items, shipping, request=request)
    return JsonResponse(serializers.order_json(order), status=201)


@api_view(["POST"])
def pay(request, data, order_id):
    card, payment_method_id = serializers.parse_pay_request(data)
    return _order_response(services.pay(request.user, order_id, card=card, payment_method_id=payment_method_id))


@api_view(["POST"], staff=True)
def fulfil(request, data, order_id):
    return _order_response(services.fulfil(order_id))


@api_view(["POST"], staff=True)
def cancel(request, data, order_id):
    return _order_response(services.cancel(order_id))


@api_view(["POST"])
def refunds(request, data, order_id):
    key, amount, note = serializers.parse_refund_request(
        data, request.headers.get("Idempotency-Key"), services.currency())
    outcome = services.refund(request.user, order_id, key, amount=amount, note=note)
    return JsonResponse(serializers.refund_json(outcome.obj) | outcome.extra, status=outcome.http_status)


@api_view(["GET"])
def my_orders(request, data):
    qs = (Order.objects.filter(user=request.user)
          .select_related("paypal_payment")
          .prefetch_related("lines", "paypal_payment__refunds")
          .order_by("-date_placed"))
    return JsonResponse({"orders": [serializers.order_json(o) for o in qs]})


# --- saved cards --------------------------------------------------------------------------------------

@api_view(["GET", "POST"])
def payment_methods(request, data):
    if request.method == "GET":
        return JsonResponse({"paymentMethods": [serializers.card_json(c) for c in services.list_cards(request.user)]})
    card = serializers.parse_card(data.get("card"))
    outcome = services.save_card(request.user, card)
    return JsonResponse(serializers.card_json(outcome.obj), status=outcome.http_status)


@api_view(["DELETE"])
def payment_method(request, data, payment_method_id):
    services.delete_card(request.user, payment_method_id)
    return HttpResponse(status=204)


# --- reconciliation -----------------------------------------------------------------------------------

@api_view(["GET"], staff=True)
def reconciliation(request, data):
    start = serializers.parse_iso(request.GET.get("from", ""), "from")
    end = serializers.parse_iso(request.GET.get("to", ""), "to")
    if start >= end:
        raise ApiProblem(400, "invalid_request", "from must be before to.")
    return JsonResponse(services.reconcile(start, end))
