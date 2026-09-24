"""
The one place this app talks to PayPal.

Everything PayPal-specific — the SDK client and its lifetime, request building,
the translation of every SDK failure into ``ProviderError``, and the mapping of
PayPal statuses onto outcomes — lives here, so the rest of the app deals in
plain values and one exception type.

Card data passes through this module on its way to PayPal and is never logged:
the transport logs method, path and status only, and SDK validation errors are
re-raised without their input values.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import ValidationError

from paypal import PaypalClient
from paypal.core import (
    UNSET,
    ApiError,
    ClientCredentials,
    Failure,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    OAuthProviderError,
    RawError,
    Success,
    UnsetType,
)
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CapturedPayment,
    CaptureRequest,
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
    ReauthorizeRequest,
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

logger = logging.getLogger(__name__)

# The only server the SDK declares (sdk-map.md, "Servers & auth").
SANDBOX_BASE_URL = 'https://api-m.sandbox.paypal.com'
_KNOWN_ENVIRONMENTS = {'sandbox': SANDBOX_BASE_URL}

# Transport failures raised before the request left: nothing can have happened.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

_PREFER_FULL = 'return=representation'

T = TypeVar('T')

Outcome = Literal['done', 'pending', 'failed', 'unknown']


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """A PayPal call that did not succeed.

    ``status_code`` is the HTTP status this app should answer with;
    ``outcome_unknown`` says whether the call may nevertheless have taken
    effect at PayPal; ``code`` carries PayPal's issue code when there is one.
    """

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False,
                 code: str = '', retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.code = code
        self.retryable = retryable


class ProviderConfigError(ProviderError):
    """Our PayPal configuration or credentials were refused. Never the caller's fault."""


class ProviderRejected(ProviderError):
    """PayPal refused the request itself (declined card, business rule). Nothing happened."""


class ProviderUnavailable(ProviderError):
    """PayPal could not be reached or answered unusably."""


class CardDataError(ValueError):
    """Card details that cannot be sent to PayPal. The message never contains the values."""


def _describe(error: object) -> tuple[str, str]:
    """(issue code, human description) from a PayPal error body."""
    if isinstance(error, Error):
        if not isinstance(error.details, UnsetType) and error.details:
            first = error.details[0]
            description = first.description if isinstance(first.description, str) else error.message
            return first.issue, description
        return error.name, error.message
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return '', ''
        if isinstance(body, dict):
            details = body.get('details')
            if isinstance(details, list) and details and isinstance(details[0], dict):
                return (str(details[0].get('issue', '')),
                        str(details[0].get('description') or body.get('message', '')))
            return str(body.get('name', '')), str(body.get('message', ''))
    return '', ''


def _translate(status: int, error: object, *, write: bool) -> ProviderError:
    if isinstance(error, OAuthProviderError):
        return ProviderConfigError(
            502, 'PayPal rejected the configured API credentials.', code='PAYPAL_CREDENTIALS')
    issue, description = _describe(error)
    if status in (401, 403):
        return ProviderConfigError(
            502, f'PayPal refused this request for our account (HTTP {status}).', code=issue)
    if status == 429:
        return ProviderUnavailable(
            503, 'PayPal is rate limiting requests; try again shortly.', retryable=True)
    if status in (400, 422):
        return ProviderRejected(422, description or 'PayPal rejected the request.', code=issue)
    if status in (404, 409):
        return ProviderRejected(409, description or 'PayPal reports a conflicting state.', code=issue)
    if status >= 500:
        # A write that failed on PayPal's side may still have been applied.
        return ProviderUnavailable(
            502, f'PayPal returned a server error (HTTP {status}).', outcome_unknown=write,
            code=issue, retryable=True)
    return ProviderUnavailable(502, f'PayPal returned an unexpected status (HTTP {status}).', code=issue)


