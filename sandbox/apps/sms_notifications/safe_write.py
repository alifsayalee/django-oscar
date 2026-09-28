"""
The one path every provider write takes: claim, call, check, complete.

The claim is a row with a unique ``reference`` in this project's own database, inserted *before* the provider is
called. The database rejects a second insert from any thread, process or host, so two requests for the same write
can never both reach the provider. A write whose outcome is unknown (a timeout after sending, a 5xx, an unreadable
answer) is looked up at the provider by that same reference before anything else is decided; nothing ever re-sends
under a new reference, and no timer turns "unknown" into "failed".
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Generic, TypeVar

import httpx
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from twilio_sdk.core import ApiError

from .outcomes import FAILED, PENDING, SENDING, SKIPPED, UNKNOWN
from .provider import NEVER_SENT, NotConfigured, ProviderError, provider_error

# Longer than one attempt's worst case (a single 10s timeout; the SDK does not retry), with room to spare. A claim
# still "sending" after this is treated as abandoned by a crashed request and is looked up, never re-sent.
SEND_WINDOW = timedelta(minutes=2)

R = TypeVar("R")
M = TypeVar("M", bound=models.Model)


class OutcomeUnknown(Exception):
    """The write may have landed and the provider could not (yet) say. Retry only under the same reference."""

    def __init__(self, record: Any) -> None:
        super().__init__("outcome unknown for %s" % type(record).__name__)
        self.record = record


class WriteRefused(Exception):
    """The provider refused a first attempt outright; nothing landed and the claim was released."""

    def __init__(self, record: Any, error: ProviderError) -> None:
        super().__init__(error.message)
        self.record = record
        self.error = error


class NotNeeded(Exception):
    """Raised by a ``send`` that re-checked its preconditions after claiming and found the write must not happen."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Answer:
    """What one step's provider response says, read the same way for every step."""

    provider_id: str
    status: object  # the step's own status member; ``outcome_of`` maps it
    provider_time: datetime | None
    fields: dict[str, Any]  # provider state to record on the claim row


class Unreadable(Exception):
    """A 2xx whose members this step depends on are missing: the write may have happened."""


class ClaimStore(Generic[M]):
    """insert-or-fail claims over one model with a unique ``reference`` column."""

    def __init__(self, model: type[M], defaults: dict[str, Any] | None = None) -> None:
        self.model = model
        self.defaults = defaults or {}

    def try_claim(self, ref: str) -> bool:
        try:
            with transaction.atomic():
                self.model._default_manager.create(
                    reference=ref, outcome=SENDING, claimed_at=timezone.now(), **self.defaults
                )
            return True
        except IntegrityError:
            pass
        # A first attempt that never left, or that the provider refused, released its claim: take it back atomically.
        retaken = self.model._default_manager.filter(reference=ref, outcome=FAILED, **self._released()).update(
            outcome=SENDING, claimed_at=timezone.now(), detail=""
        )
        return retaken == 1

    def _released(self) -> dict[str, Any]:
        # A released claim is "failed" with nothing at the provider.
        if any(f.name == "provider_sid" for f in self.model._meta.get_fields()):
            return {"provider_sid": ""}
        return {"provider_status": ""}

    def load(self, ref: str) -> M:
        return self.model._default_manager.get(reference=ref)

    def complete(self, ref: str, outcome: str, answer: Answer | None = None, detail: str = "") -> M:
        record = self.load(ref)
        record.outcome = outcome  # type: ignore[attr-defined]
        record.detail = detail[:255]  # type: ignore[attr-defined]
        if answer is not None:
            for name, value in answer.fields.items():
                setattr(record, name, value)
        record.save()
        return record


def safe_write(  # noqa: C901 - one linear path, kept in one place on purpose
    store: ClaimStore[M],
    ref: str,
    *,
    send: Callable[[], R],
    find: Callable[[M], R | None],
    read: Callable[[R], Answer],
    outcome_of: Callable[[object], str],
    repeat_is_safe: bool = False,
) -> M:
    """
    Make (or find) one provider write, exactly once per ``ref``.

    send()        makes the provider call
    find(record)  what ``send`` returns for the write this record claimed, or ``None``: a lookup at the provider
    read(result)  the step's response as an :class:`Answer` (raises :class:`Unreadable` when it cannot)
    outcome_of    the step's own status mapper
    """
    checking = False
    existing: M | None = None
    if not store.try_claim(ref):
        existing = store.load(ref)
        outcome = existing.outcome  # type: ignore[attr-defined]
        claimed_at = existing.claimed_at  # type: ignore[attr-defined]
        if outcome == SENDING and claimed_at > timezone.now() - SEND_WINDOW:
            return existing  # in flight elsewhere: answer "in progress", no provider call
        if outcome not in (SENDING, UNKNOWN, PENDING):
            return existing  # settled: answer from it
        checking = True  # abandoned, unresolved or still pending: look, never create

    existing_outcome = existing.outcome if existing is not None else None  # type: ignore[attr-defined]
    resending = checking and repeat_is_safe and existing_outcome != PENDING

    result: R | None = None
    if resending or not checking:
        try:
            result = send()
        except NotNeeded as e:
            return store.complete(ref, SKIPPED, detail=e.reason)
        except NotConfigured as e:
            store.complete(ref, UNKNOWN if resending else FAILED, detail="not configured")
            raise ProviderError(503, str(e)) from e
        except NEVER_SENT as e:
            if resending:
                store.complete(ref, UNKNOWN, detail="check could not reach the provider")
                raise OutcomeUnknown(store.load(ref)) from e
            record = store.complete(ref, FAILED, detail="never sent: provider unreachable")
            raise WriteRefused(record, ProviderError(502, "The SMS provider could not be reached.")) from e
        except ApiError as e:
            if e.status_code < 500 and not resending:
                error = provider_error(e.status_code, e.error)
                record = store.complete(
                    ref, FAILED, detail="refused by provider: HTTP %s code %s" % (e.status_code, error.provider_code)
                )
                raise WriteRefused(record, error) from e
            # a 5xx on a write, or any error on a check: it may have landed - look it up below
        except (httpx.RequestError, ValueError):
            pass  # sent, and no readable answer: it may have landed

    got: Answer | None = None
    if result is not None:
        try:
            got = read(result)
        except Unreadable:
            result = None  # a 2xx we cannot read: look it up rather than guess

    if got is None:
        record_for_lookup = existing if existing is not None else store.load(ref)
        try:
            result = find(record_for_lookup)
        except (ApiError, httpx.RequestError, ValueError, ProviderError, NotConfigured):
            result = None  # the lookup failed: that is not "absent", and nothing creates on it
        if result is None:
            if checking and existing_outcome == PENDING:
                return record_for_lookup  # the provider holds it; this re-read said nothing new
            store.complete(ref, UNKNOWN, detail="no answer from the provider; not found by reference yet")
            raise OutcomeUnknown(store.load(ref))
        try:
            got = read(result)
        except Unreadable as e:
            store.complete(ref, UNKNOWN, detail="provider answer unreadable")
            raise OutcomeUnknown(store.load(ref)) from e

    return store.complete(ref, outcome_of(got.status), got)
