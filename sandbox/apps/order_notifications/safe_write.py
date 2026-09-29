"""The one path every provider write goes through: claim, call, check, complete.

The claim is a ``ProviderWrite`` row whose ``ref`` is unique in the database, so
two requests (or two worker processes) racing for the same write cannot both
reach the provider. It is committed before the provider is called; callers
must not hold an open transaction around ``safe_write``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from twilio_sdk.core import ApiError

from .models import Notification, Outcome, ProviderWrite
from .provider import NEVER_SENT, ProviderRejection, describe_rejection


class OutcomeUnknown(Exception):
    """The write may or may not have happened; only the provider can settle it."""

    def __init__(self, ref: str) -> None:
        super().__init__("outcome unknown")
        self.ref = ref


class WriteRefused(Exception):
    """The provider refused a first attempt; nothing happened and the claim is released."""

    def __init__(self, rejection: ProviderRejection) -> None:
        super().__init__("refused")
        self.rejection = rejection


class WriteNeverSent(Exception):
    """A first attempt never left this process; nothing happened and the claim is released."""


@dataclass(frozen=True)
class Answer:
    """What one step's provider response says, read the same way for every step."""

    provider_id: str | None
    status: Any
    provider_time: datetime | None


@dataclass(frozen=True)
class WriteResult:
    record: ProviderWrite
    payload: Any  # the provider's response for this call, or None when answered from the record


def send_window() -> timedelta:
    # Longer than one attempt (the SDK does not retry) plus scheduling slack.
    return timedelta(seconds=float(settings.TWILIO_TIMEOUT_SECONDS) * 3 + 30)


def write_ref(trigger_key: str, step: str) -> str:
    return "%s:%s:%s" % (settings.ORDER_SMS_REFERENCE_PREFIX, trigger_key, step)


# A lookup that fails is not an absent record: none of these settle anything.
LOOKUP_FAILURES: tuple[type[BaseException], ...] = (ApiError, httpx.RequestError, ValueError) + NEVER_SENT


# -- claim store -----------------------------------------------------------


def try_claim(ref: str, step: str, notification: Notification) -> bool:
    try:
        with transaction.atomic():
            ProviderWrite.objects.create(
                ref=ref,
                step=step,
                notification=notification,
                outcome=Outcome.SENDING,
                claimed_at=timezone.now(),
            )
    except IntegrityError:
        return False  # the database rejected the second claim
    return True


def load_existing(ref: str) -> ProviderWrite:
    return ProviderWrite.objects.get(ref=ref)


def complete(
    ref: str,
    outcome: str,
    provider_id: str | None = None,
    provider_time: datetime | None = None,
) -> ProviderWrite:
    record = ProviderWrite.objects.get(ref=ref)
    if outcome == Outcome.FAILED and not provider_id and not record.provider_id:
        # Nothing reached the provider: release the claim so the same reference
        # may be sent again later.
        record.delete()
        record.outcome = outcome
        return record
    record.outcome = outcome
    if provider_id:
        record.provider_id = provider_id
    if provider_time is not None:
        record.provider_time = provider_time
    record.completed_at = timezone.now()
    record.save(update_fields=["outcome", "provider_id", "provider_time", "completed_at"])
    return record


# -- the safe write --------------------------------------------------------


def safe_write(
    ref: str,
    step: str,
    notification: Notification,
    *,
    send: Callable[[], Any],
    find: Callable[[], Any],
    read: Callable[[Any], Answer],
    outcome_of: Callable[[Any], str],
    repeat_is_safe: bool = False,
    check_on_refusal: bool = False,
) -> WriteResult:
    """Make one provider write at most once per ``ref``.

    send             makes the provider call (the reference travels inside it)
    find             looks the write up at the provider; None when not found
    read             this step's response as an Answer
    outcome_of       this step's status mapper
    repeat_is_safe   re-sending under the same reference cannot make a second one
    check_on_refusal a refused first attempt is settled by ``find`` (an undoing
                     step refused because the thing already moved on)
    """
    assert not connection.in_atomic_block, "safe_write must run outside a transaction"

    # 1. CLAIM FIRST.
    checking = False
    existing: ProviderWrite | None = None
    if not try_claim(ref, step, notification):
        existing = load_existing(ref)
        if existing.outcome == Outcome.SENDING and existing.claimed_at > timezone.now() - send_window():
            return WriteResult(existing, None)  # in flight elsewhere: make no provider call
        if existing.outcome not in (Outcome.SENDING, Outcome.UNKNOWN, Outcome.PENDING):
            return WriteResult(existing, None)  # settled: answer from the record
        checking = True  # stale sender, unresolved or still pending: LOOK, never create

    resending = checking and repeat_is_safe and existing is not None and existing.outcome != Outcome.PENDING

    # 2. CALL - a first attempt, or a check by same-reference resend.
    result: Any = None
    if resending or not checking:
        try:
            result = send()
        except NEVER_SENT as e:
            if resending:
                complete(ref, Outcome.UNKNOWN)
                raise OutcomeUnknown(ref) from e
            complete(ref, Outcome.FAILED)
            raise WriteNeverSent() from e
        except ApiError as e:
            if e.status_code < 500:
                if not resending and not check_on_refusal:
                    complete(ref, Outcome.FAILED)
                    raise WriteRefused(describe_rejection(e.status_code, e.error)) from e
                # a check (or an undoing step) is settled by the lookup below
            result = None  # a 5xx may still have landed: fall through to the lookup
        except (httpx.RequestError, ValueError):  # sent, no readable answer: may have landed
            result = None

    # 3. CHECK, by the reference we sent.
    if result is None:
        try:
            result = find()
        except LOOKUP_FAILURES:
            result = None
        if result is None:
            if checking and existing is not None and existing.outcome == Outcome.PENDING:
                return WriteResult(existing, None)  # the provider holds it; still pending
            complete(ref, Outcome.UNKNOWN)
            raise OutcomeUnknown(ref)

    # 4/5. COMPLETE from what the provider said, not from the fact that it answered.
    got = read(result)
    if not got.provider_id:
        complete(ref, Outcome.UNKNOWN)
        raise OutcomeUnknown(ref)
    record = complete(ref, outcome_of(got.status), got.provider_id, got.provider_time)
    return WriteResult(record, result)
