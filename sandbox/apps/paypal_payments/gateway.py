"""
The only module that talks to the PayPal Server SDK.

Everything PayPal-specific stops here: the client is built from Django
settings, every call goes through :func:`_call` so that each way a PayPal call
can fail is translated into one exception type (:class:`PaymentGatewayError`),
and every response member the rest of the app depends on is asserted
immediately after the call that returned it.

The SDK performs no retries and none are added here. Writes carry a
deterministic ``PayPal-Request-Id`` instead, so a caller who repeats a request
gets PayPal's original answer rather than a second hold, capture or refund.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient, ServerConfig
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
    TransactionInformation,
)
from paypal.models.enums import CheckoutPaymentIntent

from . import money

logger = logging.getLogger(__name__)

T = TypeVar('T')

# The SDK defaults to ``return=minimal``, which omits the fee breakdown and
# makes void answer with an empty body the SDK cannot decode.
REPRESENTATION = 'return=representation'

# PayPal's transaction search accepts at most 31 days per request.
SEARCH_WINDOW = timedelta(days=31)
# The largest page PayPal accepts (its 400 for 501 says "must be <= 500").
SEARCH_PAGE_SIZE = 500
SEARCH_MAX_PAGES = 1000

# Statuses whose PayPal rejection belongs to the caller (their card, their
# amount, their reference), as opposed to our credentials or PayPal itself.
_CALLER_STATUSES = {400: 422, 422: 422, 404: 409, 409: 409}
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class PaymentGatewayError(Exception):
    """
    A PayPal call that did not succeed.

    ``http_status`` is what this app's API answers with. ``outcome_unknown``
    is true when the request may have taken effect at PayPal (no reply, or a
    reply that could not be read): the caller must not treat it as a decline,
    and repeating the same request is safe because it replays the same
    PayPal-Request-Id.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        code: str,
        outcome_unknown: bool = False,
        paypal_status: int | None = None,
        issue: str | None = None,
        debug_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.paypal_status = paypal_status
        self.issue = issue
        self.debug_id = debug_id


# --------------------------------------------------------------------------
# Client construction
# --------------------------------------------------------------------------

_last_response = threading.local()


def _last_status() -> int | None:
    status: int | None = getattr(_last_response, 'status', None)
    return status


