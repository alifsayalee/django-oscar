"""
The one path every PayPal write goes through: claim, call, check, verify, complete.

The claim is a ``ProviderWrite`` row whose UNIQUE ``ref`` is inserted in its
own committed transaction *before* PayPal is called, so a double-click, a
caller retry or a second worker racing on the same step is rejected by the
database, not by a check-then-act read. The same ``ref`` travels to PayPal as
``PayPal-Request-Id``, which is how an outcome we could not read is settled:
by re-sending under the same reference (PayPal returns the original).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Any

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from pay_pal_server_sdk.core import ApiError

from .models import Outcome, ProviderWrite
from .paypal import NEVER_SENT, describe_error

logger = logging.getLogger("apps.payments")

# Longer than one call's worst case (client timeout 20s, no retries): a claim
# still "sending" after this is a sender that died, and the next request checks it.
SEND_WINDOW = timedelta(minutes=2)


@dataclass(frozen=True)
class Answer:
    """What ONE step's response says, read the same way for every step."""

    provider_id: str | None
    status: Any
    provider_time: datetime | None
    amount: str | None = None
    currency: str | None = None


@dataclass
class WriteResult:
    record: ProviderWrite
    response: Any = None  # the provider's response when THIS request obtained one

    @property
    def outcome(self) -> str:
        return self.record.outcome


class OutcomeUnknown(Exception):
    def __init__(self, record: ProviderWrite):
        super().__init__(record.ref)
        self.record = record


def provider_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = parse_datetime(value)
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


# --- the claim store -------------------------------------------------------

def try_claim(ref: str, kind: str) -> bool:
    """Insert-or-fail: exactly one caller records ``sending`` under ``ref``."""
    try:
        with transaction.atomic():
            ProviderWrite.objects.create(ref=ref, kind=kind, outcome=Outcome.SENDING, claimed_at=timezone.now())
        return True
    except IntegrityError:
        return False


def load_existing(ref: str) -> ProviderWrite:
    return ProviderWrite.objects.get(ref=ref)


def complete(
    ref: str,
    outcome: str,
    answer: Answer | None = None,
    *,
    detail: str = "",
    on_complete: Callable[[ProviderWrite, Any], None] | None = None,
    response: Any = None,
) -> ProviderWrite:
    """Record the outcome (and, in the same transaction, the domain fields built from the response)."""
    with transaction.atomic():
        record = ProviderWrite.objects.select_for_update().get(ref=ref)
        if outcome == Outcome.FAILED and answer is None and not record.provider_id:
            # Nothing happened at PayPal: release the claim so the next request may take it again.
            record.delete()
            record.outcome = Outcome.FAILED
            record.detail = detail
            return record
        record.outcome = outcome
        record.completed_at = timezone.now()
        if detail:
            record.detail = detail[:500]
        if answer is not None:
            record.provider_id = answer.provider_id or record.provider_id
            record.provider_status = str(answer.status or "")[:64]
            record.provider_time = answer.provider_time or record.provider_time
            if answer.amount is not None:
                record.amount = Decimal(str(answer.amount))
            record.currency = answer.currency or record.currency
        record.save()
        if on_complete is not None and response is not None:
            on_complete(record, response)
        return record


def _landed(check: Callable[[ApiError[Any]], Any] | None, e: ApiError[Any]) -> Any:
    if check is None or not 400 <= e.status_code < 500:
        return None
    try:
        return check(e)
    except Exception:  # a failed lookup settles nothing
        logger.warning("PayPal lookup after a rejection failed", exc_info=True)
        return None


# --- the safe write --------------------------------------------------------

