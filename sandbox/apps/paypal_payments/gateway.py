"""
The PayPal boundary: the SDK client, the error ladder, and ``safe_write`` --
the one path every PayPal write goes through.

Only this module and ``services`` import the PayPal SDK. Everything that
leaves this module as an error is a ``PaymentError`` carrying the HTTP status
to answer, a stable code, a message safe to show, and whether the write may
have happened at PayPal.
"""
from __future__ import annotations

import atexit
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, NamedTuple, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal import PaypalClient
from paypal.core import (
    ApiError, ClientCredentials, HttpRequest, HttpResponse, HttpxClient,
    OAuthProviderError, UnsetType)
from paypal.models import Error

from .models import Installation, ProviderWrite

logger = logging.getLogger(__name__)

T = TypeVar('T')

# The only server the PayPal SDK declares. Any other environment must be
# given explicitly through PAYPAL_BASE_URL.
BASE_URLS = {'sandbox': 'https://api-m.sandbox.paypal.com'}

# ISO 4217 minor units for every currency that does not have two.
EXPONENT = {
    'BIF': 0, 'CLP': 0, 'DJF': 0, 'GNF': 0, 'ISK': 0, 'JPY': 0, 'KMF': 0, 'KRW': 0, 'PYG': 0,
    'RWF': 0, 'UGX': 0, 'UYI': 0, 'VND': 0, 'VUV': 0, 'XAF': 0, 'XOF': 0, 'XPF': 0,
    'BHD': 3, 'IQD': 3, 'JOD': 3, 'KWD': 3, 'LYD': 3, 'OMR': 3, 'TND': 3,
    'CLF': 4, 'UYW': 4,
}

# Transport failures raised before the request left: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# A claim still "sending" younger than this is treated as in flight.
SEND_WINDOW = timedelta(minutes=2)


# ---------------------------------------------------------------------------
# Money, times and references
# ---------------------------------------------------------------------------

def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-EXPONENT.get(currency, 2)))


def format_amount(value: Decimal, currency: str) -> str:
    return str(quantize(value, currency))


def to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, UnsetType):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def given(value: T | UnsetType) -> T | None:
    """An SDK ``Optional`` member as a plain value, ``None`` when unset."""
    return None if isinstance(value, UnsetType) else value


def provider_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def reference(*parts: str) -> str:
    """A reference unique to this install and this operation step."""
    return '%s-%s:%s' % (
        settings.PAYPAL_REFERENCE_PREFIX, Installation.current_id(), ':'.join(parts))


def custom_id_prefix() -> str:
    return '%s-%s:' % (settings.PAYPAL_REFERENCE_PREFIX, Installation.current_id())


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LoggingTransport:
    """
    Wraps the SDK's httpx transport and logs method, URL, status and timing.

    Headers and bodies are never logged: the Authorization header carries a
    live token and request bodies carry card details.
    """

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except httpx.RequestError as exc:
            logger.warning(
                'PayPal %s %s -> %s after %.0f ms', request.method, request.url,
                type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info(
            'PayPal %s %s -> %s (%.0f ms, debug id %s)', request.method, request.url,
            response.status_code, (time.monotonic() - started) * 1000,
            response.headers.get('paypal-debug-id', '-'))
        return response

    def close(self) -> None:
        self._inner.close()


def resolve_base_url() -> str:
    if settings.PAYPAL_BASE_URL:
        return str(settings.PAYPAL_BASE_URL)
    try:
        return BASE_URLS[settings.PAYPAL_ENVIRONMENT]
    except KeyError:
        raise ImproperlyConfigured(
            'PAYPAL_ENVIRONMENT=%r has no known PayPal host; set PAYPAL_BASE_URL '
            'to the API base address for it.' % settings.PAYPAL_ENVIRONMENT) from None


def build_client() -> PaypalClient:
    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        raise ImproperlyConfigured('PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.')
    if not settings.PAYPAL_CURRENCY:
        raise ImproperlyConfigured('PAYPAL_CURRENCY must be set.')
    return PaypalClient(
        base_url=resolve_base_url(),
        custom_http_client=LoggingTransport(
            HttpxClient(timeout=float(settings.PAYPAL_TIMEOUT_SECONDS))),
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET),
    )


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def get_client() -> PaypalClient:
    """The process-wide client, built on first use (so after any fork)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = build_client()
            atexit.register(_client.close)
        return _client


def install_client(client: PaypalClient | None) -> PaypalClient | None:
    """Replace the process-wide client (tests); returns the previous one."""
    global _client
    with _client_lock:
        previous, _client = _client, client
        return previous


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PaymentError(Exception):
    """A failure answered to the API caller."""

    def __init__(self, status_code: int, code: str, message: str, *,
                 outcome_unknown: bool = False, reference: str | None = None,
                 issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.reference = reference
        # PayPal's issue codes (``Error.details[].issue``), when it sent any.
        self.issues = issues or []


def error_issues(error: object) -> list[str]:
    if isinstance(error, Error):
        details = given(error.details) or []
        return [d.issue for d in details]
    return []


def error_text(error: object) -> str:
    if isinstance(error, Error):
        parts = [error.message]
        for detail in given(error.details) or []:
            parts.append('%s: %s' % (detail.issue, given(detail.description) or ''))
        return ' '.join(parts) + ' (PayPal debug id %s)' % error.debug_id
    return 'PayPal returned an error.'


def provider_error(status: int, error: object) -> PaymentError:
    """Maps PayPal's answer onto ours (the same arms for raw and raising calls)."""
    if isinstance(error, OAuthProviderError) or status in (401, 403):
        return PaymentError(502, 'paypal_credentials_rejected',
                            'PayPal refused this site\'s API credentials.')
    if status == 429:
        return PaymentError(503, 'paypal_rate_limited', 'PayPal is rate limiting requests; retry later.')
    if status == 404:
        # We only ever address PayPal resources we created: a 404 means our
        # records and PayPal's disagree, which the caller cannot fix.
        return PaymentError(502, 'paypal_resource_missing', error_text(error))
    if 400 <= status < 500:
        return PaymentError(status, 'paypal_rejected', error_text(error), issues=error_issues(error))
    return PaymentError(502, 'paypal_unavailable', 'PayPal is unavailable.',
                        outcome_unknown=status >= 500)


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def call_read(fn: Callable[[], T]) -> T:
    """
    Runs a PayPal read with a small bounded retry.

    Reads change nothing, so any transport failure, a 429 or a 5xx is retried
    (at most three attempts, ~1.5 s of backoff). Writes never come here.
    """
    delay = 0.5
    for attempt in (1, 2, 3):
        last = attempt == 3
        try:
            return fn()
        except ApiError as exc:
            if not last and not isinstance(exc.error, OAuthProviderError) \
                    and exc.status_code in RETRYABLE_STATUS:
                time.sleep(delay)
                delay *= 2
                continue
            raise provider_error(exc.status_code, exc.error) from exc
        except httpx.RequestError as exc:
            if not last:
                time.sleep(delay)
                delay *= 2
                continue
            raise PaymentError(502, 'paypal_unreachable', 'PayPal could not be reached.') from exc
        except ValueError as exc:  # pydantic ValidationError, or a non-JSON body
            raise PaymentError(502, 'paypal_unreadable_response',
                               'PayPal returned a response this site could not read.') from exc
    raise AssertionError('unreachable')


