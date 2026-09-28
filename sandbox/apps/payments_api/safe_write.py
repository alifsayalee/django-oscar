"""
The one path every PayPal write goes through: claim, call, check, verify, complete.

* CLAIM -- a ``ProviderWrite`` row keyed by the write's reference is committed
  before PayPal is called. The database's unique constraint decides the winner,
  across threads, processes and hosts; the loser answers from the row.
* CALL -- the reference travels with the write (``PayPal-Request-Id`` and/or
  ``custom_id``).
* CHECK -- when the outcome is unknown (timeout, dropped connection, 5xx,
  unreadable 2xx) the write is looked up by that same reference: a same-id
  resend where PayPal replays the original, a lookup otherwise. Only PayPal's
  answer settles an unknown; nothing turns it into "failed" by time.
* VERIFY -- the echoed amount must equal the requested one (as ``Decimal``).
* COMPLETE -- the row records the outcome mapped from PayPal's own status, the
  PayPal id and PayPal's event time.
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, NamedTuple, TypeVar

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal.core import ApiError

from .errors import NEVER_SENT, AmountMismatch, OutcomeUnknown, paypal_detail, provider_error
from .models import ProviderWrite

logger = logging.getLogger(__name__)

T = TypeVar('T')

# Longer than one attempt's timeout plus margin: a 'sending' row younger than
# this is a request still in flight, older is one whose sender died.
SEND_WINDOW = timedelta(minutes=2)


class Answer(NamedTuple):
    """What one step's PayPal response says, read the same way for every step."""
    provider_id: str
    status: object
    provider_time: datetime | None
    amount: str | None = None
    currency: str | None = None


def provider_time(value: object) -> datetime | None:
    """Parse a PayPal RFC 3339 time; ``None`` when absent or unreadable."""
    if isinstance(value, str):
        return parse_datetime(value)
    return None


def try_claim(ref: str, **fields: Any) -> bool:
    """Insert-or-fail. ``True`` for exactly one caller per reference."""
    try:
        with transaction.atomic():
            ProviderWrite.objects.create(ref=ref, claimed_at=timezone.now(), **fields)
        return True
    except IntegrityError:
        # A 'failed' write that never reached PayPal (or was refused) released
        # its claim: take it again, atomically, under the same reference.
        return ProviderWrite.objects.filter(
            ref=ref, outcome=ProviderWrite.FAILED, provider_id='', detail__released=True,
        ).update(outcome=ProviderWrite.SENDING, claimed_at=timezone.now(), detail={}) == 1


def load_existing(ref: str) -> ProviderWrite:
    return ProviderWrite.objects.get(ref=ref)


def complete(ref: str, outcome: str, answer: Answer | None = None,
             detail: dict[str, Any] | None = None, *, release: bool = False) -> ProviderWrite:
    """Record an outcome. ``release`` frees a claim nothing was done under."""
    write = ProviderWrite.objects.get(ref=ref)
    write.outcome = outcome
    write.completed_at = timezone.now()
    if answer is not None:
        write.provider_id = answer.provider_id or write.provider_id
        write.provider_status = str(answer.status) if answer.status is not None else ''
        write.provider_time = answer.provider_time or write.provider_time
        if answer.amount is not None:
            write.provider_amount = Decimal(str(answer.amount))
    write.detail = {**(detail or {}), **({'released': True} if release else {})}
    write.save()
    return write


def _take_over(write: ProviderWrite) -> bool:
    """Become the one request that checks a stale or unknown write."""
    return ProviderWrite.objects.filter(
        pk=write.pk, outcome=write.outcome, claimed_at=write.claimed_at,
    ).update(outcome=ProviderWrite.SENDING, claimed_at=timezone.now()) == 1


