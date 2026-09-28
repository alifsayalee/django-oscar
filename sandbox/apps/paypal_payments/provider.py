"""
Every PayPal call this app makes, the reading of each response into an :class:`Answer`, and the
per-step status → outcome mappers.

Contract facts (signatures, members, enum values) come from pay-pal-server-sdk-plan.md.
No Django imports: this module type-checks under ``mypy --strict``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from paypal import PaypalClient
from paypal.core import UNSET, Failure, Success, UnsetType
from paypal.models import (
    Address, AmountWithBreakdown, CapturedPayment, CaptureRequest, CardRequest, Customer, Money,
    Order, OrderRequest, PaymentAuthorization, PaymentSource, PaymentTokenRequest,
    PaymentTokenRequestCard, PaymentTokenRequestPaymentSource, PaymentTokenResponse,
    PurchaseUnitRequest, ReauthorizeRequest, Refund, RefundRequest)
from paypal.models.enums import (
    AuthorizationStatus, CaptureStatus, CheckoutPaymentIntent, OrderStatus, RefundStatus)

from .gateway import money_str, provider_error

Outcome = Literal['done', 'pending', 'failed', 'unknown']

REPRESENTATION = 'return=representation'
# search_transactions: "The maximum supported range is 31 days."
MAX_SEARCH_WINDOW = timedelta(days=31)


@dataclass(frozen=True)
class BillingAddress:
    country_code: str
    address_line_1: str | None = None
    address_line_2: str | None = None
    admin_area_2: str | None = None
    admin_area_1: str | None = None
    postal_code: str | None = None

    def to_model(self) -> Address:
        return Address(
            country_code=self.country_code,
            address_line_1=self.address_line_1 or UNSET, address_line_2=self.address_line_2 or UNSET,
            admin_area_2=self.admin_area_2 or UNSET, admin_area_1=self.admin_area_1 or UNSET,
            postal_code=self.postal_code or UNSET)


@dataclass(frozen=True)
class CardDetails:
    """Full card details: held in memory for one request only, never stored, never logged."""
    number: str = field(repr=False)
    expiry: str = field(repr=False)            # YYYY-MM
    security_code: str = field(repr=False)
    name: str | None = None
    billing_address: BillingAddress | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return 'CardDetails(****%s)' % self.number[-4:]


@dataclass(frozen=True)
class Answer:
    """What ONE write step's response says, read the same way for every step."""
    outcome: Outcome
    provider_id: str | None
    status: str | None
    provider_time: datetime | None
    amount: Decimal | None = None
    currency: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------------
# small readers
# --------------------------------------------------------------------------------------------

def _opt(value: object) -> Any:
    return None if isinstance(value, UnsetType) else value


def _status(value: object) -> str | None:
    value = _opt(value)
    return None if value is None else str(value)