def safe_write(
    ref: str,
    *,
    kind: str,
    send: Callable[[str], Any],
    read: Callable[[Any], Answer],
    outcome_of: Callable[[object], str],
    repeat_is_safe: bool,
    sent: tuple[Decimal, str] | None = None,
    on_complete: Callable[[ProviderWrite, Any], None] | None = None,
    landed_on_rejection: Callable[[ApiError[Any]], Any] | None = None,
) -> WriteResult:
    """
    The ONE path for every PayPal write step.

    ``send(ref)`` makes the call with ``ref`` as its ``PayPal-Request-Id``. Every
    write here de-duplicates on that header, so the check for an unknown outcome
    is a resend under the same reference (``repeat_is_safe``); nothing else is a
    lookup. ``landed_on_rejection`` reads the provider's record after a 4xx that
    may mean "already done" (e.g. an authorization that is already voided).
    """
    # 1. CLAIM FIRST.
    checking = False
    existing: ProviderWrite | None = None
    if not try_claim(ref, kind):
        existing = load_existing(ref)
        if existing.outcome == Outcome.SENDING and existing.claimed_at > timezone.now() - SEND_WINDOW:
            return WriteResult(existing)  # in flight: answer "in progress", no provider call
        if existing.outcome not in (Outcome.SENDING, Outcome.UNKNOWN, Outcome.PENDING):
            return WriteResult(existing)  # done / failed / needs_review: answer from it
        if existing.outcome == Outcome.PENDING:
            return WriteResult(existing)  # a resend would only replay the pending answer
        checking = True  # stale sender or unresolved: check by a same-reference resend
    resending = checking and repeat_is_safe
    if checking and not resending:
        # No lookup exists for this step: it stays unknown, visible to an operator.
        assert existing is not None
        raise OutcomeUnknown(existing)

    # 2. CALL (a first attempt, or the check by same-reference resend).
    result: Any = None
    try:
        result = send(ref)
    except NEVER_SENT:
        if resending:
            raise OutcomeUnknown(complete(ref, Outcome.UNKNOWN)) from None
        complete(ref, Outcome.FAILED, detail="never sent")
        raise
    except ApiError as e:
        landed = _landed(landed_on_rejection, e)
        if landed is not None:
            result = landed  # an earlier attempt landed: keep it
        elif e.status_code < 500 and not resending:
            complete(ref, Outcome.FAILED, detail=describe_error(e.error))
            raise
        elif e.status_code < 500:
            # A 4xx on a check settles nothing.
            raise OutcomeUnknown(complete(ref, Outcome.UNKNOWN, detail=describe_error(e.error))) from e
        else:
            # A 5xx on a write may still have landed.
            raise OutcomeUnknown(complete(ref, Outcome.UNKNOWN, detail=describe_error(e.error))) from e
    except (httpx.RequestError, ValueError) as e:  # ValueError covers pydantic's ValidationError
        logger.warning("PayPal write %s: no readable answer (%s)", ref, type(e).__name__)
        raise OutcomeUnknown(complete(ref, Outcome.UNKNOWN, detail=type(e).__name__)) from e

    if result is None:
        raise OutcomeUnknown(complete(ref, Outcome.UNKNOWN, detail="empty response"))

    # 4. VERIFY the echoed money before keeping it.
    got = read(result)
    if sent is not None and got.amount is None:
        # The response does not say what moved: unreadable, not a mismatch and never success.
        raise OutcomeUnknown(complete(ref, Outcome.UNKNOWN, got, detail="no amount echoed"))
    if sent is not None:
        sent_amount, sent_currency = sent
        echoed = (None if got.amount is None else Decimal(str(got.amount)), got.currency)
        if echoed != (Decimal(str(sent_amount)), sent_currency):
            # It happened, but not as asked: visible as needs_review, never "done".
            logger.error("PayPal write %s echoed %s %s, expected %s %s", ref, *echoed, sent_amount, sent_currency)
            record = complete(ref, Outcome.NEEDS_REVIEW, got,
                              detail=(f"amount mismatch: PayPal reports {echoed[0]} {echoed[1]}, "
                                      f"asked {sent_amount} {sent_currency}"),
                              on_complete=on_complete, response=result)
            return WriteResult(record, result)

    # 5. COMPLETE from what PayPal SAID.
    record = complete(ref, outcome_of(got.status), got, on_complete=on_complete, response=result)
    return WriteResult(record, result)
