"""
The only module that talks to PayPal (through the ``paypal`` Server SDK).

Everything returned from here is a plain dataclass of this app's own, so no
SDK type (or its ``UNSET`` sentinel) leaks into the rest of the code, and every
SDK failure is translated into one of the ``PayPalError`` subclasses below.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import (
    ApiError, ClientCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient,
    OAuthProviderError, RawError, UNSET, UnsetType)
from paypal.models import (
    Address, AmountWithBreakdown, CaptureRequest, CardRequest, CardResponse, Customer,
    Error, Money, OrderRequest, PaymentSource, PaymentTokenRequest,
    PaymentTokenRequestCard, PaymentTokenRequestPaymentSource, PurchaseUnit,
    PurchaseUnitRequest, ReauthorizeRequest, RefundRequest)
from paypal.models.enums import CheckoutPaymentIntent

logger = logging.getLogger(__name__)

T = TypeVar('T')

# The only server the SDK declares (sdk-map.md, "Servers & auth").
SANDBOX_BASE_URL = 'https://api-m.sandbox.paypal.com'
_BASE_URLS = {'sandbox': SANDBOX_BASE_URL}

_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_BACKOFF_SECONDS = 0.5


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------

class PayPalError(Exception):
    """Base class for every failure talking to PayPal."""

    def __init__(self, message: str, *, operation: str = '') -> None:
        super().__init__(message)
        self.message = message
        self.operation = operation


class PayPalConfigurationError(PayPalError):
    """Credentials missing/rejected or the account may not use this call.
    Nothing was done at PayPal."""


class PayPalRejected(PayPalError):
    """PayPal answered and refused the request. Definitive: nothing happened."""

    def __init__(self, message: str, *, operation: str = '', status_code: int = 0,
                 name: str = '', issue: str = '', debug_id: str = '') -> None:
        super().__init__(message, operation=operation)
        self.status_code = status_code
        self.name = name
        self.issue = issue
        self.debug_id = debug_id

    @property
    def code(self) -> str:
        return self.issue or self.name or 'HTTP_%s' % self.status_code


class PayPalOutcomeUnknown(PayPalError):
    """PayPal could not be reached or its answer could not be read. The call may
    or may not have taken effect; repeat it with the same request id."""


class PayPalUnavailable(PayPalOutcomeUnknown):
    pass


class PayPalUnreadable(PayPalOutcomeUnknown):
    pass


# --------------------------------------------------------------------------
# Values handed back to the app
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BillingAddress:
    country_code: str
    address_line_1: str = ''
    address_line_2: str = ''
    admin_area_2: str = ''   # city
    admin_area_1: str = ''   # state / province
    postal_code: str = ''


@dataclass(frozen=True)
class CardInput:
    """Card details for one call. Held in memory only; never persisted or logged."""

    number: str = field(repr=False)
    expiry: str                          # YYYY-MM
    security_code: str = field(default='', repr=False)
    name: str = ''
    billing_address: Optional[BillingAddress] = None


@dataclass(frozen=True)
class AuthorizationInfo:
    id: str
    status: str
    amount: Optional[Decimal]
    currency: str
    created_at: Optional[datetime]
    expires_at: Optional[datetime]


@dataclass(frozen=True)
class OrderOutcome:
    paypal_order_id: str
    status: str
    authorization: Optional[AuthorizationInfo]
    card_brand: str
    card_last_digits: str


@dataclass(frozen=True)
class CaptureInfo:
    id: str
    status: str
    amount: Optional[Decimal]
    currency: str
    paypal_fee: Optional[Decimal]
    net_amount: Optional[Decimal]
    created_at: Optional[datetime]


@dataclass(frozen=True)
class RefundInfo:
    id: str
    status: str
    amount: Optional[Decimal]
    currency: str


@dataclass(frozen=True)
class VaultedCard:
    token_id: str
    customer_id: str
    brand: str
    last_digits: str
    expiry: str
    name: str


@dataclass(frozen=True)
class ReportedTransaction:
    transaction_id: str
    reference_id: str
    event_code: str
    initiated_at: Optional[datetime]
    amount: Optional[Decimal]
    currency: str
    fee: Optional[Decimal]
    status: str
    invoice_id: str
    custom_field: str


@dataclass(frozen=True)
class TransactionPage:
    transactions: list[ReportedTransaction]
    page: int
    total_pages: int
    last_refreshed_at: Optional[datetime]


# --------------------------------------------------------------------------
# Client lifetime
# --------------------------------------------------------------------------

class _LoggingTransport:
    """Logs method, path, status and PayPal's debug id — never headers or bodies,
    which carry the bearer token and card data."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('PayPal %s %s failed: %s', request.method, path, type(exc).__name__)
            raise
        logger.info(
            'PayPal %s %s -> %s (%.0f ms, debug_id=%s)', request.method, path,
            response.status_code, (time.monotonic() - started) * 1000,
            response.headers.get('paypal-debug-id', '-'))
        return response

    def close(self) -> None:
        self._inner.close()


