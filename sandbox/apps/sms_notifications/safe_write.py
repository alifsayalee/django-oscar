"""
The one path every provider write goes through: claim, call, check, complete.

* CLAIM - an INSERT on ``ProviderWrite.reference`` (UNIQUE). The database
  decides which request makes the call; nothing here is a per-process lock.
* CALL - only the request that won the claim ever sends.
* CHECK - when the answer is lost (timeout, 5xx, unreadable body) the write
  may have landed, so it is looked up by the reference it was sent with -
  never re-sent under a new one.
* COMPLETE - the outcome comes from what the provider said (its status), and
  the provider's own clock is stored for reconciliation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone
from twilio_sdk.core import ApiError

from . import twilio_gateway as gw
from .models import Notification, ProviderWrite

logger = logging.getLogger("apps.sms_notifications")

# Longer than one attempt (timeout, no retries) plus slack: a claim older than
# this with no answer belongs to a request that died.
SEND_WINDOW = timedelta(minutes=2)

ProviderRecord = gw.MessageRecord | str  # a record, or gw.GONE


class NotSent(Exception):
    """The write was refused before any request left: nothing happened."""


class OutcomeUnknown(Exception):
    """The write may have landed and the provider has not (yet) said so."""

    def __init__(self, write: ProviderWrite) -> None:
        super().__init__(write.reference)
        self.write = write


# --------------------------------------------------------------------------
# The claim store (the sandbox's own database)
# --------------------------------------------------------------------------


def try_claim(
    reference: str,
    operation: str,
    notification_factory: Callable[[], Notification],
    idempotency_key_hash: str = "",
) -> tuple[ProviderWrite, bool]:
    """
    Insert-or-fail. Returns ``(write, True)`` for exactly one caller; every
    other caller gets the existing record and ``False``.

    For a send, the Notification row is created in the same transaction as
    the claim, so it exists before the provider is called and disappears if
    the claim is lost.
    """
    now = timezone.now()
    try:
        with transaction.atomic():
            notification = notification_factory()
            write = ProviderWrite.objects.create(
                reference=reference,
                operation=operation,
                notification=notification,
                idempotency_key_hash=idempotency_key_hash,
                outcome=ProviderWrite.SENDING,
                claimed_at=now,
            )
        return write, True
    except IntegrityError:
        pass

    existing = ProviderWrite.objects.select_related(
        "notification", "notification__contact_number"
    ).get(reference=reference)
    # A released claim (failed, and the provider holds nothing) may be taken
    # again - by exactly one request: the conditional UPDATE's row count.
    if existing.outcome == ProviderWrite.FAILED and not existing.provider_sid:
        retaken = ProviderWrite.objects.filter(
            pk=existing.pk, outcome=ProviderWrite.FAILED, provider_sid=""
        ).update(outcome=ProviderWrite.SENDING, claimed_at=now, completed_at=None)
        if retaken == 1:
            existing.refresh_from_db()
            return existing, True
        existing.refresh_from_db()
    return existing, False


def complete(
    write: ProviderWrite,
    outcome: str,
    record: ProviderRecord | None = None,
) -> ProviderWrite:
    """Record the outcome - and what the provider said - on the claim."""
    now = timezone.now()
    write.outcome = outcome
    write.completed_at = now
    if isinstance(record, gw.MessageRecord):
        if record.sid:
            write.provider_sid = record.sid
        write.provider_status = record.status_text
        write.provider_time = record.date_sent
    write.save(
        update_fields=["outcome", "completed_at", "provider_sid", "provider_status", "provider_time"]
    )
    if isinstance(record, gw.MessageRecord):
        apply_record(write.notification, record)
    return write


def apply_record(notification: Notification, record: gw.MessageRecord) -> None:
    """Keep the notification's copy of the provider's state current."""
    if record.sid and record.sid != notification.provider_sid:
        if notification.provider_sid:
            # Never re-point a notification at another message.
            logger.error("notification %s: provider sid changed; ignoring record", notification.pk)
            return
        notification.provider_sid = record.sid
    notification.provider_status = record.status_text
    notification.provider_error_code = record.error_code
    if record.date_sent is not None:
        notification.provider_date_sent = record.date_sent
    notification.last_checked_at = timezone.now()
    notification.save(
        update_fields=[
            "provider_sid",
            "provider_status",
            "provider_error_code",
            "provider_date_sent",
            "last_checked_at",
            "updated_at",
        ]
    )


# --------------------------------------------------------------------------
# The safe write
# --------------------------------------------------------------------------


def _stale(write: ProviderWrite, now: datetime) -> bool:
    return write.claimed_at <= now - SEND_WINDOW


def safe_write(
    reference: str,
    *,
    operation: str,
    notification_factory: Callable[[], Notification],
    send: Callable[[ProviderWrite], ProviderRecord],
    find: Callable[[ProviderWrite], ProviderRecord | None],
    outcome_of: Callable[[ProviderRecord], str],
    lookup_on_refusal: bool = False,
    idempotency_key_hash: str = "",
) -> ProviderWrite:
    """
    Make one provider write at most once per ``reference``.

    send(write)       the provider call; raises NotSent if it must not go out
    find(write)       the provider's record for this write, or None if it
                      has none (yet). Looks up by the reference / record id.
    outcome_of(rec)   this step's own status mapper
    lookup_on_refusal for writes on an existing record (call-off, redact):
                      a 4xx is settled by reading that record back - it says
                      whether the record is already in the asked-for state.
    """
    write, claimed = try_claim(reference, operation, notification_factory, idempotency_key_hash)

    checking = False
    if not claimed:
        if write.outcome == ProviderWrite.SENDING and not _stale(write, timezone.now()):
            return write  # in flight elsewhere: answer "in progress", no provider call
        if write.outcome not in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN, ProviderWrite.PENDING):
            return write  # done / failed / needs_review: answer from the record
        checking = True  # stale, unknown or pending: LOOK - never send again

    record: ProviderRecord | None = None
    if not checking:
        try:
            record = send(write)
        except NotSent:
            complete(write, ProviderWrite.FAILED)  # nothing left: the claim is released
            raise
        except gw.NEVER_SENT:
            complete(write, ProviderWrite.FAILED)
            raise
        except ApiError as e:
            logger.warning(
                "provider write %s answered HTTP %s %s", operation, e.status_code, gw.describe_raw_error(e.error)
            )
            if e.status_code < 500 and not lookup_on_refusal:
                complete(write, ProviderWrite.FAILED)  # refused: nothing landed
                raise
            # 5xx: may have landed. 4xx on an existing record: read it back.
        except (httpx.RequestError, ValueError):
            # Sent, and no readable answer: may have landed.
            logger.warning("provider write %s: no readable answer; looking it up", operation)

    if record is None:
        try:
            record = find(write)
        except (ApiError, httpx.RequestError, ValueError):
            record = None  # the lookup failed: that settles nothing
        if record is None:
            if checking and write.outcome == ProviderWrite.PENDING:
                return write  # the provider holds it; this re-read said nothing new
            complete(write, ProviderWrite.UNKNOWN)
            raise OutcomeUnknown(write)

    return complete(write, outcome_of(record), record)


def settle(
    write: ProviderWrite,
    *,
    find: Callable[[ProviderWrite], ProviderRecord | None],
    outcome_of: Callable[[ProviderRecord], str],
) -> ProviderWrite:
    """
    Re-read a write that is not finished (pending, unknown, stale sending)
    from the provider. Never sends; used by read endpoints and before acting
    on an earlier write.
    """
    if write.outcome not in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN, ProviderWrite.PENDING):
        return write
    if write.outcome == ProviderWrite.SENDING and not _stale(write, timezone.now()):
        return write
    try:
        record = find(write)
    except (ApiError, httpx.RequestError, ValueError):
        return write  # could not ask: nothing changes
    if record is None:
        if write.outcome == ProviderWrite.SENDING:
            return complete(write, ProviderWrite.UNKNOWN)  # an empty lookup proves nothing
        return write
    return complete(write, outcome_of(record), record)


# --------------------------------------------------------------------------
# Outcome -> the caller's answer. The only place this mapping lives.
# --------------------------------------------------------------------------


def answer_status(outcome: str) -> int:
    match outcome:
        case ProviderWrite.DONE:
            return 200
        case ProviderWrite.PENDING | ProviderWrite.SENDING:
            return 202  # accepted, not done
        case ProviderWrite.FAILED | ProviderWrite.NEEDS_REVIEW:
            return 409
        case _:
            return 504  # may have happened: never "not created"


OUTCOME_SEVERITY = {
    ProviderWrite.DONE: 0,
    ProviderWrite.PENDING: 1,
    ProviderWrite.SENDING: 1,
    ProviderWrite.FAILED: 2,
    ProviderWrite.NEEDS_REVIEW: 2,
    ProviderWrite.UNKNOWN: 3,
}


def worst_outcome(outcomes: list[str]) -> str:
    if not outcomes:
        return ProviderWrite.DONE
    return max(outcomes, key=lambda o: OUTCOME_SEVERITY.get(o, 3))
