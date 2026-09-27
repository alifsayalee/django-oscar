"""
The one path every provider write (send, schedule, cancel, redact) goes
through: claim, call, check, verify, complete.

The claim is a row in ``ProviderWrite`` whose unique ``reference`` is enforced
by the database, committed before the provider is called, so it holds across
requests, processes and restarts.
"""
import hashlib
import logging
from datetime import timedelta
from datetime import datetime
from typing import Any, Callable, NamedTuple

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from pydantic import ValidationError
from twilio_sdk.core import ApiError, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage
from twilio_sdk.models.enums import MessageEnumStatus

from .models import ProviderWrite
from .provider import NEVER_SENT, message_time

logger = logging.getLogger(__name__)

# Longer than one call's worst case (the SDK does not retry: one timeout).
SEND_WINDOW = timedelta(minutes=2)

DONE, PENDING, FAILED, UNKNOWN = (
    ProviderWrite.DONE, ProviderWrite.PENDING, ProviderWrite.FAILED, ProviderWrite.UNKNOWN)
SENDING, NEEDS_REVIEW = ProviderWrite.SENDING, ProviderWrite.NEEDS_REVIEW


class OutcomeUnknown(Exception):
    """The write may have landed and the provider could not tell us."""

    def __init__(self, write: ProviderWrite) -> None:
        super().__init__(write.reference)
        self.write = write


class Answer(NamedTuple):
    provider_id: str | None
    status: Any
    provider_time: datetime | None
    error_code: int | None = None


# ---------------------------------------------------------------- references

def deterministic_ref(*parts: object) -> str:
    """A reference unique to this install and operation - the same on every attempt."""
    return ':'.join([settings.SMS_REFERENCE_PREFIX] + [str(p) for p in parts])


def ref_token(reference: str) -> str:
    """The short token that carries a reference inside the message text."""
    return hashlib.sha256(reference.encode()).hexdigest()[:8].upper()


# ---------------------------------------------------------------- outcome mappers

def status_from_provider(status: object) -> str:
    """A send step's message status -> our outcome."""
    match status:
        case MessageEnumStatus.DELIVERED | MessageEnumStatus.READ:
            return DONE
        case (MessageEnumStatus.ACCEPTED | MessageEnumStatus.QUEUED | MessageEnumStatus.SENDING
              | MessageEnumStatus.SENT | MessageEnumStatus.SCHEDULED
              | MessageEnumStatus.PARTIALLY_DELIVERED):
            return PENDING      # accepted, not (yet known to be) delivered
        case MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return FAILED
        case MessageEnumStatus.CANCELED:
            return FAILED       # undone: it will not reach the shopper
        case _:
            return UNKNOWN      # inbound states, a value newer than the SDK, or absent


def cancel_outcome(status: object) -> str:
    """A cancel step's own mapping: its done is the message never reaching anyone."""
    match status:
        case MessageEnumStatus.CANCELED | MessageEnumStatus.FAILED | MessageEnumStatus.UNDELIVERED:
            return DONE
        case MessageEnumStatus.SCHEDULED | MessageEnumStatus.ACCEPTED | MessageEnumStatus.QUEUED:
            return PENDING      # the cancel has not taken effect yet
        case (MessageEnumStatus.SENDING | MessageEnumStatus.SENT | MessageEnumStatus.DELIVERED
              | MessageEnumStatus.READ | MessageEnumStatus.PARTIALLY_DELIVERED):
            return FAILED       # too late: it went out
        case _:
            return UNKNOWN


REDACTED, TEXT_PRESENT = 'redacted', 'text_present'


def redact_outcome(state: object) -> str:
    """A redaction step: done only when the provider echoed an empty body."""
    match state:
        case 'redacted':
            return DONE
        case 'text_present':
            return NEEDS_REVIEW     # the provider still holds text
        case _:
            return UNKNOWN          # no body in the answer: cannot tell


def read_redaction(message: ApiV2010AccountMessage) -> Answer:
    """A Message resource as an Answer whose status is the redaction state - never the text."""
    body = message.body
    if isinstance(body, UnsetType):
        state = None
    else:
        state = REDACTED if body in ('', None) else TEXT_PRESENT
    return read_message(message)._replace(status=state)


def read_message(message: ApiV2010AccountMessage) -> Answer:
    """A Message resource as an Answer (sid, status, provider clock, error code)."""
    sid = message.sid
    return Answer(
        provider_id=sid if isinstance(sid, str) and sid else None,
        status=None if isinstance(message.status, UnsetType) else message.status,
        provider_time=message_time(message),
        error_code=None if isinstance(message.error_code, UnsetType) else message.error_code)


# ---------------------------------------------------------------- store

