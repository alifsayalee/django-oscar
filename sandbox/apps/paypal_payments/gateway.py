"""
The one place this app talks to PayPal.

Everything here is built on the PayPal Server SDK (``paypal``):

* ``get_client`` - one long-lived sync client per process, configured only
  from Django settings.
* ``ProviderError`` / ``translate`` - one mapping from every failure the SDK
  can raise to the status this API answers with.
* ``*_outcome`` - PayPal's status enums sorted into done / pending / failed,
  with anything unlisted left ``unknown``.
* ``safe_write`` - the claim-call-check-verify-complete path every PayPal
  write that holds, takes, releases or returns money goes through.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from paypal import PaypalClient
from paypal.core import (
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
from paypal.models import Error
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    OrderStatus,
    RefundStatus,
)

from .models import ProviderWrite

logger = logging.getLogger("apps.paypal_payments")

# The only server the SDK declares. Any other environment must name its host
# through PAYPAL_BASE_URL rather than fall through to a default.
BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com"}

REPRESENTATION = "return=representation"

# How long PayPal de-duplicates a PayPal-Request-Id, per operation docstring
ORDERS_DEDUP_WINDOW = timedelta(hours=6)
VAULT_DEDUP_WINDOW = timedelta(hours=3)
PAYMENTS_DEDUP_WINDOW = timedelta(days=45)

# Failures raised before the request left: nothing can have reached PayPal
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

T = TypeVar("T")


# ------
# Client
# ------


class LoggingTransport:
    """Delegating transport that logs method, path, status and PayPal's debug id - never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = httpx.URL(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as e:
            logger.warning(
                "PayPal %s %s failed after %.0f ms: %s",
                request.method, path, (time.monotonic() - started) * 1000, type(e).__name__,
            )
            raise
        logger.info(
            "PayPal %s %s -> %s (%.0f ms, debug id %s)",
            request.method, path, response.status_code,
            (time.monotonic() - started) * 1000, response.headers.get("paypal-debug-id", "-"),
        )
        return response

    def close(self) -> None:
        self._inner.close()


_client_lock = threading.Lock()
_client: PaypalClient | None = None
_client_pid: int | None = None
_override: PaypalClient | None = None


def base_url() -> str:
    override: str = getattr(settings, "PAYPAL_BASE_URL", "") or ""
    if override:
        return override
    environment: str = getattr(settings, "PAYPAL_ENVIRONMENT", "") or ""
    try:
        return BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            "PAYPAL_ENVIRONMENT=%r has no known PayPal host; set PAYPAL_BASE_URL." % environment
        ) from None


def build_client(transport: HttpClient | None = None) -> PaypalClient:
    client_id: str = getattr(settings, "PAYPAL_CLIENT_ID", "") or ""
    client_secret: str = getattr(settings, "PAYPAL_CLIENT_SECRET", "") or ""
    if not client_id or not client_secret:
        raise ImproperlyConfigured("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must both be set.")
    timeout = float(getattr(settings, "PAYPAL_TIMEOUT", 20.0))
    return PaypalClient(
        base_url=base_url(),
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=timeout)),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


def get_client() -> PaypalClient:
    """The process-wide client, built lazily - after any fork - and reused."""
    global _client, _client_pid
    if _override is not None:
        return _override
    pid = os.getpid()
    client = _client
    if client is not None and _client_pid == pid:
        return client
    with _client_lock:
        if _client is None or _client_pid != pid:
            # A client inherited across fork() shares its pool with the parent: never reuse it.
            _client = build_client()
            _client_pid = pid
        return _client


@atexit.register
def _close_client() -> None:
    if _client is not None and _client_pid == os.getpid():
        _client.close()


@contextmanager
def client_override(client: PaypalClient) -> Iterator[PaypalClient]:
    """Route every call through ``client`` (used by tests with a stub transport)."""
    global _override
    previous, _override = _override, client
    try:
        yield client
    finally:
        _override = previous


# -----
# Money
# -----

# ISO 4217 minor units for every currency that does not have two
EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def quantize(value: Decimal, currency: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-EXPONENT.get(currency, 2)))


def money_str(value: Decimal, currency: str) -> str:
    return str(quantize(value, currency))


def present(value: T | UnsetType) -> T | None:
    """An SDK member, with UNSET turned into None before it leaves the gateway."""
    return None if isinstance(value, UnsetType) else value


def parse_time(value: str | UnsetType | None) -> datetime | None:
    if value is None or isinstance(value, UnsetType) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ------
# Errors
# ------


