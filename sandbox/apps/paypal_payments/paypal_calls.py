"""
Typed wrappers around the PayPal SDK operations this app uses.

Each function builds the request, runs it through ``gateway.call`` and maps the
decoded response into a small plain-Python result, resolving the SDK's
``UNSET`` sentinel to ``None`` so nothing SDK-specific leaks past this module.
Provider statuses are mapped by enum member name here and nowhere else; a value
this SDK does not list maps to ``"unknown"``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from django.views.decorators.debug import sensitive_variables
from paypal.core import UNSET, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    AuthorizationWithAdditionalData,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Customer,
    Money,
    Order,
    OrderAuthorizeResponse,
    OrderRequest,
    PaymentAuthorization,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    Refund,
    RefundRequest,
    SearchResponse,
)
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CardVerificationStatus,
    CheckoutPaymentIntent,
    OrderStatus,
    RefundStatus,
)

from . import gateway

REPRESENTATION = "return=representation"


# --------------------------------------------------------------------------
# Plain results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CardInput:
    number: str
    expiry: str  # YYYY-MM
    security_code: str
    name: str
    billing_address: dict[str, str] | None = None

    def __repr__(self) -> str:  # never print card data
        return "CardInput(****)"


@dataclass(frozen=True)
class AuthorizationInfo:
    id: str
    outcome: str  # authorized | pending | failed | voided | captured | unknown
    raw_status: str
    reason: str | None
    amount: tuple[Decimal, str] | None
    created_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True)
class OrderAuthResult:
    paypal_order_id: str
    order_outcome: str  # completed | needs_authorize | payer_action | failed | unknown
    raw_status: str
    authorization: AuthorizationInfo | None
    card_brand: str | None
    card_last_digits: str | None


@dataclass(frozen=True)
class CaptureInfo:
    id: str
    outcome: str  # completed | pending | failed | unknown
    raw_status: str
    reason: str | None
    amount: tuple[Decimal, str] | None
    gross: Decimal | None
    paypal_fee: Decimal | None
    net: Decimal | None
    created_at: datetime | None


@dataclass(frozen=True)
class RefundInfo:
    id: str
    outcome: str  # completed | pending | failed | unknown
    raw_status: str
    reason: str | None
    amount: tuple[Decimal, str] | None
    created_at: datetime | None


@dataclass(frozen=True)
class VaultedCard:
    token_id: str
    customer_id: str | None
    brand: str | None
    last_digits: str | None
    expiry: str | None
    name: str | None
    verification_failed: bool


@dataclass(frozen=True)
class ProviderTransaction:
    transaction_id: str
    event_code: str | None
    status: str | None
    initiated_at: datetime | None
    amount: tuple[Decimal, str] | None
    fee: tuple[Decimal, str] | None
    invoice_id: str | None
    custom_field: str | None
    paypal_reference_id: str | None


@dataclass
class SearchResult:
    transactions: list[ProviderTransaction] = field(default_factory=list)
    last_refreshed_at: datetime | None = None
    truncated: bool = False
    pages_fetched: int = 0


class UnreadableResponse(gateway.ProviderUnavailable):
    """A 2xx whose body lacks what we depend on: the call may have taken effect."""

    def __init__(self, what: str) -> None:
        super().__init__(
            502,
            "PayPal's response did not include %s; the outcome is unknown." % what,
            outcome_unknown=True,
        )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _s(value: str | UnsetType) -> str | None:
    return None if isinstance(value, UnsetType) else value


def _enum_str(value: object) -> str:
    if isinstance(value, UnsetType) or value is None:
        return ""
    return str(value)


_TZ_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_time(value: str | UnsetType | None) -> datetime | None:
    """Parse PayPal's RFC3339 timestamps (``Z``, ``+00:00`` or ``+0000``)."""
    if value is None or isinstance(value, UnsetType) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    text = _TZ_NO_COLON.sub(r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _money(value: Money | UnsetType) -> tuple[Decimal, str] | None:
    if isinstance(value, UnsetType):
        return None
    try:
        return Decimal(value.value), value.currency_code
    except InvalidOperation:
        return None


def _amount_only(value: Money | UnsetType) -> Decimal | None:
    parsed = _money(value)
    return parsed[0] if parsed else None


def _address(data: dict[str, str] | None) -> Address | UnsetType:
    if not data or not data.get("countryCode"):
        return UNSET
    fields = {
        "address_line_1": data.get("addressLine1"),
        "address_line_2": data.get("addressLine2"),
        "admin_area_2": data.get("adminArea2"),
        "admin_area_1": data.get("adminArea1"),
        "postal_code": data.get("postalCode"),
    }
    return Address(
        country_code=data["countryCode"],
        **{k: v for k, v in fields.items() if v},  # omit, never send null
    )


# --------------------------------------------------------------------------
# Status mapping — the only place a PayPal status becomes ours
# --------------------------------------------------------------------------


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


def order_outcome(status: object) -> str:
    match status:
        case OrderStatus.COMPLETED:
            return "completed"
        case OrderStatus.CREATED | OrderStatus.APPROVED | OrderStatus.SAVED:
            return "needs_authorize"
        case OrderStatus.PAYER_ACTION_REQUIRED:
            return "payer_action"
        case OrderStatus.VOIDED:
            return "failed"
        case _:
            return "unknown"


def capture_outcome(status: object) -> str:
    match status:
        case (
            CaptureStatus.COMPLETED
            | CaptureStatus.PARTIALLY_REFUNDED
            | CaptureStatus.REFUNDED
        ):
            return "completed"
        case CaptureStatus.PENDING:
            return "pending"
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return "failed"
        case _:
            return "unknown"


def refund_outcome(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return "completed"
        case RefundStatus.PENDING:
            return "pending"
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return "failed"
        case _:
            return "unknown"


# --------------------------------------------------------------------------
# Mappers
# --------------------------------------------------------------------------


def _authorization(
    auth: AuthorizationWithAdditionalData | PaymentAuthorization,
) -> AuthorizationInfo:
    auth_id = _s(auth.id)
    if not auth_id:
        raise UnreadableResponse("an authorization id")
    reason = None
    if not isinstance(auth.status_details, UnsetType):
        reason = _enum_str(auth.status_details.reason) or None
    return AuthorizationInfo(
        id=auth_id,
        outcome=authorization_outcome(auth.status),
        raw_status=_enum_str(auth.status),
        reason=reason,
        amount=_money(auth.amount),
        created_at=parse_time(auth.create_time),
        expires_at=parse_time(auth.expiration_time),
    )


def _order_result(order: Order | OrderAuthorizeResponse) -> OrderAuthResult:
    order_id = _s(order.id)
    if not order_id:
        raise UnreadableResponse("an order id")
    authorization = None
    if not isinstance(order.purchase_units, UnsetType) and order.purchase_units:
        payments = order.purchase_units[0].payments
        if not isinstance(payments, UnsetType) and not isinstance(
            payments.authorizations, UnsetType
        ):
            if payments.authorizations:
                authorization = _authorization(payments.authorizations[-1])
    brand = last_digits = None
    source = order.payment_source
    if not isinstance(source, UnsetType) and not isinstance(source.card, UnsetType):
        brand = _enum_str(source.card.brand) or None
        last_digits = _s(source.card.last_digits)
    return OrderAuthResult(
        paypal_order_id=order_id,
        order_outcome=order_outcome(order.status),
        raw_status=_enum_str(order.status),
        authorization=authorization,
        card_brand=brand,
        card_last_digits=last_digits,
    )


def _capture(capture: CapturedPayment) -> CaptureInfo:
    capture_id = _s(capture.id)
    if not capture_id:
        raise UnreadableResponse("a capture id")
    gross = fee = net = None
    breakdown = capture.seller_receivable_breakdown
    if not isinstance(breakdown, UnsetType):
        gross = _amount_only(breakdown.gross_amount)
        fee = _amount_only(breakdown.paypal_fee)
        net = _amount_only(breakdown.net_amount)
    reason = None
    if not isinstance(capture.status_details, UnsetType):
        reason = _enum_str(capture.status_details.reason) or None
    return CaptureInfo(
        id=capture_id,
        outcome=capture_outcome(capture.status),
        raw_status=_enum_str(capture.status),
        reason=reason,
        amount=_money(capture.amount),
        gross=gross,
        paypal_fee=fee,
        net=net,
        created_at=parse_time(capture.create_time),
    )


def _refund(refund: Refund) -> RefundInfo:
    refund_id = _s(refund.id)
    if not refund_id:
        raise UnreadableResponse("a refund id")
    reason = None
    if not isinstance(refund.status_details, UnsetType):
        reason = _enum_str(refund.status_details.reason) or None
    return RefundInfo(
        id=refund_id,
        outcome=refund_outcome(refund.status),
        raw_status=_enum_str(refund.status),
        reason=reason,
        amount=_money(refund.amount),
        created_at=parse_time(refund.create_time),
    )


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


@sensitive_variables("card", "source")
def create_authorized_order(
    *,
    request_id: str,
    order_number: str,
    custom_id: str,
    invoice_id: str,
    amount: str,
    currency: str,
    card: CardInput | None = None,
    vault_id: str | None = None,
) -> OrderAuthResult:
    """Single-step order: create with a card (or saved-card token) and intent AUTHORIZE."""
    if card is not None:
        source = PaymentSource(
            card=CardRequest(
                name=card.name,
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                billing_address=_address(card.billing_address),
            )
        )
    elif vault_id:
        source = PaymentSource(card=CardRequest(vault_id=vault_id))
    else:  # pragma: no cover - guarded by the caller
        raise ValueError("card or vault_id is required")
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=order_number,
                custom_id=custom_id,
                invoice_id=invoice_id,
                description="Order %s" % order_number,
                amount=AmountWithBreakdown(currency_code=currency, value=amount),
            )
        ],
        payment_source=source,
    )
    client = gateway.get_client()
    order = gateway.call(
        "create_order",
        lambda: client.orders.create_order(
            body, pay_pal_request_id=request_id, prefer=REPRESENTATION
        ),
        write=True,
    )
    return _order_result(order)


