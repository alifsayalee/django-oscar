"""
The one place this app talks to the PayPal SDK: client construction, request
builders, reading PayPal's answers and mapping PayPal statuses to outcomes.

Everything here is checked with ``mypy --strict``.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import UNSET, ClientCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Customer,
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
)
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CardVerificationStatus,
    CheckoutPaymentIntent,
    OrderStatus,
    RefundStatus,
)

logger = logging.getLogger(__name__)

# The only server the SDK declares (sdk-map.md, "Servers & auth").
SANDBOX_BASE_URL = 'https://api-m.sandbox.paypal.com'
ENVIRONMENT_BASE_URLS = {'sandbox': SANDBOX_BASE_URL}

# Prefer header value asking PayPal for the full resource; the SDK's default
# ("return=minimal") omits payments and the fee breakdown.
REPRESENTATION = 'return=representation'

# Outcomes (mirrors models.Outcome, kept as plain strings so this module has no Django model imports).
SENDING = 'sending'
DONE = 'done'
PENDING = 'pending'
FAILED = 'failed'
NEEDS_REVIEW = 'needs_review'
UNKNOWN = 'unknown'


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class LoggingTransport:
    """Logs method, path, status and duration of each PayPal call; never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = request.url.split('?', 1)[0]
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('PayPal %s %s -> %s (%.0f ms)', request.method, path,
                           type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info('PayPal %s %s -> %s (%.0f ms) debug_id=%s', request.method, path,
                    response.status_code, (time.monotonic() - started) * 1000,
                    response.headers.get('paypal-debug-id', '-'))
        return response

    def close(self) -> None:
        self._inner.close()


def resolve_base_url() -> str:
    """PAYPAL_BASE_URL verbatim when set, else the host for PAYPAL_ENVIRONMENT; unknown fails loudly."""
    override: str | None = getattr(settings, 'PAYPAL_BASE_URL', None)
    if override:
        return override
    environment = str(settings.PAYPAL_ENVIRONMENT).strip().lower()
    try:
        return ENVIRONMENT_BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            'PAYPAL_ENVIRONMENT=%r has no known PayPal host; set PAYPAL_BASE_URL explicitly.'
            % settings.PAYPAL_ENVIRONMENT) from None


def build_client(custom_http_client: HttpClient | None = None) -> PaypalClient:
    client_id = settings.PAYPAL_CLIENT_ID
    client_secret = settings.PAYPAL_CLIENT_SECRET
    if not client_id or not client_secret:
        raise ImproperlyConfigured('PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.')
    timeout = float(settings.PAYPAL_TIMEOUT)
    transport = custom_http_client or LoggingTransport(HttpxClient(timeout=timeout))
    return PaypalClient(
        base_url=resolve_base_url(),
        timeout=timeout,
        custom_http_client=transport,
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def get_client() -> PaypalClient:
    """The process-wide client, built lazily (after any worker fork) and closed at exit."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Replace the process-wide client (tests inject one built over a stub transport)."""
    global _client
    with _client_lock:
        _client = client


# --------------------------------------------------------------------------
# Money and time
# --------------------------------------------------------------------------

# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0, 'PYG': 0,
    'RWF': 0, 'UGX': 0, 'UYI': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
    'CLF': 4, 'UYW': 4,
}


def currency_places(currency: str) -> int:
    return _EXPONENT.get(currency.upper(), 2)


def is_representable(value: Decimal, currency: str) -> bool:
    quantum = Decimal(1).scaleb(-currency_places(currency))
    return value == value.quantize(quantum)


def format_amount(value: Decimal, currency: str) -> str:
    """Money as PayPal's string value, to the currency's own number of decimals."""
    return str(value.quantize(Decimal(1).scaleb(-currency_places(currency))))


def money(value: Decimal, currency: str) -> Money:
    return Money(currency_code=currency, value=format_amount(value, currency))


def parse_decimal(value: str | UnsetType) -> Decimal | None:
    if isinstance(value, UnsetType):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def parse_time(value: str | UnsetType) -> datetime | None:
    if isinstance(value, UnsetType) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _status_text(status: Any) -> str:
    if isinstance(status, UnsetType) or status is None:
        return ''
    return str(status)


# --------------------------------------------------------------------------
# Status -> outcome. Each write step has its own mapping; anything not listed
# (including a value newer than this SDK, or no status at all) is UNKNOWN.
# --------------------------------------------------------------------------

def authorization_outcome(status: Any) -> str:
    """For a step that creates a hold (authorize, reauthorize)."""
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return FAILED
        case _:  # CAPTURED / PARTIALLY_CAPTURED on a new hold, unlisted, absent
            return UNKNOWN


