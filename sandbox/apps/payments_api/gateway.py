"""Thin, well-guarded wrapper around the PayPal Server SDK (``paypal``).

Every PayPal interaction in this app goes through here. Responsibilities:

* own a single long-lived :class:`PaypalClient` (WSGI module-level singleton);
* build request models from the contract sheet in ``pay-pal-server-sdk-plan.md``;
* translate every SDK failure kind into one :class:`PayPalError` carrying an
  HTTP status our API can return and an operator-actionable message.

Facts (signatures, wire aliases, error unions) come from the contract sheet, not
memory. Money is a ``str`` scaled to the currency, built with ``Decimal``.
"""
from __future__ import annotations

import atexit
import logging
import threading
from decimal import ROUND_HALF_UP, Decimal

import httpx
from django.conf import settings
from pydantic import ValidationError

from paypal import PaypalClient
from paypal.core import UNSET, ApiError, ClientCredentials, OAuthProviderError, RawError
from paypal.models import (
    AmountWithBreakdown,
    CardRequest,
    Customer,
    Money,
    OrderRequest,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    RefundRequest,
    SetupTokenRequest,
    SetupTokenRequestCard,
    SetupTokenRequestPaymentSource,
    VaultTokenRequest,
)

log = logging.getLogger("oscar.paypal")

_REPRESENTATION = "return=representation"

_client = None
_client_lock = threading.Lock()


class PayPalError(Exception):
    """Normalised failure from any PayPal call.

    :param http_status: status our API should return to the caller.
    :param message: safe, operator-readable message.
    :param issues: list of PayPal issue codes when available.
    :param config: True when this is a credentials/configuration fault.
    """

    def __init__(self, http_status, message, issues=None, config=False):
        super().__init__(message)
        self.http_status = http_status
        self.message = message
        self.issues = issues or []
        self.config = config

    def as_dict(self):
        return {"error": self.message, "issues": self.issues}


