"""
The one path for every PayPal write that creates, charges, releases or
refunds money, or vaults a card: claim, call, check, verify, complete.

* **Claim** - a ``ProviderWrite`` row whose unique ``reference`` is inserted
  and committed before PayPal is called. The database's unique constraint
  decides the winner; a second request for the same operation loses the
  insert and answers from what the first one recorded.
* **Call** - the reference travels as ``PayPal-Request-Id``, which PayPal uses
  to de-duplicate a repeated request.
* **Check** - when the call may have landed without an answer (a 5xx, a read
  timeout, an unreadable 2xx), the identical request is re-sent under the same
  reference: PayPal returns the original instead of acting twice. A check that
  cannot settle it leaves the record ``unknown`` - never ``failed``.
* **Verify** - the amount PayPal echoes must equal the amount sent.
* **Complete** - the outcome comes from PayPal's status, not from the fact
  that it answered.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Any, Generic, TypeVar

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from paypal.core import ApiError, OAuthProviderError

from . import money
from .errors import NEVER_SENT, AmountMismatch, InProgress, OutcomeUnknown
from .models import ProviderWrite

logger = logging.getLogger("apps.payments.safe_write")

T = TypeVar("T")

SENDING = ProviderWrite.SENDING
DONE = ProviderWrite.DONE
PENDING = ProviderWrite.PENDING
FAILED = ProviderWrite.FAILED
NEEDS_REVIEW = ProviderWrite.NEEDS_REVIEW
UNKNOWN = ProviderWrite.UNKNOWN


@dataclass(frozen=True)
class Answer:
    """What one write's response says, read the same way for every write."""

    provider_id: str
    status: str
    outcome: str
    provider_time: datetime | None
    amount: str | None = None
    currency: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class WriteResult(Generic[T]):
    record: ProviderWrite
    # PayPal's response when this request received one; None when the
    # request was answered from the stored record of an earlier one.
    result: T | None


def send_window() -> timedelta:
    """How long a claim may sit in ``sending`` before it is treated as stale."""
    return timedelta(seconds=2 * float(getattr(settings, "PAYPAL_TIMEOUT", 20.0)) + 30)


def reference(*parts: object) -> str:
    """A reference unique to this install and operation: prefix, then the parts."""
    prefix = str(getattr(settings, "PAYPAL_REFERENCE_PREFIX", "oscar-sandbox"))
    return ":".join([prefix, *(str(p) for p in parts)])


def next_attempt(base: str) -> str:
    """
    The reference for the next attempt at an operation. An attempt PayPal
    refused keeps its reference (PayPal has seen that request id), so a new
    attempt - e.g. paying again with another card - gets the next number.
    Two requests computing the same number race on the unique claim.
    """
    refused = ProviderWrite.objects.filter(reference__startswith=base + ":", outcome=FAILED).count()
    return f"{base}:{refused + 1}"


