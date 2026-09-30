"""
The PayPal gateway: the only module that talks to the PayPal Server SDK.

Every function makes one SDK call through ``errors.call`` (so every failure
arrives as ``ProviderError``), asserts on the members the caller depends on,
and returns plain dataclasses - no SDK model or ``UNSET`` leaves this module.
Card numbers and security codes pass through here in memory only; they are
never stored or logged.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from decimal import Decimal
from typing import TypeVar

from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import UNSET, UnsetType
from pay_pal_server_sdk.models import (
    Address,
    AmountWithBreakdown,
    AuthorizationWithAdditionalData,
    CaptureRequest,
    CapturedPayment,
    CardRequest,
    CardResponse,
    Customer,
    Money,
    Order,
    OrderAuthorizeResponse,
    OrderRequest,
    OrdersCapture,
    PaymentAuthorization,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PaymentTokenResponse,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    Refund,
    RefundRequest,
    TransactionInformation,
)
from pay_pal_server_sdk.models.enums import CheckoutPaymentIntent

from . import money
from .errors import ProviderError, call, unknown_from_missing

T = TypeVar("T")

REPRESENTATION = "return=representation"

# transaction_search: "The maximum supported range is 31 days."
SEARCH_WINDOW = timedelta(days=31)
SEARCH_PAGE_SIZE = 100


def _v(value: T | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _s(value: object) -> str | None:
    """An open enum arrives as its Enum member or, when newer than the SDK, a plain str."""
    if value is None or isinstance(value, UnsetType):
        return None
    if isinstance(value, enum.Enum):
        return str(value.value)
    return str(value)


def _time(value: str | UnsetType | None) -> datetime | None:
    raw = _v(value) if value is not None else None
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _money(value: Money | UnsetType | None) -> tuple[Decimal | None, str | None]:
    m = _v(value) if value is not None else None
    if m is None:
        return None, None
    return money.parse(m.value), m.currency_code


# --------------------------------------------------------------------------
# Snapshots handed back to the service layer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CardDetails:
    """A card as the shopper typed it. Lives in memory for one request only."""

    number: str
    expiry: str  # YYYY-MM
    security_code: str
    name: str | None = None
    billing_address: dict[str, str] | None = None

    def __repr__(self) -> str:  # never let a card number reach a log or traceback
        return f"CardDetails(last4={self.number[-4:]!r}, expiry={self.expiry!r})"


@dataclass(frozen=True)
class AuthorizationSnapshot:
    id: str
    status: str | None
    amount: Decimal | None
    currency: str | None
    create_time: datetime | None
    expiration_time: datetime | None


@dataclass(frozen=True)
class CaptureSnapshot:
    id: str
    status: str | None
    amount: Decimal | None
    currency: str | None
    paypal_fee: Decimal | None
    net_amount: Decimal | None
    invoice_id: str | None
    create_time: datetime | None


@dataclass(frozen=True)
class RefundSnapshot:
    id: str
    status: str | None
    amount: Decimal | None
    currency: str | None
    custom_id: str | None
    create_time: datetime | None


@dataclass(frozen=True)
class OrderSnapshot:
    id: str
    status: str | None
    card_brand: str | None
    card_last_digits: str | None
    authorizations: list[AuthorizationSnapshot] = field(default_factory=list)
    captures: list[CaptureSnapshot] = field(default_factory=list)
    refunds: list[RefundSnapshot] = field(default_factory=list)


@dataclass(frozen=True)
class VaultedCard:
    token_id: str
    customer_id: str | None
    brand: str | None
    last_digits: str | None
    expiry: str | None


@dataclass(frozen=True)
class TransactionRow:
    transaction_id: str | None
    reference_id: str | None
    event_code: str | None
    status: str | None
    initiated_at: datetime | None
    amount: Decimal | None
    currency: str | None
    fee: Decimal | None
    invoice_id: str | None
    custom_field: str | None


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------


def _authorization(a: AuthorizationWithAdditionalData | PaymentAuthorization, what: str) -> AuthorizationSnapshot:
    auth_id = _v(a.id)
    if not auth_id:
        raise unknown_from_missing(what, "authorization id")
    amount, currency = _money(a.amount)
    return AuthorizationSnapshot(
        id=auth_id,
        status=_s(a.status),
        amount=amount,
        currency=currency,
        create_time=_time(a.create_time),
        expiration_time=_time(a.expiration_time),
    )


def _capture(c: CapturedPayment | OrdersCapture, what: str) -> CaptureSnapshot:
    capture_id = _v(c.id)
    if not capture_id:
        raise unknown_from_missing(what, "capture id")
    amount, currency = _money(c.amount)
    fee = net = None
    breakdown = _v(c.seller_receivable_breakdown)
    if breakdown is not None:
        fee, _ = _money(breakdown.paypal_fee)
        net, _ = _money(breakdown.net_amount)
    return CaptureSnapshot(
        id=capture_id,
        status=_s(c.status),
        amount=amount,
        currency=currency,
        paypal_fee=fee,
        net_amount=net,
        invoice_id=_v(c.invoice_id),
        create_time=_time(c.create_time),
    )


def _refund(r: Refund, what: str) -> RefundSnapshot:
    refund_id = _v(r.id)
    if not refund_id:
        raise unknown_from_missing(what, "refund id")
    amount, currency = _money(r.amount)
    return RefundSnapshot(
        id=refund_id,
        status=_s(r.status),
        amount=amount,
        currency=currency,
        custom_id=_v(r.custom_id),
        create_time=_time(r.create_time),
    )


def _order(o: Order | OrderAuthorizeResponse, what: str) -> OrderSnapshot:
    order_id = _v(o.id)
    if not order_id:
        raise unknown_from_missing(what, "order id")
    brand = last_digits = None
    card: CardResponse | None = None
    if isinstance(o, Order):
        order_source = _v(o.payment_source)
        card = _v(order_source.card) if order_source is not None else None
    else:
        auth_source = _v(o.payment_source)
        card = _v(auth_source.card) if auth_source is not None else None
    if card is not None:
        brand = _s(card.brand)
        last_digits = _v(card.last_digits)
    authorizations: list[AuthorizationSnapshot] = []
    captures: list[CaptureSnapshot] = []
    refunds: list[RefundSnapshot] = []
    for unit in _v(o.purchase_units) or []:
        payments = _v(unit.payments)
        if payments is None:
            continue
        authorizations += [_authorization(a, what) for a in _v(payments.authorizations) or []]
        captures += [_capture(c, what) for c in _v(payments.captures) or []]
        refunds += [_refund(r, what) for r in _v(payments.refunds) or []]
    return OrderSnapshot(
        id=order_id,
        status=_s(o.status),
        card_brand=brand,
        card_last_digits=last_digits,
        authorizations=authorizations,
        captures=captures,
        refunds=refunds,
    )


def _vaulted(t: PaymentTokenResponse, what: str) -> VaultedCard:
    token_id = _v(t.id)
    if not token_id:
        raise unknown_from_missing(what, "payment token id")
    customer = _v(t.customer)
    source = _v(t.payment_source)
    card = _v(source.card) if source is not None else None
    return VaultedCard(
        token_id=token_id,
        customer_id=_v(customer.id) if customer is not None else None,
        brand=_s(card.brand) if card is not None else None,
        last_digits=_v(card.last_digits) if card is not None else None,
        expiry=_v(card.expiry) if card is not None else None,
    )


def _address(data: dict[str, str] | None) -> Address | None:
    if not data or not data.get("country_code"):
        return None
    return Address.model_validate(
        {k: v for k, v in data.items() if k in {
            "address_line_1", "address_line_2", "admin_area_1", "admin_area_2", "postal_code", "country_code",
        } and v}
    )


def _card_request(card: CardDetails) -> CardRequest:
    kwargs: dict[str, object] = {
        "number": card.number,
        "expiry": card.expiry,
        "security_code": card.security_code,
    }
    if card.name:
        kwargs["name"] = card.name
    address = _address(card.billing_address)
    if address is not None:
        kwargs["billing_address"] = address
    return CardRequest.model_validate(kwargs)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def create_order(
    client: PayPalServerSdkClient,
    *,
    amount: Decimal,
    currency: str,
    reference_id: str,
    invoice_id: str,
    custom_id: str,
    description: str,
    request_id: str,
    card: CardDetails | None = None,
    vault_id: str | None = None,
) -> OrderSnapshot:
    """Create a PayPal order with intent AUTHORIZE, paid by a card or a vaulted card."""
    if (card is None) == (vault_id is None):
        raise ValueError("exactly one of card / vault_id is required")
    card_request = _card_request(card) if card is not None else CardRequest(vault_id=vault_id or "")
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=reference_id,
                invoice_id=invoice_id,
                custom_id=custom_id,
                description=description[:127],
                amount=AmountWithBreakdown(currency_code=currency, value=money.to_wire(amount, currency)),
            )
        ],
        payment_source=PaymentSource(card=card_request),
    )
    result = call(
        lambda: client.orders.create_order(body, pay_pal_request_id=request_id, prefer=REPRESENTATION),
        what="create order",
    )
    return _order(result, "create order")


def authorize_order(client: PayPalServerSdkClient, paypal_order_id: str, *, request_id: str) -> OrderSnapshot:
    result = call(
        lambda: client.orders.authorize_order(paypal_order_id, pay_pal_request_id=request_id, prefer=REPRESENTATION),
        what="authorize order",
    )
    return _order(result, "authorize order")


def get_order(client: PayPalServerSdkClient, paypal_order_id: str) -> OrderSnapshot:
    return _order(call(lambda: client.orders.get_order(paypal_order_id), what="get order"), "get order")


def get_authorization(client: PayPalServerSdkClient, authorization_id: str) -> AuthorizationSnapshot:
    result = call(lambda: client.payments.get_authorized_payment(authorization_id), what="get authorization")
    return _authorization(result, "get authorization")


def reauthorize(
    client: PayPalServerSdkClient, authorization_id: str, *, amount: Decimal, currency: str, request_id: str
) -> AuthorizationSnapshot:
    body = ReauthorizeRequest(amount=Money(currency_code=currency, value=money.to_wire(amount, currency)))
    result = call(
        lambda: client.payments.reauthorize_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION, body=body
        ),
        what="reauthorize",
    )
    return _authorization(result, "reauthorize")


def capture(
    client: PayPalServerSdkClient,
    authorization_id: str,
    *,
    amount: Decimal,
    currency: str,
    invoice_id: str,
    request_id: str,
) -> CaptureSnapshot:
    body = CaptureRequest(
        amount=Money(currency_code=currency, value=money.to_wire(amount, currency)),
        invoice_id=invoice_id,
        final_capture=True,
    )
    result = call(
        lambda: client.payments.capture_authorized_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION, body=body
        ),
        what="capture",
    )
    return _capture(result, "capture")


def get_capture(client: PayPalServerSdkClient, capture_id: str) -> CaptureSnapshot:
    return _capture(call(lambda: client.payments.get_captured_payment(capture_id), what="get capture"), "get capture")


def void(client: PayPalServerSdkClient, authorization_id: str, *, request_id: str) -> AuthorizationSnapshot | None:
    """Void an authorization. PayPal may answer with no body; then the caller re-reads."""
    result = call(
        lambda: client.payments.void_payment(authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION),
        what="void",
    )
    if _v(result.id) is None:
        return None
    return _authorization(result, "void")


def refund(
    client: PayPalServerSdkClient,
    capture_id: str,
    *,
    amount: Decimal,
    currency: str,
    custom_id: str,
    request_id: str,
) -> RefundSnapshot:
    body = RefundRequest(
        amount=Money(currency_code=currency, value=money.to_wire(amount, currency)),
        custom_id=custom_id,
    )
    result = call(
        lambda: client.payments.refund_captured_payment(
            capture_id, pay_pal_request_id=request_id, prefer=REPRESENTATION, body=body
        ),
        what="refund",
    )
    return _refund(result, "refund")


def vault_card(
    client: PayPalServerSdkClient, card: CardDetails, *, customer_id: str | None, request_id: str
) -> VaultedCard:
    card_kwargs: dict[str, object] = {
        "number": card.number,
        "expiry": card.expiry,
        "security_code": card.security_code,
    }
    if card.name:
        card_kwargs["name"] = card.name
    address = _address(card.billing_address)
    if address is not None:
        card_kwargs["billing_address"] = address
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard.model_validate(card_kwargs)),
        customer=Customer(id=customer_id) if customer_id else UNSET,
    )
    result = call(
        lambda: client.vault.create_payment_token(body, pay_pal_request_id=request_id),
        what="save card",
    )
    return _vaulted(result, "save card")


def list_vaulted_cards(client: PayPalServerSdkClient, customer_id: str) -> list[VaultedCard]:
    cards: list[VaultedCard] = []
    page = 1
    while True:
        result = call(
            partial(client.vault.list_customer_payment_tokens, customer_id, page_size=20, page=page),
            what="list saved cards",
        )
        cards += [_vaulted(t, "list saved cards") for t in _v(result.payment_tokens) or []]
        total_pages = _v(result.total_pages) or 1
        if page >= total_pages:
            return cards
        page += 1


def delete_vaulted_card(client: PayPalServerSdkClient, token_id: str) -> None:
    """Delete a vaulted card. A token PayPal no longer has counts as deleted."""
    try:
        call(lambda: client.vault.delete_payment_token(token_id), what="delete saved card")
    except ProviderError as e:
        if e.provider_status == 404:
            return
        raise


def search_transactions(client: PayPalServerSdkClient, start: datetime, end: datetime) -> Iterator[TransactionRow]:
    """Every transaction PayPal reports in [start, end): all 31-day windows, all pages."""
    window_start = start
    while window_start < end:
        window_end = min(window_start + SEARCH_WINDOW, end)
        page = 1
        while True:
            s = window_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            e = window_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            result = call(
                partial(
                    client.transaction_search.search_transactions,
                    s, e, balance_affecting_records_only="N", page_size=SEARCH_PAGE_SIZE, page=page,
                ),
                what="search transactions",
            )
            for detail in _v(result.transaction_details) or []:
                info = _v(detail.transaction_info)
                if info is not None:
                    yield _transaction(info)
            total_pages = _v(result.total_pages) or 1
            if page >= total_pages:
                break
            page += 1
        window_start = window_end


def _transaction(info: TransactionInformation) -> TransactionRow:
    amount, currency = _money(info.transaction_amount)
    fee, _ = _money(info.fee_amount)
    return TransactionRow(
        transaction_id=_v(info.transaction_id),
        reference_id=_v(info.paypal_reference_id),
        event_code=_v(info.transaction_event_code),
        status=_v(info.transaction_status),
        initiated_at=_time(info.transaction_initiation_date),
        amount=amount,
        currency=currency,
        fee=fee,
        invoice_id=_v(info.invoice_id),
        custom_field=_v(info.custom_field),
    )