def _call(fn: Callable[[], T], *, write: bool, what: str) -> T:
    """Run one SDK call, translating every failure kind into ProviderError."""
    try:
        return fn()
    except ApiError as e:
        logger.warning('PayPal %s failed: HTTP %s (debug id %s)', what, e.status_code,
                       e.response.headers.get('paypal-debug-id', '-'))
        raise _translate(e.status_code, e.error, write=write) from e
    except (ValidationError, ValueError) as e:
        # A body that does not decode. On a 2xx the call may have succeeded.
        logger.error('PayPal %s returned an unreadable response', what)
        raise ProviderUnavailable(
            502, 'PayPal returned a response that could not be read.', outcome_unknown=write) from e
    except _NEVER_SENT as e:
        logger.warning('PayPal %s not sent: %s', what, type(e).__name__)
        raise ProviderUnavailable(
            502, 'Could not reach PayPal; nothing was sent.', retryable=True) from e
    except httpx.RequestError as e:
        logger.warning('PayPal %s got no answer: %s', what, type(e).__name__)
        raise ProviderUnavailable(
            504, 'PayPal did not answer in time.', outcome_unknown=write, retryable=True) from e


def _read(fn: Callable[[], T], *, what: str, attempts: int = 3) -> T:
    """A read: safe to retry on transient failures."""
    for attempt in range(1, attempts + 1):
        try:
            return _call(fn, write=False, what=what)
        except ProviderError as e:
            if not e.retryable or attempt == attempts:
                raise
            time.sleep(0.5 * attempt)
    raise AssertionError('unreachable')


def _write(fn: Callable[[], T], *, what: str) -> T:
    """A write: sent exactly once. Safe repeats happen under the same PayPal-Request-Id."""
    return _call(fn, write=True, what=what)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LoggingTransport:
    """Logs method, path, status and PayPal's debug id. Never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        response = self._inner.send(request)
        logger.info('PayPal %s %s -> %s (%.0f ms, debug id %s)', request.method,
                    request.url.split('?', 1)[0], response.status_code,
                    (time.monotonic() - started) * 1000,
                    response.headers.get('paypal-debug-id', '-'))
        return response

    def close(self) -> None:
        self._inner.close()


def base_url() -> str:
    override = (settings.PAYPAL_BASE_URL or '').strip()
    if override:
        return override
    environment = (settings.PAYPAL_ENVIRONMENT or '').strip().lower()
    if not environment:
        raise ImproperlyConfigured('PAYPAL_ENVIRONMENT is not set.')
    try:
        return _KNOWN_ENVIRONMENTS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            f'PAYPAL_ENVIRONMENT={environment!r} has no known PayPal host; set PAYPAL_BASE_URL.'
        ) from None


def currency() -> str:
    value = (settings.PAYPAL_CURRENCY or '').strip().upper()
    if len(value) != 3 or not value.isalpha():
        raise ImproperlyConfigured('PAYPAL_CURRENCY must be a three-letter ISO 4217 code.')
    return value


def _build_client() -> PaypalClient:
    client_id = (settings.PAYPAL_CLIENT_ID or '').strip()
    client_secret = (settings.PAYPAL_CLIENT_SECRET or '').strip()
    if not client_id or not client_secret:
        raise ImproperlyConfigured('PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.')
    timeout = float(settings.PAYPAL_TIMEOUT_SECONDS)
    return PaypalClient(
        base_url=base_url(),
        custom_http_client=LoggingTransport(HttpxClient(timeout=timeout)),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


_client_lock = threading.Lock()
_client: PaypalClient | None = None


def get_client() -> PaypalClient:
    """The process-wide client, built on first use (after any worker fork)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _build_client()
    return _client


