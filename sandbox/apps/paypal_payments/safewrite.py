"""
The one path for every PayPal write that creates, charges or sends.

1. CLAIM: insert a ``PayPalOperation`` row under a unique reference derived from
   the operation. The database rejects a second claim, so two requests (or two
   workers) can never both send the same write.
2. CALL: send the write with that reference as its ``PayPal-Request-Id``.
3. CHECK: when the answer is missing or unreadable the write may have landed;
   look it up (or resend under the same reference where PayPal replays), never
   send it under a new reference.
4. VERIFY: compare the money PayPal echoes with what was asked, as ``Decimal``.
5. COMPLETE: record the outcome PayPal reported, with its event time, in the
   same transaction that applies it to the payment.

The claim must be committed before the provider call, which is why the views
that use this are excluded from ``ATOMIC_REQUESTS``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Generic, NamedTuple, TypeVar

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone
from paypal.core import ApiError, OAuthProviderError

from .errors import NEVER_SENT, AmountMismatch, OutcomeUnknown, provider_issues
from .gateway import REQUEST_TIMEOUT
from .models import Outcome, PayPalOperation

T = TypeVar("T")

# Longer than one attempt can take, so a claim younger than this is in flight.
SEND_WINDOW = timedelta(seconds=REQUEST_TIMEOUT * 3)

# A 4xx that says an earlier attempt under this reference already landed.
_ALREADY_PROCESSED_ISSUES = {"DUPLICATE_INVOICE_ID", "DUPLICATE_REQUEST_ID"}


class Answer(NamedTuple):
    """What one step's response says, read the same way for every step."""

    provider_id: str
    outcome: str
    provider_status: str
    provider_time: datetime | None
    amount: str | None = None
    currency: str | None = None


@dataclass
class WriteResult(Generic[T]):
    operation: PayPalOperation
    # The provider object, when this request obtained one; ``None`` when the
    # answer came from what an earlier request already recorded.
    result: T | None


Apply = Callable[[PayPalOperation, Answer, Any], None]


def try_claim(reference: str, kind: str, order_id: int | None, amount: Decimal | None, currency: str) -> bool:
    """Insert-or-fail. ``True`` only for the one caller that now holds the claim."""
    now = timezone.now()
    try:
        with transaction.atomic():
            PayPalOperation.objects.create(
                reference=reference,
                kind=kind,
                order_id=order_id,
                amount=amount,
                currency=currency,
                outcome=Outcome.SENDING,
                claimed_at=now,
            )
        return True
    except IntegrityError:
        # A failed attempt that PayPal never recorded released its claim; the
        # next request retakes it atomically and resends under the same reference.
        retaken = PayPalOperation.objects.filter(reference=reference, outcome=Outcome.FAILED, provider_id="").update(
            outcome=Outcome.SENDING, claimed_at=now, error=""
        )
        return retaken == 1


def load_existing(reference: str) -> PayPalOperation:
    return PayPalOperation.objects.get(reference=reference)


def complete(
    reference: str,
    outcome: str,
    answer: Answer | None = None,
    *,
    error: str = "",
    apply: Apply | None = None,
    result: Any = None,
) -> PayPalOperation:
    with transaction.atomic():
        op = PayPalOperation.objects.select_for_update().get(reference=reference)
        op.outcome = outcome
        if answer is not None:
            op.provider_id = answer.provider_id[:64]
            op.provider_status = answer.provider_status[:64]
            op.provider_time = answer.provider_time
        op.error = error[:255]
        op.save()
        if apply is not None and answer is not None:
            apply(op, answer, result)
    return op


def safe_write(
    *,
    reference: str,
    kind: str,
    order_id: int | None,
    send: Callable[[str], T],
    find: Callable[[str], T | None],
    read: Callable[[T], Answer],
    repeat_is_safe: bool,
    sent: tuple[Decimal, str] | None,
    apply: Apply | None = None,
    recheck_pending: bool = False,
) -> WriteResult[T]:
    """Run one provider write step exactly once, whoever asks and however often.

    send(key)        the SDK call, sending ``key`` as its PayPal-Request-Id
    find(ref)        what ``send`` returns for the write carrying ``ref``, or None
    read(result)     that response as an ``Answer``
    repeat_is_safe   PayPal replays the original for a repeated request id, so a
                     resend under the same reference is the check
    sent             the (amount, currency) asked for; None when no money moves
    apply            applies the outcome to local state, inside ``complete``
    recheck_pending  ask PayPal again when an earlier answer was "pending"
    """
    amount, currency = sent if sent is not None else (None, "")

    # 1. CLAIM FIRST. The store decides the winner; there is no check-then-act gap.
    checking = False
    if not try_claim(reference, kind, order_id, amount, currency):
        existing = load_existing(reference)
        if existing.outcome == Outcome.SENDING and existing.claimed_at > timezone.now() - SEND_WINDOW:
            return WriteResult(existing, None)  # in flight elsewhere: make no provider call
        settled = existing.outcome not in (Outcome.SENDING, Outcome.UNKNOWN)
        if settled and not (recheck_pending and existing.outcome == Outcome.PENDING):
            return WriteResult(existing, None)  # answer from what is recorded
        checking = True  # stale sender, unresolved, or pending: LOOK, never create anew

    resending = checking and repeat_is_safe

    # 2. CALL - a first attempt, or a check by same-reference resend.
    result: T | None = None
    if resending or not checking:
        try:
            result = send(reference)
        except NEVER_SENT:
            complete(reference, Outcome.UNKNOWN if resending else Outcome.FAILED, error="never sent")
            if resending:
                raise OutcomeUnknown(reference) from None
            raise
        except ApiError as e:
            if _ALREADY_PROCESSED_ISSUES.intersection(provider_issues(e)):
                pass  # an earlier attempt landed: find it below
            elif e.status_code < 500:
                refused = (e.status_code in (400, 422) and not isinstance(e.error, OAuthProviderError)) or not resending
                issues = ",".join(provider_issues(e)) or f"HTTP {e.status_code}"
                complete(reference, Outcome.FAILED if refused else Outcome.UNKNOWN, error=issues)
                if not refused:
                    raise OutcomeUnknown(reference) from e
                raise
            # a 5xx on a write may still have landed: look it up below
        except (httpx.RequestError, ValueError):
            pass  # sent, and no readable answer: may have landed

    # 3. CHECK, by the reference that was sent.
    if result is None:
        try:
            result = find(reference)
        except (ApiError, httpx.RequestError, ValueError):
            complete(reference, Outcome.UNKNOWN, error="lookup failed")
            raise OutcomeUnknown(reference) from None
        if result is None:
            complete(reference, Outcome.UNKNOWN, error="not found yet")
            raise OutcomeUnknown(reference)

    # 4. VERIFY before keeping it.
    got = read(result)
    if sent is not None and (got.amount is not None or got.outcome == Outcome.DONE):
        echoed = (None if got.amount is None else Decimal(str(got.amount)), got.currency)
        if echoed != (Decimal(str(sent[0])), sent[1]):
            review = got._replace(outcome=Outcome.NEEDS_REVIEW)
            complete(reference, Outcome.NEEDS_REVIEW, review, error="amount mismatch", apply=apply, result=result)
            raise AmountMismatch(reference, f"{sent[0]} {sent[1]}", f"{got.amount} {got.currency}")

    # 5. COMPLETE from what PayPal said, not from the fact that it answered.
    op = complete(reference, got.outcome, got, apply=apply, result=result)
    return WriteResult(op, result)