def parse_time(value: object) -> datetime | None:
    """Parse a PayPal RFC 3339 timestamp (``Z``, ``+00:00`` or ``+0000``)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


# --- the claim store --------------------------------------------------------


def try_claim(
    ref: str,
    *,
    kind: str,
    order: Any = None,
    user: Any = None,
    amount: Decimal | None = None,
    currency: str = "",
    reserve: Callable[[], None] | None = None,
) -> bool:
    """
    Insert-or-fail. Returns False when another request already holds ``ref``.
    ``reserve`` runs in the same transaction, before the insert, and may raise
    to refuse the claim (e.g. a refund that would exceed what was captured).
    """
    try:
        with transaction.atomic():
            if reserve is not None:
                reserve()
            ProviderWrite.objects.create(
                reference=ref,
                kind=kind,
                order=order,
                user=user,
                amount=amount,
                currency=currency,
                outcome=SENDING,
            )
    except IntegrityError:
        return False
    return True


def load_existing(ref: str) -> ProviderWrite:
    return ProviderWrite.objects.get(reference=ref)


def _release(ref: str) -> None:
    """Nothing reached PayPal: free the reference for the next request."""
    ProviderWrite.objects.filter(reference=ref, outcome=SENDING).delete()


def _mark(ref: str, outcome: str, detail: str = "") -> ProviderWrite:
    with transaction.atomic():
        record = ProviderWrite.objects.select_for_update().get(reference=ref)
        record.outcome = outcome
        if detail:
            record.detail = detail[:512]
        record.save(update_fields=["outcome", "detail", "updated_at"])
    return record


def _complete(
    ref: str,
    answer: Answer,
    outcome: str,
    apply: Callable[[ProviderWrite], None] | None,
) -> ProviderWrite:
    with transaction.atomic():
        record = ProviderWrite.objects.select_for_update().get(reference=ref)
        record.outcome = outcome
        record.provider_id = answer.provider_id[:64]
        record.provider_status = answer.status[:64]
        record.provider_time = answer.provider_time
        if answer.detail:
            record.detail = answer.detail[:512]
        record.save()
        if apply is not None:
            apply(record)
    return record


# --- the safe write ---------------------------------------------------------


def safe_write(
    ref: str,
    *,
    kind: str,
    send: Callable[[str], T],
    read: Callable[[T], Answer],
    apply: Callable[[ProviderWrite, T, Answer], None] | None = None,
    sent: tuple[Decimal, str] | None = None,
    order: Any = None,
    user: Any = None,
    reserve: Callable[[], None] | None = None,
) -> WriteResult[T]:
    """
    Run one PayPal write under the claim ``ref``.

    send(key)   makes the PayPal call with ``key`` as its PayPal-Request-Id.
                Every write here de-duplicates on that header, so re-sending
                the identical request under the same key is also the lookup.
    read(r)     reads PayPal's response as an ``Answer``.
    apply       updates the domain rows in the same transaction as the outcome.
    sent        the (amount, currency) asked for; None when no money moves.

    Raises ``InProgress`` when another request is sending this operation right
    now, ``OutcomeUnknown`` when PayPal's answer cannot be established, and
    re-raises the SDK/transport failure when PayPal refused or nothing was sent.
    """
    amount, currency = sent if sent is not None else (None, "")
    claimed = try_claim(
        ref, kind=kind, order=order, user=user, amount=amount, currency=currency, reserve=reserve
    )
    checking = False
    if not claimed:
        existing = load_existing(ref)
        if existing.outcome == SENDING and existing.claimed_at > timezone.now() - send_window():
            raise InProgress("This request is already being processed; try again in a moment.")
        if existing.outcome not in (SENDING, UNKNOWN):
            return WriteResult(existing, None)
        # A stale sender, or an unsettled outcome: check it under the SAME
        # reference. The re-send is the lookup; it never creates a second one.
        checking = True
        logger.info("Checking unsettled PayPal write %s", ref)

    result: T | None = None
    try:
        result = send(ref)
    except NEVER_SENT:
        if checking:
            _mark(ref, UNKNOWN)
            raise OutcomeUnknown(ref) from None
        _release(ref)
        raise
    except ApiError as exc:
        if isinstance(exc.error, OAuthProviderError):
            # The token fetch failed before the write was sent.
            if checking:
                _mark(ref, UNKNOWN)
                raise OutcomeUnknown(ref) from exc
            _release(ref)
            raise
        if exc.status_code < 500:
            if exc.status_code in (400, 404, 409, 422):
                # PayPal evaluated this request and refused it.
                _mark(ref, FAILED, detail=f"refused by PayPal (HTTP {exc.status_code})")
                raise
            # 401/403/429: PayPal did not process it.
            if checking:
                _mark(ref, UNKNOWN)
                raise OutcomeUnknown(ref) from exc
            _release(ref)
            raise
        logger.warning("PayPal write %s answered HTTP %s; checking", ref, exc.status_code)
    except (httpx.RequestError, ValueError) as exc:
        # Sent, and no readable answer: it may have landed.
        logger.warning("PayPal write %s had no readable answer (%s); checking", ref, type(exc).__name__)

    if result is None:
        try:
            result = send(ref)
        except (ApiError, httpx.RequestError, ValueError) as exc:
            _mark(ref, UNKNOWN)
            raise OutcomeUnknown(ref) from exc

    response: T = result
    answer = read(response)

    # Verify before keeping it: PayPal is authoritative for what happened, not
    # for what was asked. A success that does not echo the amount sent is
    # visible as needs_review, never recorded as done.
    if sent is not None and answer.outcome in (DONE, PENDING):
        if not money.same_money(answer.amount, answer.currency, sent[0], sent[1]):
            echoed = f"{answer.amount} {answer.currency}"
            _complete(ref, answer, NEEDS_REVIEW, None)
            raise AmountMismatch(ref, echoed)

    def _apply(record: ProviderWrite) -> None:
        if apply is not None:
            apply(record, response, answer)

    record = _complete(ref, answer, answer.outcome, _apply)
    return WriteResult(record, response)
