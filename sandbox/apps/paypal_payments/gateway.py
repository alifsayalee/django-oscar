"""
Thin, typed boundary around the PayPal SDK.

Every PayPal call made by the app goes through this module. It is the one place
that:

* builds SDK request models,
* converts every failure kind into ``PayPalError`` (one ladder, see ``_call``),
* maps PayPal status enums onto this app's outcomes, member by member, and
* turns SDK response models (with their ``UNSET`` members) into plain
  dataclasses the rest of the app can store and serialise.

Card numbers and security codes pass through here into the SDK request body
only; they are never logged or returned.
"""
import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypeVar

import httpx
from paypal.core import UNSET, ApiError, OAuthProviderError, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CaptureRequest,
    CapturedPayment,
    CardRequest,
    Customer,
    Error,
    Money,
    Order,
    OrderRequest,
    PaymentAuthorization,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PaymentTokenResponse,
    PurchaseUnitRequest,
    Refund,
    RefundRequest,
    SearchResponse,
)
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CheckoutPaymentIntent,
    OrderStatus,
    RefundStatus,
)
from pydantic import ValidationError

from .paypal_client import get_client

logger = logging.getLogger(__name__)

T = TypeVar("T")
V = TypeVar("V")

REPRESENTATION = "return=representation"

# Minor-unit exponents that differ from the usual two decimal places.
CURRENCY_EXPONENTS = {"JPY": 0, "KRW": 0, "HUF": 0, "TWD": 0, "KWD": 3, "BHD": 3, "TND": 3}

# Failures raised before a request leaves the machine: nothing reached PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Provider statuses that are a verdict on the caller's request.
CALLER_STATUSES = (400, 404, 409, 422)


class PayPalError(Exception):
    """
    A PayPal call that did not produce a usable result.

    ``status_code`` is the HTTP status this app answers with. ``outcome_unknown``
    is True when the request may have taken effect at PayPal (a timeout after
    sending, a 5xx, an unreadable success body) and must be reconciled rather
    than treated as not having happened.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool,
        issue: str = "",
        debug_id: str = "",
        provider_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.issue = issue
        self.debug_id = debug_id
        self.provider_status = provider_status

    @property
    def is_rejection(self) -> bool:
        """PayPal definitively refused the request (nothing happened)."""
        return self.provider_status in CALLER_STATUSES and not self.outcome_unknown


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _v(value: T | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def currency_exponent(currency: str) -> int:
    return CURRENCY_EXPONENTS.get(currency.upper(), 2)


def quantize(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-currency_exponent(currency)))


def format_amount(amount: Decimal, currency: str) -> str:
    return str(quantize(amount, currency))


def _money(value: Money | UnsetType | None) -> tuple[Decimal, str] | None:
    if value is None or isinstance(value, UnsetType):
        return None
    return Decimal(value.value), value.currency_code


def parse_time(value: str | UnsetType | None) -> datetime.datetime | None:
    if value is None or isinstance(value, UnsetType) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _require(value: T | UnsetType | None, what: str) -> T:
    """A member the app depends on is absent: the call's outcome is unknown."""
    if value is None or isinstance(value, UnsetType):
        raise PayPalError(
            502,
            "PayPal's response did not include %s; the outcome is unknown." % what,
            outcome_unknown=True,
        )
    return value


def _api_error(operation: str, exc: ApiError[object]) -> PayPalError:
    """Map an error status from PayPal onto this app's failure (auth first, then by status)."""
    body = exc.error
    if isinstance(body, OAuthProviderError):
        logger.error("PayPal rejected the app credentials during %s: %s", operation, body.error)
        return PayPalError(502, "The payment provider rejected this shop's credentials.", outcome_unknown=False)
    status = exc.status_code
    issue, message, debug_id = "", "", ""
    if isinstance(body, Error):
        message, debug_id = body.message, body.debug_id
        details = _v(body.details) or []
        if details:
            issue = details[0].issue
            message = _v(details[0].description) or message
        issue = issue or body.name
    logger.warning("PayPal %s failed: HTTP %s issue=%s debug_id=%s", operation, status, issue or "-", debug_id or "-")

    def failure(code: int, text: str, unknown: bool = False) -> PayPalError:
        return PayPalError(code, text, outcome_unknown=unknown, issue=issue, debug_id=debug_id, provider_status=status)

    if status in (401, 403):
        return failure(502, "The payment provider refused this shop's request.")
    if status == 429:
        return failure(503, "The payment provider is rate-limiting this shop; try again shortly.")
    if isinstance(body, Error) and status in CALLER_STATUSES:
        return failure(status, message or "The payment provider rejected the request.")
    return failure(502, "The payment provider returned an error.", unknown=status >= 500)


