"""
Outcome mapping and the one path every provider write goes through.

Every write that sends, cancels or redacts a message is made through
``safe_write``: claim first (a unique row in the database), then call, then —
when the answer is missing or unreadable — look the write up by the reference
it carried, and only then record what the provider said.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx
from django.utils import timezone
from twilio_sdk.core import UNSET, ApiError
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import MessageEnumStatus

from .models import Outcome

logger = logging.getLogger(__name__)

# Failures raised before the request left: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Longer than one attempt's timeout: a "sending" claim younger than this is
# still in flight in another request and must not be touched.
SEND_WINDOW = timedelta(seconds=60)


class OutcomeUnknown(Exception):
    """The write may have happened and the provider has not said whether it did."""

    def __init__(self, reference: str) -> None:
        super().__init__("Outcome unknown for %s" % reference)
        self.reference = reference


# --- status -> outcome --------------------------------------------------------

S = MessageEnumStatus


def delivery_outcome(status: object) -> str:
    """An immediate message: done only once the handset has it."""
    match status:
        case S.DELIVERED | S.READ:
            return Outcome.DONE
        case S.ACCEPTED | S.QUEUED | S.SENDING | S.SENT | S.SCHEDULED:
            return Outcome.PENDING
        case S.FAILED | S.UNDELIVERED | S.CANCELED | S.PARTIALLY_DELIVERED:
            return Outcome.FAILED
        case _:
            # RECEIVING/RECEIVED (inbound), a value newer than the SDK, UNSET.
            return Outcome.UNKNOWN


def schedule_outcome(status: object) -> str:
    """A follow-up create: what was asked is 'queued with the provider for later'."""
    match status:
        case S.SCHEDULED:
            return Outcome.DONE
        case S.ACCEPTED | S.QUEUED:
            return Outcome.PENDING
        case S.FAILED | S.UNDELIVERED | S.CANCELED:
            return Outcome.FAILED
        case _:
            # Including SENDING/SENT/DELIVERED: it went out now, not as asked.
            return Outcome.UNKNOWN


def cancel_outcome(status: object) -> str:
    """Calling a scheduled message off: its done is the cancelled state."""
    match status:
        case S.CANCELED:
            return Outcome.DONE
        case S.SCHEDULED | S.ACCEPTED | S.QUEUED:
            return Outcome.PENDING
        case (
            S.SENDING
            | S.SENT
            | S.DELIVERED
            | S.READ
            | S.UNDELIVERED
            | S.FAILED
            | S.PARTIALLY_DELIVERED
        ):
            # Too late: it already went out.
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def redact_outcome(echoed_body: object) -> str:
    """Content disposal is in effect when the provider echoes an empty body."""
    if echoed_body == "":
        return Outcome.DONE
    return Outcome.UNKNOWN


def answer_status(outcome: str) -> int:
    """The ONE place an outcome becomes the caller's HTTP status."""
    match outcome:
        case Outcome.DONE:
            return 200
        case Outcome.PENDING | Outcome.SENDING:
            return 202
        case Outcome.FAILED | Outcome.NEEDS_REVIEW:
            return 409
        case _:
            return 504


# --- reading a message --------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    provider_id: str
    status: object
    provider_time: datetime | None


def parse_provider_time(value: object) -> datetime | None:
    """Twilio message timestamps are RFC 2822 strings."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def read_message(message: ApiV2010AccountMessage) -> Answer | None:
    """The members every step depends on; None when the answer is truncated."""
    sid = message.sid
    if not isinstance(sid, str) or not sid or message.status is UNSET:
        return None
    return Answer(sid, message.status, parse_provider_time(message.date_sent))


def status_text(status: object) -> str:
    if status is UNSET or status is None:
        return ""
    return str(status)


# --- the claim store ----------------------------------------------------------


class ClaimRecord(Protocol):
    outcome: str
    claimed_at: datetime
    provider_sid: str


class ClaimStore(Protocol):
    reference: str

    def try_claim(self) -> bool:
        """Insert-or-fail: records `sending` for exactly one caller."""

    def load(self) -> ClaimRecord:
        """What is recorded under the reference; may still be in progress."""

    def complete(
        self,
        outcome: str,
        message: ApiV2010AccountMessage | None = None,
        status: object = UNSET,
    ) -> ClaimRecord:
        """Record the outcome (and what the provider returned)."""


def safe_write(
    store: ClaimStore,
    send: Callable[[str], ApiV2010AccountMessage],
    find: Callable[[str], ApiV2010AccountMessage | None],
    outcome_of: Callable[[object], str],
    *,
    repeat_is_safe: bool,
    status_of: Callable[[ApiV2010AccountMessage], object] | None = None,
) -> ClaimRecord:
    """
    Claim, call, check, complete.

    send(ref)      makes the provider call carrying `ref`
    find(ref)      the message that write produced, or None
    outcome_of     this step's own status mapper
    repeat_is_safe re-issuing the write cannot make a second one (cancel,
                   redact) - then the check is a same-reference resend
    status_of      what outcome_of reads (default: the message status)
    """
    ref = store.reference
    checking = False
    if not store.try_claim():
        existing = store.load()
        if (
            existing.outcome == Outcome.SENDING
            and existing.claimed_at > timezone.now() - SEND_WINDOW
        ):
            return existing  # in flight elsewhere: no provider call
        if existing.outcome not in (Outcome.SENDING, Outcome.UNKNOWN):
            return existing  # settled: answer from the record
        checking = True  # stale or unresolved: look, never create

    resending = checking and repeat_is_safe
    result: ApiV2010AccountMessage | None = None
    if resending or not checking:
        try:
            result = send(ref)
        except NEVER_SENT:
            if resending:
                store.complete(Outcome.UNKNOWN)
                raise OutcomeUnknown(ref)
            store.complete(Outcome.FAILED)  # nothing happened; claim released
            raise
        except ApiError as e:
            if e.status_code < 500:
                if resending:
                    store.complete(Outcome.UNKNOWN)
                    raise OutcomeUnknown(ref) from e
                store.complete(Outcome.FAILED)  # refused
                raise
            logger.warning("provider %s on write %s; checking", e.status_code, ref)
        except (httpx.RequestError, ValueError):
            # Sent, and no readable answer (ValueError covers pydantic's
            # ValidationError and a non-JSON body): it may have landed.
            logger.warning("no readable answer for write %s; checking", ref)

    if result is None or read_message(result) is None:
        try:
            result = find(ref)
        except (ApiError, httpx.RequestError, ValueError) as e:
            store.complete(Outcome.UNKNOWN)
            raise OutcomeUnknown(ref) from e
        if result is None or read_message(result) is None:
            # Not found yet: an empty lookup cannot prove it did not happen.
            store.complete(Outcome.UNKNOWN)
            raise OutcomeUnknown(ref)

    got = read_message(result)
    if got is None:  # unreachable: both paths above return only readable answers
        store.complete(Outcome.UNKNOWN)
        raise OutcomeUnknown(ref)
    status = status_of(result) if status_of is not None else got.status
    return store.complete(outcome_of(status), result, got.status)