# ---------------------------------------------------------------------------
# The safe write
# ---------------------------------------------------------------------------

class Answer(NamedTuple):
    """What one write step's response says, read the same way for every step."""
    provider_id: str | None
    status: str | None
    provider_time: datetime | None
    amount: Decimal | None = None
    currency: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class WriteStep:
    """
    One PayPal write step.

    ``base_ref`` identifies the operation and step; the claim reference (also
    the ``PayPal-Request-Id``) is derived from it, so it is the same on every
    attempt and every repeat of the request. PayPal de-duplicates a repeat
    under the same request id and returns the original result, so the check
    for an unknown outcome is a same-reference resend inside
    ``resend_window`` (the id's documented retention).
    """
    kind: str
    base_ref: str
    send: Callable[[str], Any]
    read: Callable[[Any], Answer]
    outcome_of: Callable[[Answer], str]
    resend_window: timedelta
    sent: tuple[Decimal, str] | None = None
    order_payment: Any = None
    user: Any = None
    # A lookup that finds the write by other means, used when the provider
    # answers that the write already happened (``is_landing``).
    find: Callable[[str], Any] | None = None
    is_landing: Callable[[ApiError], bool] | None = None
    # Re-reads a pending outcome so a repeat request can settle it.
    refresh: Callable[[ProviderWrite], Any] | None = None
    # Runs once, right after this request wins the claim and before anything
    # is sent; raising releases the claim.
    on_claimed: Callable[[ProviderWrite], None] | None = None


def current_write(base_ref: str) -> ProviderWrite | None:
    return ProviderWrite.objects.filter(
        base_ref=base_ref, released=False).order_by('-pk').first()


def _claim_ref(base_ref: str) -> str:
    generation = ProviderWrite.objects.filter(base_ref=base_ref, released=True).count()
    return base_ref if generation == 0 else '%s~%d' % (base_ref, generation)


def try_claim(step: WriteStep) -> ProviderWrite | None:
    """Insert-or-fail: the UNIQUE ref lets exactly one request hold the claim."""
    amount, currency = step.sent if step.sent else (None, '')
    try:
        with transaction.atomic():
            return ProviderWrite.objects.create(
                ref=_claim_ref(step.base_ref), kind=step.kind,
                order_payment=step.order_payment, user=step.user,
                base_ref=step.base_ref, amount=amount, currency=currency,
                outcome=ProviderWrite.SENDING)
    except IntegrityError:
        return None


def complete(write: ProviderWrite, outcome: str, answer: Answer | None = None,
             error_message: str = '') -> ProviderWrite:
    write.outcome = outcome
    if answer is not None:
        write.provider_id = answer.provider_id or write.provider_id
        write.provider_status = answer.status or ''
        write.provider_time = answer.provider_time or write.provider_time
        write.echoed_amount = answer.amount
        if answer.details:
            write.details = {**write.details, **answer.details}
    write.error_message = error_message
    if outcome == ProviderWrite.FAILED and not write.provider_id:
        # Nothing exists at PayPal: release the claim so a later request may
        # try again (under the next generation of the reference).
        write.released = True
    write.save()
    return write