def _call(operation: str, fn: Callable[[], T]) -> T:
    """Run one SDK call and translate every failure kind into ``PayPalError``."""
    try:
        return fn()
    except ApiError as exc:
        raise _api_error(operation, exc) from exc
    except ValidationError as exc:
        logger.error("PayPal %s returned a response this app could not read", operation)
        raise PayPalError(
            502, "The payment provider's response could not be read; the outcome is unknown.",
            outcome_unknown=True,
        ) from exc
    except ValueError as exc:
        logger.error("PayPal %s returned a non-JSON response", operation)
        raise PayPalError(
            502, "The payment provider's response could not be read; the outcome is unknown.",
            outcome_unknown=True,
        ) from exc
    except NEVER_SENT as exc:
        logger.warning("PayPal %s was not sent: %s", operation, type(exc).__name__)
        raise PayPalError(
            502, "The payment provider could not be reached; nothing was sent.", outcome_unknown=False
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("PayPal %s got no reply: %s", operation, type(exc).__name__)
        raise PayPalError(
            504, "The payment provider did not answer in time; the outcome is unknown.", outcome_unknown=True
        ) from exc


# ---------------------------------------------------------------------------
# Status mapping: the one place a PayPal status becomes this app's outcome
# ---------------------------------------------------------------------------


def authorization_outcome(status: object) -> str:
    match status:
        case AuthorizationStatus.CREATED:
            return "authorized"
        case AuthorizationStatus.PENDING:
            return "pending"
        case AuthorizationStatus.DENIED:
            return "failed"
        case AuthorizationStatus.VOIDED:
            return "voided"
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return "captured"
        case _:
            return "unknown"


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED | CaptureStatus.PARTIALLY_REFUNDED | CaptureStatus.REFUNDED:
            return "captured"
        case CaptureStatus.PENDING:
            return "capture_pending"
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return "capture_failed"
        case _:
            return "unknown"


def refund_outcome(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return "done"
        case RefundStatus.PENDING:
            return "pending"
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return "failed"
        case _:
            return "unknown"


# ---------------------------------------------------------------------------
# Results handed to the rest of the app
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardDetails:
    """One-off card input. Never persisted, never logged (repr is masked)."""

    number: str = field(repr=False)
    expiry: str  # YYYY-MM
    security_code: str = field(repr=False)
    name: str
    billing_address: dict[str, str]


@dataclass(frozen=True)
class AuthorizationResult:
    outcome: str
    paypal_order_id: str
    paypal_order_status: str
    authorization_id: str | None = None
    authorization_status: str = ""
    amount: tuple[Decimal, str] | None = None
    expiration_time: datetime.datetime | None = None
    create_time: datetime.datetime | None = None
    card_brand: str = ""
    card_last_digits: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CaptureResult:
    outcome: str
    capture_id: str
    capture_status: str
    amount: tuple[Decimal, str] | None
    paypal_fee: tuple[Decimal, str] | None
    net_amount: tuple[Decimal, str] | None
    create_time: datetime.datetime | None
    reason: str = ""


@dataclass(frozen=True)
class RefundResult:
    outcome: str
    refund_id: str
    refund_status: str
    amount: tuple[Decimal, str] | None
    create_time: datetime.datetime | None


@dataclass(frozen=True)
class SavedToken:
    token_id: str
    customer_id: str
    brand: str
    last_digits: str
    expiry: str
    name: str


@dataclass(frozen=True)
class ProviderTransaction:
    transaction_id: str
    event_code: str
    status: str
    initiated_at: datetime.datetime | None
    amount: tuple[Decimal, str] | None
    fee: tuple[Decimal, str] | None
    invoice_id: str
    custom_field: str
    reference_id: str


@dataclass(frozen=True)
class TransactionPage:
    transactions: list[ProviderTransaction]
    total_pages: int
    last_refreshed: datetime.datetime | None


def _address(data: dict[str, str]) -> Address:
    return Address(
        address_line_1=data.get("address_line_1", UNSET) or UNSET,
        address_line_2=data.get("address_line_2", UNSET) or UNSET,
        admin_area_2=data.get("admin_area_2", UNSET) or UNSET,
        admin_area_1=data.get("admin_area_1", UNSET) or UNSET,
        postal_code=data.get("postal_code", UNSET) or UNSET,
        country_code=data["country_code"],
    )


def _authorization_from_order(order: Order) -> AuthorizationResult:
    paypal_order_id = _require(order.id, "an order id")
    order_status = _v(order.status)
    card = None
    source = _v(order.payment_source)
    if source is not None:
        card = _v(source.card)
    brand = str(_v(card.brand) or "") if card is not None else ""
    last_digits = str(_v(card.last_digits) or "") if card is not None else ""

    if order_status == OrderStatus.PAYER_ACTION_REQUIRED:
        return AuthorizationResult(
            outcome="failed", paypal_order_id=paypal_order_id, paypal_order_status=str(order_status),
            card_brand=brand, card_last_digits=last_digits,
            reason="PayPal requires the shopper to complete a 3-D Secure challenge in a browser; "
            "this API does not support browser approval.",
        )
    if order_status == OrderStatus.VOIDED:
        return AuthorizationResult(
            outcome="failed", paypal_order_id=paypal_order_id, paypal_order_status=str(order_status),
            card_brand=brand, card_last_digits=last_digits, reason="PayPal voided the order.",
        )

    authorization = None
    units = _v(order.purchase_units) or []
    if units:
        payments = _v(units[0].payments)
        if payments is not None:
            authorizations = _v(payments.authorizations) or []
            if authorizations:
                authorization = authorizations[0]
    if order_status != OrderStatus.COMPLETED or authorization is None:
        return AuthorizationResult(
            outcome="unknown", paypal_order_id=paypal_order_id, paypal_order_status=str(order_status or ""),
            card_brand=brand, card_last_digits=last_digits,
            reason="PayPal did not return an authorization for the order.",
        )
    status = _v(authorization.status)
    reason = ""
    details = _v(authorization.status_details)
    if details is not None and not isinstance(details.reason, UnsetType):
        reason = str(details.reason)
    return AuthorizationResult(
        outcome=authorization_outcome(status),
        paypal_order_id=paypal_order_id,
        paypal_order_status=str(order_status),
        authorization_id=_require(authorization.id, "an authorization id"),
        authorization_status=str(status or ""),
        amount=_money(authorization.amount),
        expiration_time=parse_time(authorization.expiration_time),
        create_time=parse_time(authorization.create_time),
        card_brand=brand,
        card_last_digits=last_digits,
        reason=reason,
    )


def _authorization(payment: PaymentAuthorization) -> AuthorizationResult:
    status = _v(payment.status)
    reason = ""
    details = _v(payment.status_details)
    if details is not None and not isinstance(details.reason, UnsetType):
        reason = str(details.reason)
    return AuthorizationResult(
        outcome=authorization_outcome(status),
        paypal_order_id="",
        paypal_order_status="",
        authorization_id=_require(payment.id, "an authorization id"),
        authorization_status=str(status or ""),
        amount=_money(payment.amount),
        expiration_time=parse_time(payment.expiration_time),
        create_time=parse_time(payment.create_time),
        reason=reason,
    )


def _capture(capture: CapturedPayment) -> CaptureResult:
    status = _v(capture.status)
    breakdown = _v(capture.seller_receivable_breakdown)
    reason = ""
    details = _v(capture.status_details)
    if details is not None and not isinstance(details.reason, UnsetType):
        reason = str(details.reason)
    return CaptureResult(
        outcome=capture_outcome(status),
        capture_id=_require(capture.id, "a capture id"),
        capture_status=str(status or ""),
        amount=_money(capture.amount),
        paypal_fee=_money(breakdown.paypal_fee) if breakdown is not None else None,
        net_amount=_money(breakdown.net_amount) if breakdown is not None else None,
        create_time=parse_time(capture.create_time),
        reason=reason,
    )


def _refund(refund: Refund) -> RefundResult:
    status = _v(refund.status)
    return RefundResult(
        outcome=refund_outcome(status),
        refund_id=_require(refund.id, "a refund id"),
        refund_status=str(status or ""),
        amount=_money(refund.amount),
        create_time=parse_time(refund.create_time),
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def create_authorized_order(
    *,
    request_id: str,
    amount: Decimal,
    currency: str,
    invoice_id: str,
    custom_id: str,
    description: str,
    card: CardDetails | None = None,
    vault_id: str | None = None,
) -> AuthorizationResult:
    """Create a PayPal order with intent AUTHORIZE, paid by a card or a vaulted card."""
    if (card is None) == (vault_id is None):
        raise ValueError("Exactly one of card or vault_id is required")
    if card is not None:
        card_request = CardRequest(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            name=card.name,
            billing_address=_address(card.billing_address) if card.billing_address else UNSET,
        )
    else:
        assert vault_id is not None
        card_request = CardRequest(vault_id=vault_id)
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                amount=AmountWithBreakdown(currency_code=currency, value=format_amount(amount, currency)),
                invoice_id=invoice_id,
                custom_id=custom_id,
                description=description[:127],
            )
        ],
        payment_source=PaymentSource(card=card_request),
    )
    order = _call(
        "create_order",
        lambda: get_client().orders.create_order(body, pay_pal_request_id=request_id, prefer=REPRESENTATION),
    )
    return _authorization_from_order(order)


