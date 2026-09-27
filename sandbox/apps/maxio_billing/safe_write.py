"""
The one path every Maxio write goes through: claim, call, check, verify, complete.

The claim is a `BillingWrite` row whose UNIQUE `reference` is inserted in its own
committed transaction *before* the provider call, so a second request for the same
write - another click, another thread, another worker - is rejected by the
database, not by anything held in this process. The same reference is sent to
Maxio, and a write whose outcome is unknown is only ever settled by looking it up
by that reference; it is never re-created under a new one.

Callers must not wrap this in an outer transaction (the views are
`non_atomic_requests`): the claim has to be visible and durable before the call.
"""
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

from .errors import NEVER_SENT, OutcomeUnknown, ProviderError, provider_error
from .models import BillingWrite

logger = logging.getLogger(__name__)

T = TypeVar('T')


@dataclass(frozen=True)
class Answer:
    """What one write step's response says, read the same way for every step."""

    provider_id: str
    status: object  # the step's own status member; `outcome_of` maps it
    provider_time: datetime | None
    # What the provider echoed back of what we asked for; compared with `sent`.
    echo: tuple[Any, ...] | None = None
    provider_state: str = ''
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WriteResult:
    record: BillingWrite
    # True when this request sent the write itself; False when it answered from, or
    # settled by lookup, a write an earlier request made.
    sent_now: bool


def send_window() -> timedelta:
    """How long a `sending` claim is presumed in flight: longer than one timed-out call."""
    return timedelta(seconds=float(settings.MAXIO_TIMEOUT) * 2 + 30)


def try_claim(reference: str, fields: dict[str, Any]) -> bool:
    """Insert-or-fail. True for exactly one caller per reference."""
    now = timezone.now()
    try:
        with transaction.atomic():
            BillingWrite.objects.create(
                reference=reference, outcome=BillingWrite.SENDING, claimed_at=now, **fields)
        return True
    except IntegrityError:
        # A `failed` with no provider id is a released claim (nothing happened at
        # Maxio): re-take it atomically. Exactly one UPDATE can match.
        retaken = BillingWrite.objects.filter(
            reference=reference, outcome=BillingWrite.FAILED, provider_id='',
        ).update(outcome=BillingWrite.SENDING, claimed_at=now, detail='', updated_at=now)
        return retaken == 1


def load_existing(reference: str) -> BillingWrite:
    return BillingWrite.objects.get(reference=reference)


def complete(reference: str, outcome: str, answer: Answer | None = None,
             detail: str = '') -> BillingWrite:
    update: dict[str, Any] = {'outcome': outcome, 'detail': detail, 'updated_at': timezone.now()}
    if answer is not None:
        update.update(provider_id=answer.provider_id, provider_time=answer.provider_time,
                      provider_state=answer.provider_state, snapshot=answer.snapshot)
    BillingWrite.objects.filter(reference=reference).update(**update)
    record = load_existing(reference)
    logger.info('billing write %s -> %s (provider id %s)', reference, outcome,
                record.provider_id or '-')
    return record


class _Unsettled(Exception):
    """Sent, and no readable answer: fall through to the lookup."""


@dataclass
class SafeWrite(Generic[T]):
    """
    reference       from the operation AND the step - the same on every attempt and request
    claim_fields    what the claim row records besides the reference (user, kind, ...)
    send(ref)       makes the provider call carrying `ref` as its reference field
    find(ref)       the record carrying `ref` at the provider, or None when not found
    read(result)    this step's response as an Answer
    outcome_of      this step's status -> done / pending / failed / unknown
    sent            what we asked for, compared with Answer.echo before anything is kept
    """

    reference: str
    claim_fields: dict[str, Any]
    send: Callable[[str], T]
    find: Callable[[str], T | None]
    read: Callable[[T], Answer]
    outcome_of: Callable[[object], str]
    sent: tuple[Any, ...] | None = None

    def run(self) -> WriteResult:
        ref = self.reference

        # 1. CLAIM FIRST. The database decides the winner.
        if not try_claim(ref, self.claim_fields):
            existing = load_existing(ref)
            if (existing.outcome == BillingWrite.SENDING
                    and existing.claimed_at > timezone.now() - send_window()):
                return WriteResult(existing, sent_now=False)  # in flight: no provider call
            if existing.outcome not in (BillingWrite.SENDING, BillingWrite.UNKNOWN):
                return WriteResult(existing, sent_now=False)  # answer from what is recorded
            # A stale sender or an unresolved outcome: LOOK, never create. Maxio does not
            # document that it refuses a second create under the same reference, so a
            # resend is not a safe check here - only the lookup is.
            return WriteResult(self._check(ref), sent_now=False)

        # 2. CALL, carrying the reference.
        try:
            result: T | None = self._send(ref)
        except _Unsettled:
            result = None
        if result is None:
            return WriteResult(self._check(ref), sent_now=True)
        return WriteResult(self._verify_and_complete(ref, result), sent_now=True)

    def _send(self, ref: str) -> T:
        try:
            return self.send(ref)
        except NEVER_SENT as e:
            # A first send that never left: nothing happened. Releases the claim.
            complete(ref, BillingWrite.FAILED, detail=f'never sent: {type(e).__name__}')
            raise ProviderError(
                502, 'The billing provider could not be reached; nothing was created.',
                code='provider_unreachable') from e
        except ApiError as e:
            if e.status_code < 500:
                # The provider refused this first send: nothing was created. A
                # 401/403/429 is refused just the same. Releases the claim.
                err = provider_error(e.status_code, e.error)
                complete(ref, BillingWrite.FAILED,
                         detail='; '.join(err.details) or f'HTTP {e.status_code}')
                raise err from e
            logger.warning('billing write %s: HTTP %s, checking by reference', ref, e.status_code)
            raise _Unsettled() from e  # a 5xx on a write may still have landed
        except httpx.RequestError as e:
            logger.warning('billing write %s: %s, checking by reference', ref, type(e).__name__)
            raise _Unsettled() from e  # sent, no answer: may have landed
        except ValueError as e:  # pydantic ValidationError / non-JSON 2xx body
            logger.warning('billing write %s: unreadable response, checking by reference', ref)
            raise _Unsettled() from e

    def _check(self, ref: str) -> BillingWrite:
        """3. Settle an unknown outcome by the reference we sent - only a found record settles it."""
        try:
            found = self.find(ref)
        except (ApiError, httpx.RequestError, ValueError) as e:
            complete(ref, BillingWrite.UNKNOWN, detail=f'lookup failed: {type(e).__name__}')
            raise OutcomeUnknown(ref) from e
        if found is None:
            # Not found YET: a timed-out write can still land. Never "failed" by absence.
            complete(ref, BillingWrite.UNKNOWN, detail='not found by reference yet')
            raise OutcomeUnknown(ref)
        return self._verify_and_complete(ref, found)

    def _verify_and_complete(self, ref: str, result: T) -> BillingWrite:
        got = self.read(result)
        if not got.provider_id:
            # A 2xx that does not name what it created: it may have happened, and we
            # cannot say what. Only a later lookup by reference settles it.
            complete(ref, BillingWrite.UNKNOWN, got, detail='response carried no id')
            raise OutcomeUnknown(ref)
        # 4. VERIFY BEFORE KEEPING: it happened, but was it what we asked for?
        if self.sent is not None and got.echo != self.sent:
            return complete(ref, BillingWrite.NEEDS_REVIEW, got,
                            detail=f'asked for {self.sent!r}, provider has {got.echo!r}')
        # 5. COMPLETE from what the provider SAID, not from the fact that it answered.
        return complete(ref, self.outcome_of(got.status), got)
