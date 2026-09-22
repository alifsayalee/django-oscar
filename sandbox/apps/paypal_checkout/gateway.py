"""Thin, defensive wrapper around the PayPal Server SDK (``paypal`` package).

This is the ONLY module that talks to PayPal. It:

* holds one long-lived, lazily-built :class:`~paypal.PaypalClient` (sync; the
  sandbox runs under WSGI);
* reads credentials/config from Django settings (never hard-coded);
* returns plain Python dicts with ``Decimal`` money and ``str`` statuses, so no
  SDK-typed value (or the ``UNSET`` sentinel) ever crosses into the rest of the
  app;
* translates every SDK failure kind -- ``ApiError`` (with its per-operation
  error union), a failed OAuth token fetch, ``httpx`` transport errors and
  ``pydantic``/JSON decode failures -- into this app's domain exceptions.

Contract facts (signatures, error unions, empty-body behaviour) come from
``pay-pal-server-sdk-plan.md`` and were confirmed live against the sandbox.
"""
import logging
import threading
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, NoReturn, Optional, cast

import httpx
from django.conf import settings
from pydantic import ValidationError

from paypal import PaypalClient
from paypal.core import ApiError, ClientCredentials, OAuthProviderError, RawError, UNSET
from paypal.models import (
    CaptureRequestDict,
    Error,
    OrderAuthorizeRequestDict,
    OrderRequestDict,
    PaymentTokenRequestDict,
    ReauthorizeRequestDict,
    RefundRequestDict,
)

from . import exceptions as exc

log = logging.getLogger("paypal_checkout")

_client = None
_client_lock = threading.Lock()

# PayPal order/authorization statuses that mean "the buyer must approve in a
# browser" -- the task requires us to STOP and report these, not build a flow.
_PAYER_ACTION_STATUSES = {"PAYER_ACTION_REQUIRED"}


