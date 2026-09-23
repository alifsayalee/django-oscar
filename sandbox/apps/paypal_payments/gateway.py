"""
The only module that talks to PayPal.

Everything above this layer works with the plain dataclasses defined here and
with ``ProviderError``; nothing outside this module imports the SDK. The
module deliberately has no Django imports so it type-checks under
``mypy --strict`` against the SDK's own annotations.

Card numbers and security codes pass through this module in memory only. They
are never logged, never stored and never included in an exception message.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
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
    UnsetType,
)
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CaptureRequest,
    CardRequest,
    Customer,
    Error,
    Money,
    OrderRequest,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    RefundRequest,
    TransactionInformation,
)
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CheckoutPaymentIntent,
    OrderStatus,
    RefundStatus,
)

logger = logging.getLogger("apps.paypal_payments.gateway")

T = TypeVar("T")

# The only server the SDK declares (sdk-map.md, "Servers & auth"). Any other
# environment must be given an explicit PAYPAL_BASE_URL.
ENVIRONMENT_BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com"}

# Return the full resource, not just id/status/links (the SDK's default).
REPRESENTATION = "return=representation"

# Transaction search accepts at most 31 days per call (search_transactions docstring).
SEARCH_MAX_RANGE = timedelta(days=31)
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_PAGES = 50

# Currencies whose minor unit is not two digits. Every other currency uses 2.
CURRENCY_EXPONENTS = {"JPY": 0, "KRW": 0, "HUF": 0, "TWD": 0, "KWD": 3, "BHD": 3, "TND": 3}


# --------------------------------------------------------------------------
# Configuration and client lifetime
# --------------------------------------------------------------------------


class ConfigurationError(Exception):
    """PayPal is not configured correctly. Never carries a secret value."""


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str
    client_secret: str = field(repr=False)
    environment: str
    currency: str
    base_url: str = ""
    timeout: float = 20.0

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        try:
            return ENVIRONMENT_BASE_URLS[self.environment.strip().lower()]
        except KeyError:
            raise ConfigurationError(
                "PAYPAL_ENVIRONMENT=%r has no known API host; set PAYPAL_BASE_URL "
                "to the PayPal API base address for that environment." % self.environment
            ) from None

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("PAYPAL_CLIENT_ID", self.client_id),
                ("PAYPAL_CLIENT_SECRET", self.client_secret),
                ("PAYPAL_ENVIRONMENT", self.environment),
                ("PAYPAL_CURRENCY", self.currency),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError("Missing PayPal settings: " + ", ".join(missing))
        self.resolved_base_url()


class LoggingTransport:
    """Logs method, path and status of every PayPal call. Never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except httpx.HTTPError as exc:
            logger.warning("PayPal %s %s -> %s", request.method, path, type(exc).__name__)
            raise
        debug_id = response.headers.get("paypal-debug-id", "")
        logger.info(
            "PayPal %s %s -> %s (%.0f ms) %s",
            request.method, path, response.status_code,
            (time.monotonic() - started) * 1000, debug_id,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(config: PayPalConfig, transport: HttpClient | None = None) -> PaypalClient:
    config.validate()
    return PaypalClient(
        base_url=config.resolved_base_url(),
        timeout=config.timeout,
        custom_http_client=LoggingTransport(transport or HttpxClient(timeout=config.timeout)),
        oauth2=ClientCredentials(client_id=config.client_id, client_secret=config.client_secret),
    )


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """
    A PayPal call did not produce a usable result.

    ``http_status`` is what our API should answer. ``outcome_unknown`` is True
    when the request may have taken effect at PayPal (timeout after sending, 5xx,
    unreadable response); False when it definitely did not.
    """

    def __init__(
        self,
        http_status: int,
        message: str,
        *,
        outcome_unknown: bool,
        provider_status: int | None = None,
        issues: tuple[str, ...] = (),
        debug_id: str = "",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.issues = issues
        self.debug_id = debug_id

    @property
    def rejected(self) -> bool:
        """PayPal answered and said no: nothing happened."""
        return not self.outcome_unknown and self.provider_status is not None


class ProviderUnreadable(ProviderError):
    """A 2xx whose body lacks something we depend on: the call may have succeeded."""

    def __init__(self, message: str) -> None:
        super().__init__(502, message, outcome_unknown=True)


NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)
CALLER_STATUSES = {400: 422, 404: 404, 409: 409, 422: 422}


def _error_detail(err: Error) -> tuple[str, tuple[str, ...]]:
    issues: list[str] = []
    descriptions: list[str] = []
    if not isinstance(err.details, UnsetType):
        for detail in err.details:
            issues.append(detail.issue)
            if not isinstance(detail.description, UnsetType):
                descriptions.append(detail.description)
    message = "; ".join(descriptions) or err.message
    return message, tuple(issues)


def _call(action: str, fn: Callable[[], T]) -> T:
    """Run one SDK call and translate every failure kind into ProviderError."""
    try:
        return fn()
    except ApiError as e:
        if isinstance(e.error, OAuthProviderError):
            logger.error("PayPal rejected our credentials during %s: %s", action, e.error.error)
            raise ProviderError(502, "Payment provider rejected our credentials.",
                                outcome_unknown=False) from e
        status = e.status_code
        if isinstance(e.error, Error):
            message, issues = _error_detail(e.error)
            debug_id = e.error.debug_id
        else:
            message, issues, debug_id = "", (), ""
        logger.warning("PayPal %s failed: HTTP %s %s %s", action, status, ",".join(issues), debug_id)
        if status in (401, 403):
            raise ProviderError(502, "Payment provider refused the request.", outcome_unknown=False,
                                provider_status=status, issues=issues, debug_id=debug_id) from e
        if status == 429:
            raise ProviderError(503, "Payment provider is rate limiting; try again shortly.",
                                outcome_unknown=False, provider_status=status, debug_id=debug_id) from e
        if status in CALLER_STATUSES and isinstance(e.error, Error):
            raise ProviderError(CALLER_STATUSES[status], message or "Payment provider rejected the request.",
                                outcome_unknown=False, provider_status=status, issues=issues,
                                debug_id=debug_id) from e
        raise ProviderError(502, "Payment provider error.", outcome_unknown=status >= 500,
                            provider_status=status, issues=issues, debug_id=debug_id) from e
    except ValidationError as e:
        # Never log str(e): pydantic embeds input values.
        logger.error("PayPal %s: unreadable response (%d validation errors)", action, e.error_count())
        raise ProviderError(502, "Unreadable response from payment provider; outcome unknown.",
                            outcome_unknown=True) from e
    except NEVER_SENT as e:
        logger.warning("PayPal %s: not sent (%s)", action, type(e).__name__)
        raise ProviderError(502, "Payment provider unreachable; nothing was sent.",
                            outcome_unknown=False) from e
    except httpx.RequestError as e:
        logger.warning("PayPal %s: no response (%s)", action, type(e).__name__)
        raise ProviderError(504, "No response from payment provider; outcome unknown.",
                            outcome_unknown=True) from e
    except ValueError as e:
        logger.error("PayPal %s: non-JSON response", action)
        raise ProviderError(502, "Unreadable response from payment provider; outcome unknown.",
                            outcome_unknown=True) from e


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------


def opt(value: T | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


def require(value: T | UnsetType | None, what: str) -> T:
    if value is None or isinstance(value, UnsetType):
        raise ProviderUnreadable("PayPal response had no %s; outcome unknown." % what)
    return value


def exponent(currency: str) -> int:
    return CURRENCY_EXPONENTS.get(currency.upper(), 2)


def quantize(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-exponent(currency)))


def format_amount(amount: Decimal, currency: str) -> str:
    return str(quantize(amount, currency))


def money(amount: Decimal, currency: str) -> Money:
    return Money(currency_code=currency, value=format_amount(amount, currency))


def parse_money(value: Money | UnsetType | None) -> tuple[Decimal, str] | None:
    if value is None or isinstance(value, UnsetType):
        return None
    try:
        return Decimal(value.value), value.currency_code
    except InvalidOperation:
        raise ProviderUnreadable("PayPal returned a malformed amount.") from None


def parse_time(value: str | UnsetType | None) -> datetime | None:
    if value is None or isinstance(value, UnsetType) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def status_text(value: object) -> str:
    """Wire value of an open enum (member or plain str)."""
    if isinstance(value, UnsetType) or value is None:
        return ""
    return str(value)


# Provider status -> our state. Unlisted values are "unknown": neither done nor failed.

def authorization_state(status: object) -> str:
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


def capture_state(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED | CaptureStatus.PARTIALLY_REFUNDED | CaptureStatus.REFUNDED:
            return "captured"
        case CaptureStatus.PENDING:
            return "pending"
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return "failed"
        case _:
            return "unknown"


def refund_state(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return "done"
        case RefundStatus.PENDING:
            return "pending"
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return "failed"
        case _:
            return "unknown"


# --------------------------------------------------------------------------
# Inputs and results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BillingAddress:
    country_code: str
    address_line_1: str = ""
    address_line_2: str = ""
    admin_area_2: str = ""
    admin_area_1: str = ""
    postal_code: str = ""

    def to_sdk(self) -> Address:
        return Address(
            country_code=self.country_code,
            address_line_1=self.address_line_1 or UNSET,
            address_line_2=self.address_line_2 or UNSET,
            admin_area_2=self.admin_area_2 or UNSET,
            admin_area_1=self.admin_area_1 or UNSET,
            postal_code=self.postal_code or UNSET,
        )


@dataclass(frozen=True)
class CardInput:
    """Full card details. Lives in memory for one request only."""

    number: str = field(repr=False)
    expiry: str  # YYYY-MM
    security_code: str = field(repr=False)
    name: str = ""
    billing_address: BillingAddress | None = None


@dataclass(frozen=True)
class AuthorizationResult:
    authorization_id: str
    state: str
    provider_status: str
    amount: Decimal
    currency: str
    created_at: datetime | None
    expires_at: datetime | None
    paypal_order_id: str = ""
    card_brand: str = ""


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    state: str
    provider_status: str
    amount: Decimal
    currency: str
    fee: Decimal | None
    net: Decimal | None
    captured_at: datetime | None


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    state: str
    provider_status: str
    amount: Decimal
    currency: str
    refunded_at: datetime | None


@dataclass(frozen=True)
class VaultedCard:
    token_id: str
    customer_id: str
    brand: str
    last_digits: str
    expiry: str
    name: str


@dataclass(frozen=True)
class ProviderTransaction:
    transaction_id: str
    event_code: str
    status: str
    initiated_at: datetime | None
    amount: Decimal | None
    fee: Decimal | None
    currency: str
    custom_id: str
    reference_id: str


@dataclass
class SearchResult:
    transactions: list[ProviderTransaction]
    truncated: bool
    last_refreshed: str


class PayerActionRequired(ProviderError):
    def __init__(self, paypal_order_id: str) -> None:
        super().__init__(
            409,
            "PayPal requires the shopper to approve this card payment in a browser; "
            "that flow is not supported by this API.",
            outcome_unknown=False,
        )
        self.paypal_order_id = paypal_order_id


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


class PayPalGateway:
    def __init__(self, client: PaypalClient) -> None:
        self._client = client

    # -- authorize -------------------------------------------------------

    def authorize(
        self,
        *,
        amount: Decimal,
        currency: str,
        custom_id: str,
        request_id: str,
        card: CardInput | None = None,
        vault_id: str | None = None,
    ) -> AuthorizationResult:
        """Single-step create order with intent AUTHORIZE and a card payment source."""
        if (card is None) == (vault_id is None):
            raise ValueError("exactly one of card or vault_id is required")
        if card is not None:
            card_request = CardRequest(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=card.name or UNSET,
                billing_address=card.billing_address.to_sdk() if card.billing_address else UNSET,
            )
        else:
            card_request = CardRequest(vault_id=vault_id or UNSET)
        body = OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[
                PurchaseUnitRequest(
                    amount=AmountWithBreakdown(currency_code=currency, value=format_amount(amount, currency)),
                    custom_id=custom_id,
                )
            ],
            payment_source=PaymentSource(card=card_request),
        )
        order = _call("create_order", lambda: self._client.orders.create_order(
            body, pay_pal_request_id=request_id, prefer=REPRESENTATION))
        paypal_order_id = require(order.id, "order id")
        if order.status == OrderStatus.PAYER_ACTION_REQUIRED:
            raise PayerActionRequired(paypal_order_id)
        units = opt(order.purchase_units) or []
        payments = opt(units[0].payments) if units else None
        authorizations = (opt(payments.authorizations) if payments else None) or []
        if not authorizations:
            raise ProviderUnreadable(
                "PayPal order %s (status %s) carried no authorization; outcome unknown."
                % (paypal_order_id, status_text(order.status)))
        auth = authorizations[-1]
        source = opt(order.payment_source)
        source_card = opt(source.card) if source else None
        brand = status_text(source_card.brand) if source_card else ""
        amount_seen = parse_money(auth.amount)
        if amount_seen is None:
            raise ProviderUnreadable("PayPal authorization had no amount; outcome unknown.")
        return AuthorizationResult(
            authorization_id=require(auth.id, "authorization id"),
            state=authorization_state(auth.status),
            provider_status=status_text(auth.status),
            amount=amount_seen[0],
            currency=amount_seen[1],
            created_at=parse_time(auth.create_time),
            expires_at=parse_time(auth.expiration_time),
            paypal_order_id=paypal_order_id,
            card_brand=brand,
        )

    def reauthorize(self, authorization_id: str, *, amount: Decimal, currency: str,
                    request_id: str) -> AuthorizationResult:
        result = _call("reauthorize_payment", lambda: self._client.payments.reauthorize_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION,
            body=ReauthorizeRequest(amount=money(amount, currency))))
        return self._authorization(result.id, result.status, result.amount,
                                   result.create_time, result.expiration_time)

    def void(self, authorization_id: str, *, request_id: str) -> AuthorizationResult:
        result = _call("void_payment", lambda: self._client.payments.void_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION))
        return self._authorization(result.id, result.status, result.amount,
                                   result.create_time, result.expiration_time)

    @staticmethod
    def _authorization(auth_id: str | UnsetType, status: object, amount: Money | UnsetType,
                       created: str | UnsetType, expires: str | UnsetType) -> AuthorizationResult:
        seen = parse_money(amount)
        return AuthorizationResult(
            authorization_id=require(auth_id, "authorization id"),
            state=authorization_state(status),
            provider_status=status_text(status),
            amount=seen[0] if seen else Decimal(0),
            currency=seen[1] if seen else "",
            created_at=parse_time(created),
            expires_at=parse_time(expires),
        )

    # -- capture / refund ------------------------------------------------

    def capture(self, authorization_id: str, *, amount: Decimal, currency: str,
                request_id: str) -> CaptureResult:
        result = _call("capture_authorized_payment", lambda: self._client.payments.capture_authorized_payment(
            authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION,
            body=CaptureRequest(amount=money(amount, currency), final_capture=True)))
        seen = parse_money(result.amount)
        if seen is None:
            raise ProviderUnreadable("PayPal capture had no amount; outcome unknown.")
        fee = net = None
        breakdown = opt(result.seller_receivable_breakdown)
        if breakdown is not None:
            fee_seen = parse_money(breakdown.paypal_fee)
            net_seen = parse_money(breakdown.net_amount)
            fee = fee_seen[0] if fee_seen else None
            net = net_seen[0] if net_seen else None
        return CaptureResult(
            capture_id=require(result.id, "capture id"),
            state=capture_state(result.status),
            provider_status=status_text(result.status),
            amount=seen[0],
            currency=seen[1],
            fee=fee,
            net=net,
            captured_at=parse_time(result.create_time),
        )

    def refund(self, capture_id: str, *, amount: Decimal, currency: str, request_id: str) -> RefundResult:
        result = _call("refund_captured_payment", lambda: self._client.payments.refund_captured_payment(
            capture_id, pay_pal_request_id=request_id, prefer=REPRESENTATION,
            body=RefundRequest(amount=money(amount, currency))))
        seen = parse_money(result.amount)
        if seen is None:
            raise ProviderUnreadable("PayPal refund had no amount; outcome unknown.")
        return RefundResult(
            refund_id=require(result.id, "refund id"),
            state=refund_state(result.status),
            provider_status=status_text(result.status),
            amount=seen[0],
            currency=seen[1],
            refunded_at=parse_time(result.create_time),
        )

    # -- vault -----------------------------------------------------------

    def vault_card(self, card: CardInput, *, customer_id: str | None, request_id: str) -> VaultedCard:
        body = PaymentTokenRequest(
            customer=Customer(id=customer_id) if customer_id else UNSET,
            payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=card.name or UNSET,
                billing_address=card.billing_address.to_sdk() if card.billing_address else UNSET,
            )),
        )
        result = _call("create_payment_token", lambda: self._client.vault.create_payment_token(
            body, pay_pal_request_id=request_id))
        token_id = require(result.id, "payment token id")
        customer = opt(result.customer)
        source = opt(result.payment_source)
        vaulted = opt(source.card) if source else None
        return VaultedCard(
            token_id=token_id,
            customer_id=(opt(customer.id) if customer else None) or customer_id or "",
            brand=status_text(vaulted.brand) if vaulted else "",
            last_digits=(opt(vaulted.last_digits) if vaulted else None) or card.number[-4:],
            expiry=(opt(vaulted.expiry) if vaulted else None) or card.expiry,
            name=(opt(vaulted.name) if vaulted else None) or card.name,
        )

    def delete_vaulted_card(self, token_id: str) -> None:
        """Delete a payment token. Already gone (404) counts as deleted."""
        result = _call("delete_payment_token",
                       lambda: self._client.vault.with_raw_response.delete_payment_token(token_id))
        if isinstance(result, Failure):
            if result.response.status_code == 404:
                return
            error = result.error
            if isinstance(error, RawError):
                raise ProviderError(502, "Payment provider error.",
                                    outcome_unknown=result.response.status_code >= 500,
                                    provider_status=result.response.status_code)
            raise ApiErrorWrapper.of(result.response.status_code, error)

    # -- reporting -------------------------------------------------------

    def search_transactions(self, start: datetime, end: datetime) -> SearchResult:
        """
        Every transaction PayPal reports in [start, end): split into ranges of at
        most 31 days, every page of each range, bounded by SEARCH_MAX_PAGES.
        """
        found: list[ProviderTransaction] = []
        truncated = False
        last_refreshed = ""
        for window_start, window_end in _windows(start, end):
            page = 1
            for _ in range(SEARCH_MAX_PAGES):
                response = _call("search_transactions", lambda: self._client.transaction_search.search_transactions(
                    _rfc3339(window_start), _rfc3339(window_end),
                    page_size=SEARCH_PAGE_SIZE, page=page))
                last_refreshed = opt(response.last_refreshed_datetime) or last_refreshed
                details = opt(response.transaction_details) or []
                for detail in details:
                    txn = _transaction(detail.transaction_info)
                    if txn is not None and txn.initiated_at is not None and start <= txn.initiated_at < end:
                        found.append(txn)
                total_pages = opt(response.total_pages) or 1
                if page >= total_pages or not details:
                    break
                page += 1
            else:
                truncated = True
                logger.warning("Transaction search truncated at %d pages for %s..%s",
                               SEARCH_MAX_PAGES, window_start, window_end)
        return SearchResult(transactions=found, truncated=truncated, last_refreshed=last_refreshed)