class _ObservedTransport:
    """
    Wraps the SDK's own transport to log each PayPal exchange and to remember
    the last status seen on this thread.

    Only method, path, status and latency are logged: headers carry the bearer
    token and bodies can carry card data.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        path = urlsplit(request.url).path
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except httpx.HTTPError as exc:
            logger.warning(
                'PayPal %s %s failed after %.0f ms: %s',
                request.method, path, (time.monotonic() - started) * 1000, type(exc).__name__)
            raise
        _last_response.status = response.status_code
        logger.info(
            'PayPal %s %s -> %s (%.0f ms)',
            request.method, path, response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def resolve_base_url() -> str:
    """
    ``PAYPAL_BASE_URL`` verbatim when set; otherwise the host for
    ``PAYPAL_ENVIRONMENT``. The SDK declares a single host, its sandbox, so any
    other environment must name its base URL explicitly.
    """
    override = getattr(settings, 'PAYPAL_BASE_URL', None)
    if override:
        return str(override)
    environment = (getattr(settings, 'PAYPAL_ENVIRONMENT', '') or '').strip().lower()
    if environment == 'sandbox':
        return ServerConfig().base_url
    if not environment:
        raise ImproperlyConfigured('PAYPAL_ENVIRONMENT is not set.')
    raise ImproperlyConfigured(
        "PAYPAL_ENVIRONMENT is %r; only 'sandbox' has a built-in API host. "
        'Set PAYPAL_BASE_URL to the API base URL for this environment.' % environment)


def build_client(transport: HttpClient | None = None) -> PaypalClient:
    """
    Build a client from settings. ``transport`` replaces the HTTP layer
    underneath the observing wrapper (tests pass a stub here).
    """
    client_id = getattr(settings, 'PAYPAL_CLIENT_ID', '') or ''
    client_secret = getattr(settings, 'PAYPAL_CLIENT_SECRET', '') or ''
    if not client_id or not client_secret:
        # Without credentials the SDK would silently send unauthenticated requests.
        raise ImproperlyConfigured('PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must both be set.')
    timeout = float(getattr(settings, 'PAYPAL_TIMEOUT', 20.0))
    return PaypalClient(
        base_url=resolve_base_url(),
        custom_http_client=_ObservedTransport(transport or HttpxClient(timeout=timeout)),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def get_client() -> PaypalClient:
    """
    The process-wide PayPal client, built on first use.

    Built lazily so that under a forking server each worker builds its own
    connection pool after the fork. Long-lived, so the OAuth token it caches
    is reused across requests.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def reset_client() -> None:
    """Close and forget the shared client (settings changed, or tests)."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None


# --------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------

def _describe(error: object) -> tuple[str | None, str | None, str | None]:
    """(name/issue, human message, debug_id) from a typed or raw PayPal error body."""
    if isinstance(error, Error):
        issue: str | None = error.name
        message: str | None = error.message
        if not isinstance(error.details, UnsetType) and error.details:
            detail = error.details[0]
            issue = detail.issue
            if not isinstance(detail.description, UnsetType):
                message = detail.description
        return issue, message, error.debug_id
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return None, None, None
        if not isinstance(body, dict):
            return None, None, None
        issue = body.get('name')
        message = body.get('message')
        details = body.get('details')
        if isinstance(details, list) and details and isinstance(details[0], dict):
            issue = details[0].get('issue', issue)
            message = details[0].get('description', message)
        debug_id = body.get('debug_id')
        return (
            str(issue) if issue else None,
            str(message) if message else None,
            str(debug_id) if debug_id else None,
        )
    return None, None, None


def _from_api_error(operation: str, exc: ApiError[Any]) -> PaymentGatewayError:
    if isinstance(exc.error, OAuthProviderError):
        logger.error('PayPal rejected our credentials (%s) during %s', exc.error.error, operation)
        return PaymentGatewayError(
            'The payment provider rejected the configured credentials.',
            http_status=502, code='payment_provider_misconfigured', paypal_status=exc.status_code)

    issue, message, debug_id = _describe(exc.error)
    status = exc.status_code
    logger.warning(
        'PayPal %s failed: HTTP %s issue=%s debug_id=%s', operation, status, issue, debug_id)
    if status in _CALLER_STATUSES:
        return PaymentGatewayError(
            message or 'PayPal rejected the request.',
            http_status=_CALLER_STATUSES[status], code='paypal_rejected',
            paypal_status=status, issue=issue, debug_id=debug_id)
    if status in (401, 403):
        return PaymentGatewayError(
            'The payment provider refused this merchant account the operation.',
            http_status=502, code='payment_provider_refused',
            paypal_status=status, issue=issue, debug_id=debug_id)
    if status == 429:
        return PaymentGatewayError(
            'The payment provider is rate limiting requests; try again shortly.',
            http_status=503, code='payment_provider_busy', paypal_status=status, debug_id=debug_id)
    return PaymentGatewayError(
        'The payment provider failed to process the request.',
        http_status=502, code='payment_provider_error',
        paypal_status=status, issue=issue, debug_id=debug_id)


def _call(operation: str, fn: Callable[[], T]) -> T:
    """Run one SDK call, translating every failure kind into PaymentGatewayError."""
    _last_response.status = None
    try:
        return fn()
    except ApiError as exc:
        raise _from_api_error(operation, exc) from exc
    except ValueError as exc:  # pydantic.ValidationError included
        status = _last_status()
        if status is None:
            raise  # nothing was received: a bug on our side, not a PayPal outcome
        if 200 <= status < 300:
            logger.error('PayPal %s succeeded (HTTP %s) but the reply was unreadable', operation, status)
            raise PaymentGatewayError(
                'PayPal accepted the request but its reply could not be read; '
                'the outcome is being treated as unknown.',
                http_status=502, code='payment_outcome_unknown',
                outcome_unknown=True, paypal_status=status) from exc
        logger.warning('PayPal %s failed: HTTP %s with an unreadable error body', operation, status)
        if status in _CALLER_STATUSES:
            return_status = _CALLER_STATUSES[status]
            code = 'paypal_rejected'
        else:
            return_status, code = 502, 'payment_provider_error'
        raise PaymentGatewayError(
            'PayPal rejected the request.', http_status=return_status, code=code,
            paypal_status=status) from exc
    except _NEVER_SENT as exc:
        logger.warning('PayPal %s was never sent: %s', operation, type(exc).__name__)
        raise PaymentGatewayError(
            'The payment provider could not be reached; nothing was charged.',
            http_status=502, code='payment_provider_unreachable') from exc
    except httpx.RequestError as exc:
        logger.error('PayPal %s got no reply: %s', operation, type(exc).__name__)
        raise PaymentGatewayError(
            'The payment provider did not answer in time; the outcome is unknown. '
            'Repeat the same request to resume it safely.',
            http_status=504, code='payment_outcome_unknown', outcome_unknown=True) from exc


# Reads only: a read that fails transiently changes nothing at PayPal, so it is
# retried. Writes are never retried here (see the module docstring).
READ_ATTEMPTS = 3
READ_BACKOFF_SECONDS = 1.0
_TRANSIENT_PAYPAL_STATUSES = {429, 500, 502, 503, 504}


def _call_read(operation: str, fn: Callable[[], T]) -> T:
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return _call(operation, fn)
        except PaymentGatewayError as exc:
            transient = (exc.paypal_status in _TRANSIENT_PAYPAL_STATUSES
                         or exc.code in ('payment_provider_unreachable', 'payment_outcome_unknown'))
            if not transient or attempt == READ_ATTEMPTS:
                raise
            delay = READ_BACKOFF_SECONDS * 2 ** (attempt - 1)
            logger.info('Retrying PayPal %s in %.0fs (attempt %d failed: %s)',
                        operation, delay, attempt, exc.code)
            time.sleep(delay)
    raise AssertionError('unreachable')


def _unreadable(operation: str, what: str) -> PaymentGatewayError:
    logger.error('PayPal %s returned no %s', operation, what)
    return PaymentGatewayError(
        'PayPal did not report %s; the outcome is being treated as unknown.' % what,
        http_status=502, code='payment_outcome_unknown', outcome_unknown=True)


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------

def value(member: T | UnsetType) -> T | None:
    """An SDK optional member as ``None`` when PayPal omitted it."""
    return None if isinstance(member, UnsetType) else member


def amount_of(member: Money | AmountWithBreakdown | UnsetType) -> Decimal | None:
    if isinstance(member, UnsetType):
        return None
    return Decimal(member.value)


def timestamp(member: str | UnsetType) -> datetime | None:
    """Parse one of PayPal's RFC 3339 timestamps."""
    if isinstance(member, UnsetType) or not member:
        return None
    return datetime.fromisoformat(member.replace('Z', '+00:00'))


