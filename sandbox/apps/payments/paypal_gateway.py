"""The one place this app talks to the PayPal Server SDK.

Everything that knows an SDK name lives here: client construction, the logging transport, the error
ladder that turns SDK/transport failures into :class:`ProviderError`, the provider-status → outcome
maps, and the read calls (which are retried; writes never are — see ``safe_write``).
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

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
    Success,
    UnsetType,
)
from paypal.models import (
    Address,
    AmountWithBreakdown,
    AuthorizationWithAdditionalData,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Customer,
    Error,
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
    ReauthorizeRequest,
    Refund,
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

log = logging.getLogger(__name__)

# The only server the SDK declares (sdk-map.md, "Servers & auth"). Any other environment must name its
# host through PAYPAL_BASE_URL — no other URL is assumed.
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
_BASE_URLS = {"sandbox": SANDBOX_BASE_URL}

# Failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Transaction search: max range per request (docstring: 31 days — 30 used, leaving room for the
# inclusive end second) and how late records appear (docstring: up to 3 hours).
SEARCH_MAX_RANGE = timedelta(days=30)
REPORTING_LAG = timedelta(hours=3)

DONE, PENDING, FAILED, UNKNOWN = "done", "pending", "failed", "unknown"


# --------------------------------------------------------------------------------------------------
# Configuration and the client
# --------------------------------------------------------------------------------------------------


class ConfigurationError(RuntimeError):
    """The PayPal settings are missing or inconsistent."""


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str
    client_secret: str = field(repr=False)
    environment: str
    currency: str
    base_url: str
    timeout: float

    @classmethod
    def from_values(
        cls,
        *,
        client_id: str | None,
        client_secret: str | None,
        environment: str | None,
        currency: str | None,
        base_url: str | None,
        timeout: float,
    ) -> PayPalConfig:
        missing = [
            name
            for name, value in (
                ("PAYPAL_CLIENT_ID", client_id),
                ("PAYPAL_CLIENT_SECRET", client_secret),
                ("PAYPAL_ENVIRONMENT", environment),
                ("PAYPAL_CURRENCY", currency),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(f"PayPal is not configured: set {', '.join(missing)}")
        assert client_id and client_secret and environment and currency
        env = environment.strip().lower()
        if base_url:
            resolved = base_url  # used verbatim for every call, the token request included
        elif env in _BASE_URLS:
            resolved = _BASE_URLS[env]
        else:
            raise ConfigurationError(
                f"PAYPAL_ENVIRONMENT={environment!r} has no known host; set PAYPAL_BASE_URL for it"
            )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            environment=env,
            currency=currency.strip().upper(),
            base_url=resolved,
            timeout=timeout,
        )


class LoggingTransport:
    """Logs method, URL, status and latency of every PayPal call — never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except httpx.RequestError as e:
            log.warning("paypal %s %s -> %s", request.method, _redact_url(request.url), type(e).__name__)
            raise
        log.info(
            "paypal %s %s -> %s (%.0f ms, debug_id=%s)",
            request.method,
            _redact_url(request.url),
            response.status_code,
            (time.monotonic() - started) * 1000,
            response.headers.get("paypal-debug-id", "-"),
        )
        return response

    def close(self) -> None:
        self._inner.close()


def _redact_url(url: str) -> str:
    return url.split("?", 1)[0]


def build_client(config: PayPalConfig, transport: HttpClient | None = None) -> PaypalClient:
    inner = transport if transport is not None else HttpxClient(timeout=config.timeout)
    return PaypalClient(
        base_url=config.base_url,
        timeout=config.timeout,
        custom_http_client=LoggingTransport(inner),
        oauth2=ClientCredentials(client_id=config.client_id, client_secret=config.client_secret),
    )


_client_lock = threading.Lock()
_client: PaypalClient | None = None
_client_factory: Callable[[], PaypalClient] | None = None