def safe_write(
    ref: str,
    *,
    send: Callable[[str], T],
    find: Callable[[str], T | None],
    read: Callable[[T], Answer],
    outcome_of: Callable[[object], str],
    repeat_is_safe: bool,
    sent: tuple[Decimal, str] | None = None,
    landed: Callable[[ApiError[Any]], bool] | None = None,
    claim: dict[str, Any] | None = None,
    claimed: bool = False,
) -> ProviderWrite:
    """Make one PayPal write at most once, and always end knowing -- or saying
    we do not know -- what happened.

    ref             the write's reference: the same on every attempt and repeat
    send(ref)       makes the PayPal call carrying ``ref``
    find(ref)       the record the write carrying ``ref`` produced, or None
    read(result)    the step's response as an Answer
    outcome_of      the step's own status mapper
    repeat_is_safe  PayPal replays the original for a same-reference resend
    sent            the (amount, currency) asked for; None when no money moves
    landed(e)       whether a refusal says an earlier attempt already did it
    claim           extra fields for the claim row
    claimed         the caller already holds a fresh claim on ``ref``
    """
    checking = False
    if not claimed and not try_claim(ref, **(claim or {})):
        existing = load_existing(ref)
        if existing.outcome == ProviderWrite.SENDING and existing.claimed_at > timezone.now() - SEND_WINDOW:
            return existing  # in flight: answer "in progress", call nothing
        if existing.outcome not in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN):
            return existing  # settled: answer from the record
        if not _take_over(existing):
            return load_existing(ref)  # another request is checking it right now
        checking = True  # stale sender or unresolved: look, never create anew

    # A check re-sends only where PayPal replays the original for the same id.
    resending = checking and repeat_is_safe

    result: T | None = None
    if resending or not checking:
        try:
            result = send(ref)
        except NEVER_SENT:
            if resending:
                complete(ref, ProviderWrite.UNKNOWN, detail={'reason': 'check_not_sent'})
                raise OutcomeUnknown(ref)
            complete(ref, ProviderWrite.FAILED, detail={'reason': 'not_sent'}, release=True)
            raise
        except ApiError as e:
            if landed is not None and landed(e):
                pass  # an earlier attempt did it: read the record below
            elif e.status_code < 500:
                if resending:  # a check never fails the write
                    complete(ref, ProviderWrite.UNKNOWN, detail={'reason': 'check_refused', 'paypal': paypal_detail(e.error)})
                    raise OutcomeUnknown(ref) from e
                complete(ref, ProviderWrite.FAILED,
                         detail={'reason': 'refused', 'status': e.status_code, 'paypal': paypal_detail(e.error)},
                         release=True)
                raise provider_error(e.status_code, e.error) from e
            # a 5xx on a write may still have landed: fall through to the lookup
        except (httpx.RequestError, ValueError):
            pass  # sent, and no readable answer: it may have landed

    if result is None:
        try:
            result = find(ref)
        except (ApiError, httpx.RequestError, ValueError) as e:
            logger.warning('PayPal write %s: lookup failed (%s)', ref, type(e).__name__)
            complete(ref, ProviderWrite.UNKNOWN, detail={'reason': 'lookup_failed'})
            raise OutcomeUnknown(ref) from e
        if result is None:  # not found (yet): absence proves nothing
            complete(ref, ProviderWrite.UNKNOWN, detail={'reason': 'not_found_yet'})
            raise OutcomeUnknown(ref)

    got = read(result)
    if sent is not None:
        asked_amount, asked_currency = sent
        echoed = (None if got.amount is None else Decimal(str(got.amount)), got.currency)
        if echoed != (asked_amount, asked_currency):
            complete(ref, ProviderWrite.NEEDS_REVIEW, got,
                     detail={'reason': 'amount_mismatch', 'echoed': [str(echoed[0]), echoed[1]]})
            raise AmountMismatch(ref, '%s %s' % (asked_amount, asked_currency), '%s %s' % echoed)

    outcome = outcome_of(got.status)
    if outcome == ProviderWrite.DONE and not got.provider_id:
        outcome = ProviderWrite.UNKNOWN  # "done" with nothing to name is not done
    return complete(ref, outcome, got)
