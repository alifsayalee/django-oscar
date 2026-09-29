"""
The one path every Maxio write goes through: claim, call, check, verify,
complete.

* CLAIM: a ``BillingClaim`` row is inserted (and committed) before the
  provider is called. Its unique ``key`` makes the database - not a lock in
  this process - reject a second request for the same operation, whichever
  worker it arrives on.
* CALL: the write carries the claim's ``reference``; the same reference is used
  on every attempt and every repeat of the request.
* CHECK: when the answer is lost (a timeout after sending, a 5xx, an unreadable
  body) the write is looked up by that reference instead of being reported as
  failed or sent again under a new one.
* VERIFY / COMPLETE: the outcome recorded is what the provider's answer says,
  read through the step's own mapper - never merely that it answered.
"""
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, TypeVar

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.core import ApiError

from .models import BillingClaim

R = TypeVar('R')

# Failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# A claim still "sending" after this long lost its sender (the SDK makes one
# attempt, bounded by MAXIO_TIMEOUT); the next request checks on it.
SEND_WINDOW = timedelta(seconds=60)

# Outcomes only the provider's answer can settle.
UNSETTLED = (BillingClaim.SENDING, BillingClaim.UNKNOWN, BillingClaim.PENDING)


class OutcomeUnknown(Exception):
    """The write may have landed and the provider has not said whether it did."""

    def __init__(self, claim: BillingClaim) -> None:
        super().__init__('Outcome of %s is unknown' % claim.reference)
        self.claim = claim


class AmountMismatch(Exception):
    """The write happened, but not at the price that was asked for."""

    def __init__(self, claim: BillingClaim) -> None:
        super().__init__('Amount of %s differs from what was sent' % claim.reference)
        self.claim = claim


@dataclass(frozen=True)
class Answer:
    """What one step's response says, read the same way for every step."""

    provider_id: str
    # The step's own status value; the step's mapper turns it into an outcome.
    status: object
    provider_time: datetime | None
    # The echoed money (minor units), None when the response carried none.
    amount: int | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)


def new_reference(kind: str) -> str:
    # Generated once, when the claim row is first inserted, and stored with
    # it; every attempt and every repeat re-uses the stored value.
    return '%s-%s-%s' % (settings.MAXIO_REFERENCE_PREFIX, kind, secrets.token_hex(8))


def try_claim(key: str, *, kind: str, user: Any, plan_handle: str = '') -> BillingClaim | None:
    """
    Insert-or-fail. Returns the claim for exactly one caller and None for every
    other while it is held.
    """
    now = timezone.now()
    try:
        with transaction.atomic():
            return BillingClaim.objects.create(
                key=key, reference=new_reference(kind), kind=kind, user=user,
                plan_handle=plan_handle, outcome=BillingClaim.SENDING, claimed_at=now)
    except IntegrityError:
        pass
    # A released claim - a first send that never left or that the provider
    # refused, so nothing landed - may be taken again, keeping its reference.
    # One conditional UPDATE, so only one caller can win it.
    with transaction.atomic():
        taken = BillingClaim.objects.filter(
            key=key, outcome=BillingClaim.FAILED, provider_id='',
        ).update(outcome=BillingClaim.SENDING, claimed_at=now)
    if taken:
        return BillingClaim.objects.get(key=key)
    return None


def complete(claim: BillingClaim, outcome: str, answer: Answer | None = None) -> BillingClaim:
    """
    Record the outcome. A ``failed`` with no provider id releases the claim.
    """
    claim.outcome = outcome
    fields = ['outcome', 'updated_at']
    if answer is not None:
        claim.provider_id = answer.provider_id
        claim.provider_time = answer.provider_time
        claim.snapshot = answer.snapshot
        fields += ['provider_id', 'provider_time', 'snapshot']
    claim.save(update_fields=fields)
    return claim


def _take_claim(key: str, *, kind: str, user: Any, plan_handle: str) -> tuple[BillingClaim, bool]:
    """
    Step 1, CLAIM FIRST. Returns the claim and whether this request only
    checks on an earlier attempt (it lost the claim to one that is unsettled).
    """
    claim = try_claim(key, kind=kind, user=user, plan_handle=plan_handle)
    if claim is not None:
        return claim, False
    return BillingClaim.objects.get(key=key), True