def get_client() -> PaypalClient:
    """Return the process-wide PayPal client, building it once on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                base_url = settings.PAYPAL_BASE_URL or None
                _client = PaypalClient(
                    oauth2=ClientCredentials(
                        client_id=settings.PAYPAL_CLIENT_ID,
                        client_secret=settings.PAYPAL_CLIENT_SECRET,
                    ),
                    base_url=base_url,
                    timeout=45.0,
                )
    return _client


# --------------------------------------------------------------------------- #
# Value helpers
# --------------------------------------------------------------------------- #
def money_str(amount: Any) -> str:
    """Format a ``Decimal`` as a two-decimal string for PayPal (e.g. ``"25.00"``)."""
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _v(value: Any) -> Any:
    """Resolve an SDK optional member to a plain value or ``None`` (UNSET/None)."""
    if value is UNSET or value is None:
        return None
    return value


def _enum(value: Any) -> Optional[str]:
    """Coerce an open-enum member (or str) to its wire string, or ``None``."""
    value = _v(value)
    if value is None:
        return None
    # str-based enums stringify to their wire value; a plain str is returned as-is.
    return str(value)


def _money(money: Any) -> tuple[Optional[Decimal], Optional[str]]:
    """Extract ``(Decimal, currency)`` from an SDK ``Money``/amount, or ``(None, None)``."""
    money = _v(money)
    if money is None:
        return None, None
    value = _v(getattr(money, "value", None))
    currency = _v(getattr(money, "currency_code", None))
    return (Decimal(value) if value is not None else None), currency


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #
def _translate(e: Exception, *, operation: str) -> NoReturn:
    """Map an SDK/transport failure to a domain exception. Never returns."""
    if isinstance(e, ApiError):
        err = e.error
        status = e.status_code
        # A failed OAuth token fetch surfaces here, out of the operation call.
        if isinstance(err, OAuthProviderError):
            log.error("PayPal auth failed on %s: %s", operation, getattr(err, "error", None))
            raise exc.ProviderConfigError("PayPal credentials were rejected.") from e
        # 401/403/429 are ours (credentials/quota), never the caller's fault.
        if status in (401, 403):
            log.error("PayPal refused us (%s) on %s", status, operation)
            raise exc.ProviderConfigError("PayPal refused this application's credentials.") from e
        if status == 429:
            raise exc.ProviderUnavailable(
                "PayPal is rate-limiting requests; try again shortly.", status_code=503
            ) from e
        # Status-first mapping so it works for both typed (Error) and Case-B
        # (RawError-only, e.g. transaction search) operations alike.
        message = _message_of(err) or "PayPal rejected the request."
        detail = _error_details(err) if isinstance(err, Error) else None
        code = _v(getattr(err, "name", None)) if isinstance(err, Error) else None
        if status == 400:
            log.warning("PayPal rejected %s (400): %s %s", operation, message, detail)
            raise exc.BadRequest(message, code=code, details=detail) from e
        if status in (404, 409, 422):
            log.warning("PayPal rejected %s (%s): %s %s", operation, status, message, detail)
            raise exc.ProviderRejected(message, code=code, details=detail, status_code=status) from e
        # 5xx and every unmapped status -> ours to own.
        log.error("PayPal failure on %s (%s): %s", operation, status, message)
        raise exc.ProviderUnavailable("PayPal returned an unexpected error.") from e

    if isinstance(e, (ValidationError, ValueError)):
        # Decode failure -- bypasses both response modes. Outcome unknown.
        log.error("PayPal response from %s could not be read: %s", operation, e)
        raise exc.ProviderUnreadable(
            "PayPal returned a response that could not be read; the outcome is unknown."
        ) from e

    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)):
        # Never reached PayPal -- outcome known: nothing happened.
        raise exc.ProviderUnavailable("Could not reach PayPal.") from e
    if isinstance(e, httpx.RequestError):
        # May have landed -- outcome unknown.
        raise exc.ProviderUnavailable(
            "PayPal did not respond; the outcome of the request is unknown."
        ) from e
    raise e  # pragma: no cover - programming errors propagate


def _message_of(err: Any) -> Optional[str]:
    """Best-effort caller-safe message from a typed ``Error`` or a ``RawError``."""
    msg = _v(getattr(err, "message", None))
    if msg:
        return str(msg)
    if isinstance(err, RawError):
        try:
            data = err.json()
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
        except Exception:  # noqa: BLE001 - non-JSON body
            pass
        try:
            return err.text()[:300]
        except Exception:  # noqa: BLE001
            return None
    return None


def _error_details(err: Any) -> Optional[list[dict[str, Any]]]:
    details = _v(getattr(err, "details", None))
    if not details:
        return None
    out = []
    for d in details:
        out.append({
            "field": _v(getattr(d, "field", None)),
            "issue": _v(getattr(d, "issue", None)),
            "description": _v(getattr(d, "description", None)),
        })
    return out


def _guard_challenge(order_or_auth_status: Optional[str], *, operation: str) -> None:
    if order_or_auth_status in _PAYER_ACTION_STATUSES:
        log.warning("PayPal returned %s on %s -- browser approval required", order_or_auth_status, operation)
        raise exc.PaymentActionRequired()


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
def create_order(*, currency: str, value: Any, invoice_id: str, custom_id: str, request_id: str) -> dict[str, Any]:
    """Create a PayPal order with intent=AUTHORIZE. Returns ``{"id", "status"}``."""
    client = get_client()
    body: OrderRequestDict = {
        "intent": "AUTHORIZE",
        "purchase_units": [{
            "reference_id": "default",
            "invoice_id": invoice_id,
            "custom_id": custom_id,
            "amount": {"currency_code": currency, "value": money_str(value)},
        }],
    }
    try:
        order = client.orders.create_order(
            body=body, pay_pal_request_id=request_id, prefer="return=representation"
        )
    except Exception as e:  # noqa: BLE001 - translated below
        _translate(e, operation="create_order")
    order_id = _v(order.id)
    if not order_id:
        raise exc.ProviderUnreadable("PayPal did not return an order id; outcome unknown.")
    return {"id": order_id, "status": _enum(order.status)}


def authorize_order(*, paypal_order_id: str, card_source: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Authorize (hold) an order with a card source.

    ``card_source`` is either a one-off card dict or ``{"vault_id": ...}``.
    Returns ``{"authorization_id", "status", "amount", "currency", "expires_at"}``.
    """
    client = get_client()
    # card_source is a dynamically-built card dict (one-off or {"vault_id": ...});
    # both are valid CardRequestDict shapes -- cast documents the intended type.
    body = cast(OrderAuthorizeRequestDict, {"payment_source": {"card": card_source}})
    try:
        resp = client.orders.authorize_order(
            paypal_order_id, body=body, pay_pal_request_id=request_id,
            prefer="return=representation",
        )
    except Exception as e:  # noqa: BLE001
        _translate(e, operation="authorize_order")

    _guard_challenge(_enum(resp.status), operation="authorize_order")
    authz = _first_authorization(resp)
    if authz is None:
        # No authorization and not a known challenge -> unreadable/unknown outcome.
        raise exc.ProviderUnreadable(
            "PayPal did not return an authorization for the order; outcome unknown."
        )
    amount, currency = _money(getattr(authz, "amount", None))
    return {
        "authorization_id": _v(authz.id),
        "status": _enum(authz.status),
        "amount": amount,
        "currency": currency,
        "expires_at": _v(getattr(authz, "expiration_time", None)),
    }


