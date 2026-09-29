"""
Everything that touches the PayPal Server SDK.

* thin wrappers around the SDK operations this integration uses;
* ``read_*`` functions turning SDK responses into plain views (UNSET resolved);
* the per-step status mappers (``*_outcome``) — the only place a PayPal status
  becomes done / pending / failed / needs_review / unknown;
* ``translate`` — the single error ladder from SDK failures to ``ProviderError``;
* ``perform`` — the call / check / verify part of the safe write. The claim
  before it and the bookkeeping after it live in ``services``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

import httpx
from django.utils.dateparse import parse_datetime
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import UNSET, ApiError, OAuthProviderError, RawError, UnsetType
from pay_pal_server_sdk.models import (
    Address, AmountWithBreakdown, CaptureRequest, CapturedPayment, CardRequest, Customer, Error, Money,
    Order, OrderAuthorizeResponse, OrderAuthorizeResponsePaymentSource, OrderRequest, PaymentAuthorization, PaymentSource, PaymentTokenRequest,
    PaymentSourceResponse, PaymentTokenRequestCard, PaymentTokenRequestPaymentSource, PaymentTokenResponse,
    PurchaseUnitRequest,
    ReauthorizeRequest, Refund, RefundRequest, SearchResponse, TransactionInformation)
from pay_pal_server_sdk.models.enums import (
    AuthorizationStatus, CaptureStatus, CardVerificationStatus, CheckoutPaymentIntent, OrderStatus, RefundStatus)

logger = logging.getLogger('apps.paypal_payments.paypal')

REPRESENTATION = 'return=representation'
TWO_PLACES = Decimal('0.01')
SEARCH_PAGE_SIZE = 500          # PayPal rejects anything larger
SEARCH_MAX_RANGE = timedelta(days=31)

DONE = 'done'
PENDING = 'pending'
FAILED = 'failed'
NEEDS_REVIEW = 'needs_review'
UNKNOWN = 'unknown'

# Failures raised before the request left: nothing can have reached PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

T = TypeVar('T')


class ProviderError(Exception):
    """A PayPal failure as this API reports it."""

    def __init__(self, status_code: int, code: str, message: str, *,
                 outcome_unknown: bool = False, issue: str | None = None,
                 debug_id: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.issue = issue
        self.debug_id = debug_id


# --------------------------------------------------------------------------
# Money and time helpers
# --------------------------------------------------------------------------

def money_str(amount: Decimal) -> str:
    return f'{amount.quantize(TWO_PLACES):.2f}'


def to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def rfc3339(moment: datetime) -> str:
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def _set(value: T | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _time(value: str | UnsetType) -> datetime | None:
    raw = _set(value)
    return parse_datetime(raw) if raw else None


def _money(value: Money | UnsetType) -> tuple[Decimal | None, str | None]:
    money = _set(value)
    if money is None:
        return None, None
    return to_decimal(money.value), money.currency_code


# --------------------------------------------------------------------------
# Views of PayPal records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    """What one step's response says, read the same way for every step."""
    provider_id: str | None
    status: object
    provider_time: datetime | None
    amount: Decimal | None = None
    currency: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def read_authorization(result: Order | OrderAuthorizeResponse | PaymentAuthorization) -> Answer:
    """An authorization, from a create/authorize/get order response or the authorization itself."""
    details: dict[str, Any] = {}
    if isinstance(result, PaymentAuthorization):
        auth: Any = result
    else:
        details['paypal_order_id'] = _set(result.id)
        details['order_status'] = _set(result.status)
        source = _set(result.payment_source)
        card = None
        if isinstance(source, (PaymentSourceResponse, OrderAuthorizeResponsePaymentSource)):
            card = _set(source.card)
        if card is not None:
            details['card_brand'] = _set(card.brand)
            details['card_last_digits'] = _set(card.last_digits)
        auth = None
        units = _set(result.purchase_units) or []
        payments = _set(units[0].payments) if units else None
        authorizations = (_set(payments.authorizations) or []) if payments is not None else []
        if authorizations:
            auth = authorizations[0]
        if auth is None:
            return Answer(None, None, None, details=details)
    amount, currency = _money(auth.amount)
    details['expires_at'] = _time(auth.expiration_time)
    return Answer(_set(auth.id), _set(auth.status), _time(auth.create_time), amount, currency, details)


