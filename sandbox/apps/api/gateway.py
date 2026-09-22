"""The single boundary between this app and PayPal.

Every PayPal interaction goes through the :class:`PayPalGateway` here; nothing else in
the app imports the ``paypal`` SDK. The gateway owns one long-lived, lazily-built
``PaypalClient`` (sync, because the sandbox runs under WSGI) and translates every SDK
failure mode into a single :class:`PayPalError` so callers have one exception type.

Facts encoded here come from ``pay-pal-server-sdk-plan.md`` (verified against the live
sandbox), not from memory:

* card + ``intent=AUTHORIZE`` ``create_order`` auto-authorizes in one call;
* a ``payment_source`` requires the ``PayPal-Request-Id`` header (our idempotency key);
* ``prefer="return=representation"`` is mandatory on create/capture/void/refund/reauthorize,
  or a 204/empty body makes the SDK raise ``ValueError`` on decode (bypassing both
  response modes) instead of returning the body we need.
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from pydantic import ValidationError

from paypal import PaypalClient
from paypal.core import UNSET, ApiError, ClientCredentials, OAuthProviderError, RawError
from paypal.models import (
    Error,
    OrderRequestDict,
    PaymentTokenRequestDict,
    ReauthorizeRequestDict,
    RefundRequestDict,
)


def _v(value):
    """Resolve the SDK's UNSET sentinel to None so values can cross this boundary."""
    return None if value is UNSET else value

logger = logging.getLogger("apps.api.paypal")

# PayPal returns and expects amounts as strings scaled to the currency. USD (the sandbox
# default) has two decimal places; take the scale from the currency rather than hardcoding
# 2 (JPY has 0, KWD has 3). This app only ever configures one currency at a time.
_ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW", "VND", "CLP", "ISK", "HUF", "TWD"}
_THREE_DECIMAL_CURRENCIES = {"BHD", "KWD", "OMR", "TND"}


def currency_exponent(currency: str) -> int:
    if currency in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if currency in _THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def format_amount(amount: Decimal, currency: str) -> str:
    """Format a Decimal to the string PayPal expects for ``currency``, to the cent."""
    exp = currency_exponent(currency)
    quant = Decimal(1) if exp == 0 else Decimal(1).scaleb(-exp)
    return str(Decimal(amount).quantize(quant))


