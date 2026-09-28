"""
The one path every PayPal write goes through: claim, call, check, verify, complete.

* CLAIM - a ``PayPalWrite`` row is INSERTed (and committed) under the step's
  reference before PayPal is called. The database's UNIQUE constraint on
  ``ref`` rejects a second claim, whichever process or thread it comes from.
* CALL - the reference travels as ``PayPal-Request-Id``; PayPal returns the
  original result for a repeat under the same id (within its retention window).
* CHECK - when the outcome of an earlier attempt is unknown, a later request
  re-sends under the SAME reference (never a new one). A pending result is
  re-read by its PayPal id.
* VERIFY - the amount PayPal echoes must equal the amount we asked for.
* COMPLETE - the outcome comes from the step's own status, mapped by name.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone
from paypal.core import ApiError, OAuthProviderError, UnsetType

from . import gateway
from .gateway import DONE, FAILED, NEVER_SENT, PENDING, UNKNOWN, Answer, ProviderError
from .models import PayPalWrite

# Longer than one call's worst case (one timeout, no retries), so a claim still
# "sending" after this is a sender that died, not one still waiting on PayPal.
SEND_WINDOW = timedelta(minutes=2)

# How long PayPal keeps a PayPal-Request-Id (per operation docstring), i.e. how
# long a same-reference resend is still collapsed onto the original.
KEY_WINDOW = {
    PayPalWrite.AUTHORIZE: timedelta(hours=6),
    PayPalWrite.REAUTHORIZE: timedelta(days=45),
    PayPalWrite.CAPTURE: timedelta(days=45),
    PayPalWrite.VOID: timedelta(days=45),
    PayPalWrite.REFUND: timedelta(days=45),
    PayPalWrite.SAVE_CARD: timedelta(hours=3),
    PayPalWrite.DELETE_CARD: timedelta(days=3650),  # a DELETE by the token's own id is idempotent
}


class OutcomeUnknown(Exception):
    """The write may or may not have happened at PayPal; a later request re-checks it."""

    def __init__(self, record: PayPalWrite) -> None:
        super().__init__(record.ref)
        self.record = record


class WriteRefused(Exception):
    """PayPal definitively refused the write (nothing happened)."""

    def __init__(self, record: PayPalWrite, error: ProviderError) -> None:
        super().__init__(error.message)
        self.record = record
        self.error = error


@dataclass
class WriteResult:
    record: PayPalWrite
    result: Any = None  # PayPal's response read by THIS request; None when answered from the record
    answer: Answer | None = None

    @property
    def outcome(self) -> str:
        return self.record.outcome


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def try_claim(ref: str, step: str, **fields: Any) -> bool:
    """Insert-or-fail. Exactly one caller gets True for a given ref."""
    try:
        with transaction.atomic():
            PayPalWrite.objects.create(ref=ref, step=step, outcome=PayPalWrite.SENDING, **fields)
    except IntegrityError:
        return False
    return True


def load_existing(ref: str) -> PayPalWrite:
    return PayPalWrite.objects.get(ref=ref)


def complete(ref: str, outcome: str, answer: Answer | None = None, detail: str = '') -> PayPalWrite:
    with transaction.atomic():
        record = PayPalWrite.objects.select_for_update().get(ref=ref)
        record.outcome = outcome
        if answer is not None:
            record.provider_id = answer.provider_id or record.provider_id
            record.provider_status = '' if isinstance(answer.status, UnsetType) else str(answer.status)[:64]
            record.provider_time = answer.provider_time or record.provider_time
            detail = detail or answer.detail
        record.detail = detail
        record.save()
    return record


def attempt_ref(*parts: object) -> str:
    """
    The reference for the next attempt of a step: the base reference plus the
    number of earlier attempts PayPal definitively refused. A repeat of an
    attempt still in flight derives the same reference (and is rejected by the
    claim); only after a definite failure does a new attempt get a new one.
    """
    base = gateway.deterministic_ref(*parts)
    failed = PayPalWrite.objects.filter(ref__startswith=base + '-', outcome=PayPalWrite.FAILED).count()
    return '%s-%d' % (base, failed)


# ---------------------------------------------------------------------------
# The safe write
# ---------------------------------------------------------------------------

def safe_write(
    *,
    ref: str,
    step: str,
    send: Callable[[str], Any],
    read: Callable[[Any], Answer],
    outcome_of: Callable[[object], str],
    sent: tuple[Decimal, str] | None = None,
    lookup: Callable[[str], Any] | None = None,
    on_refused: Callable[[ApiError[Any]], Any] | None = None,
    refusal: Callable[[ApiError[Any]], ProviderError] | None = None,
    apply: Callable[[PayPalWrite, Any, Answer], None] | None = None,
    claimed: bool | None = None,
    claim_fields: dict[str, Any] | None = None,
) -> WriteResult:
    """
    ref         derived from the operation and step - the same on every attempt and every repeat
    send(ref)   makes the PayPal call with ``ref`` as PayPal-Request-Id
    read        this step's response as an Answer
    outcome_of  this step's status mapper (done / pending / failed / unknown)
    sent        (amount, currency) asked for; the echoed amount must match
    lookup(id)  re-reads a pending result by its PayPal id
    on_refused  after a 4xx on the FIRST send: may look the record up by its own id and return it
    refusal     maps a 4xx refusal to the caller's error (default: gateway.provider_error)
    apply       updates the domain in the same DB transaction that records the outcome
    claimed     the caller already took the claim (True) or lost it (False) in its own transaction
    """
    if claimed is None:
        claimed = try_claim(ref, step, **(claim_fields or {}))

    result: Any = None
    resending = False
    if not claimed:
        existing = load_existing(ref)
        if existing.outcome == PayPalWrite.SENDING and existing.claimed_at > timezone.now() - SEND_WINDOW:
            return WriteResult(existing)  # in flight: answer "in progress", no PayPal call
        if existing.outcome in (PayPalWrite.DONE, PayPalWrite.FAILED, PayPalWrite.NEEDS_REVIEW):
            return WriteResult(existing)
        if existing.outcome == PayPalWrite.PENDING:
            if lookup is None or not existing.provider_id:
                return WriteResult(existing)
            try:
                result = lookup(existing.provider_id)
            except (ApiError, httpx.RequestError, ValueError) as e:
                gateway.log.warning('PayPal re-read of %s failed: %s', ref, gateway.describe(e))
                return WriteResult(existing)  # a failed re-read settles nothing: still pending
        else:
            # A dead sender or an unknown outcome: re-send under the SAME reference, but only
            # while PayPal still collapses it onto the original.
            if existing.claimed_at < timezone.now() - KEY_WINDOW[step]:
                record = complete(ref, PayPalWrite.UNKNOWN, detail=(
                    'Outcome unknown and PayPal no longer de-duplicates %s; verify it in the PayPal '
                    'dashboard before acting.' % ref))
                raise OutcomeUnknown(record)
            resending = True

    if result is None:
        try:
            result = send(ref)
        except NEVER_SENT as e:
            if resending:
                raise OutcomeUnknown(complete(ref, PayPalWrite.UNKNOWN, detail='PayPal unreachable on re-check.')) from e
            complete(ref, PayPalWrite.FAILED, detail='PayPal unreachable; the request was never sent.')
            raise ProviderError(502, 'paypal_unreachable',
                                'Could not reach PayPal; nothing was sent.') from e
        except ApiError as e:
            if isinstance(e.error, OAuthProviderError):
                # The token fetch failed before the write was sent: nothing happened.
                if not resending:
                    complete(ref, PayPalWrite.FAILED, detail='PayPal refused our credentials.')
                raise gateway.provider_error(e.status_code, e.error) from e
            if e.status_code < 500 and not resending:
                looked_up = on_refused(e) if on_refused is not None else None
                if looked_up is None:
                    error = (refusal or (lambda x: gateway.provider_error(x.status_code, x.error)))(e)
                    record = complete(ref, PayPalWrite.FAILED, detail=gateway.error_issue(e.error) or error.message)
                    raise WriteRefused(record, error) from e
                result = looked_up
            else:
                # A 5xx may have landed; a 4xx on a re-check settles nothing.
                gateway.log.warning('PayPal write %s: %s', ref, gateway.describe(e))
                raise OutcomeUnknown(complete(ref, PayPalWrite.UNKNOWN, detail=(
                    'PayPal answered HTTP %s; the write may have happened.' % e.status_code))) from e
        except (httpx.RequestError, ValueError) as e:
            # Sent, and no readable answer (timeout after send, dropped connection, undecodable 2xx).
            gateway.log.warning('PayPal write %s: %s', ref, gateway.describe(e))
            raise OutcomeUnknown(complete(ref, PayPalWrite.UNKNOWN, detail=(
                'No readable answer from PayPal (%s); the write may have happened.' % type(e).__name__))) from e

    got = read(result)
    outcome = outcome_of(got.status)
    detail = ''
    if sent is not None and outcome in (DONE, PENDING):
        sent_amount, sent_currency = sent
        echoed = (got.amount, got.currency)
        if echoed != (Decimal(sent_amount), sent_currency):
            outcome = PayPalWrite.NEEDS_REVIEW
            detail = 'PayPal reported %s %s, but %s %s was requested.' % (
                got.amount, got.currency, sent_amount, sent_currency)

    with transaction.atomic():
        record = complete(ref, outcome, got, detail)
        if apply is not None:
            apply(record, result, got)
    return WriteResult(record, result, got)


__all__ = [
    'DONE', 'FAILED', 'PENDING', 'UNKNOWN', 'OutcomeUnknown', 'WriteRefused', 'WriteResult',
    'attempt_ref', 'complete', 'load_existing', 'safe_write', 'try_claim',
]
