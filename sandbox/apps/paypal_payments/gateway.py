"""
The PayPal boundary. This is the only module that imports the PayPal Server
SDK: it owns the client, turns this app's intent into SDK calls, and turns
every way an SDK call can fail into one ``ProviderError``.

Nothing here touches the database. Card details pass through untouched and are
never logged; the transport wrapper logs method, path, status and timing only.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import (
    UNSET,
    ApiError,
    ClientCredentials,
    Failure,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    OAuthProviderError,
    RawError,
    Success,
    UnsetType,
)
from pay_pal_server_sdk.models import (
    Address,
    AmountWithBreakdown,
    AuthorizationWithAdditionalData,
    CaptureRequest,
    CapturedPayment,
    CardRequest,
    CardStoredCredential,
    Customer,
    Error,
    Money,
    OrderRequest,
    PaymentAuthorization,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnit,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    RefundRequest,
)
from pay_pal_server_sdk.models.enums import (
    CheckoutPaymentIntent,
    OrderStatus,
    PaymentInitiator,
    StoredPaymentSourcePaymentType,
    StoredPaymentSourceUsageType,
)
from pydantic import ValidationError

from .money import to_wire

logger = logging.getLogger(__name__)

T = TypeVar('T')

# The one server the SDK declares. Any other environment must name its host
# explicitly through PAYPAL_BASE_URL.
SANDBOX_BASE_URL = 'https://api-m.sandbox.paypal.com'
REQUEST_TIMEOUT_SECONDS = 20.0
REPRESENTATION = 'return=representation'

# PayPal's authorization honor period, and its reporting limits.
HONOR_PERIOD = timedelta(days=3)
SEARCH_WINDOW = timedelta(days=31)
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_PAGES_PER_WINDOW = 1000


class ProviderError(Exception):
    """A PayPal call that did not produce the result we needed.

    ``status_code`` is what this app answers with; ``outcome_unknown`` says
    whether the request may nevertheless have taken effect at PayPal (so the
    caller must reconcile, not assume failure); ``issue`` is PayPal's own
    machine-readable reason when it gave one.
    """

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


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class _LoggingTransport:
    """Wraps the SDK's transport to log each exchange without its headers or
    body (the auth header carries a live token; bodies may carry card data)."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        response = self._inner.send(request)
        logger.info('PayPal %s %s -> %s (%.0f ms)', request.method,
                    urlsplit(request.url).path, response.status_code,
                    (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def resolve_base_url(environment: str, override: str) -> str:
    if override:
        return override
    if environment.strip().lower() == 'sandbox':
        return SANDBOX_BASE_URL
    raise ImproperlyConfigured(
        'PAYPAL_ENVIRONMENT=%r has no built-in API address; set PAYPAL_BASE_URL.'
        % environment)


def build_client() -> PayPalServerSdkClient:
    client_id = getattr(settings, 'PAYPAL_CLIENT_ID', '')
    client_secret = getattr(settings, 'PAYPAL_CLIENT_SECRET', '')
    if not client_id or not client_secret:
        raise ImproperlyConfigured('PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.')
    base_url = resolve_base_url(getattr(settings, 'PAYPAL_ENVIRONMENT', ''),
                                getattr(settings, 'PAYPAL_BASE_URL', ''))
    # The client's own ``timeout`` only shapes its default transport, so the
    # timeout is set on the transport we hand it. Retries keep the SDK
    # default: only GET/HEAD/PUT/OPTIONS are repeated, so every PayPal write
    # here is sent once per call and replays go through our claims with the
    # same PayPal-Request-Id.
    return PayPalServerSdkClient(
        base_url=base_url,
        custom_http_client=_LoggingTransport(HttpxClient(timeout=REQUEST_TIMEOUT_SECONDS)),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


_client: PayPalServerSdkClient | None = None
_client_lock = threading.Lock()


def get_client() -> PayPalServerSdkClient:
    """The process-wide client, built on first use (so after any worker fork)
    and reused so its connection pool and OAuth token cache are shared."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def install_client(client: PayPalServerSdkClient | None) -> PayPalServerSdkClient | None:
    """Swap the process-wide client (tests, credential rotation); returns the
    previous one, which the caller owns and must close."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    return previous


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------

def _opt(value: T | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _paypal_issue(error: Error) -> tuple[str | None, str | None]:
    details = _opt(error.details) or []
    for detail in details:
        return detail.issue, _opt(detail.description)
    return None, None


def _raw_message(raw: RawError) -> str | None:
    try:
        body = raw.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        message = body.get('message') or body.get('error_description')
        return str(message) if message else None
    return None


def _from_api_error(operation: str, exc: ApiError[Any], *, write: bool) -> ProviderError:
    status, error = exc.status_code, exc.error
    if isinstance(error, OAuthProviderError):
        logger.error('PayPal refused our credentials during %s: %s', operation, error.error)
        return ProviderError(502, 'paypal_credentials_rejected',
                             'PayPal rejected this site\'s API credentials.')
    if status in (401, 403):
        logger.error('PayPal answered %s to %s', status, operation)
        return ProviderError(502, 'paypal_not_permitted',
                             'PayPal refused the request for this merchant account.')
    if status == 429:
        return ProviderError(503, 'paypal_rate_limited',
                             'PayPal is rate limiting requests; try again shortly.')
    if isinstance(error, Error):
        issue, description = _paypal_issue(error)
        logger.warning('PayPal %s rejected (%s %s %s, debug_id=%s)', operation, status,
                       error.name, issue, error.debug_id)
        if 400 <= status < 500:
            code = 409 if status == 409 else 422
            return ProviderError(code, 'paypal_rejected', description or error.message,
                                 issue=issue or error.name, debug_id=error.debug_id)
        return ProviderError(502, 'paypal_unavailable', 'PayPal could not process the request.',
                             outcome_unknown=write, issue=issue, debug_id=error.debug_id)
    message = _raw_message(error) if isinstance(error, RawError) else None
    logger.warning('PayPal %s failed with HTTP %s', operation, status)
    if 400 <= status < 500:
        return ProviderError(409 if status == 409 else 422, 'paypal_rejected',
                             message or 'PayPal rejected the request.')
    return ProviderError(502, 'paypal_unavailable', 'PayPal could not process the request.',
                         outcome_unknown=write)


def _call(operation: str, fn: Callable[[], T], *, write: bool) -> T:
    """Run one SDK call and translate every failure kind into ProviderError."""
    try:
        return fn()
    except ApiError as exc:
        raise _from_api_error(operation, exc, write=write) from exc
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError) as exc:
        # Never left this process: nothing happened at PayPal.
        logger.warning('PayPal %s not sent: %s', operation, type(exc).__name__)
        raise ProviderError(502, 'paypal_unreachable', 'PayPal could not be reached.') from exc
    except httpx.RequestError as exc:
        # Sent, but no answer: it may have taken effect.
        logger.warning('PayPal %s outcome unknown: %s', operation, type(exc).__name__)
        raise ProviderError(504, 'paypal_timeout', 'PayPal did not answer in time.',
                            outcome_unknown=write) from exc
    except (ValidationError, ValueError) as exc:
        # An unreadable answer: the call may well have succeeded.
        logger.error('PayPal %s returned an unreadable response', operation)
        raise ProviderError(502, 'paypal_unreadable_response',
                            'PayPal returned a response this site could not read.',
                            outcome_unknown=write) from exc


def _require(value: T | UnsetType, operation: str, member: str) -> T:
    if isinstance(value, UnsetType) or value is None or value == '':
        logger.error('PayPal %s response is missing %s', operation, member)
        raise ProviderError(502, 'paypal_incomplete_response',
                            'PayPal returned an incomplete response.', outcome_unknown=True)
    return value


def _money(value: Money | UnsetType) -> Decimal | None:
    money = _opt(value)
    if money is None:
        return None
    try:
        return Decimal(money.value)
    except InvalidOperation:
        return None


def _time(value: str | UnsetType) -> datetime | None:
    raw = _opt(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _text(value: object) -> str:
    # Open enums arrive as a member or a plain str; both stringify to the wire value.
    return '' if value is None or isinstance(value, UnsetType) else str(value)


def derive_request_id(base: str, purpose: str) -> str:
    """A stable PayPal-Request-Id for a follow-up call of the same operation."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, '%s:%s' % (base, purpose)))


def _amount(value: Decimal, currency: str) -> Money:
    return Money(currency_code=currency, value=to_wire(value, currency))


# ---------------------------------------------------------------------------
# Results handed back to the service layer (plain values, never SDK models)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BillingAddress:
    country_code: str
    address_line_1: str | None = None
    address_line_2: str | None = None
    admin_area_2: str | None = None
    admin_area_1: str | None = None
    postal_code: str | None = None


@dataclass(frozen=True)
class CardDetails:
    number: str = field(repr=False)
    expiry: str                       # YYYY-MM
    security_code: str | None = field(default=None, repr=False)
    name: str | None = None
    billing_address: BillingAddress | None = None


@dataclass(frozen=True)
class AuthorizationResult:
    paypal_order_id: str
    order_status: str
    authorization_id: str
    authorization_status: str
    decline_reason: str | None
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
    amount: Decimal | None
    created_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    status: str
    amount: Decimal | None
    gross_amount: Decimal | None
    paypal_fee: Decimal | None
    net_amount: Decimal | None
    created_at: datetime | None


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    status: str
    amount: Decimal | None


@dataclass(frozen=True)
class VaultedCard:
    token_id: str
    customer_id: str | None
    brand: str
    last_digits: str
    expiry: str


@dataclass(frozen=True)
class PayPalTransaction:
    transaction_id: str
    reference_id: str | None
    event_code: str | None
    status: str | None
    initiated_at: datetime | None
    amount: Decimal | None
    currency: str | None
    fee: Decimal | None
    custom_field: str | None
    invoice_id: str | None


@dataclass(frozen=True)
class TransactionReport:
    transactions: list[PayPalTransaction]
    last_refreshed_at: datetime | None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _address(address: BillingAddress | None) -> Address | UnsetType:
    if address is None:
        return UNSET
    return Address(
        country_code=address.country_code,
        address_line_1=address.address_line_1 or UNSET,
        address_line_2=address.address_line_2 or UNSET,
        admin_area_2=address.admin_area_2 or UNSET,
        admin_area_1=address.admin_area_1 or UNSET,
        postal_code=address.postal_code or UNSET,
    )


def _first_authorization(units: Sequence[PurchaseUnit] | UnsetType
                         ) -> AuthorizationWithAdditionalData | None:
    for unit in _opt(units) or []:
        payments = _opt(unit.payments)
        if payments is None:
            continue
        for authorization in _opt(payments.authorizations) or []:
            return authorization
    return None


def authorize_order_total(*, reference: str, description: str, amount: Decimal,
                          currency: str, request_id: str, card: CardDetails | None = None,
                          vault_id: str | None = None) -> AuthorizationResult:
    """Put a hold on ``amount``: a PayPal order with intent AUTHORIZE paid by
    the given card (or vaulted card). Nothing is captured."""
    if (card is None) == (vault_id is None):
        raise ValueError('exactly one of card or vault_id is required')
    if card is not None:
        card_request = CardRequest(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code or UNSET,
            name=card.name or UNSET,
            billing_address=_address(card.billing_address),
        )
    else:
        assert vault_id is not None
        card_request = CardRequest(
            vault_id=vault_id,
            stored_credential=CardStoredCredential(
                payment_initiator=PaymentInitiator.CUSTOMER,
                payment_type=StoredPaymentSourcePaymentType.UNSCHEDULED,
                usage=StoredPaymentSourceUsageType.SUBSEQUENT,
            ),
        )
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            reference_id=reference,
            custom_id=reference,
            description=description[:127],
            amount=AmountWithBreakdown(currency_code=currency, value=to_wire(amount, currency)),
        )],
        payment_source=PaymentSource(card=card_request),
    )
    client = get_client()
    order = _call('create_order', lambda: client.orders.create_order(
        body, pay_pal_request_id=request_id, prefer=REPRESENTATION), write=True)
    order_id = _require(order.id, 'create_order', 'id')
    order_status = _text(order.status)
    authorization = _first_authorization(order.purchase_units)
    source = _opt(order.payment_source)
    card_response = _opt(source.card) if source is not None else None

    if authorization is None and order_status == OrderStatus.APPROVED:
        authorized = _call('authorize_order', lambda: client.orders.authorize_order(
            order_id, pay_pal_request_id=derive_request_id(request_id, 'authorize'),
            prefer=REPRESENTATION), write=True)
        order_status = _text(authorized.status)
        authorization = _first_authorization(authorized.purchase_units)
        authorized_source = _opt(authorized.payment_source)
        if authorized_source is not None and _opt(authorized_source.card) is not None:
            card_response = _opt(authorized_source.card)

    if authorization is None:
        if order_status == OrderStatus.PAYER_ACTION_REQUIRED:
            raise ProviderError(
                422, 'payer_action_required',
                'PayPal requires the cardholder to complete a browser challenge for this '
                'card, which this API does not support. Use a different card.',
                issue='PAYER_ACTION_REQUIRED')
        logger.error('PayPal order %s is %s with no authorization', order_id, order_status)
        raise ProviderError(502, 'paypal_unexpected_state',
                            'PayPal did not return an authorization for this payment.',
                            outcome_unknown=True)

    status_details = _opt(authorization.status_details)
    held = _opt(authorization.amount)
    return AuthorizationResult(
        paypal_order_id=order_id,
        order_status=order_status,
        authorization_id=_require(authorization.id, 'create_order', 'authorization id'),
        authorization_status=_text(authorization.status),
        decline_reason=_text(status_details.reason) or None if status_details else None,
        amount=_money(authorization.amount),
        currency=held.currency_code if held is not None else None,
        created_at=_time(authorization.create_time),
        expires_at=_time(authorization.expiration_time),
        card_brand=_text(card_response.brand) if card_response else '',
        card_last_digits=_text(card_response.last_digits) if card_response else '',
    )


def _authorization_state(auth: PaymentAuthorization, operation: str) -> AuthorizationState:
    return AuthorizationState(
        authorization_id=_require(auth.id, operation, 'id'),
        status=_require(auth.status, operation, 'status'),
        amount=_money(auth.amount),
        created_at=_time(auth.create_time),
        expires_at=_time(auth.expiration_time),
    )


def get_authorization(authorization_id: str) -> AuthorizationState:
    client = get_client()
    auth = _call('get_authorized_payment',
                 lambda: client.payments.get_authorized_payment(authorization_id), write=False)
    return _authorization_state(auth, 'get_authorized_payment')


def reauthorize(authorization_id: str, *, amount: Decimal, currency: str,
                request_id: str) -> AuthorizationState:
    """Renew a hold whose honor period has lapsed; PayPal issues a new
    authorization id."""
    client = get_client()
    auth = _call('reauthorize_payment', lambda: client.payments.reauthorize_payment(
        authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION,
        body=ReauthorizeRequest(amount=_amount(amount, currency))), write=True)
    return _authorization_state(auth, 'reauthorize_payment')


def _capture_result(capture: CapturedPayment, operation: str) -> CaptureResult:
    breakdown = _opt(capture.seller_receivable_breakdown)
    return CaptureResult(
        capture_id=_require(capture.id, operation, 'id'),
        status=_require(capture.status, operation, 'status'),
        amount=_money(capture.amount),
        gross_amount=_money(breakdown.gross_amount) if breakdown else None,
        paypal_fee=_money(breakdown.paypal_fee) if breakdown else None,
        net_amount=_money(breakdown.net_amount) if breakdown else None,
        created_at=_time(capture.create_time),
    )


def capture_authorization(authorization_id: str, *, amount: Decimal, currency: str,
                          request_id: str) -> CaptureResult:
    """Take the held money. The fee and net come from PayPal's seller
    receivable breakdown; when the capture response omits it (e.g. still
    pending) the capture is read back once to fetch it."""
    client = get_client()
    capture = _call('capture_authorized_payment', lambda: client.payments.capture_authorized_payment(
        authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION,
        body=CaptureRequest(amount=_amount(amount, currency), final_capture=True)), write=True)
    result = _capture_result(capture, 'capture_authorized_payment')
    if result.paypal_fee is None or result.net_amount is None:
        try:
            fetched = _call('get_captured_payment',
                            lambda: client.payments.get_captured_payment(result.capture_id),
                            write=False)
            result = _capture_result(fetched, 'get_captured_payment')
        except ProviderError:
            logger.warning('Could not read back capture %s for its fee breakdown',
                           result.capture_id)
    return result


def void_authorization(authorization_id: str, *, request_id: str) -> str:
    """Release a hold; returns PayPal's authorization status (VOIDED)."""
    client = get_client()
    auth = _call('void_payment', lambda: client.payments.void_payment(
        authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION), write=True)
    return _require(auth.status, 'void_payment', 'status')


def refund_capture(capture_id: str, *, amount: Decimal, currency: str, request_id: str,
                   custom_id: str, note: str | None = None) -> RefundResult:
    """Refund part or all of a capture. The amount is always explicit so that
    partial refunds add up exactly."""
    client = get_client()
    refund = _call('refund_captured_payment', lambda: client.payments.refund_captured_payment(
        capture_id, pay_pal_request_id=request_id, prefer=REPRESENTATION,
        body=RefundRequest(amount=_amount(amount, currency), custom_id=custom_id,
                           note_to_payer=note or UNSET)), write=True)
    return RefundResult(
        refund_id=_require(refund.id, 'refund_captured_payment', 'id'),
        status=_require(refund.status, 'refund_captured_payment', 'status'),
        amount=_money(refund.amount),
    )


def vault_card(card: CardDetails, *, customer_id: str | None, request_id: str) -> VaultedCard:
    """Store a card in PayPal's vault; PayPal keeps the card, we keep the token."""
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code or UNSET,
            name=card.name or UNSET,
            billing_address=_address(card.billing_address),
        )),
        customer=Customer(id=customer_id) if customer_id else UNSET,
    )
    client = get_client()
    token = _call('create_payment_token', lambda: client.vault.create_payment_token(
        body, pay_pal_request_id=request_id), write=True)
    source = _opt(token.payment_source)
    vaulted = _opt(source.card) if source is not None else None
    customer = _opt(token.customer)
    return VaultedCard(
        token_id=_require(token.id, 'create_payment_token', 'id'),
        customer_id=_opt(customer.id) if customer is not None else None,
        brand=_text(vaulted.brand) if vaulted else '',
        last_digits=_text(vaulted.last_digits) if vaulted else '',
        expiry=_text(vaulted.expiry) if vaulted else card.expiry,
    )


def delete_vaulted_card(token_id: str) -> None:
    """Remove a token from PayPal's vault; a token PayPal no longer has
    counts as removed."""
    client = get_client()
    result = _call('delete_payment_token',
                   lambda: client.vault.with_raw_response.delete_payment_token(token_id),
                   write=True)
    match result:
        case Success():
            return
        case Failure(status_code=404):
            logger.info('Vault token was already gone at PayPal')
            return
        case Failure(error=error, status_code=status):
            raise _from_api_error('delete_payment_token', ApiError(error, status, result.headers),
                                  write=True)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def search_transactions(start: datetime, end: datetime) -> TransactionReport:
    """Every transaction PayPal reports for ``[start, end)``: the range is cut
    into windows PayPal accepts and each window is read to its last page."""
    client = get_client()
    seen: dict[tuple[str, str | None], PayPalTransaction] = {}
    refreshed: list[datetime] = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + SEARCH_WINDOW, end)
        page = 1
        while True:
            current_page = page
            response = _call('search_transactions', lambda: client.transaction_search.search_transactions(
                _format_time(window_start), _format_time(window_end),
                page_size=SEARCH_PAGE_SIZE, page=current_page), write=False)
            last_refreshed = _time(response.last_refreshed_datetime)
            if last_refreshed is not None:
                refreshed.append(last_refreshed)
            for detail in _opt(response.transaction_details) or []:
                info = _opt(detail.transaction_info)
                if info is None:
                    continue
                transaction_id = _opt(info.transaction_id)
                if not transaction_id:
                    continue
                amount = _opt(info.transaction_amount)
                event_code = _opt(info.transaction_event_code)
                seen[(transaction_id, event_code)] = PayPalTransaction(
                    transaction_id=transaction_id,
                    reference_id=_opt(info.paypal_reference_id),
                    event_code=event_code,
                    status=_opt(info.transaction_status),
                    initiated_at=_time(info.transaction_initiation_date),
                    amount=_money(info.transaction_amount),
                    currency=amount.currency_code if amount is not None else None,
                    fee=_money(info.fee_amount),
                    custom_field=_opt(info.custom_field),
                    invoice_id=_opt(info.invoice_id),
                )
            total_pages = _opt(response.total_pages) or 1
            if page >= total_pages:
                break
            if page >= SEARCH_MAX_PAGES_PER_WINDOW:
                raise ProviderError(502, 'paypal_report_too_large',
                                    'PayPal reported more transactions than this report can read; '
                                    'narrow the date range.')
            page += 1
        window_start = window_end
    return TransactionReport(
        transactions=sorted(seen.values(), key=lambda t: (
            t.initiated_at or datetime.min.replace(tzinfo=timezone.utc), t.transaction_id)),
        last_refreshed_at=min(refreshed) if refreshed else None,
    )
