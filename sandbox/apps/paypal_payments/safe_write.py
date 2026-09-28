"""
The safe write: every PayPal call that creates, charges, captures, voids,
refunds, vaults or deletes goes through ``safe_write``.

1. CLAIM  - insert a ``ProviderWrite`` row under a reference derived from the
            operation; the unique constraint rejects a second claim, in any
            process.
2. CALL   - send, carrying the reference as ``PayPal-Request-Id``.
3. CHECK  - when the answer is lost, ask PayPal again under the SAME reference
            (PayPal returns the original result for a repeated request id).
4. VERIFY - compare the echoed amount with what was asked, as Decimal.
5. COMPLETE from the status PayPal reported, never from the call returning.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal.core import ApiError

from .gateway import NEVER_SENT, PaymentError, describe, provider_error, same_money
from .models import InstallIdentity, ProviderWrite

logger = logging.getLogger("apps.paypal_payments")

DONE = ProviderWrite.DONE
PENDING = ProviderWrite.PENDING
FAILED = ProviderWrite.FAILED
NEEDS_REVIEW = ProviderWrite.NEEDS_REVIEW
UNKNOWN = ProviderWrite.UNKNOWN
SENDING = ProviderWrite.SENDING


@dataclass(frozen=True)
class Answer:
    """What one step's response says, read the same way for every step."""

    provider_id: str
    status: Any  # the step's own status member(s); ``outcome_of`` maps it
    provider_time: datetime | None
    amount: object = None
    currency: object = None
    status_label: str = ""


@dataclass
class WriteResult:
    record: ProviderWrite
    # The SDK response this request received, or None when the outcome was
    # answered from the stored record without a provider call.
    result: Any = None

    @property
    def outcome(self) -> str:
        return self.record.outcome


class OutcomeUnknown(PaymentError):
    def __init__(self, ref: str) -> None:
        super().__init__(
            504,
            "The payment provider's answer was lost; the operation may have gone through. "
            "Repeat the same request to settle it - it will not be applied twice.",
            code="outcome_unknown",
            outcome_unknown=True,
        )
        self.ref = ref


class AmountMismatch(PaymentError):
    def __init__(self, ref: str) -> None:
        super().__init__(
            409,
            "The payment provider processed a different amount than requested; an operator must review it.",
            code="amount_mismatch",
        )
        self.ref = ref


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def install_prefix() -> str:
    configured = (settings.PAYPAL_REFERENCE_PREFIX or "").strip()
    if configured:
        return configured
    identity = InstallIdentity.objects.first()
    if identity is None:
        try:
            with transaction.atomic():
                identity = InstallIdentity.objects.create(install_id=uuid.uuid4().hex[:12])
        except IntegrityError:
            identity = InstallIdentity.objects.first()
        assert identity is not None
    return f"osc{identity.install_id}"


def deterministic_ref(*parts: object) -> str:
    """``<install prefix>-<parts...>``: the same on every attempt and every repeat."""
    ref = "-".join([install_prefix(), *(str(p) for p in parts)])
    if len(ref) > 100:
        raise ValueError("reference too long")
    return ref


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def provider_time(value: object) -> datetime | None:
    """PayPal's RFC 3339 timestamps -> aware datetimes (None when absent)."""
    if not isinstance(value, str):
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# The claim store (the site's own database)
# ---------------------------------------------------------------------------


def send_window() -> timedelta:
    # Longer than one attempt can take, so a live sender is never overtaken.
    return timedelta(seconds=float(settings.PAYPAL_TIMEOUT) * 2 + 10)


def try_claim(ref: str, operation: str) -> bool:
    """Insert-or-fail. Commits immediately, before any provider call."""
    try:
        with transaction.atomic():
            ProviderWrite.objects.create(ref=ref, operation=operation, claimed_at=timezone.now())
        return True
    except IntegrityError:
        return False


def load_existing(ref: str) -> ProviderWrite:
    return ProviderWrite.objects.get(ref=ref)