def authorize_order(*, paypal_order_id: str, request_id: str) -> OrderAuthResult:
    client = gateway.get_client()
    response = gateway.call(
        "authorize_order",
        lambda: client.orders.authorize_order(
            paypal_order_id, pay_pal_request_id=request_id, prefer=REPRESENTATION
        ),
        write=True,
    )
    return _order_result(response)


def get_authorization(authorization_id: str) -> AuthorizationInfo:
    client = gateway.get_client()
    auth = gateway.call(
        "get_authorized_payment",
        lambda: client.payments.get_authorized_payment(authorization_id),
        write=False,
    )
    return _authorization(auth)


def reauthorize(
    *, authorization_id: str, request_id: str, amount: str, currency: str
) -> AuthorizationInfo:
    client = gateway.get_client()
    body = ReauthorizeRequest(amount=Money(currency_code=currency, value=amount))
    auth = gateway.call(
        "reauthorize_payment",
        lambda: client.payments.reauthorize_payment(
            authorization_id,
            pay_pal_request_id=request_id,
            prefer=REPRESENTATION,
            body=body,
        ),
        write=True,
    )
    return _authorization(auth)


def capture(
    *, authorization_id: str, request_id: str, amount: str, currency: str
) -> CaptureInfo:
    client = gateway.get_client()
    body = CaptureRequest(
        amount=Money(currency_code=currency, value=amount), final_capture=True
    )
    captured = gateway.call(
        "capture_authorized_payment",
        lambda: client.payments.capture_authorized_payment(
            authorization_id,
            pay_pal_request_id=request_id,
            prefer=REPRESENTATION,
            body=body,
        ),
        write=True,
    )
    return _capture(captured)


