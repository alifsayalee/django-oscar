"""
The safe write: claim, call, check, verify, complete.

Every Maxio write that creates something goes through ``safe_write``, once per
write step, with a reference derived from the operation. The claim is a row
with a unique reference, committed *before* the provider call, so a second
request for the same operation -- a double-click, a retry, another worker --
is rejected by the database and answers from what is recorded instead of
creating a second customer or subscription. A write whose answer was lost is
looked up at the provider by that same reference; a timer never settles it.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Generic, TypeVar

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.core import ApiError

from .errors import NEVER_SENT, OutcomeUnknown, translate
from .models import MaxioWriteClaim

logger = logging.getLogger('apps.subscriptions.maxio')

T = TypeVar('T')


class Unreadable(ValueError):
    """A 2xx whose body lacks what we must know (e.g. the id): the outcome is unknown."""


@dataclass(frozen=True)
class Answer:
    """What one write step's provider response says, read the same way for every step."""

    provider_id: str
    outcome: str                      # already mapped by status_from_provider (or DONE for a status-less resource)
    provider_state: str = ''
    provider_time: datetime | None = None
    mismatch: str | None = None       # it happened, but not as asked
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WriteResult:
    record: MaxioWriteClaim
    replayed: bool                    # answered from an earlier request's record


@dataclass(frozen=True)
class WriteStep(Generic[T]):
    """
    One provider write step.

    send(reference)  makes the provider call carrying ``reference``
    find(reference)  the provider's record carrying ``reference``, or None when it has none
    read(result)     the step's response as an ``Answer``; raises ``Unreadable`` when it cannot
    repeat_is_safe   the provider will not create a second one for the same reference, so a
                     same-reference resend is a safe check
    """

    kind: str
    reference: str
    send: Callable[[str], T]
    find: Callable[[str], T | None]
    read: Callable[[T], Answer]
    repeat_is_safe: bool = False


def send_window() -> timedelta:
    """How long a 'sending' claim is presumed in flight: longer than one call can take (no retries)."""
    return timedelta(seconds=max(60.0, float(settings.MAXIO_TIMEOUT) * 3))


# -- the claim store ---------------------------------------------------------

def try_claim(step: WriteStep[Any], user: Any, plan_handle: str) -> bool:
    """Insert-or-fail: exactly one caller gets True for a reference."""
    try:
        with transaction.atomic():
            MaxioWriteClaim.objects.create(
                reference=step.reference, kind=step.kind, user=user, plan_handle=plan_handle,
                outcome=MaxioWriteClaim.SENDING, claimed_at=timezone.now())
    except IntegrityError:
        return False
    return True


def load_existing(reference: str) -> MaxioWriteClaim:
    return MaxioWriteClaim.objects.get(reference=reference)


def complete(reference: str, outcome: str, answer: Answer | None = None, *, detail: str = '') -> MaxioWriteClaim:
    """Record what is known about the write. A failure with no provider record releases the claim."""
    record = load_existing(reference)
    if outcome == MaxioWriteClaim.FAILED and answer is None:
        # Never sent, or refused outright: nothing exists at the provider, so
        # the next request may claim this reference again.
        record.delete()
        logger.info('Maxio write %s failed before anything was created; claim released', reference)
        record.outcome = outcome
        record.detail = detail
        return record
    record.outcome = outcome
    record.detail = detail
    if answer is not None:
        record.provider_id = answer.provider_id
        record.provider_state = answer.provider_state
        record.provider_time = answer.provider_time
        for name, value in answer.snapshot.items():
            setattr(record, name, value)
    record.save()
    logger.info('Maxio write %s recorded as %s (provider id %s)', reference, outcome, record.provider_id or '-')
    return record


def settle(record: MaxioWriteClaim, answer: Answer) -> MaxioWriteClaim:
    """Update a record from a later provider read (reconciliation)."""
    if record.outcome == MaxioWriteClaim.NEEDS_REVIEW:
        return record
    outcome = MaxioWriteClaim.NEEDS_REVIEW if answer.mismatch else answer.outcome
    return complete(record.reference, outcome, answer, detail=answer.mismatch or '')


# -- the one write path ------------------------------------------------------