class PayPalError(Exception):
    """A single failure type at this app's boundary.

    ``status`` is the HTTP status this app should answer with. ``code`` and ``message``
    are safe to show a caller; ``detail`` is for logs only.
    """

    def __init__(self, status: int, code: str, message: str, *, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class PayPalGateway:
    def __init__(self) -> None:
        self._client: PaypalClient | None = None
        self._lock = threading.Lock()

    # -- client lifetime -------------------------------------------------------------
    @property
    def client(self) -> PaypalClient:
        # Double-checked lazy singleton: one client for the process lifetime, reused
        # across requests (never per-request — that re-fetches the OAuth token each time).
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = self._build_client()
        return self._client

    @staticmethod
    def _build_client() -> PaypalClient:
        client_id = getattr(settings, "PAYPAL_CLIENT_ID", None)
        client_secret = getattr(settings, "PAYPAL_CLIENT_SECRET", None)
        if not client_id or not client_secret:
            raise PayPalError(
                503, "paypal_unconfigured",
                "PayPal credentials are not configured on the server.",
            )
        # PAYPAL_BASE_URL is an optional verbatim override for EVERY call incl. the token
        # request; when unset we pass None so the SDK uses its sandbox default.
        base_url = getattr(settings, "PAYPAL_BASE_URL", None) or None
        return PaypalClient(
            oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
            base_url=base_url,
            timeout=float(getattr(settings, "PAYPAL_TIMEOUT", 30.0)),
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- error translation -----------------------------------------------------------
    def _translate(self, op: str, e: Exception) -> PayPalError:
        """Map every SDK failure kind to one PayPalError. Order matters: auth first."""
        if isinstance(e, ApiError):
            err = e.error
            # A failed token fetch surfaces here with an OAuth payload, not the op's union.
            if isinstance(err, OAuthProviderError):
                logger.error("PayPal auth failed on %s: %s", op, getattr(err, "error", err))
                return PayPalError(502, "paypal_auth_failed",
                                   "The server could not authenticate with PayPal.")
            if isinstance(err, Error):
                # A documented, typed provider rejection: actionable 4xx-shaped detail.
                issues: list[str] = []
                for d in (_v(err.details) or []):
                    issue = _v(getattr(d, "issue", None))
                    desc = _v(getattr(d, "description", None))
                    if issue or desc:
                        issues.append(f"{issue}: {desc}" if issue and desc else str(issue or desc))
                logger.warning("PayPal rejected %s [%s]: %s %s",
                               op, e.status_code, err.name, issues)
                status = e.status_code if 400 <= (e.status_code or 0) < 500 else 502
                message = err.message or "PayPal rejected the request."
                if issues:
                    message = f"{message} ({'; '.join(issues)})"
                return PayPalError(status, "paypal_rejected", message,
                                   detail={"name": err.name, "debug_id": err.debug_id})
            # RawError arm: an undocumented status. Rejection detail lost, not an outage.
            raw = err if isinstance(err, RawError) else None
            body = raw.text() if raw is not None else str(e)
            logger.error("PayPal raw error on %s [%s]: %s", op, e.status_code, body)
            return PayPalError(502, "paypal_error",
                               "PayPal returned an unexpected error.", detail=body)
        if isinstance(e, ValidationError):
            # Decode failure: bypasses both response modes. Outcome unknown for a write.
            logger.error("PayPal response for %s was unreadable: %s", op, e)
            return PayPalError(502, "paypal_unreadable",
                               "PayPal returned an unreadable response.")
        if isinstance(e, ValueError):
            logger.error("PayPal response for %s could not be decoded: %s", op, e)
            return PayPalError(502, "paypal_unreadable",
                               "PayPal returned an unreadable response.")
        if isinstance(e, httpx.HTTPError):
            logger.error("PayPal unreachable on %s: %s", op, e)
            return PayPalError(503, "paypal_unavailable",
                               "PayPal could not be reached. Please try again.")
        raise e  # a genuine programming error — do not swallow

    # -- operation wrappers (all with prefer=return=representation where a body matters)
    def create_authorized_order(self, *, amount: str, currency: str, custom_id: str,
                                invoice_id: str, payment_source: dict, request_id: str):
        """Create + auto-authorize a PayPal order for a direct/vaulted card.

        Returns the SDK ``Order``; the authorization lives at
        ``order.purchase_units[0].payments.authorizations[0]``. ``custom_id`` carries the
        human order number (for matching); ``invoice_id`` is the globally-unique reference.
        """
        body: OrderRequestDict = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "amount": {"currency_code": currency, "value": amount},
                "custom_id": custom_id,
                "invoice_id": invoice_id,
                "description": f"Order {custom_id}",
            }],
            "payment_source": payment_source,  # type: ignore[typeddict-item]
        }
        try:
            return self.client.orders.create_order(
                body, pay_pal_request_id=request_id, prefer="return=representation")
        except Exception as e:  # noqa: BLE001 - funneled into one type
            raise self._translate("create_order", e)

    def get_authorization(self, authorization_id: str):
        try:
            return self.client.payments.get_authorized_payment(authorization_id)
        except Exception as e:  # noqa: BLE001
            raise self._translate("get_authorized_payment", e)

    def reauthorize(self, authorization_id: str, *, amount: str, currency: str, request_id: str):
        body: ReauthorizeRequestDict = {"amount": {"currency_code": currency, "value": amount}}
        try:
            return self.client.payments.reauthorize_payment(
                authorization_id, pay_pal_request_id=request_id, prefer="return=representation",
                body=body)
        except Exception as e:  # noqa: BLE001
            raise self._translate("reauthorize_payment", e)

    def capture(self, authorization_id: str, *, request_id: str):
        try:
            return self.client.payments.capture_authorized_payment(
                authorization_id, pay_pal_request_id=request_id, prefer="return=representation",
                body={"final_capture": True})
        except Exception as e:  # noqa: BLE001
            raise self._translate("capture_authorized_payment", e)

    def void(self, authorization_id: str):
        try:
            return self.client.payments.void_payment(
                authorization_id, prefer="return=representation")
        except Exception as e:  # noqa: BLE001
            raise self._translate("void_payment", e)

    def refund(self, capture_id: str, *, amount: str | None, currency: str, request_id: str):
        body: RefundRequestDict | None = None
        if amount is not None:
            body = {"amount": {"currency_code": currency, "value": amount}}
        try:
            return self.client.payments.refund_captured_payment(
                capture_id, pay_pal_request_id=request_id, prefer="return=representation",
                body=body)
        except Exception as e:  # noqa: BLE001
            raise self._translate("refund_captured_payment", e)

    def create_payment_token(self, *, card: dict, customer_id: str | None, request_id: str):
        body: PaymentTokenRequestDict = {
            "payment_source": {"card": card},  # type: ignore[typeddict-item]
        }
        if customer_id:
            body["customer"] = {"id": customer_id}
        try:
            return self.client.vault.create_payment_token(body, pay_pal_request_id=request_id)
        except Exception as e:  # noqa: BLE001
            raise self._translate("create_payment_token", e)

    def delete_payment_token(self, token_id: str) -> None:
        try:
            self.client.vault.delete_payment_token(token_id)
        except Exception as e:  # noqa: BLE001
            raise self._translate("delete_payment_token", e)

    def search_transactions_page(self, *, start_date: str, end_date: str, page: int,
                                 page_size: int = 100) -> dict:
        """One page of PayPal's transaction report, as plain dicts (no SDK types leak out).

        Returns ``{"transactions": [...], "total_pages": int, "page": int}``.
        """
        try:
            resp = self.client.transaction_search.search_transactions(
                start_date, end_date, fields="transaction_info",
                page=page, page_size=page_size)
        except Exception as e:  # noqa: BLE001
            raise self._translate("search_transactions", e)

        transactions = []
        for detail in (_v(getattr(resp, "transaction_details", None)) or []):
            info = _v(getattr(detail, "transaction_info", None))
            if info is None:
                continue
            amount = _v(getattr(info, "transaction_amount", None))
            amount_str = None
            if amount is not None:
                value = _v(getattr(amount, "value", None))
                code = _v(getattr(amount, "currency_code", None))
                amount_str = f"{value} {code}".strip() if value is not None else None
            transactions.append({
                "transaction_id": _v(getattr(info, "transaction_id", None)),
                "reference": _v(getattr(info, "invoice_id", None))
                or _v(getattr(info, "custom_field", None)),
                "amount": amount_str,
                "status": _v(getattr(info, "transaction_status", None)),
                "date": _v(getattr(info, "transaction_initiation_date", None)),
            })
        return {
            "transactions": transactions,
            "total_pages": _v(getattr(resp, "total_pages", None)) or 1,
            "page": _v(getattr(resp, "page", None)) or page,
        }


# Module-level singleton (WSGI): built once, reused for the process lifetime.
gateway = PayPalGateway()