def get_capture(capture_id: str) -> CaptureInfo:
    client = gateway.get_client()
    captured = gateway.call(
        "get_captured_payment",
        lambda: client.payments.get_captured_payment(capture_id),
        write=False,
    )
    return _capture(captured)


def void(*, authorization_id: str, request_id: str) -> AuthorizationInfo:
    client = gateway.get_client()
    auth = gateway.call(
        "void_payment",
        lambda: client.payments.void_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION
        ),
        write=True,
    )
    return _authorization(auth)


def refund(
    *, capture_id: str, request_id: str, amount: str, currency: str, note: str | None
) -> RefundInfo:
    client = gateway.get_client()
    body = RefundRequest(amount=Money(currency_code=currency, value=amount))
    if note:
        body = RefundRequest(
            amount=Money(currency_code=currency, value=amount), note_to_payer=note[:255]
        )
    result = gateway.call(
        "refund_captured_payment",
        lambda: client.payments.refund_captured_payment(
            capture_id, pay_pal_request_id=request_id, prefer=REPRESENTATION, body=body
        ),
        write=True,
    )
    return _refund(result)


def get_refund(refund_id: str) -> RefundInfo:
    client = gateway.get_client()
    result = gateway.call(
        "get_refund", lambda: client.payments.get_refund(refund_id), write=False
    )
    return _refund(result)


