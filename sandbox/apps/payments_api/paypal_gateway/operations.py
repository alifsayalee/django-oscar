"""Every PayPal SDK call this app makes, with the request it builds and how its answer is read.

Nothing here touches the database; ``services`` composes these with the claim store.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from paypal import PaypalClient
from paypal.core import UNSET, ApiError, Failure, Success, UnsetType
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
    OrderRequest,
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
from paypal.models.enums import CheckoutPaymentIntent

from .errors import guarded_read, provider_error
from .money import to_wire
from .outcomes import OrderWithoutHold
from .safe_write import Answer

REPRESENTATION = "return=representation"  # the default "return=minimal" omits the payments we must read

# How long PayPal keeps each operation's PayPal-Request-Id (from each operation's docstring). A resend
# after this could create a second write, so the safe write stops resending and leaves it unknown.
REQUEST_ID_RETENTION = {
    "pay": timedelta(hours=6),
    "reauthorize": timedelta(days=45),
    "capture": timedelta(days=45),
    "void": timedelta(days=45),
    "refund": timedelta(days=45),
    "vault": timedelta(hours=3),
}


@dataclass(frozen=True)
class BillingAddress:
    country_code: str
    address_line_1: str = ""
    address_line_2: str = ""
    admin_area_2: str = ""
    admin_area_1: str = ""
    postal_code: str = ""

    def to_model(self) -> Address:
        fields: dict[str, str] = {
            k: v
            for k, v in (
                ("address_line_1", self.address_line_1),
                ("address_line_2", self.address_line_2),
                ("admin_area_2", self.admin_area_2),
                ("admin_area_1", self.admin_area_1),
                ("postal_code", self.postal_code),
            )
            if v
        }
        return Address(country_code=self.country_code, **fields)


@dataclass(frozen=True)
class CardInput:
    """Full card details: held in memory for one request only, never stored and never logged."""

    number: str
    expiry: str  # YYYY-MM
    security_code: str
    name: str
    billing_address: BillingAddress | None

    def __repr__(self) -> str:  # keep the PAN and CVC out of any accidental repr/log
        return f"CardInput(last4={self.number[-4:]!r}, expiry={self.expiry!r})"


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _money(m: object) -> tuple[str | None, str | None]:
    if isinstance(m, Money):
        return m.value, m.currency_code
    return None, None


# --- hold (create order with intent AUTHORIZE) --------------------------------------------------------

def create_hold(
    client: PaypalClient,
    reference: str,
    *,
    amount: Decimal,
    currency: str,
    order_number: str,
    custom_id: str,
    card: CardInput | None = None,
    vault_id: str | None = None,
) -> Order:
    if (card is None) == (vault_id is None):
        raise ValueError("exactly one of card / vault_id")
    if card is not None:
        card_request = CardRequest(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            name=card.name,
            billing_address=card.billing_address.to_model() if card.billing_address else UNSET,
        )
    else:
        assert vault_id is not None
        card_request = CardRequest(vault_id=vault_id)
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=order_number,
                custom_id=custom_id,
                invoice_id=reference,
                description=f"Order {order_number}",
                amount=AmountWithBreakdown(currency_code=currency, value=to_wire(amount, currency)),
            )
        ],
        payment_source=PaymentSource(card=card_request),
    )
    return client.orders.create_order(body, pay_pal_request_id=reference, prefer=REPRESENTATION)


def authorization_of(order: Order) -> AuthorizationWithAdditionalData | None:
    units = order.purchase_units
    if isinstance(units, UnsetType) or not units:
        return None
    payments = units[0].payments
    if isinstance(payments, UnsetType):
        return None
    auths = payments.authorizations
    if isinstance(auths, UnsetType) or not auths:
        return None
    return auths[0]


def read_hold(order: Order) -> Answer:
    auth = authorization_of(order)
    if auth is None:
        amount, currency = None, None
        units = order.purchase_units
        if not isinstance(units, UnsetType) and units and not isinstance(units[0].amount, UnsetType):
            amount, currency = units[0].amount.value, units[0].amount.currency_code
        return Answer(_str(order.id), OrderWithoutHold(order.status), parse_time(order.create_time),
                      amount, currency)
    amount, currency = _money(auth.amount)
    return Answer(_str(auth.id), auth.status, parse_time(auth.create_time), amount, currency)


def card_summary(order: Order) -> tuple[str, str]:
    """(brand, last digits) as PayPal describes the card used - never the number."""
    source = order.payment_source
    if isinstance(source, UnsetType) or isinstance(source.card, UnsetType):
        return "", ""
    card = source.card
    brand = card.brand if not isinstance(card.brand, UnsetType) else ""
    last = card.last_digits if not isinstance(card.last_digits, UnsetType) else ""
    return str(brand), last


# --- authorization ------------------------------------------------------------------------------------

def get_authorization(client: PaypalClient, authorization_id: str) -> PaymentAuthorization:
    return guarded_read(lambda: client.payments.get_authorized_payment(authorization_id))


def reauthorize(client: PaypalClient, authorization_id: str, reference: str, *,
                amount: Decimal, currency: str) -> PaymentAuthorization:
    return client.payments.reauthorize_payment(
        authorization_id,
        pay_pal_request_id=reference,
        prefer=REPRESENTATION,
        body=ReauthorizeRequest(amount=Money(currency_code=currency, value=to_wire(amount, currency))),
    )


def read_authorization(auth: PaymentAuthorization) -> Answer:
    amount, currency = _money(auth.amount)
    return Answer(_str(auth.id), auth.status, parse_time(auth.create_time), amount, currency)


def void(client: PaypalClient, authorization_id: str, reference: str) -> PaymentAuthorization:
    return client.payments.void_payment(authorization_id, pay_pal_request_id=reference, prefer=REPRESENTATION)


def read_void(auth: PaymentAuthorization) -> Answer:
    return Answer(_str(auth.id), auth.status, parse_time(auth.update_time) or parse_time(auth.create_time))


# --- capture ------------------------------------------------------------------------------------------

def capture(client: PaypalClient, authorization_id: str, reference: str, *,
            amount: Decimal, currency: str) -> CapturedPayment:
    return client.payments.capture_authorized_payment(
        authorization_id,
        pay_pal_request_id=reference,
        prefer=REPRESENTATION,
        body=CaptureRequest(amount=Money(currency_code=currency, value=to_wire(amount, currency)),
                            final_capture=True),
    )


def get_capture(client: PaypalClient, capture_id: str) -> CapturedPayment:
    return guarded_read(lambda: client.payments.get_captured_payment(capture_id))


def read_capture(cap: CapturedPayment) -> Answer:
    amount, currency = _money(cap.amount)
    return Answer(_str(cap.id), cap.status, parse_time(cap.create_time), amount, currency)


@dataclass(frozen=True)
class Breakdown:
    gross: str | None
    fee: str | None
    net: str | None


def breakdown_of(cap: CapturedPayment) -> Breakdown:
    b = cap.seller_receivable_breakdown
    if isinstance(b, UnsetType):
        return Breakdown(None, None, None)
    return Breakdown(_money(b.gross_amount)[0], _money(b.paypal_fee)[0], _money(b.net_amount)[0])


# --- refund -------------------------------------------------------------------------------------------

def refund(client: PaypalClient, capture_id: str, reference: str, *, amount: Decimal, currency: str,
           custom_id: str, note: str | None = None) -> Refund:
    body = RefundRequest(
        amount=Money(currency_code=currency, value=to_wire(amount, currency)),
        custom_id=custom_id,
        invoice_id=reference,
        note_to_payer=note if note else UNSET,
    )
    return client.payments.refund_captured_payment(capture_id, pay_pal_request_id=reference,
                                                   prefer=REPRESENTATION, body=body)


def get_refund(client: PaypalClient, refund_id: str) -> Refund:
    return guarded_read(lambda: client.payments.get_refund(refund_id))


def read_refund(r: Refund) -> Answer:
    amount, currency = _money(r.amount)
    return Answer(_str(r.id), r.status, parse_time(r.create_time), amount, currency)


# --- vault --------------------------------------------------------------------------------------------

def vault_card(client: PaypalClient, reference: str, card: CardInput, *, merchant_customer_id: str,
               customer_id: str | None) -> PaymentTokenResponse:
    customer = Customer(id=customer_id) if customer_id else Customer(merchant_customer_id=merchant_customer_id)
    body = PaymentTokenRequest(
        customer=customer,
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=card.name,
                billing_address=card.billing_address.to_model() if card.billing_address else UNSET,
            )
        ),
    )
    return client.vault.create_payment_token(body, pay_pal_request_id=reference)


@dataclass(frozen=True)
class VaultedCard:
    token_id: str
    customer_id: str | None
    brand: str
    last_digits: str
    expiry: str


def vaulted_card_of(token: PaymentTokenResponse) -> VaultedCard | None:
    if isinstance(token.id, UnsetType) or isinstance(token.payment_source, UnsetType):
        return None
    card = token.payment_source.card
    if isinstance(card, UnsetType):
        return None
    customer_id = None
    if not isinstance(token.customer, UnsetType) and not isinstance(token.customer.id, UnsetType):
        customer_id = token.customer.id
    return VaultedCard(
        token_id=token.id,
        customer_id=customer_id,
        brand=str(card.brand) if not isinstance(card.brand, UnsetType) else "",
        last_digits=card.last_digits if not isinstance(card.last_digits, UnsetType) else "",
        expiry=card.expiry if not isinstance(card.expiry, UnsetType) else "",
    )


def get_token(client: PaypalClient, token_id: str) -> PaymentTokenResponse:
    return guarded_read(lambda: client.vault.get_payment_token(token_id))


def read_vault(token: PaymentTokenResponse) -> Answer:
    vaulted = vaulted_card_of(token)
    # No status member exists on this response; the token existing with a card IS the done state.
    return Answer(vaulted.token_id if vaulted else None, vaulted is not None,
                  parse_time((token.model_extra or {}).get("create_time")))


def delete_token(client: PaypalClient, token_id: str) -> int:
    """Delete a vaulted card. Returns the HTTP status; raises ProviderError on a definite refusal.

    The operation returns None, so the raw peer is the only way to see the status code.
    """
    try:
        result = client.vault.with_raw_response.delete_payment_token(token_id)
    except ApiError as exc:  # a failed token fetch raises even in raw mode
        raise provider_error(exc.status_code, exc.error) from exc
    match result:
        case Success(response=response):
            return response.status_code
        case Failure(error=error, response=response):
            if response.status_code == 404:
                return 404  # already gone
            raise provider_error(response.status_code, error)
    raise AssertionError("unreachable")


# --- transaction search -------------------------------------------------------------------------------

SEARCH_WINDOW = timedelta(days=31)  # the maximum range search_transactions supports
SEARCH_PAGE_SIZE = 100


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SearchPage:
    transactions: list[TransactionInformation]
    last_refreshed: datetime | None


def search_transactions(client: PaypalClient, start: datetime, end: datetime) -> Iterator[SearchPage]:
    """Every page of every <=31-day window covering [start, end)."""
    window_start = start
    while window_start < end:
        window_end = min(window_start + SEARCH_WINDOW - timedelta(seconds=1), end)
        page = 1
        while True:
            current_page = page
            ws, we = _rfc3339(window_start), _rfc3339(window_end)
            response = guarded_read(
                lambda: client.transaction_search.search_transactions(
                    ws, we, page=current_page, page_size=SEARCH_PAGE_SIZE, fields="transaction_info",
                    balance_affecting_records_only="Y",
                )
            )
            details = response.transaction_details
            infos = []
            if not isinstance(details, UnsetType):
                infos = [d.transaction_info for d in details if not isinstance(d.transaction_info, UnsetType)]
            yield SearchPage(infos, parse_time(response.last_refreshed_datetime))
            total_pages = response.total_pages if not isinstance(response.total_pages, UnsetType) else 1
            if page >= total_pages or not infos:
                break
            page += 1
        window_start = window_end + timedelta(seconds=1)