def _first_authorization(order_like: Any) -> Any:
    units = _v(getattr(order_like, "purchase_units", None)) or []
    for unit in units:
        payments = _v(getattr(unit, "payments", None))
        if payments is None:
            continue
        auths = _v(getattr(payments, "authorizations", None)) or []
        if auths:
            return auths[0]
    return None


# --------------------------------------------------------------------------- #
# Payments (capture / reauthorize / void / refund)
# --------------------------------------------------------------------------- #
def get_authorization_status(authorization_id: str) -> Optional[str]:
    client = get_client()
    try:
        auth = client.payments.get_authorized_payment(authorization_id)
    except Exception as e:  # noqa: BLE001
        _translate(e, operation="get_authorized_payment")
    return _enum(auth.status)


def reauthorize(*, authorization_id: str, currency: str, value: Any, request_id: str) -> dict[str, Any]:
    """Renew a stale authorization. Returns ``{"authorization_id", "status"}``."""
    client = get_client()
    body: ReauthorizeRequestDict = {"amount": {"currency_code": currency, "value": money_str(value)}}
    try:
        auth = client.payments.reauthorize_payment(
            authorization_id, body=body, pay_pal_request_id=request_id,
            prefer="return=representation",
        )
    except Exception as e:  # noqa: BLE001
        _translate(e, operation="reauthorize_payment")
    return {"authorization_id": _v(auth.id), "status": _enum(auth.status)}


def capture_authorization(*, authorization_id: str, request_id: str) -> dict[str, Any]:
    """Capture (take) an authorized payment in full.

    Returns ``{"capture_id", "status", "captured", "fee", "net", "currency"}``.
    """
    client = get_client()
    capture_body: CaptureRequestDict = {"final_capture": True}
    try:
        cap = client.payments.capture_authorized_payment(
            authorization_id, body=capture_body, pay_pal_request_id=request_id,
            prefer="return=representation",
        )
    except Exception as e:  # noqa: BLE001
        _translate(e, operation="capture_authorized_payment")

    capture_id = _v(cap.id)
    if not capture_id:
        raise exc.ProviderUnreadable("PayPal did not return a capture id; outcome unknown.")
    captured, currency = _money(getattr(cap, "amount", None))
    fee = net = None
    srb = _v(getattr(cap, "seller_receivable_breakdown", None))
    if srb is not None:
        gross, gcur = _money(getattr(srb, "gross_amount", None))
        if captured is None:
            captured, currency = gross, gcur
        fee, _ = _money(getattr(srb, "paypal_fee", None))
        net, _ = _money(getattr(srb, "net_amount", None))
    return {
        "capture_id": capture_id,
        "status": _enum(cap.status),
        "captured": captured,
        "fee": fee,
        "net": net,
        "currency": currency,
    }


def void_authorization(*, authorization_id: str, request_id: str) -> Optional[str]:
    """Void (release) an authorization. Returns the resulting status.

    NB: with the default ``prefer=return=minimal`` PayPal answers 204 with an
    empty body, which the SDK cannot decode. ``return=representation`` yields a
    200 with a body, avoiding that decode failure.
    """
    client = get_client()
    try:
        resp = client.payments.void_payment(
            authorization_id, pay_pal_request_id=request_id, prefer="return=representation"
        )
    except Exception as e:  # noqa: BLE001
        _translate(e, operation="void_payment")
    status = _enum(resp.status)
    if status:
        return status
    # Some environments still answer without a body; confirm via a read.
    return get_authorization_status(authorization_id)