def configure(factory: Callable[[], PaypalClient]) -> None:
    """Install the factory the lazy client is built from (called once from the Django layer)."""
    global _client_factory, _client
    with _client_lock:
        _client_factory = factory
        if _client is not None:
            _client.close()
        _client = None


def get_client() -> PaypalClient:
    """The process-wide client: built lazily on first use (after any fork), reused, closed at exit."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if _client_factory is None:
                    raise ConfigurationError("PayPal client factory not configured")
                _client = _client_factory()
    return _client


def use_client(client: PaypalClient | None) -> None:
    """Replace the live client (tests: a client over a stub transport)."""
    global _client
    with _client_lock:
        _client = client


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


# --------------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------------


class ProviderError(Exception):
    """A PayPal failure, already classified for the HTTP boundary."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        issues: list[str] | None = None,
        debug_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.issues = issues or []
        self.debug_id = debug_id


class ProviderConfigError(ProviderError):
    """PayPal refused OUR credentials or permissions — nothing the caller can fix."""


class ProviderRejected(ProviderError):
    """PayPal rejected the request (a 4xx the caller can act on). Nothing happened."""


class ProviderUnavailable(ProviderError):
    """Transport failure, 429 or 5xx. ``outcome_unknown`` says whether a write may have landed."""


class OutcomeUnknown(ProviderError):
    def __init__(self, ref: str) -> None:
        super().__init__(
            504,
            "outcome_unknown",
            "PayPal did not confirm the outcome. It may have been applied; it is recorded for "
            "reconciliation and repeating the same request will re-check it rather than repeat it.",
            outcome_unknown=True,
        )
        self.ref = ref


class AmountMismatch(ProviderError):
    def __init__(self, ref: str, expected: str, echoed: str) -> None:
        super().__init__(
            502,
            "amount_mismatch",
            f"PayPal applied {echoed} where {expected} was requested; flagged for review.",
        )
        self.ref = ref


def error_issues(e: ApiError[Any]) -> list[str]:
    body = e.error
    if isinstance(body, Error) and not isinstance(body.details, UnsetType):
        return [d.issue for d in body.details]
    return []


def error_debug_id(e: ApiError[Any]) -> str | None:
    return e.error.debug_id if isinstance(e.error, Error) else None


def translate(e: BaseException) -> ProviderError:
    """The single failure ladder. Order matters: auth first, never-sent before may-have-landed."""
    if isinstance(e, ProviderError):
        return e
    if isinstance(e, ApiError):
        issues = error_issues(e)
        debug_id = error_debug_id(e)
        if isinstance(e.error, OAuthProviderError) or e.status_code in (401, 403):
            return ProviderConfigError(
                502,
                "provider_auth",
                "PayPal refused this application's credentials or permissions.",
                issues=issues,
                debug_id=debug_id,
            )
        if e.status_code == 429:
            return ProviderUnavailable(503, "provider_rate_limited", "PayPal is rate limiting; retry later.")
        if 400 <= e.status_code < 500:
            message = e.error.message if isinstance(e.error, Error) else "PayPal rejected the request."
            return ProviderRejected(e.status_code, "provider_rejected", message, issues=issues, debug_id=debug_id)
        return ProviderUnavailable(
            502, "provider_error", "PayPal failed to process the request.", outcome_unknown=True, debug_id=debug_id
        )
    if isinstance(e, NEVER_SENT):
        return ProviderUnavailable(502, "provider_unreachable", "PayPal could not be reached; nothing was sent.")
    if isinstance(e, httpx.RequestError):
        return ProviderUnavailable(504, "provider_no_response", "PayPal did not answer.", outcome_unknown=True)
    if isinstance(e, ValidationError | ValueError):
        return ProviderUnavailable(
            502, "provider_unreadable", "PayPal's answer could not be read.", outcome_unknown=True
        )
    raise e


# --------------------------------------------------------------------------------------------------
# Status → outcome (the ONE place a provider status becomes ours; unlisted → unknown, never done)
# --------------------------------------------------------------------------------------------------