_client_lock = threading.Lock()
_client: Optional[PaypalClient] = None


def _setting(name: str) -> str:
    value = getattr(settings, name, None)
    if not value:
        raise ImproperlyConfigured('%s is not configured' % name)
    return str(value)


def resolve_base_url() -> str:
    override = getattr(settings, 'PAYPAL_BASE_URL', None)
    if override:
        return str(override)
    environment = _setting('PAYPAL_ENVIRONMENT').strip().lower()
    try:
        return _BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            'PAYPAL_ENVIRONMENT=%r has no known API host; set PAYPAL_BASE_URL '
            'to the API base address for it' % environment) from None


def currency() -> str:
    return _setting('PAYPAL_CURRENCY').strip().upper()


def build_client(http_client: Optional[HttpClient] = None) -> PaypalClient:
    timeout = float(getattr(settings, 'PAYPAL_TIMEOUT_SECONDS', 20.0))
    transport = http_client or HttpxClient(timeout=timeout)
    return PaypalClient(
        base_url=resolve_base_url(),
        timeout=timeout,
        custom_http_client=_LoggingTransport(transport),
        oauth2=ClientCredentials(
            client_id=_setting('PAYPAL_CLIENT_ID'),
            client_secret=_setting('PAYPAL_CLIENT_SECRET')),
    )


