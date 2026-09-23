"""JSON HTTP endpoints for the PayPal checkout API.

Callers authenticate with Django's own session login (the way the sandbox already
authenticates them); the caller's identity is taken from ``request.user``.
Operator actions (fulfil, cancel, reconciliation) require ``is_staff``; every
other endpoint is shopper-scoped and acts only on the caller's own data.

The views are CSRF-exempt so the JSON API is drivable by a programmatic client
holding a session cookie; they still require an authenticated user.
"""
import json
from functools import wraps

from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import serializers, services
from .exceptions import (
    ApiClientError,
    ChallengeRequired,
    ProviderConfigError,
    ProviderError,
    ProviderRejected,
    ProviderUnavailable,
    ProviderUnreadable,
)


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise ApiClientError("Request body must be valid JSON.")
    if not isinstance(data, dict):
        raise ApiClientError("Request body must be a JSON object.")
    return data


def api(*, staff=False):
    """Wrap a view with session auth, optional staff gate, and error translation."""
    def decorator(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({"error": "Authentication required."}, status=401)
            if staff and not request.user.is_staff:
                return JsonResponse({"error": "Staff privileges required."}, status=403)
            try:
                return view(request, *args, **kwargs)
            except ApiClientError as exc:
                return JsonResponse({"error": exc.message}, status=exc.status_code)
            except ChallengeRequired as exc:
                return JsonResponse(
                    {"error": exc.message, "code": "challenge_required"},
                    status=exc.status_code)
            except ProviderRejected as exc:
                return JsonResponse(
                    {"error": exc.message, "providerStatus": exc.provider_status,
                     "debugId": exc.debug_id},
                    status=exc.status_code)
            except (ProviderUnavailable, ProviderUnreadable) as exc:
                return JsonResponse(
                    {"error": exc.message, "outcomeUnknown": exc.outcome_unknown},
                    status=exc.status_code)
            except (ProviderConfigError, ProviderError) as exc:
                return JsonResponse({"error": exc.message}, status=exc.status_code)
        return wrapped
    return decorator


# -- Flow 1: orders --------------------------------------------------------

@api()
@require_http_methods(["POST"])
def create_order(request):
    body = _json_body(request)
    order, payment = services.create_order(request.user, body.get("items"))
    data = serializers.order_dict(order, payment)
    return JsonResponse(data, status=201)


@api()
@require_http_methods(["POST"])
def pay_order(request, order_id):
    body = _json_body(request)
    payment = services.pay_order(
        request.user, order_id,
        card=body.get("card"),
        payment_method_id=body.get("paymentMethodId"),
    )
    return JsonResponse(serializers.order_dict(payment.order, payment), status=200)


@api(staff=True)
@require_http_methods(["POST"])
def fulfil_order(request, order_id):
    payment = services.fulfil_order(order_id)
    return JsonResponse(serializers.order_dict(payment.order, payment), status=200)


@api(staff=True)
@require_http_methods(["POST"])
def cancel_order(request, order_id):
    payment = services.cancel_order(order_id)
    return JsonResponse(serializers.order_dict(payment.order, payment), status=200)


@api()
@require_http_methods(["POST"])
def create_refund(request, order_id):
    body = _json_body(request)
    payment, refund = services.refund_order(
        request.user, order_id,
        amount=body.get("amount"),
        idempotency_key=body.get("idempotencyKey"),
    )
    data = serializers.refund_dict(refund)
    data["refundId"] = refund.refund_id or f"local-{refund.id}"
    data["order"] = serializers.order_dict(payment.order, payment)
    return JsonResponse(data, status=201)


@api()
@require_http_methods(["GET"])
def my_orders(request):
    orders = services.list_orders(request.user)
    return JsonResponse(
        {"orders": [serializers.order_dict(order) for order in orders]}, status=200)


@api(staff=True)
@require_http_methods(["GET"])
def reconciliation(request):
    from_raw = request.GET.get("from")
    to_raw = request.GET.get("to")
    if not from_raw or not to_raw:
        raise ApiClientError("`from` and `to` query parameters are required.")
    from_dt = _parse_instant(from_raw, "from")
    to_dt = _parse_instant(to_raw, "to")
    report = services.reconcile(from_dt, to_dt)
    return JsonResponse(report, status=200)


def _parse_instant(raw, name):
    dt = parse_datetime(raw)
    if dt is None:
        raise ApiClientError(f"`{name}` must be an ISO-8601 date-time.")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return dt


# -- Flow 2: saved cards ---------------------------------------------------

@api()
@require_http_methods(["GET", "POST"])
def payment_methods(request):
    if request.method == "POST":
        body = _json_body(request)
        saved = services.save_card(request.user, body.get("card"))
        data = serializers.saved_card_dict(saved)
        return JsonResponse(data, status=201)
    cards = services.list_saved_cards(request.user)
    return JsonResponse(
        {"paymentMethods": [serializers.saved_card_dict(c) for c in cards]}, status=200)


@api()
@require_http_methods(["DELETE"])
def delete_payment_method(request, payment_method_id):
    services.delete_saved_card(request.user, payment_method_id)
    return JsonResponse({"deleted": True, "paymentMethodId": payment_method_id}, status=200)
