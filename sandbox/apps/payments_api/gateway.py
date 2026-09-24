"""
The one place this site talks to PayPal.

Everything here is plain Python over the PayPal Server SDK (``paypal``): it
turns SDK models into small frozen dataclasses (resolving the SDK's ``UNSET``
sentinel), and every SDK failure into a :class:`PayPalError` that says what the
caller of our API should see and whether the PayPal write may have landed.

The SDK performs no retries. This module retries reads on transient failures,
and retries writes only under the same ``PayPal-Request-Id`` (PayPal
de-duplicates on it), so a retry can never charge, capture or refund twice.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

import httpx
from paypal import PaypalClient
from paypal.core import (
    UNSET,
    ApiError,
    ClientCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    OAuthProviderError,
    RawError,
    UnsetType,
)
from paypal.models import (
    Address,
    AmountBreakdown,
    AmountWithBreakdown,
    CaptureRequest,
    CapturedPayment,
    CardRequest,
    CardResponse,
    Customer,
    Error,
    ItemRequest,
    Money,
    Order,
    OrderAuthorizeResponse,
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
from paypal.models.enums import CheckoutPaymentIntent, OrderStatus
from pydantic import ValidationError

logger = logging.getLogger(__name__)

T = TypeVar('T')

# The SDK's only declared server (sdk-map "Servers & auth").
SANDBOX_BASE_URL = 'https://api-m.sandbox.paypal.com'
RETURN_REPRESENTATION = 'return=representation'

# PayPal's transaction search accepts at most 31 days per request.
SEARCH_WINDOW = timedelta(days=31)
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_PAGES = 1000

# ISO 4217 minor units for every currency that does not have two.
_CURRENCY_EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0,
    'PYG': 0, 'RWF': 0, 'UGX': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
}

# Failures raised before the request left this process: nothing can have landed.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Backoff between retries; a module attribute so tests can replace it.
_sleep: Callable[[float], None] = time.sleep


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PayPalError(Exception):
    """
    A PayPal call that did not succeed.

    ``status_code`` is the HTTP status our own API should answer with;
    ``outcome_unknown`` is True when the request may have taken effect at
    PayPal even though we could not read the answer.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        outcome_unknown: bool = False,
        provider_status: int | None = None,
        issues: Sequence[str] = (),
        debug_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.issues = tuple(issues)
        self.debug_id = debug_id

    def has_issue(self, *issues: str) -> bool:
        return any(issue in self.issues for issue in issues)


class PayPalConfigError(PayPalError):
    """Our credentials or configuration were refused; nothing was attempted."""


class PayPalRejected(PayPalError):
    """PayPal refused the request itself (validation, business rule, decline)."""


class PayPalUnavailable(PayPalError):
    """PayPal could not be reached, or did not give a readable answer."""


# ---------------------------------------------------------------------------
# Values handed out of this module (never SDK models, never UNSET)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Amount:
    value: Decimal
    currency: str


@dataclass(frozen=True)
class CardDetails:
    """Card data for a single request. Never persisted and never logged."""

    number: str = field(repr=False)
    expiry: str  # YYYY-MM
    security_code: str | None = field(default=None, repr=False)
    name: str | None = None
    billing_address: Mapping[str, str] | None = None


@dataclass(frozen=True)
class LineItem:
    name: str
    sku: str
    unit_amount: Decimal
    quantity: int


@dataclass(frozen=True)
class AuthorizationInfo:
    authorization_id: str
    status: str
    status_reason: str | None
    amount: Amount | None
    created_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True)
class AuthorizeResult:
    paypal_order_id: str
    order_status: str
    authorization: AuthorizationInfo | None
    card_brand: str | None
    card_last_digits: str | None


@dataclass(frozen=True)
class CaptureInfo:
    capture_id: str
    status: str
    status_reason: str | None
    amount: Amount | None
    gross_amount: Amount | None
    paypal_fee: Amount | None
    net_amount: Amount | None