def get_client():
    """Return the process-wide PayPal client, building it lazily once."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
                    raise PayPalError(
                        503,
                        "PayPal credentials are not configured (set PAYPAL_CLIENT_ID "
                        "and PAYPAL_CLIENT_SECRET).",
                        config=True,
                    )
                _client = PaypalClient(
                    oauth2=ClientCredentials(
                        client_id=settings.PAYPAL_CLIENT_ID,
                        client_secret=settings.PAYPAL_CLIENT_SECRET,
                    ),
                    base_url=settings.PAYPAL_BASE_URL or None,
                    timeout=30.0,
                )
                atexit.register(_close_client)
    return _client


def _close_client():
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass
        _client = None


# --------------------------------------------------------------------------- #
# Error boundary
# --------------------------------------------------------------------------- #
def _issues(error):
    details = getattr(error, "details", None)
    if not details or details is UNSET:
        return []
    out = []
    for d in details:
        issue = getattr(d, "issue", None)
        if issue and issue is not UNSET:
            out.append(str(issue))
    return out


def _translate(exc, op):
    """Map any SDK failure to a :class:`PayPalError`. Never returns."""
    if isinstance(exc, ApiError):
        error = exc.error
        if isinstance(error, OAuthProviderError):
            log.error("PayPal auth failure on %s: %s", op, getattr(error, "error", ""))
            raise PayPalError(
                503, "PayPal credentials were rejected.", config=True
            ) from exc
        if isinstance(error, RawError):
            # Undocumented status (often 500) — outcome may be unknown.
            status = 502 if exc.status_code >= 500 else exc.status_code
            raise PayPalError(
                status, f"PayPal returned an unexpected error ({exc.status_code})."
            ) from exc
        # Typed ``Error`` body (4xx business validation).
        issues = _issues(error)
        message = getattr(error, "message", None) or "PayPal rejected the request."
        client_status = exc.status_code if 400 <= exc.status_code < 500 else 502
        raise PayPalError(client_status, str(message), issues=issues) from exc
    if isinstance(exc, ValidationError):
        # Decode failure (2xx drift) or a body we built wrong: outcome unknown.
        log.exception("PayPal response could not be read on %s", op)
        raise PayPalError(
            502, "PayPal sent a response we could not read; the outcome is unknown."
        ) from exc
    if isinstance(exc, httpx.HTTPError):
        log.exception("PayPal unreachable on %s", op)
        raise PayPalError(
            502, "PayPal is currently unreachable; the outcome is unknown."
        ) from exc
    raise exc


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def money_str(value):
    """Format a Decimal/number as a 2dp currency string (e.g. ``"24.99"``)."""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _amount_of(money):
    """Extract a Decimal from an SDK ``Money`` (or None)."""
    if not money or money is UNSET:
        return None
    val = getattr(money, "value", None)
    if not val or val is UNSET:
        return None
    return Decimal(str(val))


def _card_request(card, currency):
    """Build a one-off ``CardRequest`` from validated shopper input."""
    kwargs = {
        "number": card["number"],
        "expiry": card["expiry"],
    }
    if card.get("security_code"):
        kwargs["security_code"] = card["security_code"]
    if card.get("name"):
        kwargs["name"] = card["name"]
    return CardRequest(**kwargs)


# --------------------------------------------------------------------------- #
# Order authorization (create_order auto-authorizes for a direct card)
# --------------------------------------------------------------------------- #
class ChallengeRequired(PayPalError):
    """PayPal wants the shopper to approve in a browser (3DS/redirect)."""

    def __init__(self):
        super().__init__(
            422,
            "PayPal requires the shopper to approve this payment in a browser; "
            "a browser approval step is required and is not supported by this API.",
        )


def create_authorized_order(*, currency, amount, order_number, card=None, vault_id=None,
                            request_id):
    """Create a PayPal order (intent AUTHORIZE) paid by card and return the SDK
    ``Order``. For a direct card the authorization is created at the same time.
    """
    client = get_client()
    if vault_id:
        source = PaymentSource(card=CardRequest(vault_id=vault_id))
    else:
        source = PaymentSource(card=_card_request(card, currency))
    body = OrderRequest(
        intent="AUTHORIZE",
        purchase_units=[
            PurchaseUnitRequest(
                amount=AmountWithBreakdown(
                    currency_code=currency, value=money_str(amount)
                ),
                custom_id=str(order_number),
            )
        ],
        payment_source=source,
    )
    try:
        order = client.orders.create_order(
            body=body, pay_pal_request_id=request_id, prefer=_REPRESENTATION
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as PayPalError
        _translate(exc, "create_order")
    return order


def extract_authorization(order):
    """Return the first authorization (or raise a clear error).

    Detects the browser-challenge case (no authorization + PAYER_ACTION_REQUIRED
    or a payer-action link) and reports it rather than inventing a round-trip.
    """
    pus = getattr(order, "purchase_units", None)
    if pus and pus is not UNSET:
        payments = getattr(pus[0], "payments", None)
        if payments and payments is not UNSET:
            auths = getattr(payments, "authorizations", None)
            if auths and auths is not UNSET and len(auths) > 0:
                return auths[0]
    # No authorization present — is the shopper being asked to approve?
    status = getattr(order, "status", None)
    if status == "PAYER_ACTION_REQUIRED":
        raise ChallengeRequired()
    links = getattr(order, "links", None) or []
    if links is not UNSET:
        for link in links:
            if getattr(link, "rel", None) in ("payer-action", "approve"):
                raise ChallengeRequired()
    raise PayPalError(
        502, "PayPal did not return a payment authorization; the outcome is unknown."
    )


def capture_authorization(authorization_id, request_id):
    client = get_client()
    try:
        cap = client.payments.capture_authorized_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=_REPRESENTATION
        )
    except Exception as exc:  # noqa: BLE001
        _translate(exc, "capture_authorized_payment")
    if not getattr(cap, "id", None) or cap.id is UNSET:
        raise PayPalError(502, "PayPal capture returned no id; the outcome is unknown.")
    return cap


def reauthorize(authorization_id, request_id):
    client = get_client()
    try:
        return client.payments.reauthorize_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=_REPRESENTATION
        )
    except Exception as exc:  # noqa: BLE001
        _translate(exc, "reauthorize_payment")


def void_authorization(authorization_id):
    client = get_client()
    try:
        # Must request a representation: the default (return=minimal) is a 204
        # with an empty body, which the SDK cannot decode.
        return client.payments.void_payment(
            authorization_id, prefer=_REPRESENTATION
        )
    except Exception as exc:  # noqa: BLE001
        _translate(exc, "void_payment")


def refund_capture(capture_id, *, currency, amount, request_id):
    client = get_client()
    body = RefundRequest(amount=Money(currency_code=currency, value=money_str(amount)))
    try:
        refund = client.payments.refund_captured_payment(
            capture_id, pay_pal_request_id=request_id, body=body,
            prefer=_REPRESENTATION,
        )
    except Exception as exc:  # noqa: BLE001
        _translate(exc, "refund_captured_payment")
    if not getattr(refund, "id", None) or refund.id is UNSET:
        raise PayPalError(502, "PayPal refund returned no id; the outcome is unknown.")
    return refund


# --------------------------------------------------------------------------- #
# Vault (saved cards)
# --------------------------------------------------------------------------- #
def vault_card(*, card, customer_id=None, request_id):
    """Vault a card: create a setup token then a payment token.

    Returns the SDK ``PaymentTokenResponse``.
    """
    client = get_client()
    card_kwargs = {"number": card["number"], "expiry": card["expiry"]}
    if card.get("security_code"):
        card_kwargs["security_code"] = card["security_code"]
    if card.get("name"):
        card_kwargs["name"] = card["name"]
    setup_card = SetupTokenRequestCard(**card_kwargs)
    setup_kwargs = {
        "payment_source": SetupTokenRequestPaymentSource(card=setup_card),
    }
    if customer_id:
        setup_kwargs["customer"] = Customer(id=customer_id)
    try:
        setup = client.vault.create_setup_token(
            body=SetupTokenRequest(**setup_kwargs), pay_pal_request_id=request_id
        )
    except Exception as exc:  # noqa: BLE001
        _translate(exc, "create_setup_token")
    if not getattr(setup, "id", None) or setup.id is UNSET:
        raise PayPalError(502, "PayPal setup token has no id; the outcome is unknown.")

    token_kwargs = {
        "payment_source": PaymentTokenRequestPaymentSource(
            token=VaultTokenRequest(id=setup.id, type_="SETUP_TOKEN")
        )
    }
    if customer_id:
        token_kwargs["customer"] = Customer(id=customer_id)
    try:
        token = client.vault.create_payment_token(
            body=PaymentTokenRequest(**token_kwargs), pay_pal_request_id=request_id + "-t"
        )
    except Exception as exc:  # noqa: BLE001
        _translate(exc, "create_payment_token")
    if not getattr(token, "id", None) or token.id is UNSET:
        raise PayPalError(502, "PayPal payment token has no id; the outcome is unknown.")
    return token


def delete_vault_token(token_id):
    """Delete a vaulted token. Returns True when PayPal accepted the delete."""
    client = get_client()
    try:
        result = client.vault.with_raw_response.delete_payment_token(token_id)
    except Exception as exc:  # noqa: BLE001
        _translate(exc, "delete_payment_token")
    return result.response.status_code in (200, 204)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def search_transactions_page(start_date, end_date, page):
    client = get_client()
    try:
        return client.transaction_search.search_transactions(
            start_date, end_date, fields="all", page_size=100, page=page
        )
    except Exception as exc:  # noqa: BLE001
        _translate(exc, "search_transactions")
