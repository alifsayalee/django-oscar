"""
The one path every write to the provider goes through: claim, call, check,
complete.

The claim is a ``ProviderWrite`` row with a unique reference, committed before
the provider is called. A request that loses the claim never makes a new
write; its only provider call is a lookup of what the first one did.
"""
import base64
import hashlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, NamedTuple

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from pydantic import ValidationError
from twilio_sdk.core import ApiError

from .gateway import NEVER_SENT, provider_code, provider_message
from .models import Notification, Outcome, ProviderWrite

logger = logging.getLogger(__name__)

# Longer than one attempt plus its lookup: a claim younger than this is still in flight.
SEND_WINDOW = timedelta(minutes=2)

# Where a first attempt's refusal (a provider 4xx) leaves the write.
FAIL_ON_REFUSAL = 'fail'       # nothing landed: record failed, release the claim
LOOKUP_ON_REFUSAL = 'lookup'   # an undoing step: read the record to see where it stands

GONE = object()   # the provider no longer has the record


class Answer(NamedTuple):
    provider_id: str
    status: Any                 # the step's own status value - ``outcome_of`` maps it
    provider_time: datetime | None = None   # the provider's clock for the event, when it gives one
    provider_status: str = ''
    error_code: int | None = None
    error_message: str = ''


class OutcomeUnknown(Exception):
    """The write may have landed and the provider has not (yet) said so."""

    def __init__(self, write: ProviderWrite) -> None:
        super().__init__(write.reference)
        self.write = write


class WriteRefused(Exception):
    """The provider refused a first attempt; nothing landed and the claim is released."""

    def __init__(self, write: ProviderWrite, status_code: int, code: int | None, message: str) -> None:
        super().__init__(message)
        self.write = write
        self.status_code = status_code
        self.code = code
        self.message = message


class NeverSent(Exception):
    """A first attempt never left this process; nothing happened and the claim is released."""

    def __init__(self, write: ProviderWrite) -> None:
        super().__init__(write.reference)
        self.write = write


def deterministic_ref(*parts: object) -> str:
    """A write's reference: this install's prefix, then what identifies the operation and step."""
    return ':'.join([settings.SMS_REFERENCE_PREFIX, *[str(p) for p in parts]])


def body_token(reference: str) -> str:
    """A short, stable token for ``reference`` that travels in a message body - its only searchable field."""
    digest = hashlib.sha256(reference.encode()).digest()
    return base64.b32encode(digest).decode()[:8]


# Store
# =====

def try_claim(ref: str, notification: Notification, step: str) -> bool:
    """Insert-or-fail. True for exactly one caller while the claim is held."""
    try:
        with transaction.atomic():
            ProviderWrite.objects.create(
                reference=ref, notification=notification, step=step,
                outcome=Outcome.SENDING, claimed_at=timezone.now())
        return True
    except IntegrityError:
        pass
    # A released claim (a first attempt that never left, or was refused) may be taken again.
    # The conditional update is atomic: the row count names the one winner.
    taken = ProviderWrite.objects.filter(
        reference=ref, outcome=Outcome.FAILED, provider_id='',
    ).update(outcome=Outcome.SENDING, claimed_at=timezone.now(), completed_at=None,
             error_code=None, error_message='')
    return taken == 1


def load_existing(ref: str) -> ProviderWrite | None:
    return ProviderWrite.objects.filter(reference=ref).first()


def complete(ref: str, outcome: str, answer: Answer | None = None, *,
             error_code: int | None = None, error_message: str = '') -> ProviderWrite:
    fields: dict[str, Any] = {'outcome': outcome, 'completed_at': timezone.now()}
    if answer is not None:
        fields.update(
            provider_id=answer.provider_id or '',
            provider_status=answer.provider_status or '',
            provider_time=answer.provider_time,
            error_code=answer.error_code,
            error_message=(answer.error_message or '')[:255],
        )
    else:
        fields.update(error_code=error_code, error_message=(error_message or '')[:255])
    ProviderWrite.objects.filter(reference=ref).update(**fields)
    return ProviderWrite.objects.get(reference=ref)


# The safe write
# ==============

def safe_write(ref: str, *, notification: Notification, step: str,
               send: Callable[[str], Any], find: Callable[[str], Any], read: Callable[[Any], Answer],
               outcome_of: Callable[[Any], str], repeat_is_safe: bool = False,
               on_refusal: str = FAIL_ON_REFUSAL, lookup_only: bool = False) -> ProviderWrite | None:
    """
    Make (or find) one provider write, exactly once per ``ref``.

    send(key)     makes the provider call, with ``key`` as its reference
    find(ref)     the provider's record for the write carrying ``ref``, or None
    read(result)  the record as an ``Answer``
    outcome_of    the step's own status mapper
    repeat_is_safe  a resend under the same reference cannot make a second one
    lookup_only   never send: only re-read an existing claim (a status refresh)

    Returns the ``ProviderWrite``; raises ``OutcomeUnknown``, ``WriteRefused``
    or ``NeverSent``.
    """
    checking = False
    existing: ProviderWrite | None = None
    if lookup_only:
        existing = load_existing(ref)
        if existing is None or existing.outcome in (Outcome.DONE, Outcome.FAILED, Outcome.NEEDS_REVIEW):
            return existing
        if existing.outcome == Outcome.SENDING and existing.claimed_at > timezone.now() - SEND_WINDOW:
            return existing
        checking = True
    elif not try_claim(ref, notification, step):
        existing = ProviderWrite.objects.get(reference=ref)
        if existing.outcome == Outcome.SENDING and existing.claimed_at > timezone.now() - SEND_WINDOW:
            return existing    # in flight elsewhere: answer "in progress", no provider call
        if existing.outcome not in (Outcome.SENDING, Outcome.UNKNOWN, Outcome.PENDING):
            return existing    # settled: answer from it
        checking = True        # stale sender, unresolved or still pending: look, never create

    was_pending = existing is not None and existing.outcome == Outcome.PENDING
    resending = checking and repeat_is_safe and not was_pending

    result = None
    if resending or not checking:
        try:
            result = send(ref)
        except NEVER_SENT:
            if resending:
                raise OutcomeUnknown(complete(ref, Outcome.UNKNOWN))
            write = complete(ref, Outcome.FAILED, error_message='Never reached the provider.')
            raise NeverSent(write)
        except ApiError as e:
            if 400 <= e.status_code < 500:
                if on_refusal == FAIL_ON_REFUSAL and not resending:
                    code, message = provider_code(e.error), provider_message(e.error)
                    write = complete(ref, Outcome.FAILED, error_code=code,
                                     error_message=message or 'Refused by the provider.')
                    raise WriteRefused(write, e.status_code, code, message)
                # An undoing step, or a check: read where the record stands.
            # A 5xx may still have landed: fall through to the lookup.
        except (httpx.RequestError, ValidationError, ValueError):
            pass    # sent, and no readable answer: it may have landed

    if result is None:
        try:
            result = find(ref)
        except (ApiError, httpx.RequestError, ValidationError, ValueError):
            result = None   # a failed lookup is not an absence
        if result is None:
            if checking and was_pending:
                return existing
            raise OutcomeUnknown(complete(ref, Outcome.UNKNOWN))

    got = read(result)
    outcome = outcome_of(got.status) if got.provider_id or result is GONE else Outcome.UNKNOWN
    write = complete(ref, outcome, got)
    if outcome == Outcome.UNKNOWN:
        logger.warning('Provider write %s completed with an unmapped status %r', ref, got.provider_status)
    return write