def authorization_outcome(status: object) -> str:
    """For writes that ask for a hold (create/authorize/reauthorize)."""
    match status:
        case AuthorizationStatus.CREATED | AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return FAILED
        case _:
            return UNKNOWN


def void_outcome(status: object) -> str:
    """For the void write, whose done is the hold being released."""
    match status:
        case AuthorizationStatus.VOIDED | AuthorizationStatus.DENIED:
            return DONE
        case AuthorizationStatus.CREATED | AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED
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
            return FAILED  # taken, then given back: no longer in effect
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


def order_outcome(status: object) -> str:
    """The checkout order's own status, before looking at its authorization."""
    match status:
        case OrderStatus.COMPLETED:
            return DONE
        case OrderStatus.APPROVED | OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.PAYER_ACTION_REQUIRED:
            return PENDING
        case OrderStatus.VOIDED:
            return FAILED
        case _:
            return UNKNOWN


# --------------------------------------------------------------------------------------------------
# Reading responses
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """What one write step's response says, read the same way for every step."""

    outcome: str
    provider_id: str
    provider_status: str
    provider_time: datetime | None
    amount: Decimal | None
    currency: str | None
    detail: dict[str, object] = field(default_factory=dict)


def value(v: object) -> str | None:
    """An SDK scalar/enum as a plain string, or None when UNSET."""
    if v is UNSET or v is None:
        return None
    return str(v)


def parse_time(v: object) -> datetime | None:
    s = value(v)
    if not s:
        return None
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def money(m: Money | UnsetType) -> tuple[Decimal | None, str | None]:
    if isinstance(m, UnsetType):
        return None, None
    try:
        return Decimal(m.value), m.currency_code
    except InvalidOperation as e:  # an unreadable answer, classified like any other decode failure
        raise ValueError(f"PayPal returned an unreadable amount {m.value!r}") from e


def first_authorization(result: Order | OrderAuthorizeResponse) -> AuthorizationWithAdditionalData | None:
    if isinstance(result.purchase_units, UnsetType) or not result.purchase_units:
        return None
    payments = result.purchase_units[0].payments
    if isinstance(payments, UnsetType) or isinstance(payments.authorizations, UnsetType):
        return None
    return payments.authorizations[0] if payments.authorizations else None


def read_authorization(
    auth: PaymentAuthorization | AuthorizationWithAdditionalData, outcome_map: Callable[[object], str]
) -> Answer:
    amount, currency = money(auth.amount)
    provider_id = value(auth.id)
    outcome = outcome_map(auth.status) if provider_id else UNKNOWN
    reason = None if isinstance(auth.status_details, UnsetType) else value(auth.status_details.reason)
    return Answer(
        outcome=outcome,
        provider_id=provider_id or "",
        provider_status=value(auth.status) or "",
        provider_time=parse_time(auth.create_time),
        amount=amount,
        currency=currency,
        detail={"expiration_time": value(auth.expiration_time), "status_reason": reason},
    )


def read_checkout_order(result: Order | OrderAuthorizeResponse | PaymentAuthorization) -> Answer:
    """A create/authorize step's answer: the order's status first, then its authorization's."""
    if isinstance(result, PaymentAuthorization):  # reached through the lookup, not the create call
        return read_authorization(result, authorization_outcome)
    order_id = value(result.id) or ""
    order_status = value(result.status) or ""
    card: dict[str, object] = {}
    source = result.payment_source
    if not isinstance(source, UnsetType) and not isinstance(source.card, UnsetType):
        card = {"last_digits": value(source.card.last_digits), "brand": value(source.card.brand)}
    base: dict[str, object] = {"paypal_order_id": order_id, "paypal_order_status": order_status, "card": card}
    auth = first_authorization(result)
    if order_outcome(result.status) == DONE and auth is not None:
        answer = read_authorization(auth, authorization_outcome)
        return replace(answer, detail={**answer.detail, **base})
    outcome = order_outcome(result.status)
    if outcome == DONE:  # COMPLETED but no authorization to read: cannot tell what landed
        outcome = UNKNOWN
    base["payer_action_required"] = result.status == OrderStatus.PAYER_ACTION_REQUIRED
    return Answer(
        outcome=outcome if order_id else UNKNOWN,
        provider_id=order_id,
        provider_status=order_status,
        provider_time=parse_time(result.create_time),
        amount=None,
        currency=None,
        detail=base,
    )


