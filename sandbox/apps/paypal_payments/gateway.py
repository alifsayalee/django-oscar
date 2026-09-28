"""
Everything that talks to PayPal, through the PayPal Server SDK (``paypal``).

The rest of the app never imports the SDK: it calls the functions here, which
build requests, read the parts of each response the app depends on, and map
each step's status onto one of ``done`` / ``pending`` / ``failed`` /
``unknown``.

The SDK performs no retries and neither does this module. A write whose
outcome is unknown is re-checked by a later request under the same
``PayPal-Request-Id`` (see ``safe_write``).
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
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
    CardVerificationStatus,
    CheckoutPaymentIntent,
    OrderStatus,
    RefundStatus,
)

log = logging.getLogger('apps.paypal_payments')

# The only host the SDK declares. Any other environment needs PAYPAL_BASE_URL.
BASE_URLS = {'sandbox': 'https://api-m.sandbox.paypal.com'}

# Outcomes of one provider write step
DONE, PENDING, FAILED, UNKNOWN = 'done', 'pending', 'failed', 'unknown'

# Failures raised before the request left: nothing can have happened at PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """A PayPal failure, already translated for our API's caller."""

    def __init__(self, status_code: int, code: str, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown


def error_issue(error: object) -> str:
    """PayPal's issue codes and descriptions from a typed error body, for operators."""
    if not isinstance(error, Error):
        return ''
    parts = []
    if not isinstance(error.details, UnsetType):
        for d in error.details:
            description = d.description if not isinstance(d.description, UnsetType) else ''
            parts.append('%s: %s' % (d.issue, description) if description else d.issue)
    return '; '.join(parts) or error.message


def error_issues(error: object) -> list[str]:
    if isinstance(error, Error) and not isinstance(error.details, UnsetType):
        return [d.issue for d in error.details]
    return []


def provider_error(status: int, error: object) -> ProviderError:
    """The one map from a PayPal error response to our caller's error."""
    if isinstance(error, OAuthProviderError) or status in (401, 403):
        return ProviderError(502, 'paypal_credentials_rejected', 'PayPal refused this shop\'s credentials.')
    if status == 429:
        return ProviderError(503, 'paypal_rate_limited', 'PayPal is rate-limiting this shop; try again shortly.')
    if 400 <= status < 500:
        issue = error_issue(error)
        return ProviderError(422 if status in (400, 422) else 409, 'paypal_rejected',
                             'PayPal rejected the request%s' % (': %s' % issue if issue else '.'))
    return ProviderError(502, 'paypal_unavailable', 'PayPal is unavailable.', outcome_unknown=status >= 500)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LoggingTransport:
    """Logs method, path, status and duration of each PayPal call - never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = httpx.URL(request.url).path
        try:
            response = self._inner.send(request)
        except httpx.RequestError as e:
            log.warning('PayPal %s %s -> %s', request.method, path, type(e).__name__)
            raise
        log.info('PayPal %s %s -> %s (%.0f ms)', request.method, path, response.status_code,
                 (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def base_url() -> str:
    override = getattr(settings, 'PAYPAL_BASE_URL', '')
    if override:
        return str(override)
    environment = getattr(settings, 'PAYPAL_ENVIRONMENT', '')
    try:
        return BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            'PAYPAL_ENVIRONMENT=%r has no known PayPal host; set PAYPAL_BASE_URL.' % environment) from None


def currency() -> str:
    code = getattr(settings, 'PAYPAL_CURRENCY', '')
    if not code:
        raise ImproperlyConfigured('PAYPAL_CURRENCY is not set.')
    return str(code).upper()


def build_client(transport: HttpClient | None = None) -> PaypalClient:
    client_id = getattr(settings, 'PAYPAL_CLIENT_ID', '')
    client_secret = getattr(settings, 'PAYPAL_CLIENT_SECRET', '')
    if not client_id or not client_secret:
        raise ImproperlyConfigured('PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.')
    timeout = float(getattr(settings, 'PAYPAL_TIMEOUT_SECONDS', 20.0))
    return PaypalClient(
        base_url=base_url(),
        timeout=timeout,
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=timeout)),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def get_client() -> PaypalClient:
    """The process-wide client, built on first use (after any worker fork)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Swap the process-wide client (tests inject one with a stub transport)."""
    global _client
    with _client_lock:
        _client = client


# ---------------------------------------------------------------------------
# References and money
# ---------------------------------------------------------------------------

def reference_prefix() -> str:
    configured = getattr(settings, 'PAYPAL_REFERENCE_PREFIX', '')
    if configured:
        return str(configured)
    from .models import InstallIdentity
    identity = InstallIdentity.objects.filter(key='default').first()
    if identity is None:
        try:
            with transaction.atomic():
                identity = InstallIdentity.objects.create(key='default', prefix='osc' + uuid.uuid4().hex[:8])
        except IntegrityError:
            identity = InstallIdentity.objects.get(key='default')
    return identity.prefix


def deterministic_ref(*parts: object) -> str:
    """A reference derived from the operation: the same on every attempt and every repeat."""
    return '-'.join([reference_prefix()] + [str(p) for p in parts])


# ISO 4217 minor units for currencies that do not have two.
EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0, 'PYG': 0,
    'RWF': 0, 'UGX': 0, 'UYI': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0, 'HUF': 2,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
    'CLF': 4, 'UYW': 4,
}


def quantum(code: str) -> Decimal:
    return Decimal(1).scaleb(-EXPONENT.get(code, 2))


def format_amount(value: Decimal, code: str) -> str:
    return str(value.quantize(quantum(code)))


def money(value: Decimal, code: str) -> Money:
    return Money(currency_code=code, value=format_amount(value, code))


def parse_money(m: Money | UnsetType) -> tuple[Decimal | None, str | None]:
    if isinstance(m, UnsetType):
        return None, None
    try:
        return Decimal(m.value), m.currency_code
    except InvalidOperation:
        return None, m.currency_code


def parse_time(value: str | UnsetType) -> datetime | None:
    if isinstance(value, UnsetType) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def _str(value: Any) -> str:
    return '' if isinstance(value, UnsetType) or value is None else str(value)


# ---------------------------------------------------------------------------
# Status -> outcome, one mapper per step
# ---------------------------------------------------------------------------

def authorization_outcome(status: object) -> str:
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return FAILED  # refused, or held and then released: no hold in effect
        case _:
            # CAPTURED / PARTIALLY_CAPTURED are not a fresh hold; unlisted or absent is unknown
            return UNKNOWN


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED | CaptureStatus.PARTIALLY_REFUNDED:
            return DONE  # the capture is in effect (a partial refund does not undo it)
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED | CaptureStatus.REFUNDED:
            return FAILED
        case _:
            return UNKNOWN


def void_outcome(status: object) -> str:
    """The call-off's own mapper: the released state is its done."""
    match status:
        case AuthorizationStatus.VOIDED | AuthorizationStatus.DENIED:
            return DONE  # nothing is held
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED  # too late: the money was taken
        case _:
            return UNKNOWN  # CREATED / PENDING / unlisted: still held as far as we know


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


# ---------------------------------------------------------------------------
# What one step's response says
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    provider_id: str
    status: object  # the step's own status member; the step's mapper turns it into an outcome
    provider_time: datetime | None
    amount: Decimal | None = None
    currency: str | None = None
    detail: str = ''


@dataclass(frozen=True)
class CardInput:
    """A one-off card, held in memory only for the duration of the PayPal call."""

    number: str
    expiry: str  # YYYY-MM
    security_code: str
    name: str
    billing_address: Address | None = None

    def for_order(self) -> CardRequest:
        if self.billing_address is None:
            return CardRequest(number=self.number, expiry=self.expiry, security_code=self.security_code,
                               name=self.name)
        return CardRequest(number=self.number, expiry=self.expiry, security_code=self.security_code,
                           name=self.name, billing_address=self.billing_address)

    def for_vault(self) -> PaymentTokenRequestCard:
        if self.billing_address is None:
            return PaymentTokenRequestCard(number=self.number, expiry=self.expiry,
                                           security_code=self.security_code, name=self.name)
        return PaymentTokenRequestCard(number=self.number, expiry=self.expiry, security_code=self.security_code,
                                       name=self.name, billing_address=self.billing_address)

    def __repr__(self) -> str:  # keep card data out of logs and tracebacks
        return 'CardInput(****%s)' % self.number[-4:]


PAYER_ACTION_REQUIRED = 'PAYER_ACTION_REQUIRED'


def read_authorize(order: Order) -> Answer:
    """The single-step create-order response, read as the authorization it made."""
    if order.status == OrderStatus.PAYER_ACTION_REQUIRED:
        return Answer(_str(order.id), PAYER_ACTION_REQUIRED, None,
                      detail='The card issuer requires the shopper to approve this payment in a browser '
                             '(3-D Secure), which this API does not support.')
    auth = first_authorization(order)
    if auth is None:
        return Answer(_str(order.id), UNSET, None, detail='PayPal returned no authorization for the order.')
    amount, code = parse_money(auth.amount)
    detail = ''
    if not isinstance(auth.status_details, UnsetType) and not isinstance(auth.status_details.reason, UnsetType):
        detail = 'PayPal reason: %s' % auth.status_details.reason
    return Answer(_str(auth.id), auth.status, parse_time(auth.create_time), amount, code, detail)


def authorize_outcome(status: object) -> str:
    if status == PAYER_ACTION_REQUIRED:
        return FAILED
    return authorization_outcome(status)


def first_authorization(order: Order) -> Any:
    if isinstance(order.purchase_units, UnsetType):
        return None
    for unit in order.purchase_units:
        if isinstance(unit.payments, UnsetType) or isinstance(unit.payments.authorizations, UnsetType):
            continue
        if unit.payments.authorizations:
            return unit.payments.authorizations[0]
    return None


def read_authorization(auth: PaymentAuthorization) -> Answer:
    amount, code = parse_money(auth.amount)
    return Answer(_str(auth.id), auth.status, parse_time(auth.create_time), amount, code)


def read_capture(capture: CapturedPayment) -> Answer:
    amount, code = parse_money(capture.amount)
    return Answer(_str(capture.id), capture.status, parse_time(capture.create_time), amount, code)


def read_refund(refund: Refund) -> Answer:
    amount, code = parse_money(refund.amount)
    return Answer(_str(refund.id), refund.status, parse_time(refund.create_time), amount, code)


def capture_breakdown(capture: CapturedPayment) -> dict[str, Decimal | None]:
    """What PayPal reported for the capture: gross, PayPal's fee, and the merchant's net."""
    b = capture.seller_receivable_breakdown
    if isinstance(b, UnsetType):
        return {'gross': parse_money(capture.amount)[0], 'fee': None, 'net': None}
    return {'gross': parse_money(b.gross_amount)[0], 'fee': parse_money(b.paypal_fee)[0],
            'net': parse_money(b.net_amount)[0]}


def authorization_expiry(order_or_auth: Any) -> datetime | None:
    return parse_time(order_or_auth.expiration_time)


# ---------------------------------------------------------------------------
# Writes. Each takes the reference and sends it as PayPal-Request-Id.
# ---------------------------------------------------------------------------

REPRESENTATION = 'return=representation'


def authorize(ref: str, *, amount: Decimal, code: str, order_number: str, invoice_id: str,
              card: CardInput | None = None, vault_id: str | None = None) -> Order:
    """Single-step create order: intent AUTHORIZE with the card as payment source."""
    if vault_id:
        source = CardRequest(vault_id=vault_id)
    elif card is not None:
        source = card.for_order()
    else:
        raise ValueError('a card or a vault id is required')
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            amount=AmountWithBreakdown(currency_code=code, value=format_amount(amount, code)),
            custom_id=order_number,
            invoice_id=invoice_id,
            description='Order %s' % order_number,
        )],
        payment_source=PaymentSource(card=source),
    )
    return get_client().orders.create_order(body, pay_pal_request_id=ref, prefer=REPRESENTATION)


