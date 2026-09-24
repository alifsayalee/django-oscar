"""
JSON API views.

Callers authenticate with Django's session login (``POST /api/login``, or the
storefront's own login) and send the CSRF token on every unsafe request, as
any session-authenticated Django client does. Fulfil, cancel and
reconciliation are restricted to staff; everything else acts only on the
caller's own orders and cards.

Views run outside ATOMIC_REQUESTS: a claim must be committed before PayPal is
called, so each service manages its own transactions.
"""
import functools
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from oscar.core.loading import get_model
from paypal.core import ApiError

from . import cards, payments, presenters
from .claims import AmountMismatch, OutcomeUnknown
from .errors import ApiProblem, bad_request
from .gateway import translate
from .models import PaymentOperation, PayPalPayment
from .orders import place_order
from .reconciliation import MAX_RANGE, reconcile

log = logging.getLogger(__name__)

Order = get_model("order", "Order")

View = Callable[..., HttpResponse]


def _problem_response(problem: ApiProblem) -> JsonResponse:
    return JsonResponse(problem.as_dict(), status=problem.status_code)


def api(methods: tuple[str, ...], *, auth: str = "user") -> Callable[[View], View]:
    """JSON endpoint: method check, authentication, and one error boundary."""

    def decorate(view: View) -> View:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if request.method not in methods:
                return JsonResponse(
                    {"error": "method_not_allowed", "message": "Use %s." % " or ".join(methods)}, status=405
                )
            if auth in ("user", "staff") and not request.user.is_authenticated:
                return JsonResponse({"error": "not_authenticated", "message": "Log in first."}, status=401)
            if auth == "staff" and not request.user.is_staff:
                return JsonResponse({"error": "forbidden", "message": "Staff only."}, status=403)
            write = request.method != "GET"
            try:
                return view(request, *args, **kwargs)
            except ApiProblem as problem:
                return _problem_response(problem)
            except OutcomeUnknown as exc:
                return _problem_response(ApiProblem(
                    504, "outcome_unknown",
                    "PayPal has not confirmed the outcome yet. Nothing more was sent; repeat the same request "
                    "to check again - it will not charge twice.",
                    outcome_unknown=True, extra={"reference": exc.op.reference},
                ))
            except AmountMismatch as exc:
                return _problem_response(ApiProblem(
                    502, "amount_mismatch",
                    "PayPal reported a different amount than was requested; the payment is held for review.",
                    extra={"reference": exc.op.reference},
                ))
            except (ApiError, httpx.HTTPError, ImproperlyConfigured) as exc:
                return _problem_response(translate(exc, write=write))
            except ValueError as exc:  # an unreadable PayPal body (pydantic ValidationError, non-JSON)
                log.exception("Unreadable PayPal response in %s", request.path)
                return _problem_response(translate(exc, write=write))

        return transaction.non_atomic_requests(wrapper)

    return decorate


def _json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise bad_request("The request body must be JSON.") from None
    if not isinstance(body, dict):
        raise bad_request("The request body must be a JSON object.")
    return body


_EXPIRY = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ADDRESS_FIELDS = {
    "line1": "address_line_1", "line2": "address_line_2", "city": "admin_area_2",
    "state": "admin_area_1", "postalCode": "postal_code", "countryCode": "country_code",
}


@sensitive_variables()
def _card_input(raw: object) -> payments.CardInput:
    if not isinstance(raw, dict):
        raise bad_request("'card' must be an object with number and expiry.")
    number = re.sub(r"[\s-]", "", str(raw.get("number", "")))
    if not number.isdigit() or not 12 <= len(number) <= 19:
        raise bad_request("card.number must be 12 to 19 digits.")
    expiry = str(raw.get("expiry", ""))
    if not _EXPIRY.match(expiry):
        raise bad_request("card.expiry must be YYYY-MM.")
    security_code = str(raw.get("securityCode", "") or "")
    if security_code and (not security_code.isdigit() or len(security_code) not in (3, 4)):
        raise bad_request("card.securityCode must be 3 or 4 digits.")
    name = str(raw.get("name", "") or "")[:300]
    address: dict[str, str] = {}
    raw_address = raw.get("billingAddress")
    if raw_address is not None:
        if not isinstance(raw_address, dict):
            raise bad_request("card.billingAddress must be an object.")
        for key, wire in _ADDRESS_FIELDS.items():
            value = raw_address.get(key)
            if value:
                address[wire] = str(value)
        if "country_code" not in address or len(address["country_code"]) != 2:
            raise bad_request("card.billingAddress.countryCode must be a 2-letter ISO country code.")
        address["country_code"] = address["country_code"].upper()
    return payments.CardInput(number=number, expiry=expiry, security_code=security_code, name=name,
                              billing_address=address)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@ensure_csrf_cookie
@api(("GET",), auth="none")
def csrf(request: HttpRequest) -> HttpResponse:
    return JsonResponse({"csrfToken": get_token(request)})


@sensitive_post_parameters()
@sensitive_variables()
@api(("POST",), auth="none")
def session_login(request: HttpRequest) -> HttpResponse:
    body = _json_body(request)
    username, password = body.get("username"), body.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise bad_request("username and password are required.")
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return JsonResponse({"error": "invalid_credentials", "message": "Wrong username or password."}, status=401)
    login(request, user)
    return JsonResponse({"user": {"id": user.pk, "username": user.get_username(), "isStaff": user.is_staff},
                         "csrfToken": get_token(request)})


@api(("POST",), auth="none")
def session_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@api(("POST",))
def create_order(request: HttpRequest) -> HttpResponse:
    payment = place_order(request.user, _json_body(request))
    body = presenters.order(payment)
    return JsonResponse(body, status=201)