def read_capture(capture: CapturedPayment) -> Answer:
    amount, currency = money(capture.amount)
    provider_id = value(capture.id)
    detail: dict[str, object] = {}
    breakdown = capture.seller_receivable_breakdown
    if not isinstance(breakdown, UnsetType):
        detail["gross"] = breakdown.gross_amount.value
        fee, _ = money(breakdown.paypal_fee)
        net, _ = money(breakdown.net_amount)
        detail["fee"] = None if fee is None else str(fee)
        detail["net"] = None if net is None else str(net)
    return Answer(
        outcome=capture_outcome(capture.status) if provider_id else UNKNOWN,
        provider_id=provider_id or "",
        provider_status=value(capture.status) or "",
        provider_time=parse_time(capture.create_time),
        amount=amount,
        currency=currency,
        detail=detail,
    )


def read_refund(refund: Refund) -> Answer:
    amount, currency = money(refund.amount)
    provider_id = value(refund.id)
    return Answer(
        outcome=refund_outcome(refund.status) if provider_id else UNKNOWN,
        provider_id=provider_id or "",
        provider_status=value(refund.status) or "",
        provider_time=parse_time(refund.create_time),
        amount=amount,
        currency=currency,
    )


# --------------------------------------------------------------------------------------------------
# Reads (retried: they are idempotent)
# --------------------------------------------------------------------------------------------------

T = TypeVar("T")


def _is_transient(e: BaseException) -> bool:
    if isinstance(e, httpx.RequestError):
        return True
    return isinstance(e, ApiError) and e.status_code in (429, 500, 502, 503, 504)


def read_with_retry(call: Callable[[], T], *, attempts: int = 3, backoff: float = 0.5) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except (ApiError, httpx.RequestError) as e:
            if attempt == attempts or not _is_transient(e):
                raise
            time.sleep(backoff * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _data_not_available_yet(e: ApiError[Any]) -> bool:
    """PayPal answers 404 "Data for the given start date is not available" for a window that starts past its
    reporting horizon (observed in sandbox) — that means "no records yet", not a failure."""
    if e.status_code != 404:
        return False
    try:
        body = e.response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "not available" in str(body.get("message", "")).lower()


def search_transactions(
    start: datetime, end: datetime, *, page_size: int = 100, meta: dict[str, object] | None = None
) -> Iterator[TransactionInformation]:
    """Every PayPal transaction record initiated in [start, end): all windows, all pages, narrowed back.

    ``meta`` (optional) receives ``last_refreshed`` (PayPal's reporting horizon) and, when part of the range
    lies beyond it, ``unavailable_from``.
    """
    client = get_client()
    meta = meta if meta is not None else {}
    seen: set[tuple[str | None, str | None, str | None]] = set()
    window_start = start
    while window_start < end:
        window_end = min(end, window_start + SEARCH_MAX_RANGE)
        page = 1
        while True:
            try:
                response = read_with_retry(
                    lambda: client.transaction_search.search_transactions(
                        rfc3339(window_start),
                        rfc3339(window_end),
                        fields="transaction_info",
                        balance_affecting_records_only="N",
                        page_size=page_size,
                        page=page,
                    )
                )
            except ApiError as e:
                if not _data_not_available_yet(e):
                    raise
                meta["unavailable_from"] = rfc3339(window_start)  # later windows are later still
                return
            if not isinstance(response.last_refreshed_datetime, UnsetType):
                meta["last_refreshed"] = response.last_refreshed_datetime
            details = [] if isinstance(response.transaction_details, UnsetType) else response.transaction_details
            for detail in details:
                info = detail.transaction_info
                if isinstance(info, UnsetType):
                    continue
                key = (
                    value(info.transaction_id),
                    value(info.transaction_event_code),
                    value(info.transaction_initiation_date),
                )
                initiated = parse_time(info.transaction_initiation_date)
                if key in seen or initiated is None or not start <= initiated < end:
                    continue  # a window boundary repeat, or outside the caller's instants
                seen.add(key)
                yield info
            total_pages = response.total_pages if isinstance(response.total_pages, int) else page
            if page >= total_pages or not details:
                break
            page += 1
        window_start = window_end


# --------------------------------------------------------------------------------------------------
# Request builders — card data only ever lives in these request models, never in the database or logs
# --------------------------------------------------------------------------------------------------


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
            address_line_1=self.address_line_1 or UNSET,
            address_line_2=self.address_line_2 or UNSET,
            admin_area_2=self.admin_area_2 or UNSET,
            admin_area_1=self.admin_area_1 or UNSET,
            postal_code=self.postal_code or UNSET,
        )