@dataclass(frozen=True)
class RefundInfo:
    refund_id: str
    status: str
    status_reason: str | None
    amount: Amount | None
    paypal_fee: Amount | None
    net_amount: Amount | None


@dataclass(frozen=True)
class VaultedCard:
    token_id: str
    customer_id: str | None
    brand: str | None
    last_digits: str
    expiry: str | None


@dataclass(frozen=True)
class TransactionRecord:
    transaction_id: str | None
    reference_id: str | None
    event_code: str | None
    status: str | None
    amount: Amount | None
    fee: Amount | None
    invoice_id: str | None
    custom_field: str | None
    initiated_at: str | None


@dataclass(frozen=True)
class TransactionSearchResult:
    transactions: list[TransactionRecord]
    windows: int
    pages: int
    last_refreshed: str | None
    # Windows PayPal has no report data for yet (its reporting lags live activity).
    unavailable_windows: list[tuple[datetime, datetime]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Configuration and the client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    base_url: str
    currency: str
    timeout: float
    reference_prefix: str


def resolve_base_url(environment: str, override: str) -> str:
    """``PAYPAL_BASE_URL`` wins verbatim; otherwise the environment selects the host."""
    if override:
        return override
    if environment.strip().lower() == 'sandbox':
        return SANDBOX_BASE_URL
    raise PayPalConfigError(
        'PAYPAL_ENVIRONMENT=%r has no known PayPal host; set PAYPAL_BASE_URL.' % environment,
        status_code=502, code='PAYPAL_NOT_CONFIGURED')


class LoggingTransport:
    """Logs method, path, status and PayPal's debug id. Never bodies, headers or query strings."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = httpx.URL(request.url).path
        try:
            response = self._inner.send(request)
        except httpx.HTTPError as exc:
            logger.warning('PayPal %s %s failed: %s', request.method, path, type(exc).__name__)
            raise
        logger.info(
            'PayPal %s %s -> %s (%.0f ms, debug id %s)',
            request.method, path, response.status_code, (time.monotonic() - started) * 1000,
            response.headers.get('paypal-debug-id', '-'))
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(config: PayPalConfig, transport: HttpClient | None = None) -> PaypalClient:
    if not config.client_id or not config.client_secret:
        raise PayPalConfigError(
            'PayPal credentials are not configured (PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET).',
            status_code=502, code='PAYPAL_NOT_CONFIGURED')
    return PaypalClient(
        base_url=config.base_url,
        timeout=config.timeout,
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=config.timeout)),
        oauth2=ClientCredentials(client_id=config.client_id, client_secret=config.client_secret),
    )


class Gateway:
    """PayPal operations used by this site. Hold one per process; it owns a connection pool."""

    def __init__(self, client: PaypalClient, config: PayPalConfig) -> None:
        self._client = client
        self.config = config

    def close(self) -> None:
        self._client.close()

    # -- authorize -----------------------------------------------------------

    def authorize(
        self,
        *,
        request_id: str,
        reference: str,
        invoice_id: str,
        description: str,
        amount: Decimal,
        currency: str,
        items: Sequence[LineItem],
        card: CardDetails | None = None,
        vault_id: str | None = None,
    ) -> AuthorizeResult:
        """
        Put a hold on ``amount``: a single-step PayPal order with intent
        AUTHORIZE, paid by the card or saved card given. PayPal authorizes as
        part of creating the order; when it answers APPROVED instead, the
        authorization is requested explicitly under a derived request id.
        """
        if (card is None) == (vault_id is None):
            raise ValueError('pass exactly one of card or vault_id')
        if card is not None:
            card_request = CardRequest(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code if card.security_code else UNSET,
                name=card.name if card.name else UNSET,
                billing_address=_address(card.billing_address),
            )
        else:
            card_request = CardRequest(vault_id=vault_id or UNSET)
        body = OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[_purchase_unit(reference, invoice_id, description, amount, currency, items)],
            payment_source=PaymentSource(card=card_request),
        )
        order = self._write(
            'create_order',
            lambda: self._client.orders.create_order(
                body, pay_pal_request_id=request_id, prefer=RETURN_REPRESENTATION))
        order_id = _require(order.id, 'create_order', 'id')
        result: Order | OrderAuthorizeResponse = order
        if _enum_str(order.status) == OrderStatus.APPROVED.value:
            result = self._authorize_approved(order_id, request_id + '-authorize')
        return AuthorizeResult(
            paypal_order_id=order_id,
            order_status=_enum_str(result.status) or 'UNKNOWN',
            authorization=_first_authorization(result),
            card_brand=_card_brand(result),
            card_last_digits=_card_last_digits(result),
        )

    def _authorize_approved(self, order_id: str, request_id: str) -> OrderAuthorizeResponse:
        return self._write(
            'authorize_order',
            lambda: self._client.orders.authorize_order(
                order_id, pay_pal_request_id=request_id, prefer=RETURN_REPRESENTATION))

    # -- authorizations --------------------------------------------------------

    def get_authorization(self, authorization_id: str) -> AuthorizationInfo:
        auth = self._read(
            'get_authorized_payment',
            lambda: self._client.payments.get_authorized_payment(authorization_id))
        return _authorization_info(auth, 'get_authorized_payment')

    def reauthorize(self, authorization_id: str, *, request_id: str) -> AuthorizationInfo:
        auth = self._write(
            'reauthorize_payment',
            lambda: self._client.payments.reauthorize_payment(
                authorization_id, pay_pal_request_id=request_id, prefer=RETURN_REPRESENTATION))
        return _authorization_info(auth, 'reauthorize_payment')

    def void(self, authorization_id: str, *, request_id: str) -> AuthorizationInfo:
        auth = self._write(
            'void_payment',
            lambda: self._client.payments.void_payment(
                authorization_id, pay_pal_request_id=request_id, prefer=RETURN_REPRESENTATION))
        return _authorization_info(auth, 'void_payment')

    # -- capture and refund ----------------------------------------------------

    def capture(
        self, authorization_id: str, *, request_id: str, amount: Decimal, currency: str, invoice_id: str,
    ) -> CaptureInfo:
        body = CaptureRequest(
            amount=Money(currency_code=currency, value=format_amount(amount, currency)),
            final_capture=True,
            invoice_id=invoice_id,
        )
        captured = self._write(
            'capture_authorized_payment',
            lambda: self._client.payments.capture_authorized_payment(
                authorization_id, pay_pal_request_id=request_id, prefer=RETURN_REPRESENTATION, body=body))
        return _capture_info(captured, 'capture_authorized_payment')

    def get_capture(self, capture_id: str) -> CaptureInfo:
        captured = self._read(
            'get_captured_payment', lambda: self._client.payments.get_captured_payment(capture_id))
        return _capture_info(captured, 'get_captured_payment')

    def refund(self, capture_id: str, *, request_id: str, amount: Decimal, currency: str) -> RefundInfo:
        body = RefundRequest(amount=Money(currency_code=currency, value=format_amount(amount, currency)))
        refund = self._write(
            'refund_captured_payment',
            lambda: self._client.payments.refund_captured_payment(
                capture_id, pay_pal_request_id=request_id, prefer=RETURN_REPRESENTATION, body=body))
        return _refund_info(refund)

    # -- vault -----------------------------------------------------------------

    def vault_card(self, card: CardDetails, *, request_id: str, customer_id: str | None) -> VaultedCard:
        body = PaymentTokenRequest(
            customer=Customer(id=customer_id) if customer_id else UNSET,
            payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code if card.security_code else UNSET,
                name=card.name if card.name else UNSET,
                billing_address=_address(card.billing_address),
            )),
        )
        token = self._write(
            'create_payment_token',
            lambda: self._client.vault.create_payment_token(body, pay_pal_request_id=request_id))
        return _vaulted_card(token)

    def delete_vaulted_card(self, token_id: str) -> None:
        """Remove a vault token. A token PayPal no longer knows counts as removed."""
        try:
            self._write('delete_payment_token', lambda: self._client.vault.delete_payment_token(token_id))
        except PayPalRejected as exc:
            if exc.provider_status == 404:
                return
            raise

    # -- reporting -------------------------------------------------------------

    def search_transactions(self, start: datetime, end: datetime) -> TransactionSearchResult:
        """
        PayPal's own record of transactions between ``start`` and ``end``:
        every page of every 31-day window the range spans.
        """
        records: list[TransactionRecord] = []
        unavailable: list[tuple[datetime, datetime]] = []
        windows = pages = 0
        last_refreshed: str | None = None
        window_start = start
        while window_start < end:
            window_end = min(window_start + SEARCH_WINDOW, end)
            windows += 1
            page = 1
            while True:
                try:
                    result = self._search_page(window_start, window_end, page)
                except PayPalRejected as exc:
                    # PayPal answers 404 when it has not yet processed report
                    # data for the window's start: nothing to list there yet.
                    if exc.provider_status != 404:
                        raise
                    unavailable.append((window_start, window_end))
                    break
                pages += 1
                details = _value(result.transaction_details) or []
                for detail in details:
                    info = _value(detail.transaction_info)
                    if info is not None:
                        records.append(TransactionRecord(
                            transaction_id=_value(info.transaction_id),
                            reference_id=_value(info.paypal_reference_id),
                            event_code=_value(info.transaction_event_code),
                            status=_value(info.transaction_status),
                            amount=_amount(info.transaction_amount),
                            fee=_amount(info.fee_amount),
                            invoice_id=_value(info.invoice_id),
                            custom_field=_value(info.custom_field),
                            initiated_at=_value(info.transaction_initiation_date),
                        ))
                last_refreshed = _value(result.last_refreshed_datetime) or last_refreshed
                total_pages = _value(result.total_pages)
                if total_pages is not None:
                    if page >= total_pages:
                        break
                elif len(details) < SEARCH_PAGE_SIZE:
                    break
                if page >= SEARCH_MAX_PAGES:
                    raise PayPalUnavailable(
                        'Transaction search returned more than %d pages.' % SEARCH_MAX_PAGES,
                        status_code=502, code='PAYPAL_SEARCH_TOO_LARGE')
                page += 1
            window_start = window_end
        return TransactionSearchResult(records, windows, pages, last_refreshed, unavailable)

    def _search_page(self, start: datetime, end: datetime, page: int) -> SearchResponse:
        return self._read(
            'search_transactions',
            lambda: self._client.transaction_search.search_transactions(
                _rfc3339(start), _rfc3339(end),
                fields='transaction_info', page_size=SEARCH_PAGE_SIZE, page=page))

    # -- call discipline -------------------------------------------------------

    def _read(self, operation: str, call: Callable[[], T]) -> T:
        return _call_with_retries(operation, call, attempts=3, write=False)

    def _write(self, operation: str, call: Callable[[], T]) -> T:
        # Every write here is either keyed by PayPal-Request-Id or naturally
        # idempotent (deleting a vault token), so a resend cannot duplicate it.
        return _call_with_retries(operation, call, attempts=2, write=True)


def _call_with_retries(operation: str, call: Callable[[], T], *, attempts: int, write: bool) -> T:
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            return _translate(operation, call, write=write)
        except PayPalError as exc:
            retryable = isinstance(exc, PayPalUnavailable) and exc.code in _RETRYABLE_CODES
            if not retryable or attempt == attempts:
                raise
            logger.info('PayPal %s: retrying after %s (attempt %d)', operation, exc.code, attempt)
            _sleep(delay)
            delay *= 2
    raise AssertionError('unreachable')  # pragma: no cover


_RETRYABLE_CODES = frozenset({'PAYPAL_UNREACHABLE', 'PAYPAL_NO_RESPONSE', 'PAYPAL_SERVER_ERROR',
                              'PAYPAL_RATE_LIMITED'})


def _translate(operation: str, call: Callable[[], T], *, write: bool) -> T:
    """The error ladder: every SDK failure kind becomes one PayPalError."""
    try:
        return call()
    except ApiError as exc:
        raise _from_api_error(operation, exc, write=write) from exc
    except ValidationError as exc:
        # Checked before ValueError: pydantic's ValidationError subclasses it.
        raise PayPalUnavailable(
            'PayPal returned a response this site could not read.',
            status_code=502, code='PAYPAL_UNREADABLE_RESPONSE', outcome_unknown=write) from exc
    except ValueError as exc:
        raise PayPalUnavailable(
            'PayPal returned a response this site could not read.',
            status_code=502, code='PAYPAL_UNREADABLE_RESPONSE', outcome_unknown=write) from exc
    except _NEVER_SENT as exc:
        raise PayPalUnavailable(
            'PayPal could not be reached; nothing was sent.',
            status_code=502, code='PAYPAL_UNREACHABLE', outcome_unknown=False) from exc
    except httpx.RequestError as exc:
        raise PayPalUnavailable(
            'PayPal did not answer in time.',
            status_code=504, code='PAYPAL_NO_RESPONSE', outcome_unknown=write) from exc


def _from_api_error(operation: str, exc: ApiError[Any], *, write: bool) -> PayPalError:
    status = exc.status_code
    if isinstance(exc.error, OAuthProviderError):
        logger.error('PayPal token request refused: %s', exc.error.error)
        return PayPalConfigError(
            'PayPal refused this site\'s API credentials.', status_code=502,
            code='PAYPAL_AUTH_FAILED', provider_status=status)
    name, message, issues, debug_id = _error_details(exc.error)
    logger.warning('PayPal %s -> HTTP %s %s %s (debug id %s)', operation, status, name, ','.join(issues), debug_id)
    if status in (401, 403):
        return PayPalConfigError(
            'PayPal refused this site\'s credentials or permissions.', status_code=502,
            code='PAYPAL_FORBIDDEN', provider_status=status, issues=issues, debug_id=debug_id)
    if status == 429:
        return PayPalUnavailable(
            'PayPal is rate limiting this site; try again shortly.', status_code=503,
            code='PAYPAL_RATE_LIMITED', provider_status=status, debug_id=debug_id)
    if status in (400, 404, 409, 422):
        return PayPalRejected(
            message or 'PayPal rejected the request.', status_code=status, code=name or 'PAYPAL_REJECTED',
            provider_status=status, issues=issues, debug_id=debug_id)
    if status >= 500:
        return PayPalUnavailable(
            'PayPal failed to process the request.', status_code=502, code='PAYPAL_SERVER_ERROR',
            outcome_unknown=write, provider_status=status, debug_id=debug_id)
    return PayPalUnavailable(
        'PayPal answered unexpectedly.', status_code=502, code='PAYPAL_UNEXPECTED_RESPONSE',
        provider_status=status, debug_id=debug_id)


def _error_details(error: object) -> tuple[str | None, str | None, list[str], str | None]:
    if isinstance(error, Error):
        details = _value(error.details) or []
        return error.name, error.message, [d.issue for d in details], error.debug_id
    if isinstance(error, RawError):
        try:
            payload = json.loads(error.text())
        except ValueError:
            return None, None, [], None
        if not isinstance(payload, dict):
            return None, None, [], None
        raw_details = payload.get('details')
        issues = [str(d.get('issue')) for d in raw_details if isinstance(d, dict) and d.get('issue')] \
            if isinstance(raw_details, list) else []
        name = payload.get('name') or payload.get('error')
        message = payload.get('message') or payload.get('error_description')
        return (
            str(name) if name else None,
            str(message) if message else None,
            issues,
            str(payload['debug_id']) if payload.get('debug_id') else None,
        )
    return None, None, [], None


# ---------------------------------------------------------------------------
# Process-wide gateway
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_gateway: Gateway | None = None
_gateway_pid: int | None = None
_override: Gateway | None = None


def get_gateway(config_factory: Callable[[], PayPalConfig]) -> Gateway:
    """
    The process-wide gateway, built lazily on first use so a forking server
    builds it after the fork, and closed at interpreter exit.
    """
    global _gateway, _gateway_pid
    if _override is not None:
        return _override
    with _lock:
        if _gateway is None or _gateway_pid != os.getpid():
            config = config_factory()
            _gateway = Gateway(build_client(config), config)
            _gateway_pid = os.getpid()
            atexit.register(_gateway.close)
        return _gateway


@contextmanager
def use_gateway(gateway: Gateway) -> Iterator[Gateway]:
    """Swap in a gateway (e.g. over a stub transport) for the duration of a block."""
    global _override
    previous, _override = _override, gateway
    try:
        yield gateway
    finally:
        _override = previous


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def currency_exponent(currency: str) -> int:
    return _CURRENCY_EXPONENT.get(currency.upper(), 2)


def format_amount(value: Decimal, currency: str) -> str:
    """A PayPal amount string with the currency's own number of decimals."""
    return str(value.quantize(Decimal(1).scaleb(-currency_exponent(currency))))


def _purchase_unit(
    reference: str, invoice_id: str, description: str, amount: Decimal, currency: str,
    items: Sequence[LineItem],
) -> PurchaseUnitRequest:
    item_total = sum((item.unit_amount * item.quantity for item in items), Decimal('0'))
    # Only itemise when the items add up exactly; PayPal rejects a breakdown that does not.
    itemised = bool(items) and format_amount(item_total, currency) == format_amount(amount, currency)
    return PurchaseUnitRequest(
        reference_id=reference,
        custom_id=reference,
        invoice_id=invoice_id,
        description=description[:127],
        amount=AmountWithBreakdown(
            currency_code=currency,
            value=format_amount(amount, currency),
            breakdown=AmountBreakdown(
                item_total=Money(currency_code=currency, value=format_amount(item_total, currency)),
            ) if itemised else UNSET,
        ),
        items=[
            ItemRequest(
                name=item.name[:127],
                sku=item.sku[:127] if item.sku else UNSET,
                quantity=str(item.quantity),
                unit_amount=Money(currency_code=currency, value=format_amount(item.unit_amount, currency)),
            )
            for item in items
        ] if itemised else UNSET,
    )


def _address(address: Mapping[str, str] | None) -> Address | UnsetType:
    if not address or not address.get('country_code'):
        return UNSET
    return Address(
        country_code=address['country_code'],
        address_line_1=address.get('address_line_1') or UNSET,
        address_line_2=address.get('address_line_2') or UNSET,
        admin_area_2=address.get('admin_area_2') or UNSET,
        admin_area_1=address.get('admin_area_1') or UNSET,
        postal_code=address.get('postal_code') or UNSET,
    )


def _value(value: T | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _enum_str(value: object) -> str | None:
    """An open enum member or an unknown wire string, as its wire value."""
    if value is None or isinstance(value, UnsetType):
        return None
    return str(value)


def _require(value: T | UnsetType, operation: str, member: str) -> T:
    if isinstance(value, UnsetType) or value is None:
        # The write may have taken effect; we just cannot name what it made.
        raise PayPalUnavailable(
            'PayPal %s returned no %s.' % (operation, member),
            status_code=502, code='PAYPAL_UNREADABLE_RESPONSE', outcome_unknown=True)
    return value


def _amount(money: Money | UnsetType | None) -> Amount | None:
    if money is None or isinstance(money, UnsetType):
        return None
    try:
        return Amount(Decimal(money.value), money.currency_code)
    except InvalidOperation:
        return None


def _timestamp(value: str | UnsetType) -> datetime | None:
    if isinstance(value, UnsetType) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds')


def _first_authorization(order: Order | OrderAuthorizeResponse) -> AuthorizationInfo | None:
    units = _value(order.purchase_units) or []
    for unit in units:
        payments = _value(unit.payments)
        if payments is None:
            continue
        for auth in _value(payments.authorizations) or []:
            auth_id = _value(auth.id)
            if auth_id:
                details = _value(auth.status_details)
                return AuthorizationInfo(
                    authorization_id=auth_id,
                    status=_enum_str(auth.status) or 'UNKNOWN',
                    status_reason=_enum_str(details.reason) if details is not None else None,
                    amount=_amount(auth.amount),
                    created_at=_timestamp(auth.create_time),
                    expires_at=_timestamp(auth.expiration_time),
                )
    return None


def _card(order: Order | OrderAuthorizeResponse) -> CardResponse | None:
    if isinstance(order, Order):
        source = _value(order.payment_source)
        return _value(source.card) if source is not None else None
    authorize_source = _value(order.payment_source)
    return _value(authorize_source.card) if authorize_source is not None else None


def _card_brand(order: Order | OrderAuthorizeResponse) -> str | None:
    card = _card(order)
    return _enum_str(card.brand) if card is not None else None


def _card_last_digits(order: Order | OrderAuthorizeResponse) -> str | None:
    card = _card(order)
    return _value(card.last_digits) if card is not None else None


def _authorization_info(auth: PaymentAuthorization, operation: str) -> AuthorizationInfo:
    details = _value(auth.status_details)
    return AuthorizationInfo(
        authorization_id=_require(auth.id, operation, 'id'),
        status=_enum_str(auth.status) or 'UNKNOWN',
        status_reason=_enum_str(details.reason) if details is not None else None,
        amount=_amount(auth.amount),
        created_at=_timestamp(auth.create_time),
        expires_at=_timestamp(auth.expiration_time),
    )


def _capture_info(captured: CapturedPayment, operation: str) -> CaptureInfo:
    breakdown = _value(captured.seller_receivable_breakdown)
    details = _value(captured.status_details)
    return CaptureInfo(
        capture_id=_require(captured.id, operation, 'id'),
        status=_enum_str(captured.status) or 'UNKNOWN',
        status_reason=_enum_str(details.reason) if details is not None else None,
        amount=_amount(captured.amount),
        gross_amount=_amount(breakdown.gross_amount) if breakdown is not None else None,
        paypal_fee=_amount(breakdown.paypal_fee) if breakdown is not None else None,
        net_amount=_amount(breakdown.net_amount) if breakdown is not None else None,
    )


def _refund_info(refund: Refund) -> RefundInfo:
    breakdown = _value(refund.seller_payable_breakdown)
    details = _value(refund.status_details)
    return RefundInfo(
        refund_id=_require(refund.id, 'refund_captured_payment', 'id'),
        status=_enum_str(refund.status) or 'UNKNOWN',
        status_reason=_enum_str(details.reason) if details is not None else None,
        amount=_amount(refund.amount),
        paypal_fee=_amount(breakdown.paypal_fee) if breakdown is not None else None,
        net_amount=_amount(breakdown.net_amount) if breakdown is not None else None,
    )


def _vaulted_card(token: PaymentTokenResponse) -> VaultedCard:
    token_id = _require(token.id, 'create_payment_token', 'id')
    source = _value(token.payment_source)
    card = _value(source.card) if source is not None else None
    customer = _value(token.customer)
    return VaultedCard(
        token_id=token_id,
        customer_id=_value(customer.id) if customer is not None else None,
        brand=_enum_str(card.brand) if card is not None else None,
        last_digits=_require(card.last_digits if card is not None else UNSET,
                             'create_payment_token', 'payment_source.card.last_digits'),
        expiry=_value(card.expiry) if card is not None else None,
    )