def read_capture(result: CapturedPayment) -> Answer:
    amount, currency = _money(result.amount)
    details: dict[str, Any] = {'fee': None, 'net': None}
    breakdown = _set(result.seller_receivable_breakdown)
    if breakdown is not None:
        details['gross'] = to_decimal(breakdown.gross_amount.value)
        details['fee'] = _money(breakdown.paypal_fee)[0]
        details['net'] = _money(breakdown.net_amount)[0]
    return Answer(_set(result.id), _set(result.status), _time(result.create_time), amount, currency, details)


def read_void(result: PaymentAuthorization) -> Answer:
    return Answer(_set(result.id), _set(result.status), _time(result.update_time))


def read_refund(result: Refund) -> Answer:
    amount, currency = _money(result.amount)
    return Answer(_set(result.id), _set(result.status), _time(result.create_time), amount, currency)


def read_payment_token(result: PaymentTokenResponse) -> Answer:
    details: dict[str, Any] = {}
    customer = _set(result.customer)
    details['customer_id'] = _set(customer.id) if customer is not None else None
    source = _set(result.payment_source)
    card = _set(source.card) if source is not None else None
    status: object = None
    last_digits = _set(card.last_digits) if card is not None else None
    if card is not None and last_digits:
        details['brand'] = _set(card.brand)
        details['last_digits'] = _set(card.last_digits)
        details['expiry'] = _set(card.expiry)
        # No status on a payment token: the vaulted card echoed back, and not
        # failing verification, is what "saved" looks like.
        verification = _set(card.verification_status)
        status = verification if verification is not None else 'VAULTED'
    return Answer(_set(result.id), status, None, details=details)


# --------------------------------------------------------------------------
# Status → outcome, one mapper per step
# --------------------------------------------------------------------------

def authorization_outcome(status: object) -> str:
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return FAILED
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return NEEDS_REVIEW       # money already taken: not what a hold asked for
        case _:
            return UNKNOWN


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return FAILED
        case CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED:
            return FAILED             # taken, then given back: not in effect as asked
        case _:
            return UNKNOWN


def cancel_outcome(status: object) -> str:
    """Void: the undone state is this step's done."""
    match status:
        case AuthorizationStatus.VOIDED:
            return DONE
        case AuthorizationStatus.DENIED:
            return DONE               # nothing was ever held
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED             # too late: the money moved
        case _:
            return UNKNOWN


