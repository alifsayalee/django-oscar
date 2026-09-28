"""The one path every PayPal write goes through: claim, call, check, verify, complete.

The claim is taken in a store that outlives the request and the process (a unique-indexed DB row),
BEFORE the provider call, under a reference derived from the operation and step. The same reference
travels to PayPal as ``PayPal-Request-Id``, which PayPal de-duplicates, so a check after an unknown
outcome is a resend under the SAME reference and never a new write.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Generic, NamedTuple, Protocol, TypeVar

import httpx
from paypal.core import ApiError, OAuthProviderError

from .errors import NEVER_SENT, AmountMismatch, OutcomeUnknown, ProviderError, issues_of, provider_error
from .outcomes import Outcome

log = logging.getLogger("apps.payments_api.paypal")

T = TypeVar("T")

# Longer than one attempt's worst case (the transport timeout); a claim still "sending" after this was
# abandoned by a crashed or killed request, and the next request checks it instead of waiting forever.
SEND_WINDOW = timedelta(minutes=2)


class Answer(NamedTuple):
    """What ONE step's response says, read the same way for every step."""

    provider_id: str | None
    status: object
    provider_time: datetime | None
    amount: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class WriteRecord:
    reference: str
    outcome: Outcome
    provider_id: str | None
    provider_time: datetime | None
    claimed_at: datetime


@dataclass(frozen=True)
class WriteResult(Generic[T]):
    record: WriteRecord
    payload: T | None  # the provider's response when THIS request made or found it; None when answered from the store

    @property
    def outcome(self) -> Outcome:
        return self.record.outcome


class ClaimStore(Protocol):
    def try_claim(self, reference: str, operation: str) -> bool: ...

    def load_existing(self, reference: str) -> WriteRecord: ...

    def complete(
        self,
        reference: str,
        outcome: Outcome,
        provider_id: str | None = None,
        provider_time: datetime | None = None,
        detail: str = "",
    ) -> WriteRecord: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money_matches(sent: tuple[Decimal, str], got: Answer) -> bool:
    if got.amount is None or got.currency is None:
        return False
    try:
        return (Decimal(got.amount), got.currency.upper()) == (sent[0], sent[1].upper())
    except InvalidOperation:
        return False


def safe_write(
    store: ClaimStore,
    reference: str,
    operation: str,
    *,
    send: Callable[[str], T],
    read: Callable[[T], Answer],
    outcome_of: Callable[[object], Outcome],
    find: Callable[[str], T | None] | None = None,
    repeat_is_safe: bool = True,
    resend_window: timedelta | None = None,
    sent: tuple[Decimal, str] | None = None,
    refused_is_unknown: Callable[[ApiError[object]], bool] = lambda e: e.status_code == 409,
    conflict_resends: int = 2,
    conflict_backoff: float = 1.0,
) -> WriteResult[T]:
    """Make (or find) ONE provider write under ``reference``.

    send(ref)        the SDK call, sending ``ref`` as its PayPal-Request-Id
    read(result)     this step's response as an Answer
    outcome_of       this step's status mapper
    find(ref)        a lookup by reference, when the provider offers one; None -> not found yet
    repeat_is_safe   PayPal returns the original for a repeat under the same PayPal-Request-Id
    resend_window    how long PayPal keeps the request id; past it a resend could make a second write
    sent             (amount, currency) asked for; the echoed money must match
    conflict_resends how many times a 409/5xx is re-checked by resending the same reference
    """
    checking = False
    if not store.try_claim(reference, operation):
        existing = store.load_existing(reference)
        if existing.outcome == "sending" and existing.claimed_at > _now() - SEND_WINDOW:
            return WriteResult(existing, None)  # in flight elsewhere: answer "in progress", no provider call
        if existing.outcome not in ("sending", "unknown"):
            return WriteResult(existing, None)  # settled: answer from the record
        checking = True  # stale sender or unresolved: LOOK, never create anew

    within_window = True
    if checking and resend_window is not None:
        existing = store.load_existing(reference)
        within_window = existing.claimed_at > _now() - resend_window
    resending = checking and repeat_is_safe and within_window

    result: T | None = None
    sends_left = 1 + (conflict_resends if repeat_is_safe else 0)
    while (resending or not checking) and sends_left > 0 and result is None:
        sends_left -= 1
        try:
            result = send(reference)
        except NEVER_SENT as exc:
            if resending:
                store.complete(reference, "unknown", detail="check could not reach PayPal")
                raise OutcomeUnknown(reference) from exc
            store.complete(reference, "failed", detail="never sent")
            raise ProviderError(502, "paypal_unreachable", "PayPal could not be reached; nothing was sent.") from exc
        except ApiError as exc:
            if isinstance(exc.error, OAuthProviderError):
                # The token fetch failed before the write was sent: nothing happened.
                store.complete(reference, "unknown" if resending else "failed", detail="token fetch refused")
                raise provider_error(exc.status_code, exc.error) from exc
            if exc.status_code < 500 and not refused_is_unknown(exc):
                mapped = provider_error(exc.status_code, exc.error)
                if resending:
                    store.complete(reference, "unknown", detail=f"check refused: {mapped.code}")
                    raise OutcomeUnknown(reference) from exc
                store.complete(reference, "failed", detail=",".join(mapped.issues)[:250] or mapped.code)
                raise mapped from exc
            log.warning("paypal %s answered %s %s for %s; checking by reference", operation, exc.status_code,
                        ",".join(issues_of(exc.error)), reference)
            if sends_left > 0:
                # A conflict (e.g. PayPal serialising concurrent refunds of one capture) or a 5xx: the same
                # PayPal-Request-Id is de-duplicated, so resending it is the check, and never a second write.
                time.sleep(conflict_backoff)
                resending = True
                continue
            break
        except httpx.RequestError as exc:
            log.warning("paypal %s: no answer (%s) for %s; checking by reference", operation,
                        type(exc).__name__, reference)
            break
        except ValueError as exc:  # pydantic ValidationError or a non-JSON 2xx body
            log.warning("paypal %s: unreadable answer (%s) for %s", operation, type(exc).__name__, reference)
            break

    if result is None:
        try:
            result = find(reference) if find is not None else None
        except (ApiError, httpx.RequestError, ValueError) as exc:
            store.complete(reference, "unknown", detail="lookup failed")
            raise OutcomeUnknown(reference) from exc
        if result is None:
            store.complete(reference, "unknown", detail="not confirmed")
            raise OutcomeUnknown(reference)

    got = read(result)
    if sent is not None and not _money_matches(sent, got):
        store.complete(reference, "needs_review", got.provider_id, got.provider_time,
                       detail=f"amount mismatch: asked {sent[0]} {sent[1]}, got {got.amount} {got.currency}")
        raise AmountMismatch(reference, got.amount, got.currency)

    record = store.complete(reference, outcome_of(got.status), got.provider_id, got.provider_time)
    return WriteResult(record, result)