def _answered_from_record(claim: BillingClaim) -> bool:
    """Whether a repeat is answered from the record, with no provider call."""
    if claim.outcome == BillingClaim.SENDING:
        # In flight, unless its sender is gone.
        return claim.claimed_at > timezone.now() - SEND_WINDOW
    # done / failed / needs_review; unknown and pending are checked again.
    return claim.outcome not in UNSETTLED


def _send(claim: BillingClaim, send: Callable[[str], R], *, resending: bool) -> R | None:
    """
    Step 2, CALL - a first attempt, or a check by a same-reference resend.
    Returns None when the answer was lost and the write may have landed.
    """
    try:
        return send(claim.reference)
    except NEVER_SENT:
        if resending:
            # A check that never left says nothing.
            complete(claim, BillingClaim.UNKNOWN)
            raise OutcomeUnknown(claim)
        complete(claim, BillingClaim.FAILED)  # nothing happened: released
        raise
    except ApiError as e:
        if e.status_code < 500 and not resending:
            complete(claim, BillingClaim.FAILED)  # refused: released
            raise
        # A 4xx on a check (e.g. "reference already taken") settles nothing,
        # and a 5xx may have landed: look it up.
        return None
    except (httpx.RequestError, ValueError):
        # Sent, and no readable answer (ValueError covers pydantic's
        # ValidationError and a non-JSON body): it may have landed.
        return None


def _find(reference: str, find: Callable[[str], R | None], read: Callable[[R, str], Answer]) -> Answer | None:
    """Step 3, CHECK, by the reference that was sent. None when nothing settles it."""
    try:
        found = find(reference)
    except (ApiError, httpx.RequestError, ValueError):
        return None  # a failed lookup is not an absence
    return _named(read(found, reference)) if found is not None else None


def _named(answer: Answer) -> Answer | None:
    # A 2xx that names nothing (a truncated body) is as unreadable as a failed
    # decode: it may have landed.
    return answer if answer.provider_id else None


def safe_write(
    key: str,
    *,
    kind: str,
    user: Any,
    send: Callable[[str], R],
    find: Callable[[str], R | None],
    read: Callable[[R, str], Answer],
    outcome_of: Callable[[object], str],
    repeat_is_safe: bool,
    plan_handle: str = '',
    sent_amount: int | None = None,
) -> BillingClaim:
    """
    Make one provider write at most once per ``key``.

    send(reference)         makes the provider call carrying ``reference``
    find(reference)         the record carrying ``reference``, or None when the
                            provider has none
    read(result, reference) the response as an Answer
    outcome_of(status)      the step's status mapper
    repeat_is_safe          the provider will not make a second record for the
                            same reference, so a resend is a safe check
    sent_amount             the price asked for, verified against the echo
    """
    claim, checking = _take_claim(key, kind=kind, user=user, plan_handle=plan_handle)
    if checking and _answered_from_record(claim):
        return claim
    # A request that lost the claim never makes a new write: it looks the
    # earlier one up, or resends it under the same reference where that is safe.
    resending = checking and repeat_is_safe and claim.outcome != BillingClaim.PENDING

    got = None
    if resending or not checking:
        result = _send(claim, send, resending=resending)
        got = _named(read(result, claim.reference)) if result is not None else None
    if got is None:
        got = _find(claim.reference, find, read)
    if got is None:
        if checking and claim.outcome == BillingClaim.PENDING:
            return claim  # the provider holds it; still pending
        complete(claim, BillingClaim.UNKNOWN)
        raise OutcomeUnknown(claim)

    # 4. VERIFY what was made against what was asked.
    if sent_amount is not None and got.amount != sent_amount:
        complete(claim, BillingClaim.NEEDS_REVIEW, got)
        raise AmountMismatch(claim)

    # 5. COMPLETE from what the provider said.
    return complete(claim, outcome_of(got.status), got)