def reauthorize(ref: str, authorization_id: str, *, amount: Decimal, code: str) -> PaymentAuthorization:
    return get_client().payments.reauthorize_payment(
        authorization_id, pay_pal_request_id=ref, prefer=REPRESENTATION,
        body=ReauthorizeRequest(amount=money(amount, code)))


def capture(ref: str, authorization_id: str, *, amount: Decimal, code: str) -> CapturedPayment:
    return get_client().payments.capture_authorized_payment(
        authorization_id, pay_pal_request_id=ref, prefer=REPRESENTATION,
        body=CaptureRequest(amount=money(amount, code), final_capture=True))


def void(ref: str, authorization_id: str) -> PaymentAuthorization:
    return get_client().payments.void_payment(authorization_id, pay_pal_request_id=ref, prefer=REPRESENTATION)


def refund(ref: str, capture_id: str, *, amount: Decimal, code: str) -> Refund:
    return get_client().payments.refund_captured_payment(
        capture_id, pay_pal_request_id=ref, prefer=REPRESENTATION,
        body=RefundRequest(amount=money(amount, code), custom_id=ref))


def get_authorization(authorization_id: str) -> PaymentAuthorization:
    return get_client().payments.get_authorized_payment(authorization_id)


def get_capture(capture_id: str) -> CapturedPayment:
    return get_client().payments.get_captured_payment(capture_id)


