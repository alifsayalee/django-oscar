"""
The one path every write to Maxio goes through: claim, call, check, verify,
complete.

The claim is a ``MaxioWrite`` row inserted before the provider call; the
database's unique constraint on ``reference`` rejects a second claim for the
same write from any request, thread or process. When a call's outcome is
unknown, the write is looked up at Maxio by the same reference - it is never
re-sent under a new one.
"""
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Generic, NamedTuple, TypeVar

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.core import ApiError

from .errors import OutcomeUnknown, error_messages
from .maxio import NEVER_SENT
from .models import MaxioInstall, MaxioWrite

logger = logging.getLogger(__name__)

R = TypeVar('R')

# Longer than a write's worst case (one attempt, 30s timeout) plus its check.
SEND_WINDOW = timedelta(seconds=120)

SETTLED = (MaxioWrite.DONE, MaxioWrite.PENDING, MaxioWrite.FAILED, MaxioWrite.NEEDS_REVIEW)


class Answer(NamedTuple):
    """What one write's response says, read the same way for every step."""
    provider_id: str | None
    status: Any
    provider_time: datetime | None
    provider_state: str = ''
    as_asked: bool = True  # False when Maxio did something other than what we asked


@dataclass
class WriteResult(Generic[R]):
    record: MaxioWrite
    result: R | None = None  # Maxio's response, when this request received one


_install_prefix: str | None = None


def reference_prefix() -> str:
    """The prefix on every reference we send: configured, or generated once per database."""
    global _install_prefix
    configured = getattr(settings, 'MAXIO_REFERENCE_PREFIX', None)
    if configured:
        return str(configured)
    if _install_prefix is None:
        try:
            with transaction.atomic():
                install, _ = MaxioInstall.objects.get_or_create(
                    id=1, defaults={'install_id': uuid.uuid4().hex[:12]})
        except IntegrityError:  # another process created it first
            install = MaxioInstall.objects.get(id=1)
        _install_prefix = f'oscar-{install.install_id}'
    return _install_prefix


# --- the claim store -------------------------------------------------------

def try_claim(ref: str, *, kind: str, user: Any, plan_handle: str = '') -> bool:
    """Insert-or-fail. Exactly one caller wins a claim for ``ref``."""
    now = timezone.now()
    try:
        with transaction.atomic():
            MaxioWrite.objects.create(
                reference=ref, kind=kind, user=user, plan_handle=plan_handle,
                outcome=MaxioWrite.SENDING, claimed_at=now)
        return True
    except IntegrityError:
        # A released claim (failed without landing) may be taken again, atomically.
        return MaxioWrite.objects.filter(
            reference=ref, outcome=MaxioWrite.FAILED, provider_id__isnull=True,
        ).update(outcome=MaxioWrite.SENDING, claimed_at=now, completed_at=None) == 1


def take_over(record: MaxioWrite) -> bool:
    """Compare-and-set: one caller gets to check a stale or unknown write."""
    return MaxioWrite.objects.filter(
        pk=record.pk, outcome=record.outcome, claimed_at=record.claimed_at,
    ).update(outcome=MaxioWrite.SENDING, claimed_at=timezone.now()) == 1


def load_existing(ref: str) -> MaxioWrite:
    return MaxioWrite.objects.get(reference=ref)


def complete(ref: str, outcome: str, provider_id: str | None = None,
             provider_time: datetime | None = None, provider_state: str = '') -> MaxioWrite:
    """Record the outcome. A ``failed`` with no provider id releases the claim."""
    fields: dict[str, Any] = {'outcome': outcome, 'completed_at': timezone.now()}
    if provider_id is not None:
        fields['provider_id'] = provider_id
    if provider_time is not None:
        fields['provider_time'] = provider_time
    if provider_state:
        fields['provider_state'] = provider_state
    MaxioWrite.objects.filter(reference=ref).update(**fields)
    record = load_existing(ref)
    log = logger.info if outcome == MaxioWrite.DONE else logger.warning
    log('Maxio write %s -> %s (provider id %s)', ref, outcome, record.provider_id)
    return record


def in_flight(record: MaxioWrite) -> bool:
    return (record.outcome == MaxioWrite.SENDING
            and record.claimed_at > timezone.now() - SEND_WINDOW)


# --- the write -------------------------------------------------------------

