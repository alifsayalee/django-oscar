"""Thin, well-guarded wrapper over the PayPal Server SDK (``paypal`` package).

Every PayPal interaction in this app goes through here. Failures from the SDK
(``ApiError``, decode ``ValueError``, ``httpx`` transport errors) are translated
into a single :class:`PayPalError` domain exception carrying an HTTP status the
view layer can surface, so no SDK type leaks past this module.
"""

import logging
import threading

import httpx
from django.conf import settings
from pydantic import ValidationError

from paypal import PaypalClient
from paypal.core import ApiError, OAuthProviderError, RawError, Success, UNSET, ClientCredentials

logger = logging.getLogger("paypal_api")

_client = None
_client_lock = threading.Lock()


class PayPalError(Exception):
    """A PayPal failure translated for the API boundary."""

    def __init__(self, message, http_status=502, code="paypal_error", detail=None,
                 paypal_status=None):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code
        self.detail = detail
        self.paypal_status = paypal_status


def base_url():
    """Resolve the PayPal API base URL.

    ``PAYPAL_BASE_URL``, when set, is used verbatim for every call (token
    included). Otherwise it is derived from ``PAYPAL_ENVIRONMENT``.
    """
    override = (getattr(settings, "PAYPAL_BASE_URL", "") or "").strip()
    if override:
        return override
    env = (getattr(settings, "PAYPAL_ENVIRONMENT", "sandbox") or "sandbox").strip().lower()
    if env in ("live", "production"):
        return "https://api-m.paypal.com"
    return "https://api-m.sandbox.paypal.com"


def get_client():
    """Return the process-wide, long-lived sync client (built lazily once)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                client_id = getattr(settings, "PAYPAL_CLIENT_ID", "") or ""
                client_secret = getattr(settings, "PAYPAL_CLIENT_SECRET", "") or ""
                if not client_id or not client_secret:
                    raise PayPalError(
                        "PayPal credentials are not configured", 503, "not_configured"
                    )
                _client = PaypalClient(
                    base_url=base_url(),
                    oauth2=ClientCredentials(
                        client_id=client_id, client_secret=client_secret
                    ),
                    timeout=30.0,
                )
    return _client


def _v(value):
    """Return None for the SDK's UNSET sentinel, else the value."""
    if value is UNSET:
        return None
    return value


def _client_status(paypal_status):
    if paypal_status and 400 <= paypal_status < 500:
        return paypal_status
    return 502


def _translate(label, e):
    err = e.error
    if isinstance(err, OAuthProviderError):
        logger.error("PayPal auth failure during %s: %s", label, getattr(err, "error", err))
        return PayPalError(
            "PayPal rejected the API credentials", 502, "auth_config"
        )
    status = e.status_code
    if isinstance(err, RawError):
        try:
            text = err.text()
        except Exception:  # pragma: no cover - defensive
            text = ""
        logger.warning("PayPal %s failed (%s): %s", label, status, text[:500])
        return PayPalError(
            "PayPal returned an error during %s" % label,
            _client_status(status),
            "paypal_error",
            detail=text[:500],
            paypal_status=status,
        )
    # Typed Error body
    name = _v(getattr(err, "name", None)) or ""
    message = _v(getattr(err, "message", None)) or ""
    issues = []
    for d in (_v(getattr(err, "details", None)) or []):
        issues.append(
            {
                "issue": _v(getattr(d, "issue", None)),
                "description": _v(getattr(d, "description", None)),
            }
        )
    logger.warning("PayPal %s rejected (%s): %s %s", label, status, name, issues)
    return PayPalError(
        message or name or "PayPal rejected the request",
        _client_status(status),
        name or "paypal_error",
        detail={"name": name, "issues": issues},
        paypal_status=status,
    )


def _execute(label, fn):
    """Run ``fn(client)`` translating every SDK failure into PayPalError."""
    client = get_client()
    try:
        return fn(client)
    except PayPalError:
        raise
    except ApiError as e:
        raise _translate(label, e)
    except (ValidationError, ValueError) as e:
        logger.error("PayPal %s returned an unreadable response: %s", label, e)
        raise PayPalError(
            "PayPal returned an unreadable response during %s" % label,
            502,
            "unreadable",
        ) from e
    except httpx.HTTPError as e:
        logger.error("PayPal %s transport failure: %s", label, e)
        raise PayPalError("PayPal is currently unreachable", 503, "unavailable") from e


# --------------------------------------------------------------------------
# Money / extraction helpers
# --------------------------------------------------------------------------

def _money(m):
    m = _v(m)
    if m is None:
        return None
    return _v(getattr(m, "value", None))


def _first_authorization(order):
    units = _v(order.purchase_units) or []
    for unit in units:
        payments = _v(getattr(unit, "payments", None))
        if payments is None:
            continue
        auths = _v(getattr(payments, "authorizations", None)) or []
        if auths:
            return auths[0]
    return None


# --------------------------------------------------------------------------
# Orders / payments
# --------------------------------------------------------------------------

def authorize_with_card(*, currency, value, order_number, card, request_id):
    """Single-step create + authorize an order using a direct card or vault id.

    ``card`` is a plain dict for ``payment_source.card`` (either raw card fields
    or ``{"vault_id": ...}``). Returns the authorization details.
    """
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "amount": {"currency_code": currency, "value": value},
                "custom_id": order_number,
                "invoice_id": order_number,
                "description": "Oscar order %s" % order_number,
            }
        ],
        "payment_source": {"card": card},
    }

    order = _execute(
        "authorize",
        lambda c: c.orders.create_order(
            body, pay_pal_request_id=request_id, prefer="return=representation"
        ),
    )
    auth = _first_authorization(order)
    if auth is None or not _v(getattr(auth, "id", None)):
        # No hold produced. In sandbox with the test card this does not happen;
        # a real 3DS challenge would land here — we stop rather than build a
        # browser round-trip.
        raise PayPalError(
            "PayPal did not place a hold on the card. The payment likely "
            "requires browser-based approval (3-D Secure), which this API does "
            "not perform. Order status was '%s'." % (_v(order.status) or "unknown"),
            402,
            "challenge_required",
            detail={"orderStatus": _v(order.status)},
        )
    return {
        "order_id": _v(order.id),
        "order_status": _v(order.status),
        "authorization_id": _v(auth.id),
        "authorization_status": _v(getattr(auth, "status", None)),
        "authorization_expiry": _v(getattr(auth, "expiration_time", None)),
    }