def get_refund(refund_id: str) -> Refund:
    return get_client().payments.get_refund(refund_id)


@dataclass(frozen=True)
class SavedToken:
    http_status: int
    token: PaymentTokenResponse


VAULT_SAVED = 'SAVED'  # our marker for a 2xx vault response carrying a card: the response has no status enum


def save_card(ref: str, card: CardInput, customer_id: str | None) -> SavedToken:
    source = PaymentTokenRequestPaymentSource(card=card.for_vault())
    body = (PaymentTokenRequest(payment_source=source, customer=Customer(id=customer_id)) if customer_id
            else PaymentTokenRequest(payment_source=source))
    result = get_client().vault.with_raw_response.create_payment_token(body, pay_pal_request_id=ref)
    if isinstance(result, Failure):
        result.unwrap()  # raises ApiError with the decoded error body
    assert isinstance(result, Success)
    return SavedToken(result.response.status_code, result.payload)


def read_saved_card(saved: SavedToken) -> Answer:
    token = saved.token
    card = token.payment_source.card if not isinstance(token.payment_source, UnsetType) else UNSET
    if not 200 <= saved.http_status < 300 or isinstance(card, UnsetType) or isinstance(token.id, UnsetType):
        return Answer(_str(token.id), UNSET, None, detail='PayPal returned no saved card.')
    if card.verification_status == CardVerificationStatus.FAILED:
        return Answer(token.id, CardVerificationStatus.FAILED, None, detail='The card failed verification.')
    return Answer(token.id, VAULT_SAVED, None)