def get_authorization(authorization_id: str) -> AuthorizationResult:
    payment = _call(
        "get_authorized_payment", lambda: get_client().payments.get_authorized_payment(authorization_id)
    )
    return _authorization(payment)


def reauthorize(authorization_id: str, *, request_id: str) -> AuthorizationResult:
    payment = _call(
        "reauthorize_payment",
        lambda: get_client().payments.reauthorize_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION
        ),
    )
    return _authorization(payment)


def capture(authorization_id: str, *, request_id: str) -> CaptureResult:
    captured = _call(
        "capture_authorized_payment",
        lambda: get_client().payments.capture_authorized_payment(
            authorization_id,
            pay_pal_request_id=request_id,
            prefer=REPRESENTATION,
            body=CaptureRequest(final_capture=True),
        ),
    )
    return _capture(captured)


def get_capture(capture_id: str) -> CaptureResult:
    captured = _call("get_captured_payment", lambda: get_client().payments.get_captured_payment(capture_id))
    return _capture(captured)


def void(authorization_id: str, *, request_id: str) -> AuthorizationResult:
    payment = _call(
        "void_payment",
        lambda: get_client().payments.void_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION
        ),
    )
    return _authorization(payment)


def refund(capture_id: str, *, request_id: str, amount: Decimal, currency: str) -> RefundResult:
    body = RefundRequest(amount=Money(currency_code=currency, value=format_amount(amount, currency)))
    result = _call(
        "refund_captured_payment",
        lambda: get_client().payments.refund_captured_payment(
            capture_id, pay_pal_request_id=request_id, prefer=REPRESENTATION, body=body
        ),
    )
    return _refund(result)