def refund_outcome(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return DONE
        case RefundStatus.PENDING:
            return PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return FAILED
        case _:
            return UNKNOWN


def vault_outcome(status: object) -> str:
    match status:
        case 'VAULTED' | CardVerificationStatus.VERIFIED:
            return DONE
        case CardVerificationStatus.FAILED:
            return FAILED
        case _:
            return UNKNOWN


# --------------------------------------------------------------------------
# Error ladder
# --------------------------------------------------------------------------

def issue_of(error: object) -> str | None:
    if isinstance(error, Error):
        details = _set(error.details)
        if details:
            return details[0].issue
    return None


def _issue_description(error: object) -> str | None:
    if isinstance(error, Error):
        details = _set(error.details)
        if details:
            return _set(details[0].description)
    return None


def translate_status(status: int, error: object) -> ProviderError:
    """Map PayPal's answer to a failure this API reports. Never exposes str(e)."""
    if isinstance(error, OAuthProviderError) or status in (401, 403):
        return ProviderError(502, 'paypal_auth_failed',
                             'PayPal refused this site\'s credentials; nothing was processed.')
    if status == 429:
        return ProviderError(503, 'paypal_rate_limited', 'PayPal is rate-limiting requests; try again shortly.')
    debug_id = error.debug_id if isinstance(error, Error) else None
    if 400 <= status < 500:
        issue = issue_of(error)
        description = _issue_description(error)
        message = description or 'PayPal rejected the request.'
        return ProviderError(status, (issue or 'paypal_rejected').lower(), message, issue=issue,
                             debug_id=debug_id)
    return ProviderError(502, 'paypal_unavailable', 'PayPal did not complete the request.',
                         outcome_unknown=True, debug_id=debug_id)


def translate(exc: BaseException) -> ProviderError:
    if isinstance(exc, ApiError):
        return translate_status(exc.status_code, exc.error)
    if isinstance(exc, NEVER_SENT):
        return ProviderError(502, 'paypal_unreachable', 'PayPal could not be reached; nothing was sent.')
    if isinstance(exc, httpx.RequestError):
        return ProviderError(504, 'outcome_unknown', 'PayPal did not answer in time; the outcome is unknown.',
                             outcome_unknown=True)
    if isinstance(exc, ValueError):  # pydantic.ValidationError included: unreadable body
        return ProviderError(502, 'paypal_unreadable', 'PayPal returned a response this site could not read.',
                             outcome_unknown=True)
    raise exc


# --------------------------------------------------------------------------
# The safe write: call, check, verify
# --------------------------------------------------------------------------

@dataclass
class StepResult:
    outcome: str
    answer: Answer | None = None
    error: ProviderError | None = None
    # True when nothing reached PayPal or PayPal refused it outright: the
    # claim may be released and the same reference used again.
    released: bool = False
    result: Any = None


def perform(*, send: Callable[[], Any], find: Callable[[], Any], read: Callable[[Any], Answer],
            outcome_of: Callable[[object], str], checking: bool, repeat_is_safe: bool,
            sent: tuple[Decimal, str] | None = None,
            landed: Callable[[ApiError, bool], bool] = lambda e, resending: False) -> StepResult:
    """Make (or check) one provider write under an already-held claim.

    send     the write, carrying the claim's reference (idempotency header / invoice id)
    find     the lookup for a write that may have landed: by that reference, or
             by the record's own id; returns None when nothing is found (yet)
    checking the claim was found in an unresolved state: never make a new write,
             only resend under the SAME reference (when repeat_is_safe) or look up
    landed   whether an error body means an earlier attempt already landed
    """
    resending = checking and repeat_is_safe
    result: Any = None
    if resending or not checking:
        try:
            result = send()
        except NEVER_SENT as exc:
            if resending:
                return StepResult(UNKNOWN, error=translate(exc))
            return StepResult(FAILED, error=translate(exc), released=True)
        except ApiError as exc:
            if isinstance(exc.error, OAuthProviderError):
                # The token fetch failed before the write was built: nothing sent.
                error = translate(exc)
                return StepResult(UNKNOWN if resending else FAILED, error=error, released=not resending)
            if landed(exc, resending):
                pass                            # an earlier attempt landed: look it up
            elif exc.status_code < 500:
                if not resending:
                    return StepResult(FAILED, error=translate(exc), released=True)
                # a 4xx on a check settles nothing: fall through to the lookup
            # a 5xx on a write may still have landed: fall through to the lookup
        except (httpx.RequestError, ValueError):
            pass                                # sent, no readable answer: may have landed

    if result is None:
        try:
            result = find()
        except (ApiError, httpx.RequestError, ValueError) as exc:
            logger.warning('PayPal lookup failed: %s', type(exc).__name__)
            result = None
        if result is None:
            return StepResult(UNKNOWN, error=ProviderError(
                504, 'outcome_unknown',
                'PayPal has not confirmed whether this went through. Repeat the same request to check again; '
                'it will not be processed twice.', outcome_unknown=True))

    answer = read(result)
    if sent is not None and answer.provider_id is not None:
        sent_amount, sent_currency = sent
        # Decimal equality: "10.0" and "10.00" are the same money.
        if answer.amount is None or answer.amount != sent_amount or answer.currency != sent_currency:
            logger.error('PayPal amount mismatch on %s: sent %s %s, got %s %s', answer.provider_id,
                         sent_amount, sent_currency, answer.amount, answer.currency)
            return StepResult(NEEDS_REVIEW, answer=answer, result=result, error=ProviderError(
                409, 'amount_mismatch', 'PayPal processed a different amount than requested; flagged for review.'))
    return StepResult(outcome_of(answer.status), answer=answer, result=result)


# --------------------------------------------------------------------------
# SDK operations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CardDetails:
    """Card data in transit only — never stored, never logged (repr hides it)."""
    number: str = field(repr=False)
    expiry: str = field(repr=False)
    security_code: str = field(repr=False)
    name: str = field(repr=False, default='')
    billing_address: dict[str, str] | None = field(repr=False, default=None)

    def address(self) -> Address | None:
        if not self.billing_address:
            return None
        return Address(**self.billing_address)


def card_source(card: CardDetails) -> PaymentSource:
    address = card.address()
    return PaymentSource(card=CardRequest(
        number=card.number, expiry=card.expiry, security_code=card.security_code,
        name=card.name or UNSET,
        billing_address=address if address is not None else UNSET,
    ))


def vaulted_card_source(token_id: str) -> PaymentSource:
    return PaymentSource(card=CardRequest(vault_id=token_id))


def create_authorization(client: PayPalServerSdkClient, reference: str, amount: Decimal, currency: str,
                         source: PaymentSource, *, custom_id: str, description: str) -> Order:
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            amount=AmountWithBreakdown(currency_code=currency, value=money_str(amount)),
            invoice_id=reference,
            custom_id=custom_id,
            description=description[:127],
        )],
        payment_source=source,
    )
    return client.orders.create_order(body, pay_pal_request_id=reference, prefer=REPRESENTATION)