def save_card_outcome(status: object) -> str:
    match status:
        case 'SAVED':
            return DONE
        case CardVerificationStatus.FAILED:
            return FAILED
        case _:
            return UNKNOWN


@dataclass(frozen=True)
class Deleted:
    http_status: int


DELETED = 'DELETED'


def delete_card(token_id: str) -> Deleted:
    """Delete a vault token; a 404 means PayPal no longer has it, which is what we asked for."""
    result = get_client().vault.with_raw_response.delete_payment_token(token_id)
    if isinstance(result, Failure):
        if result.response.status_code == 404:
            return Deleted(404)
        result.unwrap()
    return Deleted(result.response.status_code)


def read_deleted(deleted: Deleted, token_id: str) -> Answer:
    ok = 200 <= deleted.http_status < 300 or deleted.http_status == 404
    return Answer(token_id, DELETED if ok else UNSET, None)


def delete_outcome(status: object) -> str:
    return DONE if status == DELETED else UNKNOWN


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def search_transactions(start: datetime, end: datetime, page: int) -> SearchResponse:
    return get_client().transaction_search.search_transactions(
        rfc3339(start), rfc3339(end), page_size=100, page=page)


def rfc3339(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def describe(e: BaseException) -> str:
    """A log-safe one-liner for an exception from a PayPal call (no bodies, no card data)."""
    if isinstance(e, ApiError):
        return 'HTTP %s %s' % (e.status_code, error_issue(e.error))
    return type(e).__name__