def save_card(card: CardDetails, *, request_id: str, customer_id: str | None) -> SavedToken:
    body = PaymentTokenRequest(
        customer=Customer(id=customer_id) if customer_id else UNSET,
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=card.name,
                billing_address=_address(card.billing_address) if card.billing_address else UNSET,
            )
        ),
    )
    token = _call(
        "create_payment_token",
        lambda: get_client().vault.create_payment_token(body, pay_pal_request_id=request_id),
    )
    return _saved_token(token)


def _saved_token(token: PaymentTokenResponse) -> SavedToken:
    token_id = _require(token.id, "a payment token id")
    customer = _v(token.customer)
    customer_id = _require(_v(customer.id) if customer is not None else None, "a customer id")
    source = _v(token.payment_source)
    card = _v(source.card) if source is not None else None
    return SavedToken(
        token_id=token_id,
        customer_id=customer_id,
        brand=str(_v(card.brand) or "") if card is not None else "",
        last_digits=str(_v(card.last_digits) or "") if card is not None else "",
        expiry=str(_v(card.expiry) or "") if card is not None else "",
        name=str(_v(card.name) or "") if card is not None else "",
    )


def delete_token(token_id: str) -> None:
    """Delete a vaulted card. A 404 means it is already gone, which is the goal."""
    try:
        _call("delete_payment_token", lambda: get_client().vault.delete_payment_token(token_id))
    except PayPalError as exc:
        if exc.provider_status == 404:
            return
        raise


def search_transactions(
    start: datetime.datetime, end: datetime.datetime, *, page: int, page_size: int = 100
) -> TransactionPage:
    """One page of PayPal's balance-affecting transactions in [start, end] (<= 31 days)."""

    def fmt(value: datetime.datetime) -> str:
        return value.astimezone(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    response: SearchResponse = _call(
        "search_transactions",
        lambda: get_client().transaction_search.search_transactions(
            fmt(start), fmt(end), fields="transaction_info", page_size=page_size, page=page
        ),
    )
    records = []
    for detail in _v(response.transaction_details) or []:
        info = _v(detail.transaction_info)
        if info is None:
            continue
        records.append(
            ProviderTransaction(
                transaction_id=str(_v(info.transaction_id) or ""),
                event_code=str(_v(info.transaction_event_code) or ""),
                status=str(_v(info.transaction_status) or ""),
                initiated_at=parse_time(info.transaction_initiation_date),
                amount=_money(info.transaction_amount),
                fee=_money(info.fee_amount),
                invoice_id=str(_v(info.invoice_id) or ""),
                custom_field=str(_v(info.custom_field) or ""),
                reference_id=str(_v(info.paypal_reference_id) or ""),
            )
        )
    return TransactionPage(
        transactions=records,
        total_pages=int(_v(response.total_pages) or 0),
        last_refreshed=parse_time(response.last_refreshed_datetime),
    )