def parse_time(value: object) -> datetime | None:
    value = _opt(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _money(value: object) -> tuple[Decimal | None, str | None]:
    money = _opt(value)
    if isinstance(money, Money):
        return Decimal(money.value), money.currency_code
    return None, None


def _money_value(value: object) -> str | None:
    amount, _ = _money(value)
    return None if amount is None else str(amount)


def rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# --------------------------------------------------------------------------------------------
# status -> outcome, one mapper per step (plan: OPERATION OUTCOMES)
# --------------------------------------------------------------------------------------------

def authorization_outcome(status: str | None) -> Outcome:
    """Authorize / reauthorize step: is the hold in effect?"""
    match status:
        case AuthorizationStatus.CREATED | AuthorizationStatus.CAPTURED \
                | AuthorizationStatus.PARTIALLY_CAPTURED:
            return 'done'
        case AuthorizationStatus.PENDING:
            return 'pending'
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return 'failed'
        case _:
            return 'unknown'


def order_authorize_outcome(order_status: str | None, authorization_status: str | None) -> Outcome:
    """Authorize step read through the order that carries it."""
    match order_status:
        case OrderStatus.COMPLETED:
            return authorization_outcome(authorization_status)
        case OrderStatus.PAYER_ACTION_REQUIRED | OrderStatus.VOIDED:
            return 'failed'
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED:
            return 'pending'
        case _:
            return 'unknown'


def capture_outcome(status: str | None) -> Outcome:
    match status:
        case CaptureStatus.COMPLETED:
            return 'done'
        case CaptureStatus.PENDING:
            return 'pending'
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return 'failed'
        case CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED:
            return 'failed'          # happened, then undone: never success for the capture step
        case _:
            return 'unknown'


def void_outcome(status: str | None) -> Outcome:
    """The call-off's own mapper: its done is the released hold."""
    match status:
        case AuthorizationStatus.VOIDED | AuthorizationStatus.DENIED:
            return 'done'
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return 'failed'          # too late: money was taken
        case _:
            return 'unknown'


def refund_outcome(status: str | None) -> Outcome:
    match status:
        case RefundStatus.COMPLETED:
            return 'done'
        case RefundStatus.PENDING:
            return 'pending'
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return 'failed'
        case _:
            return 'unknown'


def vault_outcome(token_id: str | None, last_digits: str | None) -> Outcome:
    """PaymentTokenResponse has no status member: the vaulted card echoed back is the evidence."""
    return 'done' if token_id and last_digits else 'unknown'


# --------------------------------------------------------------------------------------------
# authorize (create_order, intent AUTHORIZE, single step with the card / vault id)
# --------------------------------------------------------------------------------------------

def create_authorization(client: PaypalClient, *, ref: str, amount: Decimal, currency: str,
                         custom_id: str, invoice_id: str, description: str,
                         card: CardDetails | None = None, vault_id: str | None = None) -> Order:
    if card is not None:
        card_request = CardRequest(
            number=card.number, expiry=card.expiry, security_code=card.security_code,
            name=card.name or UNSET,
            billing_address=card.billing_address.to_model() if card.billing_address else UNSET)
    elif vault_id:
        card_request = CardRequest(vault_id=vault_id)
    else:
        raise ValueError('card or vault_id is required')
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            amount=AmountWithBreakdown(currency_code=currency, value=money_str(amount, currency)),
            custom_id=custom_id, invoice_id=invoice_id, description=description[:127])],
        payment_source=PaymentSource(card=card_request))
    return client.orders.create_order(body, pay_pal_request_id=ref, prefer=REPRESENTATION)


def get_order(client: PaypalClient, paypal_order_id: str) -> Order:
    return client.orders.get_order(paypal_order_id)


def read_order_authorization(order: Order) -> Answer:
    order_status = _status(order.status)
    authorization = None
    units = _opt(order.purchase_units) or []
    if units:
        payments = _opt(units[0].payments)
        if payments is not None:
            authorizations = _opt(payments.authorizations) or []
            if authorizations:
                authorization = authorizations[-1]
    data: dict[str, Any] = {'paypal_order_id': _opt(order.id), 'paypal_order_status': order_status,
                            'payer_action_required': order_status == OrderStatus.PAYER_ACTION_REQUIRED}
    source = _opt(order.payment_source)
    card = _opt(source.card) if source is not None else None
    if card is not None:
        data['card'] = {'brand': _status(card.brand), 'last_digits': _opt(card.last_digits),
                        'expiry': _opt(card.expiry)}
    if authorization is None:
        return Answer(order_authorize_outcome(order_status, None) if order_status != OrderStatus.COMPLETED
                      else 'unknown', _opt(order.id), order_status, parse_time(order.create_time), data=data)
    auth_status = _status(authorization.status)
    amount, currency = _money(authorization.amount)
    data.update({'authorization_id': _opt(authorization.id), 'authorization_status': auth_status,
                 'expiration_time': _opt(authorization.expiration_time),
                 'status_reason': _status(_opt(authorization.status_details).reason)
                 if _opt(authorization.status_details) is not None else None})
    return Answer(order_authorize_outcome(order_status, auth_status), _opt(authorization.id), auth_status,
                  parse_time(authorization.create_time), amount, currency, data)


