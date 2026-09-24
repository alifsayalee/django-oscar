"""
The one path every Maxio write that creates something goes through:
claim, call, check, verify, complete.

The claim is a row committed *before* the provider call; the database's unique
constraint rejects a second claim for the same operation, so a double-click or
a second worker can never send the write twice. When the answer is lost, the
outcome is settled by looking the write up by the reference it was sent with,
never by sending it again under a new one.
"""
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Generic, NamedTuple, TypeVar

import httpx
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from maxio_advanced_billing.core import ApiError, RequestOptionsDict

from .errors import NEVER_SENT, OutcomeUnknown, ProviderError
from .models import Outcome

# Longer than one attempt (10 s timeout, the SDK never retries): a claim still
# "sending" after this is a sender that died, and gets checked, not re-sent.
SEND_WINDOW = timedelta(seconds=60)

R = TypeVar('R')
M = TypeVar('M', bound=models.Model)


class Answer(NamedTuple):
    """What one write's response says, read the same way for every write."""
    provider_id: int | None
    outcome: str                        # already mapped by status_from_provider
    provider_time: datetime | None      # the provider's clock
    amount_in_cents: int | None = None  # the echoed money; None for a write that moves none
    fields: dict[str, Any] = {}         # extra columns to record from the answer


class ClaimContended(ProviderError):
    def __init__(self, reference: str) -> None:
        super().__init__(409, 'request_in_progress',
                         'The same request is being processed; repeat it shortly.',
                         detail={'reference': reference})


def idempotency_options(reference: str) -> RequestOptionsDict:
    """Per-claim Idempotency-Key, stable across attempts, instead of the SDK's random one."""
    key = uuid.uuid5(uuid.NAMESPACE_URL, reference)
    return {'extra_headers': {'Idempotency-Key': str(key)}}


class Claim(Generic[M]):
    """A claim held in a Django model row, identified by ``lookup``."""

    def __init__(self, model: type[M], reference: str, lookup: dict[str, Any],
                 defaults: dict[str, Any], provider_id_field: str) -> None:
        self.model = model
        self.reference = reference
        self.lookup = lookup
        self.defaults = defaults
        self.provider_id_field = provider_id_field

    def try_claim(self) -> bool:
        """Insert-or-fail: the unique constraints decide the one winner."""
        try:
            with transaction.atomic():
                self.model._default_manager.create(
                    **self.lookup, **self.defaults, reference=self.reference,
                    outcome=Outcome.SENDING, claimed_at=timezone.now())
        except IntegrityError:
            return False
        return True

    def load(self) -> M | None:
        return self.model._default_manager.filter(**self.lookup).first()

    def complete(self, outcome: str, provider_id: int | None = None,
                 provider_time: datetime | None = None, fields: dict[str, Any] | None = None,
                 ) -> M | None:
        """Record what is known. A failure with nothing at the provider releases the claim."""
        if outcome == Outcome.FAILED and provider_id is None:
            self.model._default_manager.filter(
                reference=self.reference, outcome__in=[Outcome.SENDING, Outcome.UNKNOWN],
            ).delete()
            return None
        record = self.model._default_manager.get(reference=self.reference)
        setattr(record, 'outcome', outcome)
        if provider_id is not None:
            setattr(record, self.provider_id_field, provider_id)
        if provider_time is not None:
            setattr(record, 'provider_time', provider_time)
        for name, value in (fields or {}).items():
            setattr(record, name, value)
        record.save()
        return record


@dataclass(frozen=True)
class WriteResult(Generic[M]):
    record: M
    in_flight: bool   # another request holds a fresh claim; nothing was sent


def safe_write(
    claim: Claim[M],
    send: Callable[[], R],
    find: Callable[[], R | None],
    read: Callable[[R], Answer],
    *,
    repeat_is_safe: bool = False,
    expected_amount_in_cents: int | None = None,
) -> WriteResult[M]:
    """
    send()          makes the provider call, carrying ``claim.reference``
    find()          what ``send`` would have returned for that reference, or None
    read(result)    the response as an Answer
    repeat_is_safe  the provider refuses a second one for this reference, so a
                    same-reference resend is a safe check
    """
    # 1. CLAIM FIRST.
    checking = False
    if not claim.try_claim():
        existing = claim.load()
        if existing is None and not claim.try_claim():
            # The holder released it between our insert and read, and someone re-took it.
            existing = claim.load()
            if existing is None:
                raise ClaimContended(claim.reference)
        if existing is not None:
            outcome = getattr(existing, 'outcome')
            claimed_at: datetime = getattr(existing, 'claimed_at')
            if outcome == Outcome.SENDING and claimed_at > timezone.now() - SEND_WINDOW:
                return WriteResult(existing, True)     # in flight: no provider call
            if outcome not in (Outcome.SENDING, Outcome.UNKNOWN):
                return WriteResult(existing, False)    # settled: answer from it
            checking = True                             # stale or unresolved: check, never create anew

    resending = checking and repeat_is_safe

    # 2. CALL, carrying the reference.
    result: R | None = None
    if resending or not checking:
        try:
            result = send()
        except NEVER_SENT:
            if resending:                       # a check that never left says nothing
                claim.complete(Outcome.UNKNOWN)
                raise OutcomeUnknown(claim.reference)
            claim.complete(Outcome.FAILED)      # a first send that never left: nothing happened
            raise
        except ApiError as e:
            if e.status_code < 500:
                refused = e.status_code in (400, 422) or not resending
                if not refused:
                    claim.complete(Outcome.UNKNOWN)
                    raise OutcomeUnknown(claim.reference) from e
                claim.complete(Outcome.FAILED)
                raise
            # a 5xx on a write may still have landed: look it up below
        except (httpx.RequestError, ValueError):
            pass                                # sent, no readable answer: may have landed
        if result is not None and read(result).provider_id is None:
            result = None                       # a 2xx that names nothing: may have landed

    # 3. CHECK by the reference we sent.
    if result is None:
        try:
            result = find()
        except (ApiError, httpx.RequestError, ValueError) as e:
            claim.complete(Outcome.UNKNOWN)
            raise OutcomeUnknown(claim.reference) from e
        if result is None:                      # not found YET: an empty lookup proves nothing
            claim.complete(Outcome.UNKNOWN)
            raise OutcomeUnknown(claim.reference)

    got = read(result)
    if got.provider_id is None:                 # accepted, but we cannot name what it created
        claim.complete(Outcome.UNKNOWN)
        raise OutcomeUnknown(claim.reference)

    # 4. VERIFY before keeping it.
    outcome = got.outcome
    if expected_amount_in_cents is not None and got.amount_in_cents != expected_amount_in_cents:
        outcome = Outcome.NEEDS_REVIEW

    # 5. COMPLETE from what the provider said.
    record = claim.complete(outcome, got.provider_id, got.provider_time, got.fields)
    assert record is not None
    return WriteResult(record, False)