@sensitive_variables("card", "body")
def vault_card(
    *, request_id: str, card: CardInput, customer_id: str | None
) -> VaultedCard:
    body = PaymentTokenRequest(
        customer=Customer(id=customer_id) if customer_id else UNSET,
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                name=card.name,
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                billing_address=_address(card.billing_address),
            )
        ),
    )
    client = gateway.get_client()
    token = gateway.call(
        "create_payment_token",
        lambda: client.vault.create_payment_token(body, pay_pal_request_id=request_id),
        write=True,
    )
    token_id = _s(token.id)
    if not token_id:
        raise UnreadableResponse("a payment token id")
    customer = None
    if not isinstance(token.customer, UnsetType):
        customer = _s(token.customer.id)
    brand = last_digits = expiry = name = None
    verification_failed = False
    if not isinstance(token.payment_source, UnsetType) and not isinstance(
        token.payment_source.card, UnsetType
    ):
        entity = token.payment_source.card
        brand = _enum_str(entity.brand) or None
        last_digits = _s(entity.last_digits)
        expiry = _s(entity.expiry)
        name = _s(entity.name)
        verification_failed = (
            entity.verification_status == CardVerificationStatus.FAILED
        )
    return VaultedCard(
        token_id=token_id,
        customer_id=customer,
        brand=brand,
        last_digits=last_digits,
        expiry=expiry,
        name=name,
        verification_failed=verification_failed,
    )


def delete_token(token_id: str) -> None:
    """Delete a vaulted card. PayPal's 'not found' counts as already deleted."""
    client = gateway.get_client()
    try:
        gateway.call(
            "delete_payment_token",
            lambda: client.vault.delete_payment_token(token_id),
            write=True,
        )
    except gateway.ProviderRejected as e:
        if e.provider_status != 404:
            raise


def _transaction(details: object) -> ProviderTransaction | None:
    info = getattr(details, "transaction_info", UNSET)
    if isinstance(info, UnsetType):
        return None
    txn_id = _s(info.transaction_id)
    if not txn_id:
        return None
    return ProviderTransaction(
        transaction_id=txn_id,
        event_code=_s(info.transaction_event_code),
        status=_s(info.transaction_status),
        initiated_at=parse_time(info.transaction_initiation_date),
        amount=_money(info.transaction_amount),
        fee=_money(info.fee_amount),
        invoice_id=_s(info.invoice_id),
        custom_field=_s(info.custom_field),
        paypal_reference_id=_s(info.paypal_reference_id),
    )


MAX_WINDOW_DAYS = (
    31  # search_transactions docstring: "The maximum supported range is 31 days."
)
PAGE_SIZE = 100
MAX_PAGES_PER_WINDOW = 100


def _pages(start: datetime, end: datetime) -> Iterator[SearchResponse]:
    client = gateway.get_client()
    page = 1
    for _ in range(MAX_PAGES_PER_WINDOW):
        current = page
        response = gateway.call(
            "search_transactions",
            lambda: client.transaction_search.search_transactions(
                format_time(start),
                format_time(end),
                balance_affecting_records_only="N",
                page_size=PAGE_SIZE,
                page=current,
            ),
            write=False,
        )
        yield response
        total_pages = response.total_pages
        details = response.transaction_details
        if (
            isinstance(total_pages, UnsetType)
            or isinstance(details, UnsetType)
            or not details
        ):
            return
        if page >= total_pages:
            return
        page += 1
    raise _PageCapReached()


class _PageCapReached(Exception):
    pass


def search_transactions(start: datetime, end: datetime) -> SearchResult:
    """Every transaction PayPal reports in [start, end], walking all pages of every ≤31-day window."""
    from datetime import timedelta

    result = SearchResult()
    seen: set[tuple[object, ...]] = (
        set()
    )  # adjoining windows may both report a boundary record
    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=MAX_WINDOW_DAYS), end)
        try:
            for response in _pages(window_start, window_end):
                result.pages_fetched += 1
                refreshed = parse_time(response.last_refreshed_datetime)
                if refreshed and (
                    result.last_refreshed_at is None
                    or refreshed < result.last_refreshed_at
                ):
                    result.last_refreshed_at = refreshed
                if not isinstance(response.transaction_details, UnsetType):
                    for details in response.transaction_details:
                        txn = _transaction(details)
                        if txn is None:
                            continue
                        key = (
                            txn.transaction_id,
                            txn.event_code,
                            txn.initiated_at,
                            txn.amount,
                        )
                        if key not in seen:
                            seen.add(key)
                            result.transactions.append(txn)
        except _PageCapReached:
            result.truncated = True
        window_start = window_end
    return result
