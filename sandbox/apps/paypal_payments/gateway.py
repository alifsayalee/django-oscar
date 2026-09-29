"""
The PayPal boundary.

This is the only module that imports the PayPal Server SDK. It owns the
process-wide client, turns every SDK outcome into a plain dataclass, and
turns every failure into a :class:`PayPalError` that says whether anything
may have happened at PayPal (``outcome_unknown``).

Card numbers and security codes pass through here on their way to PayPal
and are never logged or stored.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import (
    UNSET,
    ApiError,
    ClientCredentials,
    OAuthProviderError,
    RawError,
    UnsetType,
)
from pay_pal_server_sdk.models import (
    Address,
    AmountWithBreakdown,
    CaptureRequest,
    CardRequest,
    Customer,
    Error,
    Money,
    OrderRequest,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    RefundRequest,
)
from pay_pal_server_sdk.models.enums import CheckoutPaymentIntent
from pydantic import ValidationError

logger = logging.getLogger("apps.paypal_payments.gateway")

T = TypeVar("T")

# The only host the PayPal SDK declares. Any other environment must name its
# host through PAYPAL_BASE_URL rather than have one guessed for it.
ENVIRONMENT_BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com"}

# Every write asks for the full resource; the SDK default (return=minimal)
# omits amounts, fees and nested payments.
REPRESENTATION = "return=representation"

# ISO 4217 minor units for currencies that do not have two decimal places.
_EXPONENT = {
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "UYI": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
}

# The Transaction Search API accepts at most 31 days per request.
MAX_SEARCH_WINDOW_DAYS = 31


def currency_exponent(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-currency_exponent(currency)))


def format_amount(value: Decimal, currency: str) -> str:
    return str(quantize(value, currency))


# ======
# Errors
# ======


class PayPalError(Exception):
    """
    A PayPal call that did not succeed.

    ``http_status`` is the status this app should answer with;
    ``outcome_unknown`` is True when the request may have taken effect at
    PayPal (so it must be settled by replaying the same PayPal-Request-Id,
    never by issuing a new one).
    """

    def __init__(
        self,
        http_status: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        issue: str | None = None,
        provider_status: int | None = None,
        debug_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.issue = issue
        self.provider_status = provider_status
        self.debug_id = debug_id


class PayPalNotConfigured(PayPalError):
    def __init__(self, message: str) -> None:
        super().__init__(503, "paypal_not_configured", message)


def _from_api_error(operation: str, e: ApiError[Any]) -> PayPalError:
    status = e.status_code
    error = e.error
    if isinstance(error, OAuthProviderError):
        logger.error(
            "PayPal refused the app credentials during %s: %s", operation, error.error
        )
        return PayPalError(
            502,
            "paypal_auth_failed",
            "PayPal rejected this site's API credentials.",
            provider_status=status,
        )
    issue: str | None = None
    description: str | None = None
    debug_id: str | None = None
    if isinstance(error, Error):
        debug_id = error.debug_id
        description = error.message
        if not isinstance(error.details, UnsetType) and error.details:
            first = error.details[0]
            issue = first.issue
            if not isinstance(first.description, UnsetType):
                description = first.description
    elif isinstance(error, RawError):
        description = None
    logger.warning(
        "PayPal %s failed: HTTP %s issue=%s debug_id=%s",
        operation,
        status,
        issue,
        debug_id,
    )
    if status in (401, 403):
        return PayPalError(
            502,
            "paypal_auth_failed",
            "PayPal refused this site's credentials or permissions for this operation.",
            issue=issue,
            provider_status=status,
            debug_id=debug_id,
        )
    if status == 429:
        return PayPalError(
            503,
            "paypal_rate_limited",
            "PayPal is rate-limiting this site; try again shortly.",
            provider_status=status,
            debug_id=debug_id,
        )
    if 400 <= status < 500:
        return PayPalError(
            409 if status in (404, 409) else 422,
            "paypal_rejected",
            description or "PayPal rejected the request.",
            issue=issue,
            provider_status=status,
            debug_id=debug_id,
        )
    return PayPalError(
        502,
        "paypal_unavailable",
        "PayPal failed to process the request.",
        outcome_unknown=True,
        issue=issue,
        provider_status=status,
        debug_id=debug_id,
    )


def _unknown(operation: str, what: str) -> PayPalError:
    return PayPalError(
        502,
        "paypal_unreadable_response",
        f"PayPal's response to {operation} could not be read ({what}); "
        "the operation may have taken effect and will be settled on retry.",
        outcome_unknown=True,
    )


def _req(value: T | UnsetType, operation: str, what: str) -> T:
    if isinstance(value, UnsetType):
        raise _unknown(operation, f"missing {what}")
    return value


def _opt(value: T | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _parse_decimal(value: str, operation: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise _unknown(operation, "malformed amount") from None


def _parse_time(value: str | UnsetType | None) -> datetime | None:
    if value is None or isinstance(value, UnsetType):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _money(value: Money | UnsetType, operation: str, what: str) -> tuple[Decimal, str]:
    money = _req(value, operation, what)
    return _parse_decimal(money.value, operation), money.currency_code


def _opt_money(value: Money | UnsetType, operation: str) -> Decimal | None:
    if isinstance(value, UnsetType):
        return None
    return _parse_decimal(value.value, operation)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ================
# Inputs / results
# ================


@dataclass(frozen=True)
class AddressInput:
    country_code: str
    line1: str = ""
    line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""


@dataclass(frozen=True, repr=False)
class CardInput:
    """Raw card details. ``repr`` is suppressed so they never reach a log."""

    number: str
    expiry: str  # YYYY-MM
    security_code: str
    name: str = ""
    billing_address: AddressInput | None = None

    def __repr__(self) -> str:
        return "CardInput(<redacted>)"


@dataclass(frozen=True)
class AuthorizationResult:
    paypal_order_id: str
    order_status: str
    authorization_id: str | None
    authorization_status: str | None
    amount: Decimal | None
    currency: str | None
    expires_at: datetime | None
    created_at: datetime | None
    card_brand: str
    card_last_digits: str


@dataclass(frozen=True)
class AuthorizationState:
    authorization_id: str
    status: str
    amount: Decimal | None
    currency: str | None
    expires_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    status: str
    amount: Decimal
    currency: str
    paypal_fee: Decimal | None
    net_amount: Decimal | None


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    status: str
    amount: Decimal | None
    currency: str | None


@dataclass(frozen=True)
class VaultedCard:
    token_id: str
    customer_id: str
    brand: str
    last_digits: str
    expiry: str  # YYYY-MM


@dataclass(frozen=True)
class TransactionRecord:
    transaction_id: str
    event_code: str
    status: str
    initiated_at: datetime | None
    amount: Decimal | None
    currency: str | None
    fee: Decimal | None
    invoice_id: str
    custom_field: str
    reference_id: str


@dataclass(frozen=True)
class TransactionPage:
    transactions: list[TransactionRecord]
    page: int
    total_pages: int
    last_refreshed_at: datetime | None


def _address(address: AddressInput) -> Address:
    return Address(
        country_code=address.country_code,
        address_line_1=address.line1 or UNSET,
        address_line_2=address.line2 or UNSET,
        admin_area_2=address.city or UNSET,
        admin_area_1=address.state or UNSET,
        postal_code=address.postal_code or UNSET,
    )


# =======
# Gateway
# =======


class PayPalGateway:
    def __init__(self, client: PayPalServerSdkClient) -> None:
        self._client = client

    def close(self) -> None:
        self._client.close()

    def _call(self, operation: str, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except ApiError as e:
            raise _from_api_error(operation, e) from e
        except ValidationError as e:
            logger.error(
                "PayPal %s returned an undecodable body (%s errors)",
                operation,
                e.error_count(),
            )
            raise _unknown(operation, "undecodable body") from e
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ProxyError,
        ) as e:
            logger.error("PayPal %s was never sent: %s", operation, type(e).__name__)
            raise PayPalError(
                502,
                "paypal_unreachable",
                "PayPal could not be reached; nothing was sent.",
            ) from e
        except httpx.RequestError as e:
            logger.error("PayPal %s got no reply: %s", operation, type(e).__name__)
            raise PayPalError(
                504,
                "paypal_timeout",
                "PayPal did not answer in time; the operation may have taken effect and "
                "will be settled when the request is repeated.",
                outcome_unknown=True,
            ) from e
        except ValueError as e:
            logger.error("PayPal %s returned a non-JSON body", operation)
            raise _unknown(operation, "non-JSON body") from e

    # -- Orders / authorizations --------------------------------------------------

    def authorize(
        self,
        *,
        request_id: str,
        amount: Decimal,
        currency: str,
        order_number: str,
        invoice_id: str,
        description: str,
        card: CardInput | None = None,
        vault_id: str | None = None,
    ) -> AuthorizationResult:
        """Create a PayPal order with intent AUTHORIZE, paid by a card or a vaulted card."""
        op = "create_order"
        if (card is None) == (vault_id is None):
            raise ValueError("exactly one of card or vault_id is required")
        if card is not None:
            card_request = CardRequest(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=card.name or UNSET,
                billing_address=(
                    _address(card.billing_address) if card.billing_address else UNSET
                ),
            )
        else:
            assert vault_id is not None
            card_request = CardRequest(vault_id=vault_id)
        body = OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[
                PurchaseUnitRequest(
                    reference_id=order_number,
                    custom_id=order_number,
                    invoice_id=invoice_id,
                    description=description[:127],
                    amount=AmountWithBreakdown(
                        currency_code=currency, value=format_amount(amount, currency)
                    ),
                )
            ],
            payment_source=PaymentSource(card=card_request),
        )
        order = self._call(
            op,
            lambda: self._client.orders.create_order(
                body, pay_pal_request_id=request_id, prefer=REPRESENTATION
            ),
        )
        order_id = _req(order.id, op, "order id")
        status = str(_req(order.status, op, "order status"))
        brand = last_digits = ""
        source = _opt(order.payment_source)
        if source is not None:
            card_response = _opt(source.card)
            if card_response is not None:
                brand = str(_opt(card_response.brand) or "")
                last_digits = _opt(card_response.last_digits) or ""
        authorization = None
        units = _opt(order.purchase_units) or []
        if units:
            payments = _opt(units[0].payments)
            if payments is not None:
                authorizations = _opt(payments.authorizations) or []
                if authorizations:
                    authorization = authorizations[0]
        if authorization is None:
            return AuthorizationResult(
                paypal_order_id=order_id,
                order_status=status,
                authorization_id=None,
                authorization_status=None,
                amount=None,
                currency=None,
                expires_at=None,
                created_at=None,
                card_brand=brand,
                card_last_digits=last_digits,
            )
        held, held_currency = _money(authorization.amount, op, "authorization amount")
        return AuthorizationResult(
            paypal_order_id=order_id,
            order_status=status,
            authorization_id=_req(authorization.id, op, "authorization id"),
            authorization_status=str(
                _req(authorization.status, op, "authorization status")
            ),
            amount=held,
            currency=held_currency,
            expires_at=_parse_time(authorization.expiration_time),
            created_at=_parse_time(authorization.create_time),
            card_brand=brand,
            card_last_digits=last_digits,
        )

    def get_authorization(self, authorization_id: str) -> AuthorizationState:
        op = "get_authorized_payment"
        auth = self._call(
            op, lambda: self._client.payments.get_authorized_payment(authorization_id)
        )
        return self._authorization_state(
            op,
            auth.id,
            auth.status,
            auth.amount,
            auth.expiration_time,
            auth.create_time,
        )

    def reauthorize(
        self, authorization_id: str, *, request_id: str
    ) -> AuthorizationState:
        op = "reauthorize_payment"
        auth = self._call(
            op,
            lambda: self._client.payments.reauthorize_payment(
                authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION
            ),
        )
        return self._authorization_state(
            op,
            auth.id,
            auth.status,
            auth.amount,
            auth.expiration_time,
            auth.create_time,
        )

    def void(self, authorization_id: str, *, request_id: str) -> AuthorizationState:
        op = "void_payment"
        auth = self._call(
            op,
            lambda: self._client.payments.void_payment(
                authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION
            ),
        )
        return self._authorization_state(
            op,
            auth.id,
            auth.status,
            auth.amount,
            auth.expiration_time,
            auth.create_time,
        )

    @staticmethod
    def _authorization_state(
        op: str,
        auth_id: str | UnsetType,
        status: object,
        amount: Money | UnsetType,
        expires: str | UnsetType,
        created: str | UnsetType,
    ) -> AuthorizationState:
        if isinstance(status, UnsetType):
            raise _unknown(op, "missing authorization status")
        return AuthorizationState(
            authorization_id=_req(auth_id, op, "authorization id"),
            status=str(status),
            amount=_opt_money(amount, op),
            currency=None if isinstance(amount, UnsetType) else amount.currency_code,
            expires_at=_parse_time(expires),
            created_at=_parse_time(created),
        )

    # -- Captures / refunds ---------------------------------------------------------

    def capture(
        self, authorization_id: str, *, request_id: str, amount: Decimal, currency: str
    ) -> CaptureResult:
        op = "capture_authorized_payment"
        body = CaptureRequest(
            amount=Money(currency_code=currency, value=format_amount(amount, currency)),
            final_capture=True,
        )
        capture = self._call(
            op,
            lambda: self._client.payments.capture_authorized_payment(
                authorization_id,
                pay_pal_request_id=request_id,
                prefer=REPRESENTATION,
                body=body,
            ),
        )
        captured, captured_currency = _money(capture.amount, op, "capture amount")
        fee = net = None
        breakdown = _opt(capture.seller_receivable_breakdown)
        if breakdown is not None:
            fee = _opt_money(breakdown.paypal_fee, op)
            net = _opt_money(breakdown.net_amount, op)
        return CaptureResult(
            capture_id=_req(capture.id, op, "capture id"),
            status=str(_req(capture.status, op, "capture status")),
            amount=captured,
            currency=captured_currency,
            paypal_fee=fee,
            net_amount=net,
        )

    def refund(
        self, capture_id: str, *, request_id: str, amount: Decimal, currency: str
    ) -> RefundResult:
        op = "refund_captured_payment"
        body = RefundRequest(
            amount=Money(currency_code=currency, value=format_amount(amount, currency))
        )
        refund = self._call(
            op,
            lambda: self._client.payments.refund_captured_payment(
                capture_id,
                pay_pal_request_id=request_id,
                prefer=REPRESENTATION,
                body=body,
            ),
        )
        amount_value = _opt(refund.amount)
        return RefundResult(
            refund_id=_req(refund.id, op, "refund id"),
            status=str(_req(refund.status, op, "refund status")),
            amount=_parse_decimal(amount_value.value, op) if amount_value else None,
            currency=amount_value.currency_code if amount_value else None,
        )

    # -- Vault --------------------------------------------------------------------

    def vault_card(
        self,
        *,
        request_id: str,
        card: CardInput,
        customer_id: str | None,
        merchant_customer_id: str,
    ) -> VaultedCard:
        op = "create_payment_token"
        customer = (
            Customer(id=customer_id)
            if customer_id
            else Customer(merchant_customer_id=merchant_customer_id)
        )
        body = PaymentTokenRequest(
            customer=customer,
            payment_source=PaymentTokenRequestPaymentSource(
                card=PaymentTokenRequestCard(
                    number=card.number,
                    expiry=card.expiry,
                    security_code=card.security_code,
                    name=card.name or UNSET,
                    billing_address=(
                        _address(card.billing_address)
                        if card.billing_address
                        else UNSET
                    ),
                )
            ),
        )
        token = self._call(
            op,
            lambda: self._client.vault.create_payment_token(
                body, pay_pal_request_id=request_id
            ),
        )
        token_customer = _req(token.customer, op, "customer")
        source = _req(token.payment_source, op, "payment source")
        card_entity = _req(source.card, op, "card")
        return VaultedCard(
            token_id=_req(token.id, op, "payment token id"),
            customer_id=_req(token_customer.id, op, "customer id"),
            brand=str(_opt(card_entity.brand) or "CARD"),
            last_digits=_req(card_entity.last_digits, op, "card last digits"),
            expiry=_opt(card_entity.expiry) or card.expiry,
        )

    def delete_vaulted_card(self, token_id: str) -> None:
        """Delete a vault token. A token PayPal no longer has counts as deleted."""
        try:
            self._call(
                "delete_payment_token",
                lambda: self._client.vault.delete_payment_token(token_id),
            )
        except PayPalError as e:
            if e.provider_status == 404:
                return
            raise

    # -- Reporting ----------------------------------------------------------------

    def search_transactions(
        self, start: datetime, end: datetime, *, page: int, page_size: int = 100
    ) -> TransactionPage:
        op = "search_transactions"
        response = self._call(
            op,
            lambda: self._client.transaction_search.search_transactions(
                _rfc3339(start),
                _rfc3339(end),
                fields="transaction_info",
                page=page,
                page_size=page_size,
            ),
        )
        records: list[TransactionRecord] = []
        for detail in _opt(response.transaction_details) or []:
            info = _opt(detail.transaction_info)
            if info is None:
                continue
            amount = _opt(info.transaction_amount)
            records.append(
                TransactionRecord(
                    transaction_id=_opt(info.transaction_id) or "",
                    event_code=_opt(info.transaction_event_code) or "",
                    status=_opt(info.transaction_status) or "",
                    initiated_at=_parse_time(info.transaction_initiation_date),
                    amount=_parse_decimal(amount.value, op) if amount else None,
                    currency=amount.currency_code if amount else None,
                    fee=_opt_money(info.fee_amount, op),
                    invoice_id=_opt(info.invoice_id) or "",
                    custom_field=_opt(info.custom_field) or "",
                    reference_id=_opt(info.paypal_reference_id) or "",
                )
            )
        return TransactionPage(
            transactions=records,
            page=_opt(response.page) or page,
            total_pages=_opt(response.total_pages) or 1,
            last_refreshed_at=_parse_time(response.last_refreshed_datetime),
        )


# ===================
# Process-wide client
# ===================

_lock = threading.Lock()
_gateway: PayPalGateway | None = None


def base_url() -> str:
    override = getattr(settings, "PAYPAL_BASE_URL", "") or ""
    if override:
        return str(override)
    environment = str(getattr(settings, "PAYPAL_ENVIRONMENT", "") or "").strip().lower()
    if not environment:
        raise PayPalNotConfigured("PAYPAL_ENVIRONMENT is not configured.")
    try:
        return ENVIRONMENT_BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            f"PAYPAL_ENVIRONMENT={environment!r} has no known PayPal host; "
            "set PAYPAL_BASE_URL to the API base address for that environment."
        ) from None


def configured_currency() -> str:
    currency = str(getattr(settings, "PAYPAL_CURRENCY", "") or "").strip().upper()
    if not currency:
        raise PayPalNotConfigured("PAYPAL_CURRENCY is not configured.")
    return currency


def _build() -> PayPalGateway:
    client_id = str(getattr(settings, "PAYPAL_CLIENT_ID", "") or "")
    client_secret = str(getattr(settings, "PAYPAL_CLIENT_SECRET", "") or "")
    if not client_id or not client_secret:
        raise PayPalNotConfigured(
            "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not configured."
        )
    client = PayPalServerSdkClient(
        base_url=base_url(),
        timeout=float(getattr(settings, "PAYPAL_TIMEOUT", 20.0)),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )
    return PayPalGateway(client)


def get_gateway() -> PayPalGateway:
    """The process-wide gateway, built on first use (so after any worker fork)."""
    global _gateway  # pylint: disable=global-statement
    if _gateway is None:
        with _lock:
            if _gateway is None:
                _gateway = _build()
    return _gateway


def set_gateway(gateway: PayPalGateway | None) -> PayPalGateway | None:
    """Swap the process-wide gateway (tests); returns the previous one."""
    global _gateway  # pylint: disable=global-statement
    with _lock:
        previous, _gateway = _gateway, gateway
    return previous


@atexit.register
def _close() -> None:
    if _gateway is not None:
        _gateway.close()