def release(write: ProviderWrite) -> None:
    ProviderWrite.objects.filter(pk=write.pk).update(released=True)


def outcome_unknown(write: ProviderWrite, why: str) -> PaymentError:
    complete(write, ProviderWrite.UNKNOWN, error_message=why)
    return PaymentError(
        504, 'outcome_unknown',
        'PayPal did not confirm the result. It may have been applied; repeat the same '
        'request to check.', outcome_unknown=True, reference=write.ref)


def safe_write(step: WriteStep) -> ProviderWrite:
    """Claim, call, check, verify, complete -- for one PayPal write step."""
    write = try_claim(step)
    checking = False
    if write is None:
        existing = current_write(step.base_ref)
        if existing is None:
            # Lost a race for a generation that was just released; the
            # caller's repeat will take the next one.
            raise PaymentError(409, 'write_in_progress',
                               'This operation is being processed; retry shortly.')
        write = existing
        if write.outcome == ProviderWrite.SENDING and write.claimed_at > timezone.now() - SEND_WINDOW:
            return write
        if write.outcome == ProviderWrite.PENDING and step.refresh is not None:
            return _refresh(step, write)
        if write.outcome not in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN):
            return write
        checking = True
    elif step.on_claimed is not None:
        try:
            step.on_claimed(write)
        except Exception:
            complete(write, ProviderWrite.FAILED, error_message='Not sent: precondition failed.')
            raise

    resending = checking and timezone.now() - write.claimed_at < step.resend_window
    result = None
    landed_elsewhere = False
    if resending or not checking:
        try:
            result = step.send(write.ref)
        except NEVER_SENT as exc:
            if resending:
                raise outcome_unknown(write, 'Check could not reach PayPal.') from exc
            complete(write, ProviderWrite.FAILED, error_message='PayPal could not be reached; nothing was sent.')
            raise PaymentError(502, 'paypal_unreachable',
                               'PayPal could not be reached; nothing was sent.') from exc
        except ApiError as exc:
            if isinstance(exc.error, OAuthProviderError):
                # The token fetch failed before the operation was sent.
                if resending:
                    raise outcome_unknown(write, 'Check could not authenticate.') from exc
                complete(write, ProviderWrite.FAILED, error_message='PayPal refused our credentials.')
                raise provider_error(exc.status_code, exc.error) from exc
            if step.is_landing is not None and step.is_landing(exc):
                landed_elsewhere = True
            elif exc.status_code < 500:
                if resending:
                    raise outcome_unknown(write, 'Check was refused: %s' % error_text(exc.error)) from exc
                complete(write, ProviderWrite.FAILED, error_message=error_text(exc.error))
                raise provider_error(exc.status_code, exc.error) from exc
            # a 5xx: the write may still have landed -- check below
        except (httpx.RequestError, ValueError):
            pass  # sent, and no readable answer: it may have landed

    if result is None:
        if step.find is not None and (landed_elsewhere or not resending):
            finder = step.find
        elif not checking:
            finder = step.send  # a same-reference resend is the lookup
        else:
            raise outcome_unknown(
                write, 'Outcome unknown and PayPal no longer de-duplicates this reference; '
                       'an operator must check it at PayPal.')
        try:
            result = finder(write.ref)
        except (ApiError, httpx.RequestError, ValueError) as exc:
            raise outcome_unknown(write, 'Could not confirm the result with PayPal.') from exc
        if result is None:
            raise outcome_unknown(write, 'PayPal has no record of the write yet.')

    return _settle(step, write, result)


def _settle(step: WriteStep, write: ProviderWrite, result: Any) -> ProviderWrite:
    try:
        got = step.read(result)
    except ValueError as exc:
        raise outcome_unknown(write, 'PayPal answered in a form this site could not read.') from exc
    outcome = step.outcome_of(got)
    # Verify before keeping it: PayPal is authoritative for what happened,
    # not for what was asked.
    if step.sent is not None and outcome in (ProviderWrite.DONE, ProviderWrite.PENDING):
        sent_amount, sent_currency = step.sent
        if got.amount is None or got.amount != sent_amount or got.currency != sent_currency:
            message = 'PayPal reported %s %s where %s %s was asked for.' % (
                got.amount, got.currency, sent_amount, sent_currency)
            complete(write, ProviderWrite.NEEDS_REVIEW, got, error_message=message)
            logger.error('Amount mismatch on %s: %s', write.ref, message)
            return write
    return complete(write, outcome, got)


def _refresh(step: WriteStep, write: ProviderWrite) -> ProviderWrite:
    assert step.refresh is not None
    try:
        result = call_read(lambda: step.refresh(write))  # type: ignore[misc]
    except PaymentError:
        return write  # still pending as far as we know
    if result is None:
        return write
    return _settle(step, write, result)
