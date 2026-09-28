"""
The PayPal Server SDK boundary: the only module that imports ``paypal``.

Every function here makes exactly one PayPal call and hands back plain
dataclasses, with the provider's status already mapped to one of
``done`` / ``pending`` / ``failed`` / ``unknown`` by that step's own mapper.
Card details pass straight through to PayPal and are never logged or kept.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, TypeVar, cast
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables

from paypal import PaypalClient
from paypal.core import (
    ApiError,
    ClientCredentials,
    Failure,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    OAuthProviderError,
    RawError,
    UNSET,
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
    CheckoutPaymentIntent,
    OrderStatus,
    RefundStatus,
)

log = logging.getLogger(__name__)

_F = TypeVar('_F', bound=Callable[..., Any])


def _sensitive(*names: str) -> Callable[[_F], _F]:
    """Typed ``sensitive_variables``: masks card data in Django error reports."""
    return cast(Callable[[_F], _F], sensitive_variables(*names))

# The one server this SDK declares (paypal/server/server_config.py). Any other
# host has to be given explicitly through PAYPAL_BASE_URL.
SANDBOX_BASE_URL = 'https://api-m.sandbox.paypal.com'

DONE, PENDING, FAILED, UNKNOWN = 'done', 'pending', 'failed', 'unknown'

# ISO 4217 minor units for every currency that does not have two.
_EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0, 'PYG': 0,
    'RWF': 0, 'UGX': 0, 'UYI': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
    'CLF': 4, 'UYW': 4,
}


def minor_unit_places(currency: str) -> int:
    return _EXPONENT.get(currency, 2)


def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-minor_unit_places(currency)))


def format_amount(value: Decimal, currency: str) -> str:
    return str(quantize(value, currency))


# --------------------------------------------------------------------------
# Transport: logging, and the status of the last response for the error map
# --------------------------------------------------------------------------

_last_status: ContextVar[int | None] = ContextVar('paypal_last_status', default=None)


class ObservingTransport:
    """
    Wraps the SDK's httpx transport. Logs method, path and status only (never
    headers or bodies: they carry the bearer token and card data), and records
    the last response status so an undecodable *error* body can be told apart
    from an undecodable *success* body.
    """

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        _last_status.set(None)
        path = urlsplit(request.url).path
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except httpx.HTTPError as exc:
            log.warning('PayPal %s %s -> %s', request.method, path, type(exc).__name__)
            raise
        _last_status.set(response.status_code)
        log.info(
            'PayPal %s %s -> %s (%.0f ms, debug_id=%s)',
            request.method, path, response.status_code,
            (time.monotonic() - started) * 1000,
            response.headers.get('paypal-debug-id', '-'),
        )
        return response

    def close(self) -> None:
        self._inner.close()


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

def resolve_base_url(environment: str, override: str) -> str:
    if override:
        return override
    if environment == 'sandbox':
        return SANDBOX_BASE_URL
    raise ImproperlyConfigured(
        'PAYPAL_ENVIRONMENT=%r has no built-in API address; set PAYPAL_BASE_URL.' % environment)


_client: PaypalClient | None = None
_client_lock = threading.Lock()


@_sensitive('client_secret')
def build_client() -> PaypalClient:
    client_id = settings.PAYPAL_CLIENT_ID
    client_secret = settings.PAYPAL_CLIENT_SECRET
    if not client_id or not client_secret:
        # Without them the SDK would silently send unauthenticated requests.
        raise ImproperlyConfigured('PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.')
    timeout = float(settings.PAYPAL_TIMEOUT)
    return PaypalClient(
        base_url=resolve_base_url(settings.PAYPAL_ENVIRONMENT, settings.PAYPAL_BASE_URL),
        timeout=timeout,
        # With a custom transport the client's timeout= no longer reaches the
        # wire, so the transport carries it.
        custom_http_client=ObservingTransport(HttpxClient(timeout=timeout)),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


def get_client() -> PaypalClient:
    """One long-lived client per process, built lazily (i.e. after any fork)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Swap the process client (tests, credential rotation)."""
    global _client
    with _client_lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

NEVER_SENT = 'never_sent'   # nothing reached PayPal: known, nothing happened
REFUSED = 'refused'         # PayPal answered with a rejection: nothing happened
MAYBE = 'maybe'             # the write may have landed


@dataclass(frozen=True)
class Classified:
    kind: str
    http_status: int          # what our API answers
    message: str
    provider_status: int | None = None
    issues: tuple[str, ...] = ()


class ProviderError(Exception):
    def __init__(self, http_status: int, message: str, *, outcome_unknown: bool = False,
                 issues: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.issues = issues


_NEVER_SENT_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


def _describe(err: Error) -> tuple[str, tuple[str, ...]]:
    issues: tuple[str, ...] = ()
    detail = ''
    if not isinstance(err.details, UnsetType):
        issues = tuple(d.issue for d in err.details)
        descriptions = [d.description for d in err.details if not isinstance(d.description, UnsetType)]
        if descriptions:
            detail = ' ' + ' '.join(descriptions)
    return err.message + detail, issues


def _raw_message(body: RawError) -> str:
    try:
        data = body.json()
    except ValueError:
        return ''
    if isinstance(data, dict):
        return str(data.get('message') or data.get('error_description') or '')
    return ''


def classify(exc: BaseException) -> Classified | None:
    """Map any failure of a PayPal call to what happened. None = not a PayPal failure."""
    if isinstance(exc, ApiError):
        body: Any = exc.error
        status = exc.status_code
        if isinstance(body, OAuthProviderError):
            return Classified(NEVER_SENT, 502, 'PayPal refused our API credentials.', status)
        if status in (401, 403):
            return Classified(REFUSED, 502, 'PayPal refused our API credentials.', status)
        if status == 429:
            return Classified(REFUSED, 503, 'PayPal is rate-limiting requests; try again shortly.', status)
        if 400 <= status < 500:
            message: str = 'PayPal rejected the request.'
            issues: tuple[str, ...] = ()
            if isinstance(body, Error):
                message, issues = _describe(body)
            elif isinstance(body, RawError):
                message = _raw_message(body) or message
            http = 409 if status == 409 else 422 if status in (400, 422) else 502
            return Classified(REFUSED, http, message, status, issues)
        return Classified(MAYBE, 502, 'PayPal is unavailable.', status)
    if isinstance(exc, _NEVER_SENT_EXC):
        return Classified(NEVER_SENT, 502, 'Could not reach PayPal; nothing was sent.')
    if isinstance(exc, httpx.RequestError):
        return Classified(MAYBE, 504, 'No response from PayPal.')
    if isinstance(exc, ValueError):
        # pydantic.ValidationError or a non-JSON body. Which side of the
        # response it came from decides what it means.
        last = _last_status.get()
        if last is not None and 400 <= last < 500:
            return Classified(REFUSED, 422 if last in (400, 422) else 502,
                              'PayPal rejected the request (unreadable error body).', last)
        return Classified(MAYBE, 502, 'Unreadable response from PayPal.', last)
    return None


def to_provider_error(exc: BaseException) -> ProviderError:
    c = classify(exc)
    if c is None:
        raise exc
    return ProviderError(c.http_status, c.message, outcome_unknown=c.kind == MAYBE, issues=c.issues)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    """What one PayPal write (or lookup) said, read the same way for every step."""
    outcome: str
    provider_id: str
    status: str
    provider_time: datetime | None = None
    amount: Decimal | None = None
    currency: str | None = None
    message: str = ''
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CardInput:
    number: str
    expiry: str                     # YYYY-MM
    security_code: str
    name: str = ''
    billing_address: dict[str, str] | None = None

    def __repr__(self) -> str:      # never let card data reach a log line
        return 'CardInput(****%s)' % self.number[-4:]


def _opt(value: Any) -> Any:
    return None if isinstance(value, UnsetType) else value


def _text(value: Any) -> str:
    v = _opt(value)
    return '' if v is None else str(v)        # str enums stringify to their wire value


def _time(value: Any) -> datetime | None:
    v = _opt(value)
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except ValueError:
        return None


def _money(value: Any) -> tuple[Decimal | None, str | None]:
    m = _opt(value)
    if m is None:
        return None, None
    return Decimal(m.value), m.currency_code


def _address(data: dict[str, str] | None) -> Address | UnsetType:
    if not data or not data.get('countryCode'):
        return UNSET
    members = {
        'address_line_1': data.get('addressLine1'),
        'address_line_2': data.get('addressLine2'),
        'admin_area_2': data.get('city'),
        'admin_area_1': data.get('state'),
        'postal_code': data.get('postalCode'),
    }
    # Omitted, never "" or None: these members are Optional (T | UNSET).
    return Address(country_code=data['countryCode'], **{k: v for k, v in members.items() if v})


def authorization_outcome(status: Any) -> str:
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case (AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED
              | AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED):
            return FAILED           # no longer an open hold
        case _:
            return UNKNOWN


def void_outcome(status: Any) -> str:
    match status:
        case AuthorizationStatus.VOIDED:
            return DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED           # too late: the money was taken
        case _:
            return UNKNOWN


def capture_outcome(status: Any) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case (CaptureStatus.DECLINED | CaptureStatus.FAILED
              | CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED):
            return FAILED
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


def _authorization_answer(auth: PaymentAuthorization | AuthorizationWithAdditionalData,
                          outcome_of: Any = authorization_outcome, **extra: Any) -> Answer:
    amount, currency = _money(auth.amount)
    return Answer(
        outcome=outcome_of(_opt(auth.status)),
        provider_id=_text(auth.id),
        status=_text(auth.status),
        provider_time=_time(auth.update_time) or _time(auth.create_time),
        amount=amount,
        currency=currency,
        extra={
            'created_at': _time(auth.create_time),
            'expires_at': _time(auth.expiration_time),
            'reason': _text(auth.status_details.reason) if not isinstance(auth.status_details, UnsetType) else '',
            **extra,
        },
    )


def _order_answer(order: Order) -> Answer:
    order_id = _text(order.id)
    card = {}
    source = _opt(order.payment_source)
    if source is not None and _opt(source.card) is not None:
        card = {'card_brand': _text(source.card.brand), 'card_last_digits': _text(source.card.last_digits)}
    if _opt(order.status) == OrderStatus.PAYER_ACTION_REQUIRED:
        return Answer(FAILED, '', _text(order.status), message=(
            'PayPal requires the shopper to complete a 3-D Secure challenge in a browser, '
            'which this integration does not support.'), extra={'paypal_order_id': order_id})
    units = _opt(order.purchase_units) or []
    payments = _opt(units[0].payments) if units else None
    auths = (_opt(payments.authorizations) or []) if payments is not None else []
    if not auths:
        # e.g. CREATED/APPROVED with no authorization yet, or a body missing it
        return Answer(UNKNOWN if _opt(order.status) is None else PENDING, '', _text(order.status),
                      extra={'paypal_order_id': order_id, **card})
    return _authorization_answer(auths[0], paypal_order_id=order_id, **card)


def _capture_answer(cap: CapturedPayment) -> Answer:
    amount, currency = _money(cap.amount)
    fee = net = None
    breakdown = _opt(cap.seller_receivable_breakdown)
    if breakdown is not None:
        fee = _money(breakdown.paypal_fee)[0]
        net = _money(breakdown.net_amount)[0]
    return Answer(
        outcome=capture_outcome(_opt(cap.status)),
        provider_id=_text(cap.id),
        status=_text(cap.status),
        provider_time=_time(cap.create_time),
        amount=amount,
        currency=currency,
        extra={'paypal_fee': fee, 'net_amount': net},
    )


def _refund_answer(refund: Refund) -> Answer:
    amount, currency = _money(refund.amount)
    return Answer(
        outcome=refund_outcome(_opt(refund.status)),
        provider_id=_text(refund.id),
        status=_text(refund.status),
        provider_time=_time(refund.create_time),
        amount=amount,
        currency=currency,
    )


def _token_answer(token: PaymentTokenResponse) -> Answer:
    source = _opt(token.payment_source)
    card = _opt(source.card) if source is not None else None
    customer = _opt(token.customer)
    token_id = _text(token.id)
    last_digits = _text(card.last_digits) if card is not None else ''
    # The token response carries no status member: the documented success body
    # is the token itself, so an id with the card description is "done".
    outcome = DONE if token_id and last_digits else UNKNOWN
    return Answer(
        outcome=outcome,
        provider_id=token_id,
        status='CREATED' if outcome == DONE else '',
        # create_time is not a modelled member; unknown fields are preserved
        provider_time=_time((token.model_extra or {}).get('create_time')),
        extra={
            'customer_id': _text(customer.id) if customer is not None else '',
            'brand': _text(card.brand) if card is not None else '',
            'last_digits': last_digits,
            'expiry': _text(card.expiry) if card is not None else '',
        },
    )


# --------------------------------------------------------------------------
# Operations (one PayPal call each)
# --------------------------------------------------------------------------

@_sensitive('card', 'card_request')
def create_authorized_order(request_id: str, *, amount: Decimal, currency: str, invoice_id: str,
                            custom_id: str, description: str, card: CardInput | None = None,
                            vault_id: str | None = None) -> Answer:
    if card is not None:
        card_request = CardRequest(
            number=card.number, expiry=card.expiry, security_code=card.security_code,
            name=card.name or UNSET, billing_address=_address(card.billing_address),
        )
    elif vault_id:
        card_request = CardRequest(vault_id=vault_id)
    else:
        raise ValueError('a card or a vault id is required')
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            amount=AmountWithBreakdown(currency_code=currency, value=format_amount(amount, currency)),
            invoice_id=invoice_id,
            custom_id=custom_id,
            description=description[:127],
        )],
        payment_source=PaymentSource(card=card_request),
    )
    order = get_client().orders.create_order(
        body, pay_pal_request_id=request_id, prefer='return=representation')
    return _order_answer(order)


def get_authorization(authorization_id: str) -> Answer:
    return _authorization_answer(get_client().payments.get_authorized_payment(authorization_id))


def get_authorization_for_void(authorization_id: str) -> Answer:
    return _authorization_answer(get_client().payments.get_authorized_payment(authorization_id),
                                 outcome_of=void_outcome)


def reauthorize(request_id: str, authorization_id: str, *, amount: Decimal, currency: str) -> Answer:
    auth = get_client().payments.reauthorize_payment(
        authorization_id, pay_pal_request_id=request_id, prefer='return=representation',
        body=ReauthorizeRequest(amount=Money(currency_code=currency, value=format_amount(amount, currency))))
    return _authorization_answer(auth)


def capture(request_id: str, authorization_id: str, *, amount: Decimal, currency: str) -> Answer:
    cap = get_client().payments.capture_authorized_payment(
        authorization_id, pay_pal_request_id=request_id, prefer='return=representation',
        body=CaptureRequest(amount=Money(currency_code=currency, value=format_amount(amount, currency)),
                            final_capture=True))
    return _capture_answer(cap)


def get_capture(capture_id: str) -> Answer:
    return _capture_answer(get_client().payments.get_captured_payment(capture_id))


def void(request_id: str, authorization_id: str) -> Answer:
    auth = get_client().payments.void_payment(
        authorization_id, pay_pal_request_id=request_id, prefer='return=representation')
    return _authorization_answer(auth, outcome_of=void_outcome)


def refund(request_id: str, capture_id: str, *, amount: Decimal, currency: str) -> Answer:
    result = get_client().payments.refund_captured_payment(
        capture_id, pay_pal_request_id=request_id, prefer='return=representation',
        body=RefundRequest(amount=Money(currency_code=currency, value=format_amount(amount, currency))))
    return _refund_answer(result)


def get_refund(refund_id: str) -> Answer:
    return _refund_answer(get_client().payments.get_refund(refund_id))


@_sensitive('card', 'card_request')
def vault_card(request_id: str, card: CardInput, *, customer_id: str = '') -> Answer:
    card_request = PaymentTokenRequestCard(
        number=card.number, expiry=card.expiry, security_code=card.security_code,
        name=card.name or UNSET, billing_address=_address(card.billing_address),
    )
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=card_request),
        customer=Customer(id=customer_id) if customer_id else UNSET,
    )
    return _token_answer(get_client().vault.create_payment_token(body, pay_pal_request_id=request_id))


def delete_vaulted_card(token_id: str) -> Answer:
    result = get_client().vault.with_raw_response.delete_payment_token(token_id)
    if isinstance(result, Failure):
        result.unwrap()             # raises the ApiError the error map reads
    # delete is idempotent at PayPal (a repeat answers 204), so 2xx = gone
    return Answer(DONE, token_id, 'DELETED')


@dataclass(frozen=True)
class ReportedTransaction:
    transaction_id: str
    event_code: str
    status: str                 # D / P / S / V
    initiated_at: datetime | None
    amount: Decimal | None
    currency: str | None
    fee: Decimal | None
    invoice_id: str
    custom_field: str


@dataclass(frozen=True)
class TransactionPage:
    transactions: list[ReportedTransaction]
    page: int
    total_pages: int


def _rfc3339(value: datetime) -> str:
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


def search_transactions(start: datetime, end: datetime, *, page: int, page_size: int = 100) -> TransactionPage:
    """One page of PayPal's transaction report; ``start``/``end`` span at most 31 days."""
    result = get_client().transaction_search.search_transactions(
        _rfc3339(start), _rfc3339(end), page=page, page_size=page_size)
    rows: list[ReportedTransaction] = []
    for detail in _opt(result.transaction_details) or []:
        info = _opt(detail.transaction_info)
        if info is None:
            continue
        amount, currency = _money(info.transaction_amount)
        rows.append(ReportedTransaction(
            transaction_id=_text(info.transaction_id),
            event_code=_text(info.transaction_event_code),
            status=_text(info.transaction_status),
            initiated_at=_time(info.transaction_initiation_date),
            amount=amount,
            currency=currency,
            fee=_money(info.fee_amount)[0],
            invoice_id=_text(info.invoice_id),
            custom_field=_text(info.custom_field),
        ))
    total_pages = _opt(result.total_pages)
    return TransactionPage(rows, _opt(result.page) or page, int(total_pages) if total_pages else 0)


__all__ = [
    'Answer', 'CardInput', 'ProviderError', 'RawError', 'ReportedTransaction', 'TransactionPage',
    'classify', 'to_provider_error', 'get_client', 'set_client', 'format_amount', 'quantize',
    'DONE', 'PENDING', 'FAILED', 'UNKNOWN', 'NEVER_SENT', 'REFUSED', 'MAYBE',
]
