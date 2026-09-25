"""
The safe write: the one path every Maxio write that creates something goes through.

claim (DB unique constraint, committed before the call) -> call with the
reference -> on an unknown outcome, look it up by that same reference ->
record what the provider said.
"""
import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Generic, TypeVar

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.core import ApiError

from .errors import NEVER_SENT, OutcomeUnknown
from .models import Outcome, ProviderClaim

logger = logging.getLogger('apps.subscriptions')

R = TypeVar('R')
C = TypeVar('C', bound=ProviderClaim)


@dataclass(frozen=True)
class Answer:
    """What one provider response says, already mapped to our outcome vocabulary."""
    provider_id: int | None
    outcome: str
    provider_time: datetime.datetime | None = None
    provider_state: str = ''


@dataclass(frozen=True)
class WriteResult(Generic[C, R]):
    record: C
    result: R | None       # the provider's response when this request talked to Maxio
    sent: bool             # True when this request's own first attempt made the write


def send_window() -> timedelta:
    """How long a 'sending' claim is presumed in flight: longer than one attempt can take."""
    timeout = float(getattr(settings, 'MAXIO_TIMEOUT', 10.0))
    return timedelta(seconds=max(60.0, 3 * timeout))


def try_claim(model: type[C], ref: str, fields: dict[str, Any]) -> bool:
    """Insert-or-fail. Also re-takes a released claim (failed with nothing at the provider)."""
    now = timezone.now()
    try:
        with transaction.atomic():
            model.objects.create(reference=ref, outcome=Outcome.SENDING, claimed_at=now, **fields)
        return True
    except IntegrityError:
        retaken = model.objects.filter(
            reference=ref, outcome=Outcome.FAILED, provider_id__isnull=True,
        ).update(outcome=Outcome.SENDING, claimed_at=now, last_error='')
        return bool(retaken == 1)


def take_over_check(model: type[C], record: C) -> bool:
    """Atomically become the one request that checks a stale or unknown claim."""
    updated: int = model.objects.filter(
        pk=record.pk, outcome=record.outcome, claimed_at=record.claimed_at,
    ).update(outcome=Outcome.SENDING, claimed_at=timezone.now())
    return updated == 1


def complete(model: type[C], ref: str, outcome: str, answer: Answer | None = None,
             error: str = '') -> C:
    updates: dict[str, Any] = {'outcome': outcome}
    if answer is not None:
        if answer.provider_id is not None:
            updates['provider_id'] = answer.provider_id
        if answer.provider_time is not None:
            updates['provider_time'] = answer.provider_time
        if answer.provider_state and hasattr(model, 'provider_state'):
            updates['provider_state'] = answer.provider_state
    if error:
        updates['last_error'] = error[:2000]
    model.objects.filter(reference=ref).update(**updates)
    record: C = model.objects.get(reference=ref)
    return record


def safe_write(
    model: type[C],
    ref: str,
    *,
    claim_fields: dict[str, Any],
    send: Callable[[str], R],
    find: Callable[[str], R | None],
    read: Callable[[R], Answer],
    repeat_is_safe: bool = False,
    may_be_duplicate: Callable[[ApiError[Any]], bool] = lambda e: False,
) -> WriteResult[C, R]:
    """
    ref               derived from the operation; the same on every attempt and every repeat
    send(ref)         the provider create, carrying ``ref`` as its reference field
    find(ref)         the provider record carrying ``ref``, or None when not found
    read(result)      the response as an Answer (status already mapped)
    repeat_is_safe    the provider refuses a second record with the same reference
    may_be_duplicate  an ApiError that may mean "reference already taken" -> look it up
    """
    # 1. CLAIM FIRST. The database decides the winner.
    checking = False
    if not try_claim(model, ref, claim_fields):
        existing = model.objects.get(reference=ref)
        if existing.outcome == Outcome.SENDING and existing.claimed_at > timezone.now() - send_window():
            return WriteResult(existing, None, False)          # in flight elsewhere: no provider call
        if existing.outcome not in (Outcome.SENDING, Outcome.UNKNOWN):
            return WriteResult(existing, None, False)          # settled: answer from the record
        if not take_over_check(model, existing):
            return WriteResult(model.objects.get(reference=ref), None, False)
        checking = True                                        # stale or unknown: check, never create anew

    resending = checking and repeat_is_safe

    # 2. CALL with the reference (first attempt, or a same-reference resend as the check).
    result: R | None = None
    rejection: ApiError[Any] | None = None
    if resending or not checking:
        try:
            result = send(ref)
        except NEVER_SENT as e:
            if resending:
                complete(model, ref, Outcome.UNKNOWN, error=type(e).__name__)
                raise OutcomeUnknown(ref) from e
            complete(model, ref, Outcome.FAILED, error=type(e).__name__)   # nothing left: release
            raise
        except ApiError as e:
            if may_be_duplicate(e):
                rejection = e                                  # an earlier attempt may have landed
            elif e.status_code < 500:
                refused = e.status_code in (400, 422) or not resending
                complete(model, ref, Outcome.FAILED if refused else Outcome.UNKNOWN,
                         error=f'HTTP {e.status_code}')
                if not refused:
                    raise OutcomeUnknown(ref) from e
                raise
            # a 5xx may still have landed: fall through to the lookup
            logger.warning('Maxio write %s answered HTTP %s; checking by reference', ref, e.status_code)
        except (httpx.RequestError, ValueError) as e:          # sent, no readable answer
            logger.warning('Maxio write %s outcome unknown (%s); checking by reference', ref, type(e).__name__)

    if result is not None and read(result).provider_id is None:
        logger.warning('Maxio write %s answered without an id; checking by reference', ref)
        result = None                                          # accepted, but we cannot name it

    # 3. CHECK by the reference we sent.
    if result is None:
        try:
            result = find(ref)
        except (ApiError, httpx.RequestError, ValueError) as e:
            complete(model, ref, Outcome.UNKNOWN, error=f'lookup failed: {type(e).__name__}')
            raise OutcomeUnknown(ref) from e
        if result is None:
            if rejection is not None:                          # refused, and nothing carries our reference
                complete(model, ref, Outcome.FAILED, error=f'HTTP {rejection.status_code}')
                raise rejection
            complete(model, ref, Outcome.UNKNOWN, error='not found by reference yet')
            raise OutcomeUnknown(ref)

    # 4. COMPLETE from what the provider said.
    answer = read(result)
    if answer.provider_id is None:
        complete(model, ref, Outcome.UNKNOWN, answer, error='response carried no id')
        raise OutcomeUnknown(ref)
    record = complete(model, ref, answer.outcome, answer)
    return WriteResult(record, result, not checking)
