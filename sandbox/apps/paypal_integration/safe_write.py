"""
The one path every PayPal write goes through: claim, call, check, verify,
complete.

The claim is a ``ProviderWrite`` row with a UNIQUE ``ref``, committed before
PayPal is called, so a second request for the same step (a double-click, a
retry, a second worker) is rejected by the database. The same ``ref`` is sent
as the PayPal-Request-Id, which PayPal de-duplicates: when an earlier attempt
got no answer, resending under the same reference is how we find out what
happened, never a second write.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import gateway
from .models import ProviderWrite

log = logging.getLogger(__name__)

# Passed as `claimed` when the caller already tried the claim itself (under its own lock).
NOT_CLAIMED = object()


class OutcomeUnknown(Exception):
    """PayPal may have acted; only a later check under the same ref can say."""

    def __init__(self, record):
        super().__init__(record.ref)
        self.record = record


class AmountMismatch(Exception):
    def __init__(self, record, echoed):
        super().__init__(record.ref)
        self.record = record
        self.echoed = echoed


def send_window():
    """How long a claim may sit in `sending` before another request checks it."""
    return timedelta(seconds=float(settings.PAYPAL_TIMEOUT) * 2 + 30)


def try_claim(ref, **fields):
    """
    Insert-or-fail. Returns the claimed record, or None when another request
    holds it. A released claim (failed with nothing created at PayPal) is
    re-taken by an atomic conditional UPDATE, so exactly one caller wins.
    """
    now = timezone.now()
    try:
        with transaction.atomic():
            return ProviderWrite.objects.create(ref=ref, claimed_at=now, **fields)
    except IntegrityError:
        pass
    retaken = ProviderWrite.objects.filter(
        ref=ref, outcome=ProviderWrite.FAILED, provider_id='',
    ).update(outcome=ProviderWrite.SENDING, claimed_at=now, message='')
    if retaken:
        return ProviderWrite.objects.get(ref=ref)
    return None


def _take_for_check(record):
    """Only one request re-checks a stale or unknown claim at a time."""
    now = timezone.now()
    taken = ProviderWrite.objects.filter(
        pk=record.pk, outcome=record.outcome, claimed_at=record.claimed_at,
    ).update(outcome=ProviderWrite.SENDING, claimed_at=now)
    if not taken:
        return None
    record.refresh_from_db()
    return record


def complete(record, outcome, answer=None, message='', on_complete=None):
    """Record the outcome (and apply its local effects) in one transaction."""
    with transaction.atomic():
        record.outcome = outcome
        if answer is not None:
            record.provider_id = answer.provider_id or record.provider_id
            record.provider_status = answer.status[:32]
            record.provider_time = answer.provider_time or record.provider_time
            message = message or answer.message
        record.message = message[:512]
        record.save()
        if on_complete is not None and answer is not None:
            on_complete(record, answer)
    return record


def safe_write(ref, *, kind, send, find=None, repeat_is_safe, sent=None,
               resend_window=None, on_complete=None, refresh=None, claimed=None, **claim_fields):
    """
    ref            derived from the operation and step; also the PayPal-Request-Id
    send(ref)      the PayPal call, returning a gateway.Answer
    find(ref)      a lookup of what `send` made (an Answer, or None when not found)
    repeat_is_safe PayPal will not make a second one for this ref (it de-duplicates
                   by PayPal-Request-Id), so the check is a resend under the same ref
    resend_window  how long PayPal keeps the request id; past it, never resend
    sent           (amount, currency) asked for; the echoed money must match
    on_complete    applies the local effects of an answer, atomically with the outcome
    refresh(rec)   re-reads a `pending` write from PayPal (read-only), or None
    claimed        a record the caller already claimed with try_claim, or NOT_CLAIMED
    """
    if claimed is None:
        record = try_claim(ref, kind=kind, **claim_fields)
    else:
        record = None if claimed is NOT_CLAIMED else claimed
    checking = False
    if record is None:
        existing = ProviderWrite.objects.get(ref=ref)
        if existing.outcome == ProviderWrite.SENDING and existing.claimed_at > timezone.now() - send_window():
            return existing                          # in flight: no provider call
        if existing.outcome == ProviderWrite.PENDING and refresh is not None and existing.provider_id:
            return _refresh(existing, refresh, on_complete)
        if existing.outcome not in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN):
            return existing                          # settled: answer from the record
        record = _take_for_check(existing)
        if record is None:
            return ProviderWrite.objects.get(ref=ref)
        checking = True

    within_window = resend_window is None or record.date_created > timezone.now() - resend_window
    resending = checking and repeat_is_safe and within_window

    answer = None
    if resending or not checking:
        try:
            answer = send(ref)
        except Exception as exc:
            c = gateway.classify(exc)
            if c is None:                            # not a PayPal failure: a bug here
                complete(record, ProviderWrite.UNKNOWN, message='internal error during send')
                raise
            log.warning('PayPal write %s failed: %s (%s)', ref, c.kind, c.provider_status)
            if c.kind in (gateway.NEVER_SENT, gateway.REFUSED):
                if resending:                        # a check proves nothing: still unknown
                    complete(record, ProviderWrite.UNKNOWN, message=c.message)
                    raise OutcomeUnknown(record) from exc
                complete(record, ProviderWrite.FAILED, message=c.message)   # releases the claim
                raise gateway.ProviderError(c.http_status, c.message, issues=c.issues) from exc
            # MAYBE: sent, no readable answer - fall through to the check

    if answer is None:
        if find is None or (find is send and not within_window):
            complete(record, ProviderWrite.UNKNOWN, message='PayPal outcome unknown; needs operator review')
            raise OutcomeUnknown(record)
        try:
            answer = find(ref)
        except Exception as exc:
            if gateway.classify(exc) is None:
                raise
            complete(record, ProviderWrite.UNKNOWN, message='PayPal outcome unknown; check failed')
            raise OutcomeUnknown(record) from exc
        if answer is None:
            complete(record, ProviderWrite.UNKNOWN, message='PayPal outcome unknown; not found yet')
            raise OutcomeUnknown(record)

    # Money is verified on whatever took effect; a refusal carries none to compare.
    if sent is not None and answer.outcome in (gateway.DONE, gateway.PENDING):
        sent_amount, sent_currency = sent
        echoed = (answer.amount, answer.currency)
        if answer.amount is None or Decimal(answer.amount) != Decimal(sent_amount) or answer.currency != sent_currency:
            complete(record, ProviderWrite.NEEDS_REVIEW, answer,
                     message='PayPal answered with %s %s, expected %s %s' % (
                         answer.amount, answer.currency, sent_amount, sent_currency))
            raise AmountMismatch(record, echoed)

    return complete(record, answer.outcome, answer, on_complete=on_complete)


def _refresh(record, refresh, on_complete):
    try:
        answer = refresh(record)
    except Exception as exc:
        if gateway.classify(exc) is None:
            raise
        return record                                # still pending as far as we know
    if answer is None or answer.outcome == record.outcome:
        return record
    return complete(record, answer.outcome, answer, on_complete=on_complete)
