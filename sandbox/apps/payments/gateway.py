"""The single boundary between this app and the PayPal SDK.

Every PayPal interaction goes through here. Nothing outside this module imports the
SDK. Calls return plain Python data (dicts / ``None``); failures are translated into
one exception type, :class:`PayPalError`, carrying an HTTP status the API layer can
map and a message an operator can act on.

Contract facts (signatures, wire aliases, error unions, idempotency behaviour) come
from ``pay-pal-server-sdk-plan.md`` at the repo root, grounded in the SDK map and a
live sandbox smoke -- never from memory.
"""

import logging
import threading
from decimal import Decimal

import httpx
from django.conf import settings
from django.utils.dateparse import parse_datetime
from pydantic import ValidationError

from paypal import PaypalClient
from paypal.core import UNSET, ApiError, ClientCredentials, OAuthProviderError, RawError
from paypal.models import (
    Address,
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

logger = logging.getLogger("sandbox.payments")

_client = None
_client_lock = threading.Lock()


class PayPalError(Exception):
    """A PayPal failure translated for this app's API boundary.

    ``status_code`` is a sensible HTTP status for the caller. ``outcome_unknown``
    marks failures where the request may have taken effect but the result could not
    be read (transport drop, undecodable 2xx) -- the caller must not assume failure.
    """

    def __init__(self, message, status_code=502, *, issues=None, outcome_unknown=False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.issues = issues or []
        self.outcome_unknown = outcome_unknown


def get_client():
    """Return the long-lived, module-scoped SDK client (built lazily, once)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                base_url = settings.PAYPAL_BASE_URL or None
                if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
                    raise PayPalError(
                        "PayPal credentials are not configured on the server.",
                        status_code=503,
                    )
                _client = PaypalClient(
                    oauth2=ClientCredentials(
                        client_id=settings.PAYPAL_CLIENT_ID,
                        client_secret=settings.PAYPAL_CLIENT_SECRET,
                    ),
                    base_url=base_url,
                    timeout=60.0,
                )
    return _client


def reset_client():
    """Drop the cached client (used by tests that swap credentials/transport)."""
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # pragma: no cover
                pass
        _client = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _v(value):
    """Map the SDK's UNSET sentinel to None so values are safe to hand out."""
    return None if value is UNSET else value


def money_str(amount):
    """Format a Decimal as a 2-decimal string PayPal accepts."""
    return f"{Decimal(amount):.2f}"


def _issues_from(error):
    """Pull operator-facing issue codes out of a typed PayPal Error body."""
    issues = []
    details = _v(getattr(error, "details", UNSET))
    if details:
        for d in details:
            issue = _v(getattr(d, "issue", UNSET))
            desc = _v(getattr(d, "description", UNSET))
            if issue:
                issues.append(f"{issue}: {desc}" if desc else issue)
    return issues


def _translate(exc, operation):
    """Translate an SDK/transport exception into a PayPalError. Reordered per the
    error-handling ladder: auth (config) -> typed -> raw -> transport."""
    if isinstance(exc, ApiError):
        error = exc.error
        status = exc.status_code
        if isinstance(error, OAuthProviderError):
            logger.error("PayPal credentials rejected during %s", operation)
            return PayPalError(
                "PayPal credentials were rejected by the provider.", status_code=502
            )
        if isinstance(error, RawError):
            return PayPalError(
                f"PayPal returned an unexpected error during {operation}.",
                status_code=502 if status >= 500 else status,
            )
        # Typed Error body.
        issues = _issues_from(error)
        message = _v(getattr(error, "message", UNSET)) or "PayPal rejected the request."
        # A provider 4xx is the caller's fault; 5xx is the provider's.
        client_status = status if 400 <= status < 500 else 502
        return PayPalError(
            f"{operation} was rejected by PayPal: {message}",
            status_code=client_status,
            issues=issues,
        )
    if isinstance(exc, (ValidationError, ValueError)):
        return PayPalError(
            f"PayPal sent an unreadable response during {operation}; the outcome is unknown.",
            status_code=502,
            outcome_unknown=True,
        )
    if isinstance(exc, httpx.HTTPError):
        return PayPalError(
            f"PayPal was unreachable during {operation}; the outcome is unknown.",
            status_code=504,
            outcome_unknown=True,
        )
    raise exc


def _address(billing):
    if not billing:
        return UNSET
    return Address(
        address_line_1=billing.get("address_line_1", UNSET) or UNSET,
        address_line_2=billing.get("address_line_2", UNSET) or UNSET,
        admin_area_2=billing.get("admin_area_2", UNSET) or UNSET,
        admin_area_1=billing.get("admin_area_1", UNSET) or UNSET,
        postal_code=billing.get("postal_code", UNSET) or UNSET,
        country_code=billing.get("country_code", UNSET) or UNSET,
    )


def _first_authorization(order):
    for pu in _v(order.purchase_units) or []:
        payments = _v(pu.payments)
        if payments and _v(payments.authorizations):
            return payments.authorizations[0]
    return None


# ---------------------------------------------------------------------------
# Flow 1 -- pay / fulfil / cancel / refund
# ---------------------------------------------------------------------------


def create_authorized_order(
    *, amount, currency, invoice_id, custom_id, request_id, card=None, vault_id=None
):
    """Create a PayPal order with intent AUTHORIZE and process the card, which places
    a hold for ``amount``. Returns the order and authorization identifiers/status.

    ``card`` is a dict of raw card fields (one-off payment); ``vault_id`` names a
    saved card. Exactly one must be given. The hold equals ``amount`` to the cent.
    """
    client = get_client()
    if vault_id:
        card_request = CardRequest(vault_id=vault_id)
    else:
        card_request = CardRequest(
            name=card.get("name", UNSET) or UNSET,
            number=card["number"],
            expiry=card["expiry"],
            security_code=card.get("security_code", UNSET) or UNSET,
            billing_address=_address(card.get("billing_address")),
        )
    body = OrderRequest(
        intent="AUTHORIZE",
        purchase_units=[
            PurchaseUnitRequest(
                amount=AmountWithBreakdown(
                    currency_code=currency, value=money_str(amount)
                ),
                invoice_id=invoice_id,
                custom_id=custom_id,
            )
        ],
        payment_source=PaymentSource(card=card_request),
    )
    try:
        order = client.orders.create_order(
            body, pay_pal_request_id=request_id, prefer="return=representation"
        )
    except Exception as exc:  # noqa: BLE001 - translated below
        raise _translate(exc, "authorising the payment")

    order_status = _v(order.status)
    if order_status == "PAYER_ACTION_REQUIRED":
        raise PayPalError(
            "This card requires the shopper to approve the payment in a browser "
            "(3-D Secure challenge). Browser approval is not supported here.",
            status_code=409,
        )
    authorization = _first_authorization(order)
    if authorization is None or not _v(authorization.id):
        raise PayPalError(
            "PayPal did not authorize the payment (the card may have been declined).",
            status_code=402,
        )
    auth_status = _v(authorization.status)
    if auth_status in ("DENIED", "VOIDED"):
        raise PayPalError(
            f"PayPal declined the authorization (status {auth_status}).",
            status_code=402,
        )
    expiry = _v(authorization.expiration_time)
    return {
        "paypal_order_id": _v(order.id),
        "order_status": order_status,
        "authorization_id": _v(authorization.id),
        "authorization_status": auth_status,
        "authorization_expiry": parse_datetime(expiry) if expiry else None,
    }


def get_authorization(authorization_id):
    client = get_client()
    try:
        auth = client.payments.get_authorized_payment(authorization_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "reading the authorization")
    expiry = _v(auth.expiration_time)
    return {
        "status": _v(auth.status),
        "expiry": parse_datetime(expiry) if expiry else None,
    }


def reauthorize(*, authorization_id, amount, currency, request_id):
    """Renew a stale authorization so its funds are guaranteed again before capture."""
    client = get_client()
    from paypal.models import ReauthorizeRequest

    try:
        auth = client.payments.reauthorize_payment(
            authorization_id,
            body=ReauthorizeRequest(
                amount=Money(currency_code=currency, value=money_str(amount))
            ),
            pay_pal_request_id=request_id,
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "renewing the authorization")
    new_id = _v(auth.id) or authorization_id
    expiry = _v(auth.expiration_time)
    return {
        "authorization_id": new_id,
        "status": _v(auth.status),
        "expiry": parse_datetime(expiry) if expiry else None,
    }


def _breakdown(captured):
    srb = _v(captured.seller_receivable_breakdown)
    gross = fee = net = None
    if srb:
        if _v(srb.gross_amount):
            gross = Decimal(srb.gross_amount.value)
        if _v(srb.paypal_fee):
            fee = Decimal(srb.paypal_fee.value)
        if _v(srb.net_amount):
            net = Decimal(srb.net_amount.value)
    return gross, fee, net


def capture(*, authorization_id, request_id):
    """Capture an authorization -- this is where the money is actually taken.

    Returns the capture id/status and the money breakdown PayPal reported
    (captured amount, PayPal's fee, net proceeds).
    """
    client = get_client()
    try:
        captured = client.payments.capture_authorized_payment(
            authorization_id,
            pay_pal_request_id=request_id,
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "capturing the payment")
    capture_id = _v(captured.id)
    if not capture_id:
        raise PayPalError(
            "PayPal did not return a capture id; the outcome is unknown.",
            status_code=502,
            outcome_unknown=True,
        )
    gross, fee, net = _breakdown(captured)
    amount = _v(captured.amount)
    return {
        "capture_id": capture_id,
        "status": _v(captured.status),
        "gross_amount": gross if gross is not None else (Decimal(amount.value) if amount else None),
        "paypal_fee": fee,
        "net_amount": net,
        "currency": amount.currency_code if amount else None,
    }


def void(authorization_id):
    """Void (release) an authorization before capture, so no money ever moved.

    PayPal returns ``204 No Content`` unless a representation is requested; we ask
    for one so the SDK can decode a body. As a safety net, an empty-body decode
    failure is treated as success and confirmed by re-reading the authorization.
    """
    client = get_client()
    try:
        client.payments.void_payment(
            authorization_id, prefer="return=representation"
        )
        return {"status": "VOIDED"}
    except (ValidationError, ValueError):
        # Empty 204 body -> decode failure. The void almost certainly succeeded;
        # confirm by reading the authorization back.
        try:
            state = get_authorization(authorization_id)
        except PayPalError:
            return {"status": "VOIDED"}
        if state["status"] in ("VOIDED", None):
            return {"status": "VOIDED"}
        raise PayPalError(
            "PayPal did not confirm the void; the outcome is unknown.",
            status_code=502,
            outcome_unknown=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "cancelling the authorization")


def refund(*, capture_id, amount, currency, request_id, invoice_id=None, note=None):
    """Refund a captured payment, fully or partially. Idempotent on ``request_id``
    (a repeat under the same key returns the same refund at PayPal)."""
    client = get_client()
    body = RefundRequest(
        amount=Money(currency_code=currency, value=money_str(amount)),
        invoice_id=invoice_id or UNSET,
        note_to_payer=note or UNSET,
    )
    try:
        refunded = client.payments.refund_captured_payment(
            capture_id,
            body=body,
            pay_pal_request_id=request_id,
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "refunding the payment")
    refund_id = _v(refunded.id)
    if not refund_id:
        raise PayPalError(
            "PayPal did not return a refund id; the outcome is unknown.",
            status_code=502,
            outcome_unknown=True,
        )
    return {"refund_id": refund_id, "status": _v(refunded.status)}


# ---------------------------------------------------------------------------
# Flow 2 -- saved cards (vault)
# ---------------------------------------------------------------------------


def vault_card(*, merchant_customer_id, card, request_id):
    """Vault a card via the setup-token -> payment-token flow. Returns the vault id
    plus a safe descriptor (brand, last four digits, expiry). No PAN/CVV retained."""
    client = get_client()
    setup_body = SetupTokenRequest(
        customer=Customer(merchant_customer_id=merchant_customer_id),
        payment_source=SetupTokenRequestPaymentSource(
            card=SetupTokenRequestCard(
                name=card.get("name", UNSET) or UNSET,
                number=card["number"],
                expiry=card["expiry"],
                security_code=card.get("security_code", UNSET) or UNSET,
                billing_address=_address(card.get("billing_address")),
            )
        ),
    )
    try:
        setup = client.vault.create_setup_token(
            setup_body, pay_pal_request_id=f"{request_id}-setup"
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "saving the card")
    setup_id = _v(setup.id)
    if not setup_id:
        raise PayPalError(
            "PayPal did not return a setup token; the card was not saved.",
            status_code=502,
            outcome_unknown=True,
        )
    token_body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(
            token=VaultTokenRequest(id=setup_id, type_="SETUP_TOKEN")
        )
    )
    try:
        token = client.vault.create_payment_token(
            token_body, pay_pal_request_id=f"{request_id}-token"
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "saving the card")
    vault_id = _v(token.id)
    if not vault_id:
        raise PayPalError(
            "PayPal did not return a vault id; the card was not saved.",
            status_code=502,
            outcome_unknown=True,
        )
    customer = _v(token.customer)
    customer_id = _v(customer.id) if customer else ""
    brand = last_digits = expiry = name = ""
    source = _v(token.payment_source)
    if source and _v(source.card):
        c = source.card
        brand = _v(c.brand) or ""
        last_digits = _v(c.last_digits) or ""
        expiry = _v(c.expiry) or ""
        name = _v(c.name) or ""
    return {
        "vault_id": vault_id,
        "customer_id": customer_id or "",
        "brand": str(brand),
        "last_digits": last_digits,
        "expiry": expiry,
        "name": name,
    }


def delete_vault_token(vault_id):
    """Delete a vaulted payment token at PayPal (returns None / 204 on success)."""
    client = get_client()
    try:
        client.vault.delete_payment_token(vault_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "removing the saved card")


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def search_transactions_page(*, start_date, end_date, page, page_size=100):
    """Fetch one page of PayPal's transaction report. Returns (items, total_pages).

    ``items`` are dicts describing each transaction. Case B error union (RawError
    only), so failures translate through the raw arm.
    """
    client = get_client()
    try:
        result = client.transaction_search.search_transactions(
            start_date,
            end_date,
            fields="transaction_info",
            page_size=page_size,
            page=page,
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "reading PayPal transactions")
    items = []
    for td in _v(result.transaction_details) or []:
        info = _v(td.transaction_info)
        if not info:
            continue
        amount = _v(info.transaction_amount)
        fee = _v(info.fee_amount)
        items.append(
            {
                "transaction_id": _v(info.transaction_id),
                "invoice_id": _v(info.invoice_id),
                "custom_field": _v(info.custom_field),
                "status": _v(info.transaction_status),
                "amount": amount.value if amount else None,
                "currency": amount.currency_code if amount else None,
                "fee": fee.value if fee else None,
                "initiation_date": _v(info.transaction_initiation_date),
                "event_code": _v(info.transaction_event_code),
            }
        )
    total_pages = _v(result.total_pages) or 1
    return items, total_pages