def get_authorization(auth_id):
    auth = _execute(
        "get_authorization", lambda c: c.payments.get_authorized_payment(auth_id)
    )
    return {
        "status": _v(getattr(auth, "status", None)),
        "expiry": _v(getattr(auth, "expiration_time", None)),
    }


def reauthorize(*, auth_id, currency, value):
    auth = _execute(
        "reauthorize",
        lambda c: c.payments.reauthorize_payment(
            auth_id,
            prefer="return=representation",
            body={"amount": {"currency_code": currency, "value": value}},
        ),
    )
    return {
        "authorization_id": _v(getattr(auth, "id", None)),
        "status": _v(getattr(auth, "status", None)),
        "expiry": _v(getattr(auth, "expiration_time", None)),
    }


def capture(*, auth_id, request_id, currency=None, value=None):
    """Capture an authorization in full (final capture)."""
    body = {"final_capture": True}
    if value is not None and currency is not None:
        body["amount"] = {"currency_code": currency, "value": value}
    cap = _execute(
        "capture",
        lambda c: c.payments.capture_authorized_payment(
            auth_id,
            pay_pal_request_id=request_id,
            prefer="return=representation",
            body=body,
        ),
    )
    srb = _v(getattr(cap, "seller_receivable_breakdown", None))
    gross = fee = net = None
    if srb is not None:
        gross = _money(getattr(srb, "gross_amount", None))
        fee = _money(getattr(srb, "paypal_fee", None))
        net = _money(getattr(srb, "net_amount", None))
    return {
        "capture_id": _v(getattr(cap, "id", None)),
        "status": _v(getattr(cap, "status", None)),
        "amount": _money(getattr(cap, "amount", None)),
        "gross": gross,
        "fee": fee,
        "net": net,
    }


def void(auth_id):
    """Void (release) an authorization. Uses representation to avoid a 204 body."""
    auth = _execute(
        "void",
        lambda c: c.payments.void_payment(auth_id, prefer="return=representation"),
    )
    return {"status": _v(getattr(auth, "status", None)) or "VOIDED"}


def refund(*, capture_id, request_id, currency, value):
    ref = _execute(
        "refund",
        lambda c: c.payments.refund_captured_payment(
            capture_id,
            pay_pal_request_id=request_id,
            prefer="return=representation",
            body={"amount": {"currency_code": currency, "value": value}},
        ),
    )
    return {
        "refund_id": _v(getattr(ref, "id", None)),
        "status": _v(getattr(ref, "status", None)),
        "amount": _money(getattr(ref, "amount", None)),
    }


# --------------------------------------------------------------------------
# Vault (saved cards)
# --------------------------------------------------------------------------

def create_vault_token(*, card, customer_id=None, request_id):
    payment_source = {"card": card}
    body = {"payment_source": payment_source}
    if customer_id:
        body["customer"] = {"id": customer_id}
    resp = _execute(
        "vault_create",
        lambda c: c.vault.create_payment_token(body, pay_pal_request_id=request_id),
    )
    token_id = _v(getattr(resp, "id", None))
    if not token_id:
        raise PayPalError(
            "PayPal did not return a vault token id; card was not saved",
            502,
            "unreadable",
        )
    cust = _v(getattr(resp, "customer", None))
    out_customer = _v(getattr(cust, "id", None)) if cust is not None else None
    brand = last = expiry = None
    ps = _v(getattr(resp, "payment_source", None))
    if ps is not None:
        card_resp = _v(getattr(ps, "card", None))
        if card_resp is not None:
            brand = _v(getattr(card_resp, "brand", None))
            last = _v(getattr(card_resp, "last_digits", None))
            expiry = _v(getattr(card_resp, "expiry", None))
    return {
        "token_id": token_id,
        "customer_id": out_customer,
        "brand": brand,
        "last_digits": last,
        "expiry": expiry,
    }


def delete_vault_token(token_id):
    """Delete a vault token. Idempotent: an already-gone token is treated OK."""
    client = get_client()
    try:
        result = client.vault.with_raw_response.delete_payment_token(token_id)
    except ApiError as e:
        if e.status_code == 404:
            return True
        raise _translate("vault_delete", e)
    except ValueError:
        # 404 with empty body decodes to ValueError; treat as already gone.
        return True
    except httpx.HTTPError as e:
        raise PayPalError("PayPal is currently unreachable", 503, "unavailable") from e
    if isinstance(result, Success):
        return True
    status = result.response.status_code
    if status == 404:
        return True
    raise PayPalError(
        "PayPal could not delete the saved card", _client_status(status),
        "paypal_error", paypal_status=status,
    )


# --------------------------------------------------------------------------
# Transaction search (reconciliation)
# --------------------------------------------------------------------------

def search_transactions(*, start_date, end_date, page, page_size=100):
    resp = _execute(
        "search_transactions",
        lambda c: c.transaction_search.search_transactions(
            start_date,
            end_date,
            fields="transaction_info",
            page_size=page_size,
            page=page,
        ),
    )
    return resp