class ProviderError(Exception):
    """A PayPal failure, already mapped to the status this API answers with."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        code: str = "paypal_error",
        outcome_unknown: bool = False,
        issue: str | None = None,
        debug_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.issue = issue
        self.debug_id = debug_id


class OutcomeUnknown(ProviderError):
    def __init__(self, ref: str) -> None:
        super().__init__(
            504,
            "PayPal did not confirm the outcome. It may have happened; repeat the same request to check.",
            code="outcome_unknown",
            outcome_unknown=True,
        )
        self.ref = ref


class AmountMismatch(ProviderError):
    def __init__(self, ref: str, echoed: tuple[Decimal | None, str | None]) -> None:
        super().__init__(
            409,
            "PayPal reported an amount (%s %s) different from the one requested; flagged for review." % echoed,
            code="needs_review",
        )
        self.ref = ref


def error_summary(error: object) -> tuple[str, str | None, str | None]:
    """(message, issue, debug_id) from a decoded PayPal error body."""
    if isinstance(error, Error):
        details = present(error.details) or []
        issue = details[0].issue if details else None
        description = present(details[0].description) if details else None
        return (description or error.message), issue, error.debug_id
    return "PayPal rejected the request.", None, None


def provider_error(status: int, error: object) -> ProviderError:
    if isinstance(error, OAuthProviderError) or status in (401, 403):
        return ProviderError(502, "PayPal refused this application's credentials.", code="paypal_auth_failed")
    if status == 429:
        return ProviderError(503, "PayPal is rate-limiting this application; try again shortly.", code="paypal_rate_limited")
    if 400 <= status < 500:
        message, issue, debug_id = error_summary(error)
        return ProviderError(
            422 if status == 422 else (409 if status == 409 else 400 if status == 400 else 502),
            message, code="paypal_rejected", issue=issue, debug_id=debug_id,
        )
    if isinstance(error, RawError):
        return ProviderError(502, "PayPal is unavailable.", code="paypal_unavailable", outcome_unknown=status >= 500)
    message, issue, debug_id = error_summary(error)
    return ProviderError(502, "PayPal is unavailable.", code="paypal_unavailable",
                         outcome_unknown=status >= 500, issue=issue, debug_id=debug_id)


def translate(exc: BaseException) -> ProviderError:
    """Map any failure of an SDK call to a ProviderError (reads and first sends alike)."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        return provider_error(exc.status_code, exc.error)
    if isinstance(exc, NEVER_SENT):
        return ProviderError(502, "Could not reach PayPal; nothing was sent.", code="paypal_unreachable")
    if isinstance(exc, httpx.RequestError):
        return ProviderError(504, "PayPal did not answer in time.", code="paypal_timeout", outcome_unknown=True)
    if isinstance(exc, ValueError):  # pydantic ValidationError or a non-JSON body
        return ProviderError(502, "PayPal's response could not be read.", code="paypal_unreadable", outcome_unknown=True)
    raise exc


def call(fn: Callable[[], T], *, retries: int = 0) -> T:
    """Run a read, translating failures; reads (only) may be retried when transient."""
    attempt = 0
    while True:
        try:
            return fn()
        except (ApiError, httpx.RequestError, ValueError) as e:
            error = translate(e)
            transient = isinstance(e, httpx.RequestError) or (
                isinstance(e, ApiError) and e.status_code in (429, 500, 502, 503, 504)
            )
            if attempt < retries and transient:
                attempt += 1
                time.sleep(min(0.5 * 2 ** attempt, 4.0))
                continue
            raise error from e


def is_released(exc: BaseException) -> bool:
    """A first send that provably changed nothing at PayPal: never sent, or refused before acting."""
    if isinstance(exc, NEVER_SENT):
        return True
    if isinstance(exc, ApiError):
        return isinstance(exc.error, OAuthProviderError) or exc.status_code in (401, 403, 429)
    return False


# --------
# Outcomes
# --------


def authorization_outcome(status: object) -> str:
    """A hold on the shopper's money: done only while it is still a hold."""
    match status:
        case AuthorizationStatus.CREATED:
            return ProviderWrite.DONE
        case AuthorizationStatus.PENDING:
            return ProviderWrite.PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return ProviderWrite.FAILED
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return ProviderWrite.FAILED  # no longer a hold: taken since
        case _:
            return ProviderWrite.UNKNOWN


