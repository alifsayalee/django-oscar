"""
Claim, call, check, verify, complete: the one path every PayPal write that
creates, charges or releases money goes through.

* The claim is a ``PaymentOperation`` row whose ``reference`` is UNIQUE in the
  database, inserted and committed *before* PayPal is called. Two requests
  for the same step race on that insert and the database picks the winner; the
  loser never makes a new write.
* The reference is sent to PayPal (``PayPal-Request-Id``, and ``invoice_id`` on
  the authorization), so an outcome lost in transit is settled by asking
  PayPal about that reference - by a lookup, or by resending under the same
  reference where PayPal de-duplicates it. A timer never settles it.
* What PayPal echoes back is checked against what was asked for before the
  outcome is recorded.
"""
import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone
from paypal.core import ApiError

from .gateway import NEVER_SENT, provider_issue
from .models import PaymentOperation

log = logging.getLogger(__name__)

# Longer than one PayPal call can take (PAYPAL_TIMEOUT, 20 s by default) plus
# our own work: a claim still "sending" inside this window has a live sender.
SEND_WINDOW = timedelta(minutes=2)

T = TypeVar("T")


@dataclass(frozen=True)
class Answer:
    """What one step's PayPal response says, read the same way for every step."""

    provider_id: str
    status: str
    outcome: str
    provider_time: datetime | None
    amount: Decimal | None = None
    currency: str | None = None
    detail: str = ""


class OutcomeUnknown(Exception):
    def __init__(self, op: PaymentOperation) -> None:
        super().__init__(op.reference)
        self.op = op


class AmountMismatch(Exception):
    def __init__(self, op: PaymentOperation, answer: Answer) -> None:
        super().__init__(op.reference)
        self.op = op
        self.answer = answer


def try_claim(**fields: Any) -> tuple[PaymentOperation, bool]:
    """Insert-or-fail. Returns ``(operation, won)``; the loser gets the row the
    winner holds. ``fields`` must include ``reference``."""
    try:
        with transaction.atomic():
            return PaymentOperation.objects.create(**fields), True
    except IntegrityError:
        return PaymentOperation.objects.get(reference=fields["reference"]), False


def retake_released(op: PaymentOperation) -> bool:
    """A claim recorded ``failed`` with no provider id never produced anything
    at PayPal, so it is released: the next request may take it again - as
    atomically as the first claim - and send under the same reference."""
    taken = PaymentOperation.objects.filter(
        pk=op.pk, outcome=PaymentOperation.FAILED, provider_id=""
    ).update(outcome=PaymentOperation.SENDING, claimed_at=timezone.now(), detail="")
    if taken:
        op.refresh_from_db()
    return taken == 1


def complete(op: PaymentOperation, outcome: str, answer: Answer | None = None, detail: str = "") -> PaymentOperation:
    op.outcome = outcome
    if answer is not None:
        if answer.provider_id:
            op.provider_id = answer.provider_id
        op.provider_status = answer.status[:32]
        if answer.provider_time is not None:
            op.provider_time = answer.provider_time
        detail = detail or answer.detail
    op.detail = detail[:255]
    op.save(update_fields=["outcome", "provider_id", "provider_status", "provider_time", "detail", "updated_at"])
    return op


def in_flight(op: PaymentOperation) -> bool:
    return op.outcome == PaymentOperation.SENDING and op.claimed_at > timezone.now() - SEND_WINDOW


def safe_write(
    op: PaymentOperation,
    won: bool,
    *,
    send: Callable[[str], T],
    find: Callable[[str], T | None],
    read: Callable[[T], Answer],
    repeat_is_safe: bool,
    sent: tuple[Decimal, str] | None,
    landed_issues: Collection[str] = (),
    refresh: Callable[[PaymentOperation], T | None] | None = None,
) -> tuple[PaymentOperation, T] | tuple[PaymentOperation, None]:
    """Run one claimed write step.

    ``won``            this request inserted the claim.
    ``send(ref)``      makes the PayPal call with ``ref`` as its request id.
    ``find(ref)``      what ``send`` returns for the write carrying ``ref``, or
                       None; where PayPal de-duplicates by request id, pass
                       ``find=send`` and ``repeat_is_safe=True``.
    ``read(result)``   the response as an ``Answer``.
    ``sent``           the (amount, currency) asked for; None when no money moves.
    ``landed_issues``  PayPal error issues meaning "an earlier attempt already
                       did this" - a landing to look up, not a rejection.
    ``refresh(op)``    re-reads a ``pending`` step's current state.

    Returns the settled operation and the PayPal result it was settled from
    (None when answered from what is already recorded).
    """
    checking = False
    if not won:
        if in_flight(op):
            return op, None  # someone else is sending it right now: no provider call
        if op.outcome == PaymentOperation.PENDING and refresh is not None and op.provider_id:
            try:
                current = refresh(op)
            except (ApiError, httpx.RequestError, ValueError):
                log.warning("Could not refresh pending %s", op.reference)
                return op, None
            if current is None:
                return op, None
            return _settle(op, read(current), sent), current
        if op.outcome not in (PaymentOperation.SENDING, PaymentOperation.UNKNOWN):
            return op, None  # done / pending / failed / needs_review: answer from the record
        checking = True  # a stale sender or an unresolved outcome: look, never create

    resending = checking and repeat_is_safe
    result: T | None = None
    if resending or not checking:
        try:
            result = send(op.reference)
        except NEVER_SENT:
            if resending:
                complete(op, PaymentOperation.UNKNOWN, detail="check could not reach PayPal")
                raise OutcomeUnknown(op) from None
            complete(op, PaymentOperation.FAILED, detail="never sent: PayPal unreachable")
            raise
        except ApiError as exc:
            issue = provider_issue(exc)
            if issue and issue in landed_issues:
                log.info("%s: PayPal reports %s; looking the earlier attempt up", op.reference, issue)
            elif exc.status_code < 500:
                refused = exc.status_code in (400, 422) or not resending
                complete(
                    op,
                    PaymentOperation.FAILED if refused else PaymentOperation.UNKNOWN,
                    detail="PayPal HTTP %s %s" % (exc.status_code, issue),
                )
                if not refused:
                    raise OutcomeUnknown(op) from exc
                raise
            # a 5xx on a write may still have landed: fall through to the lookup
        except (httpx.RequestError, ValueError):
            pass  # sent, and no readable answer: it may have landed

    if result is None:
        try:
            result = find(op.reference)
        except (ApiError, httpx.RequestError, ValueError):
            complete(op, PaymentOperation.UNKNOWN, detail="outcome not yet confirmed by PayPal")
            raise OutcomeUnknown(op) from None
        if result is None:
            complete(op, PaymentOperation.UNKNOWN, detail="outcome not yet confirmed by PayPal")
            raise OutcomeUnknown(op)

    return _settle(op, read(result), sent), result


def _settle(op: PaymentOperation, answer: Answer, sent: tuple[Decimal, str] | None) -> PaymentOperation:
    if sent is not None and answer.outcome in (PaymentOperation.DONE, PaymentOperation.PENDING):
        amount, currency = sent
        if answer.amount is None or answer.currency != currency or Decimal(answer.amount) != Decimal(amount):
            complete(
                op, PaymentOperation.NEEDS_REVIEW, answer,
                detail="PayPal echoed %s %s, expected %s %s" % (answer.amount, answer.currency, amount, currency),
            )
            log.error("%s: amount mismatch (%s %s != %s %s)", op.reference, answer.amount, answer.currency,
                      amount, currency)
            raise AmountMismatch(op, answer)
    return complete(op, answer.outcome, answer)
