"""
The one path every Maxio write that creates something goes through:
claim, call, check, verify, complete.

* CLAIM  - insert a ``MaxioWrite`` row keyed by a reference derived from the
           operation. The UNIQUE column rejects every second claim, in any
           process. The row is committed before Maxio is called.
* CALL   - send the write carrying that same reference.
* CHECK  - when the outcome is unknown (timeout, 5xx, unreadable 2xx), look
           the write up at Maxio by the reference - here, not later.
* VERIFY - the provider is authoritative for what happened, not for what we
           asked: a mismatch is recorded as ``needs_review``.
* COMPLETE - record the outcome the provider *said*.

A request that loses the claim never makes a new write. It answers from the
stored row, or - when that row is stale or unknown - only checks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Generic, TypeVar

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.core import ApiError

from .errors import NEVER_SENT, InProgress, OutcomeUnknown, from_provider_error
from .models import MaxioWrite

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger("apps.subscriptions")

R = TypeVar("R")

# Longer than the worst case of one write attempt (timeout) plus its lookup.
SEND_WINDOW = timedelta(minutes=2)


@dataclass(frozen=True)
class Answer:
    """What one write's response says, read the same way for every write."""

    provider_id: int | None
    outcome: str  # already mapped: done / pending / failed / unknown
    provider_state: str = ""
    provider_time: datetime | None = None
    mismatch: str = ""  # non-empty: it happened, but not as asked


@dataclass(frozen=True)
class WriteSpec(Generic[R]):
    reference: str
    kind: str
    user: User
    send: Callable[[str], R]  # makes the Maxio call carrying the reference
    find: Callable[[str], R | None]  # looks the write up by reference; None = not found
    read: Callable[[R], Answer]
    plan_handle: str = ""
    # Maxio refuses a second write with this reference, so a same-reference
    # resend is itself a safe check.
    repeat_is_safe: bool = False
    # An ApiError meaning "an earlier attempt with this reference landed".
    is_duplicate: Callable[[ApiError], bool] = lambda exc: False


# --- the claim store ---------------------------------------------------------

def try_claim(spec: "WriteSpec[R]") -> bool:
    """Insert-or-fail. The UNIQUE constraint decides the winner."""
    try:
        with transaction.atomic():
            MaxioWrite.objects.create(
                reference=spec.reference,
                kind=spec.kind,
                user=spec.user,
                plan_handle=spec.plan_handle,
                outcome=MaxioWrite.SENDING,
                claimed_at=timezone.now(),
            )
    except IntegrityError:
        return False
    return True


def load_existing(reference: str) -> MaxioWrite:
    return MaxioWrite.objects.get(reference=reference)


def complete(
    reference: str,
    outcome: str,
    provider_id: int | None = None,
    provider_state: str = "",
    provider_time: datetime | None = None,
    detail: str = "",
) -> MaxioWrite:
    """
    Record the outcome. A ``failed`` with no provider id (never sent, or
    refused) releases the claim: nothing exists at Maxio, so the next request
    may claim the same reference again.
    """
    with transaction.atomic():
        record = MaxioWrite.objects.select_for_update().get(reference=reference)
        if outcome == MaxioWrite.FAILED and provider_id is None and record.provider_id is None:
            record.delete()
            record.outcome = outcome
            record.detail = detail
            return record
        record.outcome = outcome
        if provider_id is not None:
            record.provider_id = provider_id
        if provider_state:
            record.provider_state = provider_state
        if provider_time is not None:
            record.provider_time = provider_time
        record.detail = detail
        record.save()
        return record


# --- the safe write ----------------------------------------------------------

def safe_write(spec: "WriteSpec[R]") -> MaxioWrite:
    ref = spec.reference
    checking = False

    # 1. CLAIM FIRST.
    if not try_claim(spec):
        existing = load_existing(ref)
        if existing.outcome == MaxioWrite.SENDING and existing.claimed_at > timezone.now() - SEND_WINDOW:
            raise InProgress(ref)  # in flight: make no provider call
        if existing.outcome not in (MaxioWrite.SENDING, MaxioWrite.UNKNOWN):
            return existing  # done / pending / failed / needs_review: answer from it
        checking = True  # stale sender or unresolved: LOOK, never create anew

    resending = checking and spec.repeat_is_safe
    result: R | None = None

    if not checking:
        # A fresh claim: adopt a record an earlier database/install with the
        # same reference prefix already created, instead of duplicating it.
        # Nothing has been sent yet, so any failure here releases the claim.
        try:
            result = spec.find(ref)
        except (ApiError, httpx.RequestError, ValueError) as exc:
            complete(ref, MaxioWrite.FAILED, detail="pre-send lookup failed")
            raise from_provider_error(exc) from exc

    # 2. CALL - a first attempt, or a check by same-reference resend.
    if result is None and (resending or not checking):
        try:
            result = spec.send(ref)
        except NEVER_SENT as exc:
            if resending:
                complete(ref, MaxioWrite.UNKNOWN, detail="check never sent")
                raise OutcomeUnknown(ref) from exc
            complete(ref, MaxioWrite.FAILED, detail="never sent")
            raise from_provider_error(exc, write=True) from exc
        except ApiError as exc:
            if spec.is_duplicate(exc):
                pass  # an earlier attempt landed: settle by lookup below
            elif exc.status_code < 500:
                refused = exc.status_code in (400, 422) or not resending
                if refused:
                    complete(ref, MaxioWrite.FAILED, detail=f"refused: HTTP {exc.status_code}")
                    raise from_provider_error(exc, write=True) from exc
                complete(ref, MaxioWrite.UNKNOWN, detail=f"check got HTTP {exc.status_code}")
                raise OutcomeUnknown(ref) from exc
            else:
                logger.warning("Maxio HTTP %s on write %s; checking by reference", exc.status_code, ref)
        except (httpx.RequestError, ValueError) as exc:
            # Sent, and no readable answer: it may have landed.
            logger.warning("No readable answer for write %s (%s); checking", ref, type(exc).__name__)

    # 3. CHECK, by the reference we sent.
    if result is None:
        try:
            result = spec.find(ref)
        except (ApiError, httpx.RequestError, ValueError) as exc:
            complete(ref, MaxioWrite.UNKNOWN, detail="lookup failed")
            raise OutcomeUnknown(ref) from exc
        if result is None:
            # Not found YET: an empty lookup cannot prove it did not happen.
            complete(ref, MaxioWrite.UNKNOWN, detail="not found by reference")
            raise OutcomeUnknown(ref)

    answer = spec.read(result)
    if answer.provider_id is None:
        complete(ref, MaxioWrite.UNKNOWN, detail="response carried no id")
        raise OutcomeUnknown(ref)

    # 4. VERIFY before keeping it.
    if answer.mismatch:
        logger.error("Maxio write %s landed but not as asked: %s", ref, answer.mismatch)
        return complete(
            ref, MaxioWrite.NEEDS_REVIEW, answer.provider_id, answer.provider_state,
            answer.provider_time, detail=answer.mismatch,
        )

    # 5. COMPLETE from what the provider said.
    return complete(ref, answer.outcome, answer.provider_id, answer.provider_state, answer.provider_time)