def complete(
    ref: str,
    outcome: str,
    provider_id: str = "",
    provider_time: datetime | None = None,
    status_label: str = "",
    detail: str = "",
) -> ProviderWrite:
    """Record what is known. A ``failed`` with no provider record releases the
    claim, so the next request may claim it again."""
    record = ProviderWrite.objects.get(ref=ref)
    record.outcome = outcome
    if provider_id:
        record.provider_id = provider_id
    if provider_time is not None:
        record.provider_time = provider_time
    if status_label:
        record.provider_status = status_label
    record.detail = detail
    if outcome == FAILED and not record.provider_id:
        record.delete()
        return record
    record.save()
    return record


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def safe_write(
    ref: str,
    operation: str,
    *,
    send: Callable[[str], Any],
    read: Callable[[Any], Answer],
    outcome_of: Callable[[Any], str],
    find: Callable[[ProviderWrite], Any] | None = None,
    repeat_is_safe: bool = True,
    sent: tuple[Decimal, str] | None = None,
    apply: Callable[[ProviderWrite, Any], None] | None = None,
) -> WriteResult:
    """
    ref             derived from the operation and the step
    send(key)       makes the PayPal call with ``key`` as PayPal-Request-Id
    read(result)    that step's response as an ``Answer``
    outcome_of      maps ``Answer.status`` to done / pending / failed / unknown
    find(record)    reads the current PayPal record for a claim that already
                    has a provider id (refreshing a pending one), or None
    repeat_is_safe  PayPal returns the original result for a repeated request
                    id, so the check for an unknown outcome is a resend
    sent            (amount, currency) asked for; None when no money moves
    apply(record, result)
                    records the answer on the domain models, in the same
                    transaction that completes the claim
    """
    checking = False
    if not try_claim(ref, operation):
        existing = load_existing(ref)
        if existing.outcome == SENDING and existing.claimed_at > timezone.now() - send_window():
            return WriteResult(existing)  # in flight elsewhere: answer "in progress", no provider call
        if existing.outcome == PENDING and existing.provider_id and find is not None:
            return _refresh(existing, find, read, outcome_of, apply)
        if existing.outcome not in (SENDING, UNKNOWN):
            return WriteResult(existing)  # done / pending / failed-with-record / needs_review
        checking = True  # stale sender or unresolved: settle it, never create anew

    resending = checking and repeat_is_safe
    result: Any = None
    if resending or not checking:
        try:
            result = send(ref)
        except NEVER_SENT as exc:
            complete(ref, UNKNOWN if resending else FAILED, detail=type(exc).__name__)
            if resending:
                raise OutcomeUnknown(ref) from exc
            raise PaymentError(502, "Could not reach the payment provider; nothing was charged.",
                               code="provider_unreachable") from exc
        except ApiError as exc:
            detail = describe(exc.status_code, exc.error)
            if exc.status_code < 500:
                if resending:
                    complete(ref, UNKNOWN, detail=detail)
                    raise OutcomeUnknown(ref) from exc
                complete(ref, FAILED, detail=detail)
                logger.info("PayPal refused %s (%s): %s", operation, ref, detail)
                raise provider_error(exc.status_code, exc.error) from exc
            logger.warning("PayPal %s (%s) answered %s", operation, ref, detail)
            # a 5xx may still have landed: fall through to the check
        except (httpx.RequestError, ValueError) as exc:
            logger.warning("PayPal %s (%s): no readable answer (%s)", operation, ref, type(exc).__name__)
            # sent, and no readable answer: may have landed

    if result is None:
        # CHECK, here: a same-reference resend (PayPal replays the original).
        if not repeat_is_safe:
            existing = load_existing(ref)
            if existing.provider_id and find is not None:
                return _refresh(existing, find, read, outcome_of, apply)
            complete(ref, UNKNOWN)
            raise OutcomeUnknown(ref)
        try:
            result = send(ref)
        except (ApiError, httpx.RequestError, ValueError) as exc:
            complete(ref, UNKNOWN, detail=type(exc).__name__)
            raise OutcomeUnknown(ref) from exc
        if result is None:
            complete(ref, UNKNOWN)
            raise OutcomeUnknown(ref)

    return _settle(ref, result, read, outcome_of, sent, apply)


def _settle(
    ref: str,
    result: Any,
    read: Callable[[Any], Answer],
    outcome_of: Callable[[Any], str],
    sent: tuple[Decimal, str] | None,
    apply: Callable[[ProviderWrite, Any], None] | None,
) -> WriteResult:
    got = read(result)
    outcome = outcome_of(got.status)
    detail = ""
    if sent is not None and outcome in (DONE, PENDING):
        # VERIFY before keeping it: PayPal is authoritative for what happened,
        # not for what was asked.
        amount, currency = sent
        if got.amount is None:
            outcome, detail = UNKNOWN, "response carried no amount to verify"
        elif not same_money(amount, currency, got.amount, got.currency):
            outcome = NEEDS_REVIEW
            detail = f"asked {amount} {currency}, PayPal echoed {got.amount} {got.currency}"
            logger.error("PayPal amount mismatch on %s: %s", ref, detail)
    with transaction.atomic():
        record = complete(ref, outcome, got.provider_id, got.provider_time, got.status_label, detail)
        if apply is not None:
            apply(record, result)
    if outcome == NEEDS_REVIEW and detail:
        raise AmountMismatch(ref)
    return WriteResult(record, result)


def _refresh(
    existing: ProviderWrite,
    find: Callable[[ProviderWrite], Any],
    read: Callable[[Any], Answer],
    outcome_of: Callable[[Any], str],
    apply: Callable[[ProviderWrite, Any], None] | None,
) -> WriteResult:
    """Re-read a claim that already names a PayPal record (pending, or unknown
    with an id) and settle it from PayPal's current status."""
    try:
        result = find(existing)
    except (ApiError, httpx.RequestError, ValueError) as exc:
        logger.warning("PayPal lookup for %s failed (%s)", existing.ref, type(exc).__name__)
        if existing.outcome == PENDING:
            return WriteResult(existing)  # still pending as far as anyone knows
        complete(existing.ref, UNKNOWN)
        raise OutcomeUnknown(existing.ref) from exc
    if result is None:
        return WriteResult(existing)
    got = read(result)
    with transaction.atomic():
        record = complete(existing.ref, outcome_of(got.status), got.provider_id or existing.provider_id,
                          got.provider_time, got.status_label)
        if apply is not None:
            apply(record, result)
    return WriteResult(record, result)
