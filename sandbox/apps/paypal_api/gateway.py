"""Thin wrapper over the PayPal SDK.

One method per PayPal operation this integration needs. Every call goes through
a single error boundary that translates the four SDK failure kinds into the
domain exceptions in :mod:`.exceptions`:

* ``ApiError`` with ``OAuthProviderError``  -> ``PaymentConfigError`` (nothing sent)
* ``ApiError`` with ``Error`` (typed 4xx)   -> ``PaymentRejected`` / ``PaymentConflict``
* ``ApiError`` with ``RawError`` (5xx/other) -> ``PaymentProviderError``
* ``pydantic.ValidationError`` (decode)      -> ``PaymentUnreadable`` (outcome unknown)
* ``httpx.HTTPError`` (transport)            -> ``PaymentUnavailable`` (outcome unknown)

Nothing here logs card data or ``str(ApiError)``. Card numbers/CVV are passed
straight to the SDK and never persisted or logged.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx
from pydantic import ValidationError

from paypal.core import ApiError, OAuthProviderError, RawError, UNSET
from paypal.models import Error

from . import money
from .exceptions import (
    ChallengeRequired,
    PaymentConfigError,
    PaymentConflict,
    PaymentProviderError,
    PaymentRejected,
    PaymentUnavailable,
    PaymentUnreadable,
    ReauthorizationNeeded,
)
from .paypal_client import get_client

logger = logging.getLogger("paypal_api")

_REPRESENTATION = "return=representation"


def _is_set(value: Any) -> bool:
    return value is not UNSET and value is not None


def _opt(value: Any) -> Any:
    """Return the value if present (not UNSET/None), else None — typed ``Any``
    so a following iteration/int() does not trip the ``X | UnsetType`` union."""
    return value if (value is not UNSET and value is not None) else None


def _money_to_decimal(m: Any) -> Decimal | None:
    """Read a PayPal ``Money`` model into a Decimal, or None if absent."""
    if not _is_set(m):
        return None
    value = getattr(m, "value", UNSET)
    currency = getattr(m, "currency_code", UNSET)
    if not _is_set(value):
        return None
    return money.parse_amount(str(value), str(currency) if _is_set(currency) else "USD")


def _issues(error: Error) -> list[dict]:
    out = []
    details = _opt(getattr(error, "details", UNSET))
    if details:
        for d in details:
            out.append(
                {
                    "issue": str(getattr(d, "issue", "") or ""),
                    "description": str(getattr(d, "description", "") or ""),
                }
            )
    return out


def _first_message(error: Error) -> str:
    issues = _issues(error)
    if issues:
        return issues[0]["issue"] or issues[0]["description"] or "Payment was rejected"
    msg = getattr(error, "message", UNSET)
    return str(msg) if _is_set(msg) else "Payment was rejected"


def _looks_expired(error: Error) -> bool:
    for i in _issues(error):
        issue = i["issue"].upper()
        if "EXPIR" in issue or issue in {
            "AUTHORIZATION_EXPIRED",
            "PAYMENT_AUTHORIZATION_EXPIRED",
        }:
            return True
    return False


class _Boundary:
    """Context manager translating SDK failures for one logical operation."""

    def __init__(self, op: str, *, on_error=None):
        self.op = op
        self.on_error = on_error  # optional callable(status, Error) -> raise/return

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        if isinstance(exc, ApiError):
            err = exc.error
            if isinstance(err, OAuthProviderError):
                logger.error("paypal auth failed on %s: %s", self.op, getattr(err, "error", ""))
                raise PaymentConfigError("PayPal credentials were rejected") from exc
            if isinstance(err, Error):
                if self.on_error is not None:
                    # Hook may raise a specialised exception (e.g. reauth needed).
                    self.on_error(exc.status_code, err)
                logger.warning(
                    "paypal rejected %s: HTTP %s %s", self.op, exc.status_code, _issues(err)
                )
                if exc.status_code == 409:
                    raise PaymentConflict(
                        _first_message(err), detail=_issues(err)
                    ) from exc
                raise PaymentRejected(
                    _first_message(err),
                    provider_status=exc.status_code,
                    detail=_issues(err),
                ) from exc
            # RawError arm
            raw = err if isinstance(err, RawError) else None
            body = raw.text()[:500] if raw is not None else ""
            logger.error("paypal provider error on %s: HTTP %s", self.op, exc.status_code)
            raise PaymentProviderError(
                f"PayPal returned an unexpected error (HTTP {exc.status_code})",
                detail=body,
            ) from exc
        if isinstance(exc, ValidationError):
            logger.error("paypal response undecodable on %s", self.op)
            raise PaymentUnreadable(
                f"PayPal response for {self.op} could not be read; outcome unknown"
            ) from exc
        if isinstance(exc, httpx.HTTPError):
            logger.error("paypal transport failure on %s: %s", self.op, exc.__class__.__name__)
            raise PaymentUnavailable(
                f"PayPal was unreachable for {self.op}; outcome unknown"
            ) from exc
        return False  # re-raise anything else (programming errors)


class PayPalGateway:
    """All PayPal traffic for the integration."""

    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        return self._client or get_client()

    # ---- Orders / authorization ------------------------------------------
    def authorize(
        self,
        *,
        amount: Decimal,
        currency: str,
        order_number: str,
        request_id: str,
        card: dict | None = None,
        vault_id: str | None = None,
    ) -> dict:
        """Create a PayPal order with ``intent=AUTHORIZE`` and a card source.

        A direct card (or vaulted card) auto-authorizes at create time, so we
        read the embedded authorization; if none is embedded and the order is
        merely ``APPROVED`` we authorize explicitly. Returns the paypal order
        id, authorization id and status.
        """
        if card is not None:
            source_card: dict = dict(card)
        elif vault_id is not None:
            source_card = {"vault_id": vault_id}
        else:  # pragma: no cover - guarded by the service layer
            raise ValueError("authorize requires either card or vault_id")

        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {
                    "amount": {
                        "currency_code": currency,
                        "value": money.format_amount(amount, currency),
                    },
                    "custom_id": order_number,
                    "invoice_id": order_number,
                }
            ],
            "payment_source": {"card": source_card},
        }

        with _Boundary("create_order"):
            order = self.client.orders.create_order(
                body, pay_pal_request_id=request_id, prefer=_REPRESENTATION
            )

        paypal_order_id = getattr(order, "id", UNSET)
        if not _is_set(paypal_order_id):
            raise PaymentUnreadable("PayPal did not return an order id; outcome unknown")

        authz = self._extract_authorization(order)
        if authz is None:
            status = str(getattr(order, "status", "") or "")
            if self._needs_browser_approval(order):
                raise ChallengeRequired(
                    "PayPal requires the shopper to approve this payment in a browser"
                )
            if status == "APPROVED":
                with _Boundary("authorize_order"):
                    resp = self.client.orders.authorize_order(
                        str(paypal_order_id), prefer=_REPRESENTATION
                    )
                authz = self._extract_authorization(resp)
            if authz is None:
                raise PaymentUnreadable(
                    f"PayPal order {paypal_order_id} carried no authorization "
                    f"(status {status}); outcome unknown"
                )

        return {
            "paypal_order_id": str(paypal_order_id),
            "authorization_id": authz["id"],
            "authorization_status": authz["status"],
        }

    def _extract_authorization(self, order_like) -> dict | None:
        units = _opt(getattr(order_like, "purchase_units", UNSET))
        if not units:
            return None
        for unit in units:
            payments = getattr(unit, "payments", UNSET)
            if not _is_set(payments):
                continue
            auths = _opt(getattr(payments, "authorizations", UNSET))
            if not auths:
                continue
            for a in auths:
                aid = getattr(a, "id", UNSET)
                if _is_set(aid):
                    return {"id": str(aid), "status": str(getattr(a, "status", "") or "")}
        return None

    def _needs_browser_approval(self, order_like) -> bool:
        status = str(getattr(order_like, "status", "") or "")
        if status in {"PAYER_ACTION_REQUIRED"}:
            return True
        links = _opt(getattr(order_like, "links", UNSET))
        if links:
            for link in links:
                rel = str(getattr(link, "rel", "") or "")
                if rel in {"payer-action", "approve"}:
                    return True
        return False

    def get_authorization_status(self, authorization_id: str) -> str:
        with _Boundary("get_authorized_payment"):
            auth = self.client.payments.get_authorized_payment(authorization_id)
        return str(getattr(auth, "status", "") or "")

    def reauthorize(self, authorization_id: str, *, request_id: str) -> dict:
        with _Boundary("reauthorize_payment"):
            auth = self.client.payments.reauthorize_payment(
                authorization_id, pay_pal_request_id=request_id, prefer=_REPRESENTATION
            )
        new_id = getattr(auth, "id", UNSET)
        if not _is_set(new_id):
            raise PaymentUnreadable("Reauthorization returned no id; outcome unknown")
        return {"authorization_id": str(new_id), "status": str(getattr(auth, "status", "") or "")}

    # ---- Capture ----------------------------------------------------------
    def capture(self, authorization_id: str, *, request_id: str) -> dict:
        """Capture an authorization. Raises ``ReauthorizationNeeded`` when the
        authorization has gone stale so the caller can renew it."""

        def _hook(status, err: Error):
            if status in (404, 422) and _looks_expired(err):
                raise ReauthorizationNeeded()

        with _Boundary("capture_authorized_payment", on_error=_hook):
            cap = self.client.payments.capture_authorized_payment(
                authorization_id,
                body={"final_capture": True},
                pay_pal_request_id=request_id,
                prefer=_REPRESENTATION,
            )
        cap_id = getattr(cap, "id", UNSET)
        if not _is_set(cap_id):
            raise PaymentUnreadable("Capture returned no id; outcome unknown")

        gross = fee = net = None
        srb = getattr(cap, "seller_receivable_breakdown", UNSET)
        if _is_set(srb):
            gross = _money_to_decimal(getattr(srb, "gross_amount", UNSET))
            fee = _money_to_decimal(getattr(srb, "paypal_fee", UNSET))
            net = _money_to_decimal(getattr(srb, "net_amount", UNSET))
        return {
            "capture_id": str(cap_id),
            "status": str(getattr(cap, "status", "") or ""),
            "gross_amount": gross,
            "paypal_fee": fee,
            "net_amount": net,
        }

    # ---- Void (cancel before fulfilment) ---------------------------------
    def void(self, authorization_id: str, *, request_id: str) -> str:
        """Void (release) an authorization. Returns the resulting status.

        Uses the raw peer so the 2xx status is observable; ``prefer=
        representation`` avoids the empty-204 decode failure.
        """
        with _Boundary("void_payment"):
            result = self.client.payments.with_raw_response.void_payment(
                authorization_id,
                pay_pal_request_id=request_id,
                prefer=_REPRESENTATION,
            )
        # Success (2xx) either way; body may carry the voided authorization.
        payload = getattr(result, "payload", None)
        if payload is not None and _is_set(getattr(payload, "status", UNSET)):
            return str(payload.status)
        return "VOIDED"

    # ---- Refund (after fulfilment) ---------------------------------------
    def refund(
        self,
        capture_id: str,
        *,
        amount: Decimal | None,
        currency: str,
        request_id: str,
        order_number: str,
    ) -> dict:
        body: dict = {"custom_id": order_number, "invoice_id": order_number}
        if amount is not None:
            body["amount"] = {
                "currency_code": currency,
                "value": money.format_amount(amount, currency),
            }
        with _Boundary("refund_captured_payment"):
            ref = self.client.payments.refund_captured_payment(
                capture_id,
                body=body,
                pay_pal_request_id=request_id,
                prefer=_REPRESENTATION,
            )
        ref_id = getattr(ref, "id", UNSET)
        if not _is_set(ref_id):
            raise PaymentUnreadable("Refund returned no id; outcome unknown")
        return {"refund_id": str(ref_id), "status": str(getattr(ref, "status", "") or "")}

    # ---- Vault (saved cards) ---------------------------------------------
    def vault_card(self, *, card: dict, request_id: str) -> dict:
        body = {"payment_source": {"card": card}}
        with _Boundary("create_payment_token"):
            token = self.client.vault.create_payment_token(body, pay_pal_request_id=request_id)
        token_id = getattr(token, "id", UNSET)
        if not _is_set(token_id):
            raise PaymentUnreadable("Vaulting returned no token id; outcome unknown")
        brand = last4 = expiry = ""
        source = getattr(token, "payment_source", UNSET)
        if _is_set(source):
            card_ent = getattr(source, "card", UNSET)
            if _is_set(card_ent):
                brand = str(getattr(card_ent, "brand", "") or "")
                last4 = str(getattr(card_ent, "last_digits", "") or "")
                expiry = str(getattr(card_ent, "expiry", "") or "")
        return {"vault_id": str(token_id), "brand": brand, "last4": last4, "expiry": expiry}

    def delete_vault_card(self, vault_id: str) -> bool:
        """Delete a vaulted card. Returns True if gone (204) or already absent."""
        with _Boundary("delete_payment_token"):
            result = self.client.vault.with_raw_response.delete_payment_token(vault_id)
        status = getattr(getattr(result, "response", None), "status_code", None)
        # Success path only reaches here; 404 would raise -> PaymentRejected,
        # which the service treats as already-gone.
        return status in (200, 204, None)

    # ---- Reconciliation ---------------------------------------------------
    def search_transactions(self, start_date: str, end_date: str, *, max_pages: int = 100) -> dict:
        """Return every transaction PayPal reports in [start, end].

        Paginates all pages with a hard backstop; the result carries a
        ``truncated`` flag if the backstop trips.
        """
        transactions: list[dict] = []
        page = 1
        total_pages = 1
        truncated = False
        while True:
            with _Boundary("search_transactions"):
                resp = self.client.transaction_search.search_transactions(
                    start_date, end_date, page_size=500, page=page
                )
            tp = _opt(getattr(resp, "total_pages", UNSET))
            if tp is not None:
                total_pages = int(tp)
            details = _opt(getattr(resp, "transaction_details", UNSET))
            if details:
                for d in details:
                    info = getattr(d, "transaction_info", UNSET)
                    if not _is_set(info):
                        continue
                    transactions.append(self._normalize_txn(info))
            if page >= total_pages:
                break
            if page >= max_pages:
                truncated = True
                break
            page += 1
        return {"transactions": transactions, "pages": min(page, total_pages), "truncated": truncated}

    def _normalize_txn(self, info) -> dict:
        def _s(name):
            v = getattr(info, name, UNSET)
            return str(v) if _is_set(v) else None

        amount = _money_to_decimal(getattr(info, "transaction_amount", UNSET))
        fee = _money_to_decimal(getattr(info, "fee_amount", UNSET))
        return {
            "transactionId": _s("transaction_id"),
            "status": _s("transaction_status"),
            "eventCode": _s("transaction_event_code"),
            "initiationDate": _s("transaction_initiation_date"),
            "amount": str(amount) if amount is not None else None,
            "fee": str(fee) if fee is not None else None,
            "invoiceId": _s("invoice_id"),
            "customField": _s("custom_field"),
        }