def paypal_money(amount: Decimal, currency: str) -> Money:
    return Money(currency_code=currency, value=money.to_paypal(amount, currency))


def _address(fields: dict[str, str] | None) -> Address | UnsetType:
    return Address(**fields) if fields else UNSET


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CardDetails:
    """A card as the caller supplied it. Never persisted, never logged."""

    number: str
    expiry: str  # YYYY-MM
    security_code: str | None = None
    name: str | None = None
    billing_address: dict[str, str] | None = None

    def __repr__(self) -> str:
        return 'CardDetails(**redacted**)'


@dataclass(frozen=True)
class Authorization:
    paypal_order_id: str
    paypal_order_status: str
    authorization_id: str
    status: str
    amount: Decimal | None
    created_at: datetime | None
    expires_at: datetime | None
    card_brand: str | None = None
    card_last_digits: str | None = None


class PayPalGateway:
    """Typed, failure-translated wrappers over the SDK operations this app uses."""

    def __init__(self, client: PaypalClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> PaypalClient:
        return self._client if self._client is not None else get_client()

    # -- orders ------------------------------------------------------------

    def authorize(
        self,
        *,
        request_id: str,
        amount: Decimal,
        currency: str,
        custom_id: str,
        description: str,
        card: CardDetails | None = None,
        vault_id: str | None = None,
    ) -> Authorization:
        """
        Create a PayPal order with ``AUTHORIZE`` intent paying by card (or by a
        vaulted card), which places a hold for ``amount`` without taking it.
        """
        if card is not None and vault_id is None:
            card_request = CardRequest(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code or UNSET,
                name=card.name or UNSET,
                billing_address=_address(card.billing_address),
            )
        elif vault_id is not None and card is None:
            card_request = CardRequest(vault_id=vault_id)
        else:
            raise ValueError('pass exactly one of card or vault_id')
        body = OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[PurchaseUnitRequest(
                amount=AmountWithBreakdown(
                    currency_code=currency, value=money.to_paypal(amount, currency)),
                custom_id=custom_id,
                description=description[:127],
            )],
            payment_source=PaymentSource(card=card_request),
        )
        order = _call('create_order', lambda: self.client.orders.create_order(
            body, pay_pal_request_id=request_id, prefer=REPRESENTATION))
        return self._authorization_from_order('create_order', order)

    @staticmethod
    def _authorization_from_order(operation: str, order: Order) -> Authorization:
        order_id = value(order.id)
        status = value(order.status)
        if not order_id or status is None:
            raise _unreadable(operation, 'an order id and status')
        if str(status) == 'PAYER_ACTION_REQUIRED':
            # A browser challenge (e.g. 3-D Secure). This integration is
            # server-to-server only, so it is surfaced rather than worked around.
            raise PaymentGatewayError(
                'PayPal requires the cardholder to complete a verification step in a '
                'browser, which this integration does not support.',
                http_status=422, code='payer_action_required', issue='PAYER_ACTION_REQUIRED')
        units = value(order.purchase_units) or []
        payments = value(units[0].payments) if units else None
        authorizations = value(payments.authorizations) if payments is not None else None
        if not authorizations:
            raise PaymentGatewayError(
                'PayPal did not authorize the payment (order status %s).' % status,
                http_status=422, code='authorization_not_created', issue=str(status))
        auth = authorizations[0]
        auth_id = value(auth.id)
        auth_status = value(auth.status)
        if not auth_id or auth_status is None:
            raise _unreadable(operation, 'an authorization id and status')
        source = value(order.payment_source)
        card = value(source.card) if source is not None else None
        brand = value(card.brand) if card is not None else None
        return Authorization(
            card_brand=str(brand) if brand is not None else None,
            card_last_digits=value(card.last_digits) if card is not None else None,
            paypal_order_id=order_id,
            paypal_order_status=str(status),
            authorization_id=auth_id,
            status=str(auth_status),
            amount=amount_of(auth.amount),
            created_at=timestamp(auth.create_time),
            expires_at=timestamp(auth.expiration_time),
        )

    # -- payments ----------------------------------------------------------

    def get_authorization(self, authorization_id: str) -> PaymentAuthorization:
        auth = _call_read('get_authorized_payment',
                     lambda: self.client.payments.get_authorized_payment(authorization_id))
        if value(auth.status) is None:
            raise _unreadable('get_authorized_payment', 'an authorization status')
        return auth

    def reauthorize(
        self, authorization_id: str, *, request_id: str, amount: Decimal, currency: str,
    ) -> PaymentAuthorization:
        body = ReauthorizeRequest(amount=paypal_money(amount, currency))
        auth = _call('reauthorize_payment', lambda: self.client.payments.reauthorize_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION, body=body))
        if not value(auth.id) or value(auth.status) is None:
            raise _unreadable('reauthorize_payment', 'the renewed authorization id')
        return auth

    def capture(
        self, authorization_id: str, *, request_id: str, amount: Decimal, currency: str,
    ) -> CapturedPayment:
        body = CaptureRequest(amount=paypal_money(amount, currency), final_capture=True)
        capture = _call('capture_authorized_payment',
                        lambda: self.client.payments.capture_authorized_payment(
                            authorization_id, pay_pal_request_id=request_id,
                            prefer=REPRESENTATION, body=body))
        if not value(capture.id) or value(capture.status) is None:
            raise _unreadable('capture_authorized_payment', 'a capture id and status')
        return capture

    def get_capture(self, capture_id: str) -> CapturedPayment:
        capture = _call_read('get_captured_payment',
                        lambda: self.client.payments.get_captured_payment(capture_id))
        if value(capture.status) is None:
            raise _unreadable('get_captured_payment', 'a capture status')
        return capture

    def void(self, authorization_id: str, *, request_id: str) -> PaymentAuthorization:
        auth = _call('void_payment', lambda: self.client.payments.void_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION))
        if value(auth.status) is None:
            raise _unreadable('void_payment', 'the authorization status')
        return auth

    def refund(
        self, capture_id: str, *, request_id: str, amount: Decimal, currency: str,
    ) -> Refund:
        body = RefundRequest(amount=paypal_money(amount, currency))
        refund = _call('refund_captured_payment',
                       lambda: self.client.payments.refund_captured_payment(
                           capture_id, pay_pal_request_id=request_id,
                           prefer=REPRESENTATION, body=body))
        if not value(refund.id) or value(refund.status) is None:
            raise _unreadable('refund_captured_payment', 'a refund id and status')
        return refund

    # -- vault -------------------------------------------------------------

    def vault_card(
        self, card: CardDetails, *, request_id: str, customer_id: str | None,
    ) -> PaymentTokenResponse:
        """Save a card in PayPal's vault; PayPal keeps the card, we keep the token id."""
        card_request = PaymentTokenRequestCard(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code or UNSET,
            name=card.name or UNSET,
            billing_address=_address(card.billing_address),
        )
        body = PaymentTokenRequest(
            payment_source=PaymentTokenRequestPaymentSource(card=card_request),
            customer=Customer(id=customer_id) if customer_id else UNSET,
        )
        token = _call('create_payment_token', lambda: self.client.vault.create_payment_token(
            body, pay_pal_request_id=request_id))
        if not value(token.id):
            raise _unreadable('create_payment_token', 'a payment token id')
        return token

    def delete_vaulted_card(self, token_id: str) -> None:
        """Delete a vaulted card. A token PayPal no longer has counts as deleted."""
        try:
            _call('delete_payment_token',
                  lambda: self.client.vault.with_raw_response.delete_payment_token(token_id).unwrap())
        except PaymentGatewayError as exc:
            if exc.paypal_status == 404:
                logger.info('Vaulted card was already gone at PayPal')
                return
            raise

    # -- reporting ---------------------------------------------------------

    def search_transactions(self, start: datetime, end: datetime) -> Iterator[TransactionInformation]:
        """
        Every transaction PayPal reports between ``start`` and ``end``, across
        as many 31-day windows and pages as the range needs.
        """
        window_start = start
        while window_start < end:
            window_end = min(window_start + SEARCH_WINDOW, end)
            yield from self._search_window(window_start, window_end)
            window_start = window_end

    def _search_window(self, start: datetime, end: datetime) -> Iterator[TransactionInformation]:
        page = 1
        while True:
            current = page
            response = _call_read('search_transactions', lambda: self.client.transaction_search.search_transactions(
                _rfc3339(start), _rfc3339(end),
                fields='transaction_info',
                balance_affecting_records_only='N',
                page_size=SEARCH_PAGE_SIZE,
                page=current,
            ))
            for detail in value(response.transaction_details) or []:
                info = value(detail.transaction_info)
                if info is not None:
                    yield info
            total_pages = value(response.total_pages) or 1
            if page >= total_pages:
                return
            if page >= SEARCH_MAX_PAGES:
                raise PaymentGatewayError(
                    'PayPal reported more than %d pages for one 31-day window; '
                    'narrow the date range.' % SEARCH_MAX_PAGES,
                    http_status=422, code='range_too_large')
            page += 1