# --------------------------------------------------------------------------------------------
# reauthorize / get authorization / void
# --------------------------------------------------------------------------------------------

def reauthorize(client: PaypalClient, authorization_id: str, *, ref: str, amount: Decimal,
                currency: str) -> PaymentAuthorization:
    return client.payments.reauthorize_payment(
        authorization_id, pay_pal_request_id=ref, prefer=REPRESENTATION,
        body=ReauthorizeRequest(amount=Money(currency_code=currency, value=money_str(amount, currency))))


def get_authorization(client: PaypalClient, authorization_id: str) -> PaymentAuthorization:
    return client.payments.get_authorized_payment(authorization_id)


def read_authorization(authorization: PaymentAuthorization, *, for_void: bool = False) -> Answer:
    status = _status(authorization.status)
    amount, currency = _money(authorization.amount)
    outcome = void_outcome(status) if for_void else authorization_outcome(status)
    return Answer(outcome, _opt(authorization.id), status,
                  parse_time(authorization.update_time) or parse_time(authorization.create_time),
                  amount, currency,
                  {'authorization_id': _opt(authorization.id), 'authorization_status': status,
                   'expiration_time': _opt(authorization.expiration_time),
                   'create_time': _opt(authorization.create_time)})


def void(client: PaypalClient, authorization_id: str, *, ref: str) -> PaymentAuthorization:
    return client.payments.void_payment(authorization_id, pay_pal_request_id=ref, prefer=REPRESENTATION)


# --------------------------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------------------------

def capture(client: PaypalClient, authorization_id: str, *, ref: str, amount: Decimal,
            currency: str) -> CapturedPayment:
    return client.payments.capture_authorized_payment(
        authorization_id, pay_pal_request_id=ref, prefer=REPRESENTATION,
        body=CaptureRequest(amount=Money(currency_code=currency, value=money_str(amount, currency)),
                            final_capture=True))


def get_capture(client: PaypalClient, capture_id: str) -> CapturedPayment:
    return client.payments.get_captured_payment(capture_id)


def read_capture(captured: CapturedPayment) -> Answer:
    status = _status(captured.status)
    amount, currency = _money(captured.amount)
    data: dict[str, Any] = {'capture_id': _opt(captured.id), 'capture_status': status}
    breakdown = _opt(captured.seller_receivable_breakdown)
    if breakdown is not None:
        data.update({'gross': _money_value(breakdown.gross_amount),
                     'paypal_fee': _money_value(breakdown.paypal_fee),
                     'net': _money_value(breakdown.net_amount)})
    details = _opt(captured.status_details)
    if details is not None:
        data['status_reason'] = _status(details.reason)
    return Answer(capture_outcome(status), _opt(captured.id), status, parse_time(captured.create_time),
                  amount, currency, data)


# --------------------------------------------------------------------------------------------
# refund
# --------------------------------------------------------------------------------------------

def refund(client: PaypalClient, capture_id: str, *, ref: str, amount: Decimal, currency: str,
           custom_id: str) -> Refund:
    return client.payments.refund_captured_payment(
        capture_id, pay_pal_request_id=ref, prefer=REPRESENTATION,
        body=RefundRequest(amount=Money(currency_code=currency, value=money_str(amount, currency)),
                           custom_id=custom_id))


def get_refund(client: PaypalClient, refund_id: str) -> Refund:
    return client.payments.get_refund(refund_id)


def read_refund(result: Refund) -> Answer:
    status = _status(result.status)
    amount, currency = _money(result.amount)
    data: dict[str, Any] = {'refund_id': _opt(result.id), 'refund_status': status}
    breakdown = _opt(result.seller_payable_breakdown)
    if breakdown is not None:
        data.update({'paypal_fee': _money_value(breakdown.paypal_fee),
                     'net': _money_value(breakdown.net_amount),
                     'total_refunded': _money_value(breakdown.total_refunded_amount)})
    return Answer(refund_outcome(status), _opt(result.id), status, parse_time(result.create_time),
                  amount, currency, data)


