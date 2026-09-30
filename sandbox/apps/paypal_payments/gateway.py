"""
Every call this app makes to PayPal, through the PayPal Server SDK.

Each function takes plain values, makes one SDK call through the error
boundary (``call_paypal``), checks the response members the caller depends on
and returns a small dataclass - so nothing outside this module touches SDK
types or the ``UNSET`` sentinel.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pay_pal_server_sdk.core import UNSET, UnsetType
from pay_pal_server_sdk.models import (
    AmountWithBreakdown,
    CaptureRequest,
    CardRequest,
    Customer,
    Money,
    OrderRequest,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    RefundRequest,
)
from pay_pal_server_sdk.models.enums import CheckoutPaymentIntent

from .client import get_client
from .errors import call_paypal, unknown_outcome
from .money import from_paypal, to_paypal

# Ask PayPal for the full resource on writes; its default ("return=minimal")
# omits the authorization, the fee breakdown and the card description.
_REPRESENTATION = "return=representation"

# transaction_search accepts at most 31 days per request.
SEARCH_WINDOW = timedelta(days=31)


@dataclass(frozen=True)
class CardDetails:
    """Card data in transit to PayPal. Never persisted; hidden from reprs and logs."""

    number: str = field(repr=False)
    expiry: str  # YYYY-MM
    security_code: str = field(repr=False)
    name: str | None = None


@dataclass(frozen=True)
class AuthorizationResult:
    paypal_order_id: str
    order_status: str
    authorization_id: str | None
    authorization_status: str | None
    amount: Decimal | None
    currency: str | None
    created_at: datetime | None
    expires_at: datetime | None
    card_brand: str
    card_last_digits: str


@dataclass(frozen=True)
class AuthorizationState:
    authorization_id: str
    status: str
    created_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    status: str
    amount: Decimal
    currency: str
    paypal_fee: Decimal | None
    net_amount: Decimal | None
    created_at: datetime | None


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    status: str


@dataclass(frozen=True)
class VaultedCard:
    token_id: str
    customer_id: str | None
    brand: str
    last_digits: str
    expiry: str
    name: str


@dataclass(frozen=True)
class PayPalTransaction:
    transaction_id: str
    event_code: str
    status: str
    initiated_at: str
    amount: Decimal | None
    currency: str | None
    fee: Decimal | None
    invoice_id: str | None
    custom_field: str | None
    reference_id: str | None


@dataclass(frozen=True)
class TransactionPage:
    transactions: list[PayPalTransaction]
    last_refreshed: str | None


def _str(value: object) -> str | None:
    # Open enums arrive as a str-Enum member or a plain str; both are str.
    return str(value) if isinstance(value, str) else None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _money(currency: str, amount: Decimal) -> Money:
    return Money(currency_code=currency, value=to_paypal(amount, currency))


def authorize(
    *,
    amount: Decimal,
    currency: str,
    invoice_id: str,
    custom_id: str,
    description: str,
    request_id: str,
    card: CardDetails | None = None,
    vault_id: str | None = None,
) -> AuthorizationResult:
    """Create a PayPal order with intent AUTHORIZE, paid by a card or a vaulted card, in one step."""
    if card is not None:
        source = CardRequest(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            name=card.name if card.name else UNSET,
        )
    elif vault_id is not None:
        source = CardRequest(vault_id=vault_id)
    else:
        raise ValueError("a card or a vault id is required")
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=custom_id,
                amount=AmountWithBreakdown(currency_code=currency, value=to_paypal(amount, currency)),
                invoice_id=invoice_id,
                custom_id=custom_id,
                description=description,
            )
        ],
        payment_source=PaymentSource(card=source),
    )
    order = call_paypal(
        "the card authorization",
        # PayPal-Request-Id is mandatory for a single-step card order, and
        # re-sending the same one returns the original order instead of a new one.
        lambda: get_client().orders.create_order(body, pay_pal_request_id=request_id, prefer=_REPRESENTATION),
    )
    order_id = _str(order.id)
    order_status = _str(order.status)
    if order_id is None or order_status is None:
        raise unknown_outcome("the card authorization", "an order id and status")

    brand = last_digits = ""
    if not isinstance(order.payment_source, UnsetType) and not isinstance(order.payment_source.card, UnsetType):
        brand = _str(order.payment_source.card.brand) or ""
        last_digits = _str(order.payment_source.card.last_digits) or ""

    auth = None
    if isinstance(order.purchase_units, list) and order.purchase_units:
        payments = order.purchase_units[0].payments
        if not isinstance(payments, UnsetType) and isinstance(payments.authorizations, list):
            auth = payments.authorizations[0] if payments.authorizations else None
    amount_value = currency_code = None
    if auth is not None and not isinstance(auth.amount, UnsetType):
        amount_value = from_paypal(auth.amount.value)
        currency_code = auth.amount.currency_code
    return AuthorizationResult(
        paypal_order_id=order_id,
        order_status=order_status,
        authorization_id=_str(auth.id) if auth else None,
        authorization_status=_str(auth.status) if auth else None,
        amount=amount_value,
        currency=currency_code,
        created_at=_time(auth.create_time) if auth else None,
        expires_at=_time(auth.expiration_time) if auth else None,
        card_brand=brand,
        card_last_digits=last_digits,
    )


def get_authorization(authorization_id: str) -> AuthorizationState:
    auth = call_paypal(
        "the authorization lookup", lambda: get_client().payments.get_authorized_payment(authorization_id)
    )
    status = _str(auth.status)
    if status is None:
        raise unknown_outcome("the authorization lookup", "a status")
    return AuthorizationState(
        authorization_id=_str(auth.id) or authorization_id,
        status=status,
        created_at=_time(auth.create_time),
        expires_at=_time(auth.expiration_time),
    )


def reauthorize(authorization_id: str, *, amount: Decimal, currency: str, request_id: str) -> AuthorizationState:
    auth = call_paypal(
        "the reauthorization",
        lambda: get_client().payments.reauthorize_payment(
            authorization_id,
            pay_pal_request_id=request_id,
            prefer=_REPRESENTATION,
            body=ReauthorizeRequest(amount=_money(currency, amount)),
        ),
    )
    new_id = _str(auth.id)
    if new_id is None:
        raise unknown_outcome("the reauthorization", "an authorization id")
    return AuthorizationState(
        authorization_id=new_id,
        status=_str(auth.status) or "CREATED",
        created_at=_time(auth.create_time),
        expires_at=_time(auth.expiration_time),
    )


def capture(
    authorization_id: str, *, amount: Decimal, currency: str, invoice_id: str, request_id: str
) -> CaptureResult:
    captured = call_paypal(
        "the capture",
        lambda: get_client().payments.capture_authorized_payment(
            authorization_id,
            pay_pal_request_id=request_id,
            prefer=_REPRESENTATION,
            body=CaptureRequest(amount=_money(currency, amount), invoice_id=invoice_id, final_capture=True),
        ),
    )
    capture_id = _str(captured.id)
    status = _str(captured.status)
    if capture_id is None or status is None or isinstance(captured.amount, UnsetType):
        raise unknown_outcome("the capture", "a capture id, status and amount")
    fee = net = None
    breakdown = captured.seller_receivable_breakdown
    if not isinstance(breakdown, UnsetType):
        if not isinstance(breakdown.paypal_fee, UnsetType):
            fee = from_paypal(breakdown.paypal_fee.value)
        if not isinstance(breakdown.net_amount, UnsetType):
            net = from_paypal(breakdown.net_amount.value)
    return CaptureResult(
        capture_id=capture_id,
        status=status,
        amount=from_paypal(captured.amount.value),
        currency=captured.amount.currency_code,
        paypal_fee=fee,
        net_amount=net,
        created_at=_time(captured.create_time),
    )


def void(authorization_id: str, *, request_id: str) -> str:
    auth = call_paypal(
        "the void",
        lambda: get_client().payments.void_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=_REPRESENTATION
        ),
    )
    status = _str(auth.status)
    if status is None:
        raise unknown_outcome("the void", "a status")
    return status


def refund(
    capture_id: str,
    *,
    amount: Decimal,
    currency: str,
    invoice_id: str,
    note: str | None,
    request_id: str,
) -> RefundResult:
    refunded = call_paypal(
        "the refund",
        lambda: get_client().payments.refund_captured_payment(
            capture_id,
            pay_pal_request_id=request_id,
            prefer=_REPRESENTATION,
            body=RefundRequest(
                amount=_money(currency, amount),
                invoice_id=invoice_id,
                note_to_payer=note if note else UNSET,
            ),
        ),
    )
    refund_id = _str(refunded.id)
    status = _str(refunded.status)
    if refund_id is None or status is None:
        raise unknown_outcome("the refund", "a refund id and status")
    return RefundResult(refund_id=refund_id, status=status)


def vault_card(card: CardDetails, *, customer_id: str | None, request_id: str) -> VaultedCard:
    body = PaymentTokenRequest(
        customer=Customer(id=customer_id) if customer_id else UNSET,
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=card.name if card.name else UNSET,
            )
        ),
    )
    token = call_paypal(
        "saving the card",
        lambda: get_client().vault.create_payment_token(body, pay_pal_request_id=request_id),
    )
    token_id = _str(token.id)
    if token_id is None:
        raise unknown_outcome("saving the card", "a payment token id")
    customer = None if isinstance(token.customer, UnsetType) else _str(token.customer.id)
    brand = last_digits = expiry = name = ""
    if not isinstance(token.payment_source, UnsetType) and not isinstance(token.payment_source.card, UnsetType):
        saved = token.payment_source.card
        brand = _str(saved.brand) or ""
        last_digits = _str(saved.last_digits) or ""
        expiry = _str(saved.expiry) or ""
        name = _str(saved.name) or ""
    return VaultedCard(
        token_id=token_id,
        customer_id=customer,
        brand=brand,
        last_digits=last_digits or card.number[-4:],
        expiry=expiry or card.expiry,
        name=name or (card.name or ""),
    )


def delete_vault_token(token_id: str) -> None:
    # Returns None on success; any failure raises through the boundary.
    call_paypal("removing the saved card", lambda: get_client().vault.delete_payment_token(token_id))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_transactions(start: datetime, end: datetime) -> TransactionPage:
    """
    PayPal's own transaction records between ``start`` and ``end``: every page
    of every <=31-day window the range splits into.
    """
    seen: dict[str, PayPalTransaction] = {}
    last_refreshed: str | None = None
    window_start = start
    while window_start < end:
        window_end = min(window_start + SEARCH_WINDOW, end)
        page = 1
        while True:
            start_s, end_s, page_n = _stamp(window_start), _stamp(window_end), page
            result = call_paypal(
                "the transaction search",
                lambda: get_client().transaction_search.search_transactions(start_s, end_s, page=page_n),
            )
            last_refreshed = _str(result.last_refreshed_datetime) or last_refreshed
            for detail in result.transaction_details if isinstance(result.transaction_details, list) else []:
                info = detail.transaction_info
                if isinstance(info, UnsetType):
                    continue
                txn_id = _str(info.transaction_id)
                if txn_id is None:
                    continue
                amount = None if isinstance(info.transaction_amount, UnsetType) else info.transaction_amount
                fee = None if isinstance(info.fee_amount, UnsetType) else info.fee_amount
                seen[txn_id] = PayPalTransaction(
                    transaction_id=txn_id,
                    event_code=_str(info.transaction_event_code) or "",
                    status=_str(info.transaction_status) or "",
                    initiated_at=_str(info.transaction_initiation_date) or "",
                    amount=from_paypal(amount.value) if amount else None,
                    currency=amount.currency_code if amount else None,
                    fee=from_paypal(fee.value) if fee else None,
                    invoice_id=_str(info.invoice_id),
                    custom_field=_str(info.custom_field),
                    reference_id=_str(info.paypal_reference_id),
                )
            total_pages = result.total_pages if isinstance(result.total_pages, int) else 1
            if page >= total_pages:
                break
            page += 1
        window_start = window_end
    return TransactionPage(transactions=list(seen.values()), last_refreshed=last_refreshed)