def get_client() -> PaypalClient:
    """The process-wide client, built lazily (so after any worker fork)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: Optional[PaypalClient]) -> None:
    """Swap the process-wide client (tests, credential rotation)."""
    global _client
    with _client_lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


# --------------------------------------------------------------------------
# Call boundary
# --------------------------------------------------------------------------

def _call(operation: str, fn: Callable[[], T], *, retry_safe: bool) -> T:
    """Run one SDK call and translate every failure kind.

    ``retry_safe`` calls are reads or writes carrying a deterministic
    PayPal-Request-Id, so one resend after a transient failure cannot act twice.
    """
    attempts = 2 if retry_safe else 1
    for attempt in range(1, attempts + 1):
        last = attempt == attempts
        try:
            return fn()
        except ApiError as exc:
            if isinstance(exc.error, OAuthProviderError):
                logger.error('PayPal credentials rejected during %s: %s',
                             operation, exc.error.error)
                raise PayPalConfigurationError(
                    'PayPal rejected the configured API credentials',
                    operation=operation) from exc
            if exc.status_code in _TRANSIENT_STATUSES and not last:
                time.sleep(_RETRY_BACKOFF_SECONDS)
                continue
            raise _translate(operation, exc) from exc
        except ValueError as exc:  # pydantic.ValidationError included
            logger.error('Unreadable PayPal response during %s', operation)
            raise PayPalUnreadable(
                'PayPal returned a response that could not be read', operation=operation) from exc
        except httpx.HTTPError as exc:
            if not last:
                time.sleep(_RETRY_BACKOFF_SECONDS)
                continue
            raise PayPalUnavailable(
                'PayPal could not be reached (%s)' % type(exc).__name__,
                operation=operation) from exc
    raise AssertionError('unreachable')  # pragma: no cover


def _translate(operation: str, exc: ApiError[Any]) -> PayPalError:
    status = exc.status_code
    error = exc.error
    if isinstance(error, Error):
        issue = ''
        message = error.message
        details = _opt(error.details)
        if details:
            issue = details[0].issue
            message = _opt(details[0].description) or message
        logger.warning('PayPal refused %s: HTTP %s %s %s (debug_id=%s)',
                       operation, status, error.name, issue, error.debug_id)
        if status in (401, 403):
            return PayPalConfigurationError(
                'PayPal refused %s for this account: %s' % (operation, message),
                operation=operation)
        return PayPalRejected(message, operation=operation, status_code=status,
                              name=error.name, issue=issue, debug_id=error.debug_id)
    text = error.text()[:500] if isinstance(error, RawError) else ''
    logger.warning('PayPal %s failed: HTTP %s', operation, status)
    if status >= 500:
        return PayPalUnavailable('PayPal failed with HTTP %s' % status, operation=operation)
    if status in (401, 403):
        return PayPalConfigurationError(
            'PayPal refused %s (HTTP %s)' % (operation, status), operation=operation)
    return PayPalRejected(text or 'PayPal refused the request (HTTP %s)' % status,
                          operation=operation, status_code=status, name='HTTP_%s' % status)


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------

def _opt(value: T | UnsetType) -> Optional[T]:
    return None if isinstance(value, UnsetType) else value


def _str(value: object) -> str:
    """An SDK string or open enum (member or unknown str) as its wire value."""
    if value is None or isinstance(value, UnsetType):
        return ''
    return str(value)


def money_value(amount: Decimal) -> str:
    return '%s' % amount.quantize(Decimal('0.01'))


def _money(value: Money | UnsetType) -> tuple[Optional[Decimal], str]:
    money = _opt(value)
    if money is None:
        return None, ''
    try:
        return Decimal(money.value), money.currency_code
    except InvalidOperation:
        raise PayPalUnreadable('PayPal returned an invalid amount %r' % money.value) from None


def _datetime(value: str | UnsetType) -> Optional[datetime]:
    text = _opt(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _required_id(operation: str, value: str | UnsetType) -> str:
    ident = _opt(value)
    if not ident:
        raise PayPalUnreadable(
            'PayPal did not return an id for %s; outcome unknown' % operation,
            operation=operation)
    return ident


def _address(address: Optional[BillingAddress]) -> Optional[Address]:
    if address is None:
        return None
    return Address(
        country_code=address.country_code,
        address_line_1=address.address_line_1 or UNSET,
        address_line_2=address.address_line_2 or UNSET,
        admin_area_2=address.admin_area_2 or UNSET,
        admin_area_1=address.admin_area_1 or UNSET,
        postal_code=address.postal_code or UNSET)


def _authorization(auth: Any) -> AuthorizationInfo:
    amount, currency_code = _money(auth.amount)
    return AuthorizationInfo(
        id=_required_id('authorization', auth.id),
        status=_str(auth.status),
        amount=amount,
        currency=currency_code,
        created_at=_datetime(auth.create_time),
        expires_at=_datetime(auth.expiration_time),
    )


def _order_outcome(order_id: str | UnsetType, status: object,
                   purchase_units: list[PurchaseUnit] | UnsetType,
                   card: CardResponse | UnsetType | None) -> OrderOutcome:
    authorization = None
    units = _opt(purchase_units) or []
    if units:
        payments = _opt(units[0].payments)
        auths = _opt(payments.authorizations) if payments is not None else None
        if auths:
            authorization = _authorization(auths[0])
    card_resp = _opt(card) if card is not None else None
    return OrderOutcome(
        paypal_order_id=_required_id('create order', order_id),
        status=_str(status),
        authorization=authorization,
        card_brand=_str(card_resp.brand) if card_resp else '',
        card_last_digits=_str(card_resp.last_digits) if card_resp else '',
    )


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

def create_authorized_order(*, amount: Decimal, currency_code: str, reference_id: str,
                            invoice_id: str, custom_id: str, description: str,
                            request_id: str, card: Optional[CardInput] = None,
                            vault_id: Optional[str] = None) -> OrderOutcome:
    """Create a PayPal order with intent AUTHORIZE, funded by a card or a vaulted card."""
    if (card is None) == (vault_id is None):
        raise ValueError('exactly one of card or vault_id is required')
    if card is not None:
        card_request = CardRequest(
            number=card.number, expiry=card.expiry,
            security_code=card.security_code or UNSET,
            name=card.name or UNSET,
            billing_address=_address(card.billing_address) or UNSET)
    else:
        assert vault_id is not None
        card_request = CardRequest(vault_id=vault_id)
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            reference_id=reference_id,
            invoice_id=invoice_id,
            custom_id=custom_id,
            description=description[:127],
            amount=AmountWithBreakdown(currency_code=currency_code, value=money_value(amount)),
        )],
        payment_source=PaymentSource(card=card_request),
    )
    order = _call('create order', lambda: get_client().orders.create_order(
        body, pay_pal_request_id=request_id, prefer='return=representation'),
        retry_safe=True)
    source = _opt(order.payment_source)
    return _order_outcome(order.id, order.status, order.purchase_units,
                          source.card if source is not None else None)


def authorize_order(paypal_order_id: str, *, request_id: str) -> OrderOutcome:
    response = _call('authorize order', lambda: get_client().orders.authorize_order(
        paypal_order_id, pay_pal_request_id=request_id, prefer='return=representation'),
        retry_safe=True)
    source = _opt(response.payment_source)
    return _order_outcome(response.id, response.status, response.purchase_units,
                          source.card if source is not None else None)


def get_authorization(authorization_id: str) -> AuthorizationInfo:
    auth = _call('get authorization', lambda: get_client().payments.get_authorized_payment(
        authorization_id), retry_safe=True)
    return _authorization(auth)


def reauthorize(authorization_id: str, *, amount: Decimal, currency_code: str,
                request_id: str) -> AuthorizationInfo:
    body = ReauthorizeRequest(amount=Money(currency_code=currency_code, value=money_value(amount)))
    auth = _call('reauthorize', lambda: get_client().payments.reauthorize_payment(
        authorization_id, pay_pal_request_id=request_id, prefer='return=representation',
        body=body), retry_safe=True)
    return _authorization(auth)


def capture(authorization_id: str, *, amount: Decimal, currency_code: str, request_id: str,
            invoice_id: str = '') -> CaptureInfo:
    body = CaptureRequest(
        amount=Money(currency_code=currency_code, value=money_value(amount)),
        invoice_id=invoice_id or UNSET, final_capture=True)
    captured = _call('capture', lambda: get_client().payments.capture_authorized_payment(
        authorization_id, pay_pal_request_id=request_id, prefer='return=representation',
        body=body), retry_safe=True)
    amount_value, currency_value = _money(captured.amount)
    fee = net = None
    breakdown = _opt(captured.seller_receivable_breakdown)
    if breakdown is not None:
        fee, _ = _money(breakdown.paypal_fee)
        net, _ = _money(breakdown.net_amount)
    return CaptureInfo(
        id=_required_id('capture', captured.id),
        status=_str(captured.status),
        amount=amount_value,
        currency=currency_value,
        paypal_fee=fee,
        net_amount=net,
        created_at=_datetime(captured.create_time),
    )


def void(authorization_id: str, *, request_id: str) -> AuthorizationInfo:
    auth = _call('void', lambda: get_client().payments.void_payment(
        authorization_id, pay_pal_request_id=request_id, prefer='return=representation'),
        retry_safe=True)
    return _authorization(auth)


def refund(capture_id: str, *, amount: Decimal, currency_code: str, request_id: str,
           note: str = '') -> RefundInfo:
    body = RefundRequest(
        amount=Money(currency_code=currency_code, value=money_value(amount)),
        note_to_payer=note[:255] or UNSET)
    result = _call('refund', lambda: get_client().payments.refund_captured_payment(
        capture_id, pay_pal_request_id=request_id, prefer='return=representation', body=body),
        retry_safe=True)
    amount_value, currency_value = _money(result.amount)
    return RefundInfo(
        id=_required_id('refund', result.id),
        status=_str(result.status),
        amount=amount_value,
        currency=currency_value,
    )


def vault_card(card: CardInput, *, request_id: str,
               customer_id: Optional[str] = None) -> VaultedCard:
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(
            number=card.number, expiry=card.expiry,
            security_code=card.security_code or UNSET,
            name=card.name or UNSET,
            billing_address=_address(card.billing_address) or UNSET)),
        customer=Customer(id=customer_id) if customer_id else UNSET)
    token = _call('save card', lambda: get_client().vault.create_payment_token(
        body, pay_pal_request_id=request_id), retry_safe=True)
    customer = _opt(token.customer)
    source = _opt(token.payment_source)
    vaulted = _opt(source.card) if source is not None else None
    return VaultedCard(
        token_id=_required_id('save card', token.id),
        customer_id=_str(customer.id) if customer is not None else '',
        brand=_str(vaulted.brand) if vaulted else '',
        last_digits=_str(vaulted.last_digits) if vaulted else '',
        expiry=_str(vaulted.expiry) if vaulted else '',
        name=_str(vaulted.name) if vaulted else '',
    )


def delete_vaulted_card(token_id: str) -> None:
    # Deleting is naturally idempotent, so a resend after a transient failure is safe.
    _call('delete saved card', lambda: get_client().vault.delete_payment_token(token_id),
          retry_safe=True)


def search_transactions(*, start: datetime, end: datetime, page: int,
                        page_size: int = 100) -> TransactionPage:
    response = _call('search transactions', lambda: get_client().transaction_search
                     .search_transactions(_rfc3339(start), _rfc3339(end),
                                          page=page, page_size=page_size),
                     retry_safe=True)
    transactions = []
    for detail in _opt(response.transaction_details) or []:
        info = _opt(detail.transaction_info)
        if info is None:
            continue
        amount, currency_code = _money(info.transaction_amount)
        fee, _ = _money(info.fee_amount)
        transactions.append(ReportedTransaction(
            transaction_id=_str(info.transaction_id),
            reference_id=_str(info.paypal_reference_id),
            event_code=_str(info.transaction_event_code),
            initiated_at=_datetime(info.transaction_initiation_date),
            amount=amount,
            currency=currency_code,
            fee=fee,
            status=_str(info.transaction_status),
            invoice_id=_str(info.invoice_id),
            custom_field=_str(info.custom_field),
        ))
    return TransactionPage(
        transactions=transactions,
        page=_opt(response.page) or page,
        total_pages=_opt(response.total_pages) or 0,
        last_refreshed_at=_datetime(response.last_refreshed_datetime),
    )


def _rfc3339(value: datetime) -> str:
    # Internet date-time with seconds, as the reporting API requires.
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