def void_outcome(status: Any) -> str:
    """For the void step: the release itself is what 'done' means."""
    match status:
        case AuthorizationStatus.VOIDED:
            return DONE
        case AuthorizationStatus.PENDING | AuthorizationStatus.CREATED:
            return PENDING
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED
        case _:  # DENIED (not our release), unlisted, absent
            return UNKNOWN


def capture_outcome(status: Any) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case (CaptureStatus.DECLINED | CaptureStatus.FAILED
              | CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED):
            return FAILED  # never taken, or taken and since given back
        case _:
            return UNKNOWN


def refund_outcome(status: Any) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return DONE
        case RefundStatus.PENDING:
            return PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return FAILED
        case _:
            return UNKNOWN


def order_outcome(status: Any) -> str:
    """The create-order envelope; the authorization inside decides when it is COMPLETED."""
    match status:
        case OrderStatus.COMPLETED:
            return DONE
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED | OrderStatus.PAYER_ACTION_REQUIRED:
            return PENDING
        case OrderStatus.VOIDED:
            return FAILED
        case _:
            return UNKNOWN


def vault_outcome(token: PaymentTokenResponse) -> str:
    if isinstance(token.id, UnsetType) or not token.id:
        return UNKNOWN
    card = token.payment_source.card if not isinstance(token.payment_source, UnsetType) else UNSET
    verification: Any = card.verification_status if not isinstance(card, UnsetType) else UNSET
    match verification:
        case CardVerificationStatus.VERIFIED:
            return DONE
        case CardVerificationStatus.FAILED:
            return FAILED
        case UnsetType():
            return DONE  # no verification requested: the token id is the saved card
        case _:
            return UNKNOWN


# --------------------------------------------------------------------------
# Reading answers into one shape
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    """What one step's response says, read the same way for every step."""

    provider_id: str
    status: str
    outcome: str
    provider_time: datetime | None
    amount: Decimal | None
    currency: str | None


def _money_parts(value: Money | AmountWithBreakdown | UnsetType) -> tuple[Decimal | None, str | None]:
    if isinstance(value, UnsetType):
        return None, None
    return parse_decimal(value.value), value.currency_code


@dataclass(frozen=True)
class AuthorizedOrder:
    """create_order's answer: the PayPal order and the hold inside it."""

    answer: Answer
    paypal_order_id: str
    order_status: str
    authorization_id: str
    authorization_expires_at: datetime | None
    card_brand: str
    card_last_digits: str


def read_authorized_order(order: Order) -> AuthorizedOrder:
    order_id = order.id if not isinstance(order.id, UnsetType) else ''
    order_status = _status_text(order.status)
    auth = None
    if not isinstance(order.purchase_units, UnsetType) and order.purchase_units:
        payments = order.purchase_units[0].payments
        if not isinstance(payments, UnsetType) and not isinstance(payments.authorizations, UnsetType) \
                and payments.authorizations:
            auth = payments.authorizations[0]
    brand = last_digits = ''
    if not isinstance(order.payment_source, UnsetType) and not isinstance(order.payment_source.card, UnsetType):
        card = order.payment_source.card
        brand = _status_text(card.brand)
        last_digits = card.last_digits if not isinstance(card.last_digits, UnsetType) else ''
    if auth is None:
        # No hold to read: the order envelope decides, but it can never be DONE without one.
        outcome = order_outcome(order.status)
        answer = Answer(order_id, order_status, UNKNOWN if outcome == DONE else outcome,
                        parse_time(order.update_time), None, None)
        return AuthorizedOrder(answer, order_id, order_status, '', None, brand, last_digits)
    amount, currency = _money_parts(auth.amount)
    outcome = authorization_outcome(auth.status)
    if outcome == DONE and order_outcome(order.status) != DONE:
        outcome = UNKNOWN
    auth_id = auth.id if not isinstance(auth.id, UnsetType) else ''
    answer = Answer(auth_id, _status_text(auth.status), outcome, parse_time(auth.create_time), amount, currency)
    return AuthorizedOrder(answer, order_id, order_status, auth_id, parse_time(auth.expiration_time),
                           brand, last_digits)


def read_authorization(auth: PaymentAuthorization, *, for_void: bool = False) -> Answer:
    amount, currency = _money_parts(auth.amount)
    outcome = void_outcome(auth.status) if for_void else authorization_outcome(auth.status)
    when = parse_time(auth.update_time if for_void else auth.create_time)
    return Answer(auth.id if not isinstance(auth.id, UnsetType) else '', _status_text(auth.status),
                  outcome, when, amount, currency)


@dataclass(frozen=True)
class CaptureBreakdown:
    gross: Decimal | None
    fee: Decimal | None
    net: Decimal | None