def authorize_order(client: PayPalServerSdkClient, paypal_order_id: str, reference: str) -> OrderAuthorizeResponse:
    return client.orders.authorize_order(paypal_order_id, pay_pal_request_id=reference, prefer=REPRESENTATION)


def get_order(client: PayPalServerSdkClient, paypal_order_id: str) -> Order:
    return client.orders.get_order(paypal_order_id)


def get_authorization(client: PayPalServerSdkClient, authorization_id: str) -> PaymentAuthorization:
    return client.payments.get_authorized_payment(authorization_id)


def reauthorize(client: PayPalServerSdkClient, authorization_id: str, reference: str, amount: Decimal,
                currency: str) -> PaymentAuthorization:
    return client.payments.reauthorize_payment(
        authorization_id, pay_pal_request_id=reference, prefer=REPRESENTATION,
        body=ReauthorizeRequest(amount=Money(currency_code=currency, value=money_str(amount))))


def capture(client: PayPalServerSdkClient, authorization_id: str, reference: str, amount: Decimal,
            currency: str) -> CapturedPayment:
    return client.payments.capture_authorized_payment(
        authorization_id, pay_pal_request_id=reference, prefer=REPRESENTATION,
        body=CaptureRequest(amount=Money(currency_code=currency, value=money_str(amount)), final_capture=True))


def get_capture(client: PayPalServerSdkClient, capture_id: str) -> CapturedPayment:
    return client.payments.get_captured_payment(capture_id)


def void(client: PayPalServerSdkClient, authorization_id: str, reference: str) -> PaymentAuthorization:
    return client.payments.void_payment(authorization_id, pay_pal_request_id=reference, prefer=REPRESENTATION)