def order_outcome(status: object) -> str:
    """The Orders-API envelope when it carries no authorization yet."""
    match status:
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED:
            return ProviderWrite.PENDING  # accepted, not authorized: the authorize step follows
        case OrderStatus.PAYER_ACTION_REQUIRED | OrderStatus.VOIDED:
            return ProviderWrite.FAILED
        case _:
            return ProviderWrite.UNKNOWN


def pay_outcome(status: object) -> str:
    """Status of a create/authorize answer: ("authorization", s) or ("order", s)."""
    match status:
        case ("authorization", s):
            return authorization_outcome(s)
        case ("order", s):
            return order_outcome(s)
        case _:
            return ProviderWrite.UNKNOWN


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return ProviderWrite.DONE
        case CaptureStatus.PENDING:
            return ProviderWrite.PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return ProviderWrite.FAILED
        case CaptureStatus.PARTIALLY_REFUNDED | CaptureStatus.REFUNDED:
            return ProviderWrite.FAILED  # taken, then given back: not in effect
        case _:
            return ProviderWrite.UNKNOWN


def void_outcome(status: object) -> str:
    """Releasing a hold: its done is the released state."""
    match status:
        case AuthorizationStatus.VOIDED | AuthorizationStatus.DENIED:
            return ProviderWrite.DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return ProviderWrite.FAILED  # too late: the money was taken
        case _:
            return ProviderWrite.UNKNOWN