def read_capture(capture: CapturedPayment) -> Answer:
    amount, currency = _money_parts(capture.amount)
    return Answer(capture.id if not isinstance(capture.id, UnsetType) else '', _status_text(capture.status),
                  capture_outcome(capture.status), parse_time(capture.create_time), amount, currency)


def read_capture_breakdown(capture: CapturedPayment) -> CaptureBreakdown:
    breakdown = capture.seller_receivable_breakdown
    if isinstance(breakdown, UnsetType):
        return CaptureBreakdown(None, None, None)
    return CaptureBreakdown(_money_parts(breakdown.gross_amount)[0],
                            _money_parts(breakdown.paypal_fee)[0],
                            _money_parts(breakdown.net_amount)[0])


def read_refund(refund: Refund) -> Answer:
    amount, currency = _money_parts(refund.amount)
    return Answer(refund.id if not isinstance(refund.id, UnsetType) else '', _status_text(refund.status),
                  refund_outcome(refund.status), parse_time(refund.create_time), amount, currency)


@dataclass(frozen=True)
class VaultedCard:
    answer: Answer
    customer_id: str
    brand: str
    last_digits: str
    expiry: str
    name: str


def read_payment_token(token: PaymentTokenResponse) -> VaultedCard:
    token_id = token.id if not isinstance(token.id, UnsetType) else ''
    customer_id = ''
    if not isinstance(token.customer, UnsetType) and not isinstance(token.customer.id, UnsetType):
        customer_id = token.customer.id
    brand = last_digits = expiry = name = status = ''
    if not isinstance(token.payment_source, UnsetType) and not isinstance(token.payment_source.card, UnsetType):
        card = token.payment_source.card
        brand = _status_text(card.brand)
        last_digits = card.last_digits if not isinstance(card.last_digits, UnsetType) else ''
        expiry = card.expiry if not isinstance(card.expiry, UnsetType) else ''
        name = card.name if not isinstance(card.name, UnsetType) else ''
        status = _status_text(card.verification_status)
    extra = token.model_extra or {}
    created = extra.get('create_time')
    answer = Answer(token_id, status, vault_outcome(token),
                    parse_time(created) if isinstance(created, str) else None, None, None)
    return VaultedCard(answer, customer_id, brand, last_digits, expiry, name)


# --------------------------------------------------------------------------
# Request builders. Card details pass straight through to PayPal and are never stored or logged.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CardDetails:
    number: str
    expiry: str            # YYYY-MM
    security_code: str
    name: str
    billing_address: dict[str, str] | None

    def __repr__(self) -> str:  # never render card data, even in a traceback or debugger
        return 'CardDetails(<redacted>)'


def _address(fields: dict[str, str] | None) -> Address | UnsetType:
    if not fields:
        return UNSET
    return Address(
        address_line_1=fields.get('address_line_1') or UNSET,
        address_line_2=fields.get('address_line_2') or UNSET,
        admin_area_2=fields.get('admin_area_2') or UNSET,
        admin_area_1=fields.get('admin_area_1') or UNSET,
        postal_code=fields.get('postal_code') or UNSET,
        country_code=fields['country_code'],
    )


def authorize_order_request(*, order_number: str, reference: str, custom_id: str,
                            amount: Decimal, currency: str,
                            card: CardDetails | None, vault_id: str | None) -> OrderRequest:
    if card is not None:
        card_request = CardRequest(
            name=card.name or UNSET,
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            billing_address=_address(card.billing_address),
        )
    elif vault_id:
        card_request = CardRequest(vault_id=vault_id)
    else:
        raise ValueError('A card or a saved card is required.')
    return OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            reference_id=order_number,
            invoice_id=reference,
            custom_id=custom_id,
            amount=AmountWithBreakdown(currency_code=currency, value=format_amount(amount, currency)),
        )],
        payment_source=PaymentSource(card=card_request),
    )


def capture_request(amount: Decimal, currency: str) -> CaptureRequest:
    return CaptureRequest(amount=money(amount, currency), final_capture=True)


def reauthorize_request(amount: Decimal, currency: str) -> ReauthorizeRequest:
    return ReauthorizeRequest(amount=money(amount, currency))


def refund_request(amount: Decimal, currency: str) -> RefundRequest:
    return RefundRequest(amount=money(amount, currency))


def payment_token_request(card: CardDetails, *, customer_id: str | None,
                          merchant_customer_id: str) -> PaymentTokenRequest:
    customer = Customer(id=customer_id) if customer_id else Customer(merchant_customer_id=merchant_customer_id)
    return PaymentTokenRequest(
        customer=customer,
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(
            name=card.name or UNSET,
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            billing_address=_address(card.billing_address),
        )),
    )