def use_client(client: PaypalClient | None) -> PaypalClient | None:
    """Replace the process-wide client (tests); returns the previous one."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    return previous


@atexit.register
def _close_client() -> None:
    client = use_client(None)
    if client is not None:
        client.close()


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------

@dataclass(frozen=True, repr=False)
class BillingAddress:
    country_code: str
    address_line_1: str = ''
    address_line_2: str = ''
    admin_area_2: str = ''
    admin_area_1: str = ''
    postal_code: str = ''


@dataclass(frozen=True, repr=False)
class CardInput:
    """Card details held only for the duration of one PayPal call."""

    number: str
    expiry: str
    security_code: str
    name: str = ''
    billing_address: BillingAddress | None = None

    def __repr__(self) -> str:
        return '<CardInput redacted>'


def _opt(value: str) -> str | UnsetType:
    return value if value else UNSET


def _address(address: BillingAddress | None) -> Address | UnsetType:
    if address is None:
        return UNSET
    return Address(
        country_code=address.country_code,
        address_line_1=_opt(address.address_line_1),
        address_line_2=_opt(address.address_line_2),
        admin_area_2=_opt(address.admin_area_2),
        admin_area_1=_opt(address.admin_area_1),
        postal_code=_opt(address.postal_code),
    )


def _safe_build(fn: Callable[[], T]) -> T:
    """Build a request model; on failure report field paths only, never values."""
    try:
        return fn()
    except ValidationError as e:
        fields = sorted({'.'.join(str(p) for p in err['loc']) for err in e.errors()})
        raise CardDataError(f"invalid card field(s): {', '.join(fields)}") from None


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def authorize_order(*, request_id: str, reference: str, invoice_id: str, order_number: str,
                    amount: str, currency_code: str, card: CardInput | None = None,
                    vault_id: str | None = None) -> Order:
    """Create a PayPal order with intent AUTHORIZE, paid by card or a vaulted card."""
    if (card is None) == (vault_id is None):
        raise ValueError('exactly one of card or vault_id is required')

    def build() -> OrderRequest:
        if card is not None:
            card_request = CardRequest(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=_opt(card.name),
                billing_address=_address(card.billing_address),
            )
        else:
            assert vault_id is not None
            card_request = CardRequest(vault_id=vault_id)
        return OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[PurchaseUnitRequest(
                reference_id=order_number,
                custom_id=reference,
                invoice_id=invoice_id,
                amount=AmountWithBreakdown(currency_code=currency_code, value=amount),
            )],
            payment_source=PaymentSource(card=card_request),
        )

    body = _safe_build(build)
    client = get_client()
    return _write(lambda: client.orders.create_order(
        body, pay_pal_request_id=request_id, prefer=_PREFER_FULL), what='create order')


def capture_authorization(*, authorization_id: str, request_id: str, amount: str,
                          currency_code: str, invoice_id: str) -> CapturedPayment:
    client = get_client()
    body = CaptureRequest(amount=Money(currency_code=currency_code, value=amount),
                          final_capture=True, invoice_id=invoice_id)
    return _write(lambda: client.payments.capture_authorized_payment(
        authorization_id, pay_pal_request_id=request_id, prefer=_PREFER_FULL, body=body),
        what='capture authorization')


def reauthorize(*, authorization_id: str, request_id: str, amount: str,
                currency_code: str) -> PaymentAuthorization:
    client = get_client()
    body = ReauthorizeRequest(amount=Money(currency_code=currency_code, value=amount))
    return _write(lambda: client.payments.reauthorize_payment(
        authorization_id, pay_pal_request_id=request_id, prefer=_PREFER_FULL, body=body),
        what='reauthorize')


def void_authorization(*, authorization_id: str, request_id: str) -> PaymentAuthorization:
    client = get_client()
    return _write(lambda: client.payments.void_payment(
        authorization_id, pay_pal_request_id=request_id, prefer=_PREFER_FULL),
        what='void authorization')


def refund_capture(*, capture_id: str, request_id: str, amount: str, currency_code: str) -> Refund:
    client = get_client()
    body = RefundRequest(amount=Money(currency_code=currency_code, value=amount))
    return _write(lambda: client.payments.refund_captured_payment(
        capture_id, pay_pal_request_id=request_id, prefer=_PREFER_FULL, body=body),
        what='refund capture')


def get_authorization(authorization_id: str) -> PaymentAuthorization:
    client = get_client()
    return _read(lambda: client.payments.get_authorized_payment(authorization_id),
                 what='get authorization')


def get_capture(capture_id: str) -> CapturedPayment:
    client = get_client()
    return _read(lambda: client.payments.get_captured_payment(capture_id), what='get capture')


def get_refund(refund_id: str) -> Refund:
    client = get_client()
    return _read(lambda: client.payments.get_refund(refund_id), what='get refund')


def vault_card(*, request_id: str, card: CardInput, customer_id: str | None) -> PaymentTokenResponse:
    def build() -> PaymentTokenRequest:
        return PaymentTokenRequest(
            customer=Customer(id=customer_id) if customer_id else UNSET,
            payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=_opt(card.name),
                billing_address=_address(card.billing_address),
            )),
        )

    body = _safe_build(build)
    client = get_client()
    return _write(lambda: client.vault.create_payment_token(body, pay_pal_request_id=request_id),
                  what='vault card')


def delete_vaulted_card(token_id: str) -> bool:
    """Delete a vault token. True when deleted, False when PayPal no longer has it."""
    client = get_client()
    result = _call(lambda: client.vault.with_raw_response.delete_payment_token(token_id),
                   write=True, what='delete vaulted card')
    match result:
        case Success():
            return True
        case Failure(error=error, response=response):
            if response.status_code == 404:
                return False
            raise _translate(response.status_code, error, write=True)
    raise AssertionError('unreachable')


def search_transactions(*, start: datetime, end: datetime, page: int, page_size: int) -> SearchResponse:
    client = get_client()
    return _read(lambda: client.transaction_search.search_transactions(
        _utc(start), _utc(end),
        fields='transaction_info',
        balance_affecting_records_only='N',
        page_size=page_size,
        page=page,
    ), what='search transactions')


# ---------------------------------------------------------------------------
# Status → outcome. Every member is named; anything else is 'unknown'.
# ---------------------------------------------------------------------------

def authorization_outcome(status: object) -> Literal['done', 'pending', 'failed', 'voided', 'captured', 'unknown']:
    match status:
        case AuthorizationStatus.CREATED:
            return 'done'
        case AuthorizationStatus.PENDING:
            return 'pending'
        case AuthorizationStatus.DENIED:
            return 'failed'
        case AuthorizationStatus.VOIDED:
            return 'voided'
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return 'captured'
        case _:
            return 'unknown'


def order_outcome(status: object) -> Literal['completed', 'payer_action', 'pending', 'failed', 'unknown']:
    match status:
        case OrderStatus.COMPLETED:
            return 'completed'
        case OrderStatus.PAYER_ACTION_REQUIRED:
            return 'payer_action'
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED:
            return 'pending'
        case OrderStatus.VOIDED:
            return 'failed'
        case _:
            return 'unknown'


def capture_outcome(status: object) -> Outcome:
    match status:
        case CaptureStatus.COMPLETED | CaptureStatus.PARTIALLY_REFUNDED | CaptureStatus.REFUNDED:
            return 'done'
        case CaptureStatus.PENDING:
            return 'pending'
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return 'failed'
        case _:
            return 'unknown'


def refund_outcome(status: object) -> Outcome:
    match status:
        case RefundStatus.COMPLETED:
            return 'done'
        case RefundStatus.PENDING:
            return 'pending'
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return 'failed'
        case _:
            return 'unknown'


def text(value: object) -> str:
    """A str member of a response model, or '' when PayPal left it out."""
    if isinstance(value, UnsetType) or value is None:
        return ''
    return str(value)


def parse_time(value: object) -> datetime | None:
    """An RFC 3339 timestamp from a response, or None when absent/unreadable."""
    raw = text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