def refund_outcome(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return ProviderWrite.DONE
        case RefundStatus.PENDING:
            return ProviderWrite.PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return ProviderWrite.FAILED
        case _:
            return ProviderWrite.UNKNOWN


VAULTED = "VAULTED"


def token_outcome(status: object) -> str:
    """A payment token carries no status member: done only when id and card came back."""
    return ProviderWrite.DONE if status == VAULTED else ProviderWrite.UNKNOWN


# ----------
# Safe write
# ----------


class Answer(NamedTuple):
    """What one write step's response says, read the same way for every step."""

    provider_id: str | None
    status: object
    provider_time: datetime | None
    amount: str | None = None
    currency: str | None = None
    data: dict[str, Any] = {}


SEND_WINDOW = timedelta(seconds=90)  # longer than the transport timeout


def request_id_for(ref: str) -> str:
    """The PayPal-Request-Id for a reference: derived, so every attempt sends the same one."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "paypal-write:" + ref))


def try_claim(
    ref: str, operation: str, order: Any, sent: tuple[Decimal, str] | None, data: dict[str, Any]
) -> tuple[ProviderWrite, bool]:
    """Insert-or-fail on the unique ``ref``; committed before any PayPal call."""
    try:
        with transaction.atomic():
            record = ProviderWrite.objects.create(
                ref=ref,
                request_id=request_id_for(ref),
                operation=operation,
                order=order,
                outcome=ProviderWrite.SENDING,
                amount=sent[0] if sent else None,
                currency=sent[1] if sent else "",
                claimed_at=timezone.now(),
                data=data,
            )
        return record, True
    except IntegrityError:
        return ProviderWrite.objects.get(ref=ref), False


def _take_over(record: ProviderWrite) -> bool:
    """Atomically move a stale or unknown claim back to 'sending' for this request only."""
    return (
        ProviderWrite.objects.filter(
            pk=record.pk, outcome=record.outcome, claimed_at=record.claimed_at
        ).update(outcome=ProviderWrite.SENDING, claimed_at=timezone.now())
        == 1
    )


def complete(
    record: ProviderWrite,
    outcome: str,
    answer: Answer | None = None,
    detail: str = "",
) -> ProviderWrite:
    if outcome == ProviderWrite.FAILED and (answer is None or not answer.provider_id) and record.pk and not detail:
        detail = "PayPal did not act on the request."
    record.outcome = outcome
    record.completed_at = timezone.now()
    if answer is not None:
        record.provider_id = answer.provider_id or record.provider_id
        record.provider_status = _status_text(answer.status)
        record.provider_time = answer.provider_time or record.provider_time
        record.data = {**record.data, **answer.data}
    if detail:
        record.detail = detail[:1000]
    record.save()
    return record


def release(record: ProviderWrite, detail: str) -> ProviderWrite:
    """A first send that changed nothing: free the reference so the next request may claim it."""
    ProviderWrite.objects.filter(pk=record.pk).delete()
    record.outcome = ProviderWrite.FAILED
    record.detail = detail
    return record


def _status_text(status: object) -> str:
    if isinstance(status, tuple):
        return ":".join(str(part) for part in status)
    return "" if status is None else str(status)


def safe_write(
    *,
    ref: str,
    operation: str,
    send: Callable[[str], T],
    read: Callable[[T], Answer],
    outcome_of: Callable[[object], str],
    dedup_window: timedelta,
    sent: tuple[Decimal, str] | None = None,
    order: Any = None,
    refresh: Callable[[ProviderWrite], Answer] | None = None,
    claim_data: dict[str, Any] | None = None,
) -> ProviderWrite:
    """
    The one path for every PayPal write that holds, takes, releases or returns money.

    ref        derived from the operation and the step: the same on every attempt and repeat
    send(rid)  makes the call with ``rid`` as its PayPal-Request-Id. PayPal returns the original
               result for a repeated id inside ``dedup_window``, so a same-id resend is the lookup
    read       this step's response as an Answer
    refresh    re-reads a ``pending`` result by the provider id stored for it
    """
    record, won = try_claim(ref, operation, order, sent, claim_data or {})
    checking = False
    if not won:
        now = timezone.now()
        if record.outcome == ProviderWrite.SENDING and record.claimed_at > now - SEND_WINDOW:
            return record  # in flight elsewhere: answer "in progress", make no call
        if record.outcome == ProviderWrite.PENDING and refresh is not None and record.provider_id:
            return _refresh(record, refresh, outcome_of)
        if record.outcome not in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN):
            return record  # settled: answer from it
        if not _take_over(record):
            return ProviderWrite.objects.get(pk=record.pk)
        record.refresh_from_db()
        checking = True
        if record.created_at < now - dedup_window:
            # Past PayPal's de-duplication window a resend could make a second one: only an operator settles it.
            complete(record, ProviderWrite.UNKNOWN, detail="Outcome unconfirmed and past PayPal's de-duplication window; check PayPal before acting.")
            raise OutcomeUnknown(ref)

    result: T | None = None
    try:
        result = send(record.request_id)
    except BaseException as e:
        if not isinstance(e, (ApiError, httpx.RequestError, ValueError)):
            complete(record, ProviderWrite.UNKNOWN, detail="Interrupted: %s" % type(e).__name__)
            raise
        if checking:
            if isinstance(e, ApiError) and 400 <= e.status_code < 500 and not is_released(e):
                # The same PayPal-Request-Id refused now means the first attempt never landed either.
                message, issue, _ = error_summary(e.error)
                complete(record, ProviderWrite.FAILED, detail=_detail(issue, message))
                raise translate(e) from e
            complete(record, ProviderWrite.UNKNOWN)  # a check that fails says nothing
            raise OutcomeUnknown(ref) from e
        if is_released(e):
            release(record, detail=type(e).__name__)
            raise translate(e) from e
        if isinstance(e, ApiError) and e.status_code < 500:
            message, issue, _ = error_summary(e.error)
            complete(record, ProviderWrite.FAILED, detail=_detail(issue, message))
            raise translate(e) from e
        # A 5xx, a timeout after sending, or an unreadable 2xx: it may have landed. Check by resending
        # under the same PayPal-Request-Id, which PayPal answers with the original result.
        try:
            result = send(record.request_id)
        except (ApiError, httpx.RequestError, ValueError) as check_error:
            complete(record, ProviderWrite.UNKNOWN)
            raise OutcomeUnknown(ref) from check_error

    got = read(result)
    outcome = outcome_of(got.status)
    if outcome == ProviderWrite.DONE and (not got.provider_id or (sent is not None and got.amount is None)):
        # "Done" with nothing to name it by, or no amount to verify: it may have happened, unconfirmed.
        complete(record, ProviderWrite.UNKNOWN, got, detail="PayPal's answer lacked the id or amount needed to confirm it.")
        raise OutcomeUnknown(ref)
    if sent is not None and got.amount is not None:
        echoed = (None if got.amount is None else Decimal(got.amount), got.currency)
        if echoed != (sent[0], sent[1]):
            complete(record, ProviderWrite.NEEDS_REVIEW, got, detail="Amount echoed by PayPal differs from the amount sent.")
            raise AmountMismatch(ref, echoed)
    return complete(record, outcome, got)


def _refresh(record: ProviderWrite, refresh: Callable[[ProviderWrite], Answer], outcome_of: Callable[[object], str]) -> ProviderWrite:
    try:
        got = refresh(record)
    except (ApiError, httpx.RequestError, ValueError):
        logger.warning("Could not refresh pending PayPal %s %s", record.operation, record.provider_id)
        return record
    return complete(record, outcome_of(got.status), got)


def _detail(issue: str | None, message: str) -> str:
    return "%s: %s" % (issue, message) if issue else message