def refund(client: PayPalServerSdkClient, capture_id: str, reference: str, amount: Decimal,
           currency: str) -> Refund:
    return client.payments.refund_captured_payment(
        capture_id, pay_pal_request_id=reference, prefer=REPRESENTATION,
        body=RefundRequest(amount=Money(currency_code=currency, value=money_str(amount))))


def get_refund(client: PayPalServerSdkClient, refund_id: str) -> Refund:
    return client.payments.get_refund(refund_id)


def vault_card(client: PayPalServerSdkClient, reference: str, card: CardDetails,
               customer_id: str | None) -> PaymentTokenResponse:
    address = card.address()
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(
            number=card.number, expiry=card.expiry, security_code=card.security_code,
            name=card.name or UNSET,
            billing_address=address if address is not None else UNSET,
        )),
        customer=Customer(id=customer_id) if customer_id else UNSET,
    )
    return client.vault.create_payment_token(body, pay_pal_request_id=reference)


@dataclass(frozen=True)
class DeleteResult:
    token_id: str
    status_code: int


def delete_payment_token(client: PayPalServerSdkClient, token_id: str) -> DeleteResult:
    """DELETE a vaulted card. The operation returns no body, so the raw peer is the only way to see its status."""
    result = client.vault.with_raw_response.delete_payment_token(token_id)
    return DeleteResult(token_id, result.status_code)


def read_delete(result: DeleteResult) -> Answer:
    return Answer(result.token_id, result.status_code, None)


def delete_outcome(status_code: object) -> str:
    """Call-off outcome of a vault delete: gone is done."""
    match status_code:
        case 200 | 204 | 404:
            return DONE
        case int() as code if 400 <= code < 500:
            return FAILED
        case _:
            return UNKNOWN


def search_transactions(client: PayPalServerSdkClient, start: datetime, end: datetime,
                        page: int) -> SearchResponse:
    return client.transaction_search.search_transactions(
        rfc3339(start), rfc3339(end), fields='transaction_info', balance_affecting_records_only='N',
        page_size=SEARCH_PAGE_SIZE, page=page)


@dataclass
class SearchPage:
    transactions: list[TransactionInformation]
    total_items: int | None
    total_pages: int
    last_refreshed: datetime | None


def iter_transaction_pages(client: PayPalServerSdkClient, start: datetime,
                           end: datetime) -> Iterator[SearchPage]:
    """Every page of every ≤31-day window in [start, end)."""
    window_start = start
    while window_start < end:
        window_end = min(end, window_start + SEARCH_MAX_RANGE - timedelta(seconds=1))
        page = 1
        while True:
            response = search_transactions(client, window_start, window_end, page)
            infos = []
            for detail in _set(response.transaction_details) or []:
                info = _set(detail.transaction_info)
                if info is not None:
                    infos.append(info)
            total_pages = _set(response.total_pages) or 1
            yield SearchPage(infos, _set(response.total_items), total_pages,
                             _time(response.last_refreshed_datetime))
            if page >= total_pages:
                break
            page += 1
        window_start = window_end


def find_authorization_by_invoice(client: PayPalServerSdkClient, invoice_id: str, since: datetime,
                                  until: datetime) -> PaymentAuthorization | None:
    """Lookup for a create_order whose answer was lost: PayPal's own records by our invoice id."""
    for page in iter_transaction_pages(client, since, until):
        for info in page.transactions:
            if _set(info.invoice_id) != invoice_id:
                continue
            transaction_id = _set(info.transaction_id)
            if not transaction_id:
                continue
            try:
                return get_authorization(client, transaction_id)
            except ApiError as exc:
                if exc.status_code == 404 or isinstance(exc.error, RawError):
                    continue                   # a capture/refund row, not the authorization
                raise
            except ValueError:
                continue                       # an undecodable 404 body: not an authorization
    return None


def info_value(info: TransactionInformation, name: str) -> Any:
    return _set(getattr(info, name))


def info_amount(info: TransactionInformation, name: str) -> tuple[Decimal | None, str | None]:
    return _money(getattr(info, name))