@dataclass(frozen=True)
class CardInput:
    """A card as the caller typed it. Held in memory for one request; repr never shows the number."""

    number: str = field(repr=False)
    expiry: str  # YYYY-MM
    security_code: str = field(repr=False)
    name: str | None = None
    billing_address: BillingAddress | None = None

    @property
    def last_digits(self) -> str:
        return self.number[-4:]


def card_source(card: CardInput | None = None, vault_id: str | None = None) -> PaymentSource:
    if vault_id:
        return PaymentSource(card=CardRequest(vault_id=vault_id))
    assert card is not None
    return PaymentSource(
        card=CardRequest(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            name=card.name or UNSET,
            billing_address=card.billing_address.to_model() if card.billing_address else UNSET,
        )
    )


def checkout_order_request(
    *,
    amount: str,
    currency: str,
    invoice_id: str,
    custom_id: str,
    reference_id: str,
    description: str,
    source: PaymentSource,
) -> OrderRequest:
    return OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=reference_id,
                invoice_id=invoice_id,
                custom_id=custom_id,
                description=description[:127],
                amount=AmountWithBreakdown(currency_code=currency, value=amount),
            )
        ],
        payment_source=source,
    )


def payment_token_request(card: CardInput, customer_id: str | None) -> PaymentTokenRequest:
    return PaymentTokenRequest(
        customer=Customer(id=customer_id) if customer_id else UNSET,
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=card.name or UNSET,
                billing_address=card.billing_address.to_model() if card.billing_address else UNSET,
            )
        ),
    )


# --------------------------------------------------------------------------------------------------
# Write sends (called only from safe_write, with the key it derived) and the lookups behind them
# --------------------------------------------------------------------------------------------------

REPRESENTATION = "return=representation"


def send_create_order(body: OrderRequest, key: str) -> Order:
    return get_client().orders.create_order(body, pay_pal_request_id=key, prefer=REPRESENTATION)


def send_authorize_order(paypal_order_id: str, key: str) -> OrderAuthorizeResponse:
    return get_client().orders.authorize_order(paypal_order_id, pay_pal_request_id=key, prefer=REPRESENTATION)


def get_checkout_order(paypal_order_id: str) -> Order:
    return read_with_retry(lambda: get_client().orders.get_order(paypal_order_id))


def send_reauthorize(authorization_id: str, amount: str, currency: str, key: str) -> PaymentAuthorization:
    return get_client().payments.reauthorize_payment(
        authorization_id,
        pay_pal_request_id=key,
        prefer=REPRESENTATION,
        body=ReauthorizeRequest(amount=Money(currency_code=currency, value=amount)),
    )


def send_capture(authorization_id: str, amount: str, currency: str, key: str) -> CapturedPayment:
    return get_client().payments.capture_authorized_payment(
        authorization_id,
        pay_pal_request_id=key,
        prefer=REPRESENTATION,
        body=CaptureRequest(amount=Money(currency_code=currency, value=amount), final_capture=True),
    )