def try_claim(reference: str, operation: str,
              on_claim: Callable[[ProviderWrite], None] | None = None,
              retry_outcomes: tuple[str, ...] = ()) -> tuple[ProviderWrite, bool]:
    """
    Insert-or-fail. Returns (write, won). ``on_claim(write)`` runs in the same
    committed transaction as the claim, so the record pointing at it exists
    before the provider is called. ``retry_outcomes`` lets an idempotent write
    (a redaction) be attempted again after it settled on one of them.
    """
    now = timezone.now()
    try:
        with transaction.atomic():
            write = ProviderWrite.objects.create(
                reference=reference, operation=operation, outcome=SENDING, claimed_at=now)
            if on_claim is not None:
                on_claim(write)
        return write, True
    except IntegrityError:
        pass
    # A failure that never reached the provider released the claim: take it
    # again, atomically (a conditional update only one caller can win).
    released = ProviderWrite.objects.filter(reference=reference).filter(
        Q(outcome=FAILED, provider_id__isnull=True) | Q(outcome__in=retry_outcomes))
    if released.update(outcome=SENDING, claimed_at=now, provider_status='', error_code=None):
        return ProviderWrite.objects.get(reference=reference), True
    return ProviderWrite.objects.get(reference=reference), False


def complete(write: ProviderWrite, outcome: str, answer: Answer | None = None) -> ProviderWrite:
    write.outcome = outcome
    if answer is not None:
        if answer.provider_id:
            write.provider_id = answer.provider_id
        if answer.status is not None:
            write.provider_status = str(answer.status)
        if answer.provider_time is not None:
            write.provider_time = answer.provider_time
        write.error_code = answer.error_code
    write.save()
    logger.info('provider write %s (%s) -> %s [%s]', write.pk, write.operation, outcome,
                write.provider_id or '-')
    return write


def says_already_done(exc: ApiError[Any], operation: str) -> bool:
    """A 4xx that means an earlier attempt already landed (checked on the body, not a lookup)."""
    if operation != ProviderWrite.CANCEL:
        return False
    try:
        body = exc.error.json()
    except ValueError:
        return False
    message = str(body.get('message', '')).lower() if isinstance(body, dict) else ''
    return 'already' in message and 'cancel' in message


# ---------------------------------------------------------------- the safe write

def _loser_must_check(write: ProviderWrite) -> bool | None:
    """
    For a request that lost the claim: None -> answer from the record and make
    no provider call (in flight elsewhere, or settled); True -> the outcome is
    unresolved, so check with the provider (never create).
    """
    if write.outcome == SENDING and write.claimed_at > timezone.now() - SEND_WINDOW:
        return None
    if write.outcome not in (SENDING, UNKNOWN):
        return None
    return True


def _call(write: ProviderWrite, operation: str, resending: bool,
          send: Callable[[ProviderWrite], ApiV2010AccountMessage]) -> ApiV2010AccountMessage | None:
    """Make the provider call. None means it may have landed and must be looked up."""
    try:
        return send(write)
    except NEVER_SENT:
        complete(write, UNKNOWN if resending else FAILED)   # a first send that never left: nothing happened
        if resending:
            raise OutcomeUnknown(write)                     # a check that never left says nothing
        raise
    except ApiError as exc:
        if says_already_done(exc, operation):
            return None                     # an earlier attempt landed: read it back
        if exc.status_code < 500:
            if resending:
                complete(write, UNKNOWN)    # a check never fails the write
                raise OutcomeUnknown(write) from exc
            complete(write, FAILED)         # refused: nothing happened, claim released
            raise
        return None                         # a 5xx may still have landed
    except (httpx.RequestError, ValidationError, ValueError):
        return None                         # sent, no readable answer: may have landed


def _look_up(write: ProviderWrite,
             find: Callable[[ProviderWrite], ApiV2010AccountMessage | None]) -> ApiV2010AccountMessage:
    """Find the write by its reference; anything short of a record leaves it unknown."""
    try:
        result = find(write)
    except (ApiError, httpx.RequestError, ValueError):
        complete(write, UNKNOWN)
        raise OutcomeUnknown(write)
    if result is None:
        complete(write, UNKNOWN)            # not found yet: a timed-out write can still land
        raise OutcomeUnknown(write)
    return result


def safe_write(reference: str, operation: str, *,
               send: Callable[[ProviderWrite], ApiV2010AccountMessage],
               find: Callable[[ProviderWrite], ApiV2010AccountMessage | None],
               read: Callable[[ApiV2010AccountMessage], Answer] = read_message,
               outcome_of: Callable[[object], str] = status_from_provider,
               repeat_is_safe: bool = False,
               on_claim: Callable[[ProviderWrite], None] | None = None,
               retry_outcomes: tuple[str, ...] = ()) -> ProviderWrite:
    """
    The one path for a provider write: claim, call, check, verify, complete.

    ``send(write)`` makes the provider call; ``find(write)`` returns the
    provider's record for this write or None; ``read`` turns either into an
    Answer; ``outcome_of`` maps its status. ``repeat_is_safe`` means a repeat
    under the same reference cannot make a second one, so the check may resend.
    Returns the completed ProviderWrite, or raises OutcomeUnknown / the first
    send's own error.
    """
    write, won = try_claim(reference, operation, on_claim, retry_outcomes)
    checking = False
    if not won:
        if _loser_must_check(write) is None:
            return write
        checking = True

    resending = checking and repeat_is_safe
    result = _call(write, operation, resending, send) if (resending or not checking) else None
    is_send = operation == ProviderWrite.SEND
    if result is None or (is_send and read(result).provider_id is None):
        result = _look_up(write, find)

    answer = read(result)
    if is_send and answer.provider_id is None:
        complete(write, UNKNOWN)            # a record we cannot name is not a known outcome
        raise OutcomeUnknown(write)
    return complete(write, outcome_of(answer.status), answer)