def _payment_response(payment: PayPalPayment, op: PaymentOperation | None, *, done_status: int = 200) -> HttpResponse:
    body = presenters.order(payment)
    if op is not None and op.outcome == PaymentOperation.FAILED:
        body.update({"error": "payment_failed", "message": op.detail or "PayPal declined the payment."})
        if op.detail == "payer_action_required":
            body["message"] = (
                "PayPal requires the shopper to complete a challenge in a browser; this integration does not "
                "support that. No money was held."
            )
        return JsonResponse(body, status=402)
    if op is None or op.outcome == PaymentOperation.DONE:
        return JsonResponse(body, status=done_status)  # the only path to success
    if op.outcome == PaymentOperation.NEEDS_REVIEW:
        body.update({"error": "needs_review", "message": op.detail})
        return JsonResponse(body, status=502)
    # sending / pending / unknown: accepted, not done
    body["paymentOutcome"] = op.outcome
    return JsonResponse(body, status=202)


@sensitive_variables()
@api(("POST",))
def pay_order(request: HttpRequest, order_number: str) -> HttpResponse:
    body = _json_body(request)
    raw_card, method_id = body.get("card"), body.get("paymentMethodId")
    if (raw_card is None) == (method_id is None):
        raise bad_request("Provide exactly one of 'card' or 'paymentMethodId'.")
    card = _card_input(raw_card) if raw_card is not None else None
    if method_id is not None and not re.fullmatch(r"[0-9a-fA-F-]{32,36}", str(method_id)):
        raise bad_request("paymentMethodId is not valid.")
    payment, op = payments.pay(request.user, order_number, card=card,
                               saved_card_id=str(method_id) if method_id is not None else None)
    return _payment_response(payment, op)


@api(("POST",), auth="staff")
def fulfil_order(request: HttpRequest, order_number: str) -> HttpResponse:
    payment, op = payments.fulfil(order_number)
    return _payment_response(payment, op)


@api(("POST",), auth="staff")
def cancel_order(request: HttpRequest, order_number: str) -> HttpResponse:
    payment, op = payments.cancel(order_number)
    return _payment_response(payment, op)


@api(("POST",))
def refund_order(request: HttpRequest, order_number: str) -> HttpResponse:
    body = _json_body(request)
    key = request.headers.get("Idempotency-Key") or body.get("idempotencyKey")
    if not isinstance(key, str) or not 1 <= len(key.strip()) <= 255:
        raise bad_request("Send an Idempotency-Key header (1-255 characters) identifying this refund.")
    payment, op = payments.refund(request.user, order_number, key.strip(), body.get("amount"))
    result = presenters.refund(op)
    result["order"] = presenters.order(payment)
    status = {PaymentOperation.DONE: 201, PaymentOperation.FAILED: 402}.get(op.outcome, 202)
    return JsonResponse(result, status=status)


@api(("GET",))
def my_orders(request: HttpRequest) -> HttpResponse:
    user_id = request.user.pk
    assert user_id is not None  # authenticated by @api
    payments_by_order = {
        p.order_id: p
        for p in PayPalPayment.objects.filter(order__user_id=user_id).select_related("order", "saved_card")
    }
    results = []
    for o in Order.objects.filter(user_id=user_id).order_by("-date_placed").prefetch_related("lines"):
        p = payments_by_order.get(o.pk)
        if p is not None:
            results.append(presenters.order(p))
        else:
            results.append({"orderId": str(o.number), "status": o.status, "placedAt": o.date_placed.isoformat(),
                            "total": str(o.total_incl_tax), "currency": o.currency, "payment": None})
    return JsonResponse({"orders": results})


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


@sensitive_variables()
@api(("GET", "POST"))
def payment_methods(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return JsonResponse({"paymentMethods": [presenters.saved_card(c) for c in cards.list_cards(request.user)]})
    card_input = _card_input(_json_body(request).get("card"))
    saved, op, created = cards.save_card(request.user, card_input)
    if saved is None:
        return JsonResponse({"status": op.outcome, "message": "PayPal is still saving this card; repeat the "
                             "request to check."}, status=202)
    return JsonResponse(presenters.saved_card(saved), status=201 if created else 200)


@api(("DELETE",))
def payment_method(request: HttpRequest, payment_method_id: str) -> HttpResponse:
    if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", payment_method_id):
        raise ApiProblem(404, "not_found", "Payment method not found.")
    if cards.delete_card(request.user, payment_method_id):
        return HttpResponse(status=204)
    return JsonResponse(
        {"paymentMethodId": payment_method_id, "deleted": True, "paypalDeletion": "pending",
         "message": "The card is removed and can no longer be used; PayPal's copy will be deleted on retry."},
        status=202,
    )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _parse_instant(value: str | None, name: str) -> datetime:
    if not value:
        raise bad_request("'%s' is required (ISO-8601 date-time with a UTC offset)." % name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "+"))
    except ValueError:
        raise bad_request("'%s' must be an ISO-8601 date-time, e.g. 2026-09-01T00:00:00Z." % name) from None
    if parsed.tzinfo is None:
        raise bad_request("'%s' must carry a UTC offset or Z." % name)
    return parsed


@api(("GET",), auth="staff")
def reconciliation(request: HttpRequest) -> HttpResponse:
    start = _parse_instant(request.GET.get("from"), "from")
    end = _parse_instant(request.GET.get("to"), "to")
    if start >= end:
        raise bad_request("'from' must be before 'to'.")
    if end - start > MAX_RANGE:
        raise bad_request("The range can cover at most three years (PayPal's reporting limit).")
    return JsonResponse(reconcile(start, end))