def refund_capture(*, capture_id: str, currency: str, value: Any, invoice_id: str, custom_id: str, request_id: str) -> dict[str, Any]:
    """Refund a capture, in full (``value=None``) or in part.

    Returns ``{"refund_id", "status", "amount", "currency"}``.
    """
    client = get_client()
    body: RefundRequestDict = {"custom_id": custom_id, "invoice_id": invoice_id}
    if value is not None:
        body["amount"] = {"currency_code": currency, "value": money_str(value)}
    try:
        refund = client.payments.refund_captured_payment(
            capture_id, body=body, pay_pal_request_id=request_id,
            prefer="return=representation",
        )
    except Exception as e:  # noqa: BLE001
        _translate(e, operation="refund_captured_payment")
    refund_id = _v(refund.id)
    if not refund_id:
        raise exc.ProviderUnreadable("PayPal did not return a refund id; outcome unknown.")
    amount, cur = _money(getattr(refund, "amount", None))
    return {"refund_id": refund_id, "status": _enum(refund.status),
            "amount": amount, "currency": cur or currency}


# --------------------------------------------------------------------------- #
# Vault (saved cards)
# --------------------------------------------------------------------------- #
def create_payment_token(*, card: dict[str, Any], customer_id: Optional[str], request_id: str) -> dict[str, Any]:
    """Vault a card. ``card`` is a one-off card dict (number/expiry/...).

    Returns ``{"vault_id", "customer_id", "brand", "last_digits", "expiry", "name"}``.
    """
    client = get_client()
    body = cast(PaymentTokenRequestDict, {"payment_source": {"card": card}})
    if customer_id:
        body["customer"] = {"id": customer_id}
    try:
        token = client.vault.create_payment_token(body=body, pay_pal_request_id=request_id)
    except Exception as e:  # noqa: BLE001
        _translate(e, operation="create_payment_token")
    vault_id = _v(token.id)
    if not vault_id:
        raise exc.ProviderUnreadable("PayPal did not return a vault id; outcome unknown.")
    customer = _v(getattr(token, "customer", None))
    out_customer = _v(getattr(customer, "id", None)) if customer else None
    card_ent = None
    source = _v(getattr(token, "payment_source", None))
    if source is not None:
        card_ent = _v(getattr(source, "card", None))
    return {
        "vault_id": vault_id,
        "customer_id": out_customer,
        "brand": _enum(getattr(card_ent, "brand", None)) if card_ent else None,
        "last_digits": _v(getattr(card_ent, "last_digits", None)) if card_ent else None,
        "expiry": _v(getattr(card_ent, "expiry", None)) if card_ent else None,
        "name": _v(getattr(card_ent, "name", None)) if card_ent else None,
    }


def delete_payment_token(vault_id: str) -> bool:
    """Delete a vaulted card. Idempotent: a 404 (already gone) is treated as success.

    ``delete_payment_token`` returns 204/None, so the raw peer is used to read the
    status code without tripping the empty-body decode.
    """
    client = get_client()
    try:
        result = client.vault.with_raw_response.delete_payment_token(vault_id)
    except Exception as e:  # noqa: BLE001
        _translate(e, operation="delete_payment_token")
    status = result.response.status_code
    if status in (200, 204, 404):
        return True
    raise exc.ProviderUnavailable("PayPal could not delete the saved card.")


# --------------------------------------------------------------------------- #
# Transaction search (reconciliation)
# --------------------------------------------------------------------------- #
def search_transactions(*, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Return PayPal's transactions across the whole date range (all pages).

    ``start_date``/``end_date`` are ISO-8601 strings PayPal accepts. Yields a list
    of ``{"transaction_id", "amount", "currency", "status", "invoice_id",
    "custom_field", "initiated_at"}``.
    """
    client = get_client()
    results = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        try:
            resp = client.transaction_search.search_transactions(
                start_date, end_date, fields="transaction_info",
                page_size=100, page=page,
            )
        except Exception as e:  # noqa: BLE001
            _translate(e, operation="search_transactions")
        total_pages = _v(getattr(resp, "total_pages", None)) or 1
        details = _v(getattr(resp, "transaction_details", None)) or []
        for d in details:
            info = _v(getattr(d, "transaction_info", None))
            if info is None:
                continue
            amount, currency = _money(getattr(info, "transaction_amount", None))
            results.append({
                "transaction_id": _v(getattr(info, "transaction_id", None)),
                "amount": amount,
                "currency": currency,
                "status": _v(getattr(info, "transaction_status", None)),
                "invoice_id": _v(getattr(info, "invoice_id", None)),
                "custom_field": _v(getattr(info, "custom_field", None)),
                "initiated_at": _v(getattr(info, "transaction_initiation_date", None)),
            })
        page += 1
    return results