# --------------------------------------------------------------------------------------------
# vault
# --------------------------------------------------------------------------------------------

def vault_card(client: PaypalClient, *, ref: str, card: CardDetails,
               customer_id: str | None) -> PaymentTokenResponse:
    card_model = PaymentTokenRequestCard(
        number=card.number, expiry=card.expiry, security_code=card.security_code,
        name=card.name or UNSET,
        billing_address=card.billing_address.to_model() if card.billing_address else UNSET)
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=card_model),
        customer=Customer(id=customer_id) if customer_id else UNSET)
    return client.vault.create_payment_token(body, pay_pal_request_id=ref)


def read_vault(token: PaymentTokenResponse) -> Answer:
    token_id = _opt(token.id)
    customer = _opt(token.customer)
    source = _opt(token.payment_source)
    card = _opt(source.card) if source is not None else None
    last_digits = _opt(card.last_digits) if card is not None else None
    data: dict[str, Any] = {'customer_id': _opt(customer.id) if customer is not None else None}
    if card is not None:
        data['card'] = {'brand': _status(card.brand), 'last_digits': last_digits,
                        'expiry': _opt(card.expiry), 'name': _opt(card.name)}
    return Answer(vault_outcome(token_id, last_digits), token_id, None,
                  parse_time(_opt((token.model_extra or {}).get('create_time'))), data=data)


def delete_token(client: PaypalClient, token_id: str) -> Answer:
    """DELETE of a vaulted card (returns ``None``, so the raw peer is the only status we can see).
    2xx → done; 404 → done (gone); anything else raises ProviderError."""
    result = client.vault.with_raw_response.delete_payment_token(token_id)
    now = datetime.now(timezone.utc)
    match result:
        case Success(response=response):
            return Answer('done', token_id, str(response.status_code), now)
        case Failure(error=error, response=response):
            if response.status_code == 404:
                return Answer('done', token_id, '404', now)
            raise provider_error(response.status_code, error)
    raise AssertionError('unreachable')


# --------------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderTransaction:
    transaction_id: str
    reference_id: str | None
    event_code: str | None
    status: str | None
    initiated_at: datetime | None
    amount: Decimal | None
    fee: Decimal | None
    currency: str | None
    invoice_id: str | None
    custom_field: str | None


def search_transactions(client: PaypalClient, start: datetime, end: datetime,
                        page_size: int = 100) -> list[ProviderTransaction]:
    """Every transaction PayPal reports in [start, end): ≤31-day windows, every page of each."""
    found: dict[str, ProviderTransaction] = {}
    window_start = start
    while window_start < end:
        window_end = min(window_start + MAX_SEARCH_WINDOW, end)
        page = 1
        while True:
            response = client.transaction_search.search_transactions(
                rfc3339(window_start), rfc3339(window_end), fields='transaction_info',
                balance_affecting_records_only='N', page_size=page_size, page=page)
            for details in _opt(response.transaction_details) or []:
                info = _opt(details.transaction_info)
                if info is None or not _opt(info.transaction_id):
                    continue
                amount, currency = _money(info.transaction_amount)
                fee, _ = _money(info.fee_amount)
                record = ProviderTransaction(
                    transaction_id=info.transaction_id, reference_id=_opt(info.paypal_reference_id),
                    event_code=_opt(info.transaction_event_code), status=_opt(info.transaction_status),
                    initiated_at=parse_time(info.transaction_initiation_date), amount=amount, fee=fee,
                    currency=currency, invoice_id=_opt(info.invoice_id), custom_field=_opt(info.custom_field))
                found[record.transaction_id] = record
            total_pages = _opt(response.total_pages) or 0
            if page >= total_pages:
                break
            page += 1
        window_start = window_end
    return [r for r in found.values() if r.initiated_at is None or start <= r.initiated_at < end]