def safe_write(step: WriteStep[T], *, user: Any, plan_handle: str = '') -> WriteResult:
    ref = step.reference

    # 1. CLAIM FIRST. The database decides the winner.
    checking = False
    if not try_claim(step, user, plan_handle):
        existing = load_existing(ref)
        if existing.outcome == MaxioWriteClaim.SENDING and existing.claimed_at > timezone.now() - send_window():
            return WriteResult(existing, replayed=True)       # in flight: no provider call
        if existing.outcome not in (MaxioWriteClaim.SENDING, MaxioWriteClaim.UNKNOWN):
            return WriteResult(existing, replayed=True)       # settled: answer from the record
        checking = True                                       # stale sender, or unresolved: LOOK, never create
        logger.info('Maxio write %s is %s; checking it at the provider', ref, existing.outcome)

    # A check is a resend under the SAME reference only where the provider
    # refuses a second one; otherwise the lookup below is the check.
    resending = checking and step.repeat_is_safe

    # 2. CALL, sending the reference.
    result: T | None = None
    if resending or not checking:
        try:
            result = step.send(ref)
        except NEVER_SENT as exc:
            if resending:                                     # a check that never left says nothing
                complete(ref, MaxioWriteClaim.UNKNOWN)
                raise OutcomeUnknown(ref) from exc
            complete(ref, MaxioWriteClaim.FAILED, detail='never sent')
            raise translate(exc, action=step.kind + ' create') from exc
        except ApiError as exc:
            if exc.status_code == 422:
                # Possibly "this reference is taken": an earlier attempt landed.
                # Only the lookup can tell a landing from a refusal.
                result = _find_or_unknown(step)
                if result is None:
                    complete(ref, MaxioWriteClaim.FAILED, detail='refused (422)')
                    raise translate(exc, action=step.kind + ' create') from exc
            elif exc.status_code < 500:
                refused = exc.status_code == 400 or not resending
                if not refused:                               # 401/403/404/409/429 on a check: still unknown
                    complete(ref, MaxioWriteClaim.UNKNOWN)
                    raise OutcomeUnknown(ref) from exc
                complete(ref, MaxioWriteClaim.FAILED, detail='refused (%s)' % exc.status_code)
                raise translate(exc, action=step.kind + ' create') from exc
            # a 5xx may still have landed: fall through to the lookup
        except (httpx.RequestError, ValueError):
            # Sent, and no readable answer: may have landed.
            logger.warning('Maxio write %s: no readable answer; looking it up', ref)

    # 3. READ what came back; an unreadable answer is settled by the lookup.
    answer: Answer | None = None
    if result is not None:
        try:
            answer = step.read(result)
        except ValueError:
            logger.warning('Maxio write %s: response lacked required members; looking it up', ref)

    # 4. CHECK HERE, by the reference we sent.
    if answer is None:
        found = _find_or_unknown(step)
        if found is None:                                     # an empty lookup cannot prove it did not happen
            complete(ref, MaxioWriteClaim.UNKNOWN)
            raise OutcomeUnknown(ref)
        try:
            answer = step.read(found)
        except ValueError as exc:
            complete(ref, MaxioWriteClaim.UNKNOWN)
            raise OutcomeUnknown(ref) from exc

    # 5. VERIFY before keeping it: it happened, but was it what we asked for?
    if answer.mismatch:
        logger.error('Maxio write %s landed but not as asked: %s', ref, answer.mismatch)
        return WriteResult(
            complete(ref, MaxioWriteClaim.NEEDS_REVIEW, answer, detail=answer.mismatch), replayed=checking)

    # 6. COMPLETE from what the provider said.
    return WriteResult(complete(ref, answer.outcome, answer), replayed=checking)


def _find_or_unknown(step: WriteStep[T]) -> T | None:
    """The lookup by reference. A lookup that itself fails leaves the write unknown."""
    try:
        return step.find(step.reference)
    except (ApiError, httpx.RequestError, ValueError) as exc:
        logger.warning('Maxio lookup for %s failed: %s', step.reference, type(exc).__name__)
        complete(step.reference, MaxioWriteClaim.UNKNOWN)
        raise OutcomeUnknown(step.reference) from exc