def send_void(authorization_id: str, key: str) -> PaymentAuthorization:
    # return=representation is required: with return=minimal sandbox answers an empty 2xx body.
    return get_client().payments.void_payment(authorization_id, pay_pal_request_id=key, prefer=REPRESENTATION)


def get_authorization(authorization_id: str) -> PaymentAuthorization:
    return read_with_retry(lambda: get_client().payments.get_authorized_payment(authorization_id))


def get_capture(capture_id: str) -> CapturedPayment:
    return read_with_retry(lambda: get_client().payments.get_captured_payment(capture_id))


def send_refund(capture_id: str, amount: str, currency: str, note: str | None, key: str) -> Refund:
    return get_client().payments.refund_captured_payment(
        capture_id,
        pay_pal_request_id=key,
        prefer=REPRESENTATION,
        body=RefundRequest(amount=Money(currency_code=currency, value=amount), note_to_payer=note or UNSET),
    )


def get_refund(refund_id: str) -> Refund:
    return read_with_retry(lambda: get_client().payments.get_refund(refund_id))


def send_create_payment_token(body: PaymentTokenRequest, key: str) -> PaymentTokenResponse:
    return get_client().vault.create_payment_token(body, pay_pal_request_id=key)


def delete_payment_token(token_id: str) -> str:
    """Delete a vaulted card. Returns DONE, or raises ProviderError (the op returns None: raw peer)."""
    try:
        result = get_client().vault.with_raw_response.delete_payment_token(token_id)
    except (ApiError, httpx.RequestError, ValueError) as e:
        raise translate(e) from e
    match result:
        case Success():
            return DONE  # 204; a repeat delete also answers 204
        case Failure(error=error, response=response):
            if response.status_code == 404:
                return DONE  # already gone
            raise translate(ApiError(error=error, response=response))
    raise AssertionError("unreachable")


def has_issue(*codes: str) -> Callable[[ApiError[Any]], bool]:
    def check(e: ApiError[Any]) -> bool:
        return any(issue in codes for issue in error_issues(e))

    return check


def find_authorization_by_invoice(invoice_id: str, since: datetime) -> PaymentAuthorization | None:
    """Kind-3 lookup for a card order whose create call went unanswered: the reporting API by invoice id.

    Reporting lags live activity by up to three hours, so "not found" means "not found yet".
    """
    end = datetime.now(timezone.utc)
    meta: dict[str, object] = {}
    for info in search_transactions(since - timedelta(hours=1), end, meta=meta):
        if value(info.invoice_id) != invoice_id:
            continue
        for candidate in (value(info.transaction_id), value(info.paypal_reference_id)):
            if not candidate:
                continue
            try:
                return get_authorization(candidate)
            except ApiError as e:
                if e.status_code != 404:
                    raise
    # "Not found" is only an answer when PayPal's reporting demonstrably covers the attempt.
    refreshed = parse_time(meta.get("last_refreshed"))
    if "unavailable_from" in meta or refreshed is None or refreshed < since + timedelta(minutes=10):
        raise ValueError("PayPal's reporting does not cover this attempt yet")
    return None


def read_payment_token(token: PaymentTokenResponse) -> Answer:
    """The vault resource has no status member: done ⇔ it names the token AND the card it holds."""
    token_id = value(token.id)
    card = None if isinstance(token.payment_source, UnsetType) else token.payment_source.card
    last_digits = None if card is None or isinstance(card, UnsetType) else value(card.last_digits)
    customer_id = None if isinstance(token.customer, UnsetType) else value(token.customer.id)
    detail: dict[str, object] = {"customer_id": customer_id}
    if card is not None and not isinstance(card, UnsetType):
        detail.update(
            last_digits=last_digits, brand=value(card.brand), expiry=value(card.expiry), name=value(card.name)
        )
    return Answer(
        outcome=DONE if token_id and last_digits else UNKNOWN,
        provider_id=token_id or "",
        provider_status="VAULTED" if token_id and last_digits else "",
        provider_time=None,
        amount=None,
        currency=None,
        detail=detail,
    )