class ApiErrorWrapper:
    """Maps a typed error body seen through with_raw_response like the parsed path does."""

    @staticmethod
    def of(status: int, error: Error) -> ProviderError:
        message, issues = _error_detail(error)
        if status in (401, 403):
            return ProviderError(502, "Payment provider refused the request.", outcome_unknown=False,
                                 provider_status=status, issues=issues, debug_id=error.debug_id)
        if status in CALLER_STATUSES:
            return ProviderError(CALLER_STATUSES[status], message, outcome_unknown=False,
                                 provider_status=status, issues=issues, debug_id=error.debug_id)
        return ProviderError(502, "Payment provider error.", outcome_unknown=status >= 500,
                             provider_status=status, issues=issues, debug_id=error.debug_id)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _windows(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    # Whole seconds on the wire: widen outward, results are narrowed back by the caller's range.
    cursor = start.replace(microsecond=0)
    final = end if end.microsecond == 0 else end.replace(microsecond=0) + timedelta(seconds=1)
    while cursor < final:
        window_end = min(cursor + SEARCH_MAX_RANGE, final)
        yield cursor, window_end
        cursor = window_end


def _transaction(info: TransactionInformation | UnsetType) -> ProviderTransaction | None:
    if isinstance(info, UnsetType):
        return None
    txn_id = opt(info.transaction_id)
    if not txn_id:
        return None
    amount = parse_money(info.transaction_amount)
    fee = parse_money(info.fee_amount)
    return ProviderTransaction(
        transaction_id=txn_id,
        event_code=opt(info.transaction_event_code) or "",
        status=opt(info.transaction_status) or "",
        initiated_at=parse_time(info.transaction_initiation_date),
        amount=amount[0] if amount else None,
        fee=fee[0] if fee else None,
        currency=amount[1] if amount else "",
        custom_id=opt(info.custom_field) or "",
        reference_id=opt(info.paypal_reference_id) or "",
    )


# --------------------------------------------------------------------------
# Process-wide client
# --------------------------------------------------------------------------

_lock = threading.Lock()
_client: PaypalClient | None = None
_client_key: PayPalConfig | None = None


def get_gateway(config: PayPalConfig) -> PayPalGateway:
    """One long-lived client per process, built on first use (after any fork)."""
    global _client, _client_key
    with _lock:
        if _client is None or _client_key != config:
            if _client is not None:
                _client.close()
            _client = build_client(config)
            _client_key = config
        return PayPalGateway(_client)


def close_client() -> None:
    global _client, _client_key
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
        _client_key = None