def safe_write(
    ref: str,
    *,
    kind: str,
    user: Any,
    send: Callable[[str], R],
    find: Callable[[str], R | None],
    read: Callable[[R], Answer],
    outcome_of: Callable[[Any], str],
    repeat_is_safe: bool = False,
    already_exists: Callable[[ApiError], bool] = lambda e: False,
    plan_handle: str = '',
) -> WriteResult[R]:
    """
    Make one provider write at most once for ``ref``.

    send(ref)       makes the call, carrying ``ref`` as the write's reference
    find(ref)       looks the write up at Maxio by that reference; None when not found
    read(result)    the response as an ``Answer``
    outcome_of      maps ``Answer.status`` to an outcome
    repeat_is_safe  Maxio refuses a second write with this reference, so a
                    same-reference resend is itself a safe check
    already_exists  whether a rejection says an earlier attempt already landed
    """
    # 1. CLAIM FIRST - the database decides the winner.
    checking = False
    if not try_claim(ref, kind=kind, user=user, plan_handle=plan_handle):
        existing = load_existing(ref)
        if existing.outcome in SETTLED or in_flight(existing):
            return WriteResult(existing)  # answered from the record; no provider call
        if not take_over(existing):
            return WriteResult(load_existing(ref))  # another request is checking it now
        checking = True  # a stale sender, or an unknown outcome: look, never create anew

    resending = checking and repeat_is_safe

    # 2. CALL - a first attempt, or a check by same-reference resend.
    result: R | None = None
    if resending or not checking:
        try:
            result = send(ref)
        except NEVER_SENT:
            complete(ref, MaxioWrite.UNKNOWN if resending else MaxioWrite.FAILED)
            if resending:
                raise OutcomeUnknown(ref)
            raise
        except ApiError as e:
            if already_exists(e):
                logger.info('Maxio write %s already landed earlier', ref)  # look it up below
            elif e.status_code < 500:
                if resending:
                    complete(ref, MaxioWrite.UNKNOWN)  # a check never fails the write
                    raise OutcomeUnknown(ref) from e
                complete(ref, MaxioWrite.FAILED)  # refused: nothing landed, claim released
                logger.warning('Maxio refused write %s: HTTP %s %s', ref, e.status_code,
                               error_messages(e.error))
                raise
            # a 5xx may still have landed - check below
        except (httpx.RequestError, ValueError) as e:  # ValueError covers pydantic's ValidationError
            logger.warning('Maxio write %s outcome unknown after %s; checking', ref, type(e).__name__)

    # 3. CHECK - look it up by the reference we sent.
    if result is None:
        try:
            result = find(ref)
        except (ApiError, httpx.RequestError, ValueError) as e:
            complete(ref, MaxioWrite.UNKNOWN)
            raise OutcomeUnknown(ref) from e
        if result is None:  # not found yet: an empty lookup does not prove it never happened
            complete(ref, MaxioWrite.UNKNOWN)
            raise OutcomeUnknown(ref)

    # 4. VERIFY - Maxio is authoritative for what happened, not for what we asked.
    got = read(result)
    if not got.as_asked:
        record = complete(ref, MaxioWrite.NEEDS_REVIEW, got.provider_id, got.provider_time,
                          got.provider_state)
        return WriteResult(record, result)

    # 5. COMPLETE from what Maxio said.
    record = complete(ref, outcome_of(got.status), got.provider_id, got.provider_time,
                      got.provider_state)
    return WriteResult(record, result)


def settle(
    record: MaxioWrite,
    *,
    find: Callable[[str], R | None],
    read: Callable[[R], Answer],
    outcome_of: Callable[[Any], str],
) -> WriteResult[R]:
    """
    Resolve an unknown (or abandoned) write by looking it up - never by
    sending it again. Used by reads that list a user's writes.
    """
    if record.outcome in SETTLED or in_flight(record) or not take_over(record):
        return WriteResult(record)
    ref = record.reference
    try:
        result = find(ref)
    except (ApiError, httpx.RequestError, ValueError):
        return WriteResult(complete(ref, MaxioWrite.UNKNOWN))
    if result is None:
        return WriteResult(complete(ref, MaxioWrite.UNKNOWN))
    got = read(result)
    outcome = outcome_of(got.status) if got.as_asked else MaxioWrite.NEEDS_REVIEW
    return WriteResult(
        complete(ref, outcome, got.provider_id, got.provider_time, got.provider_state), result)
