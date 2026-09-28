"""
The single path every PayPal write goes through: claim, call, check, verify, complete.

* **Claim** — a ``PaymentWrite`` row is inserted before the call; its unique
  ``reference`` makes the database reject a second claim for the same step, in
  any process.
* **Call** — the reference travels as ``PayPal-Request-Id``.
* **Check** — when the answer is lost (timeout, reset, 5xx, unreadable body) the
  same request is re-sent under the same reference, which PayPal answers with
  the original result instead of acting twice.
* **Verify** — the amount PayPal echoes must equal the amount sent.
* **Complete** — the outcome recorded is the one PayPal's status says.
"""
from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Generic, TypeVar

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal.core import ApiError

from .errors import NEVER_SENT, AmountMismatch, ApiProblem, OutcomeUnknown, paypal_issues, provider_error
from .models import InstallIdentity, PaymentWrite
from .money import same_money
from .paypal_client import last_status, reset_last_status

logger = logging.getLogger(__name__)

R = TypeVar("R")


@dataclass(frozen=True)
class Answer:
    """What one step's PayPal response says, read the same way for every step."""

    provider_id: str
    status: str
    provider_time: datetime | None
    amount: str | None = None
    currency: str | None = None


@dataclass
class WriteResult(Generic[R]):
    """The claim record after the write, and PayPal's response when this request saw one."""

    write: PaymentWrite
    response: R | None


def provider_time(value: object) -> datetime | None:
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is not None and timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, dt_timezone.utc)
        return parsed
    return None


def reference_prefix() -> str:
    """This install's prefix for every reference sent to PayPal."""
    configured = getattr(settings, "PAYPAL_REFERENCE_PREFIX", None)
    if configured:
        return str(configured)
    identity = InstallIdentity.objects.order_by("pk").first()
    if identity is None:
        # The first row wins; a concurrent creator reads the same first row back.
        InstallIdentity.objects.create(prefix=f"os{secrets.token_hex(5)}")
        identity = InstallIdentity.objects.order_by("pk").first()
        assert identity is not None
    return identity.prefix


def deterministic_ref(*parts: object) -> str:
    return ":".join([reference_prefix(), *(str(p) for p in parts)])


def send_window() -> timedelta:
    """How long a claim may sit in ``sending`` before another request checks on it."""
    return timedelta(seconds=float(getattr(settings, "PAYPAL_TIMEOUT", 20.0)) * 3 + 10)


# --- the claim store (the database) -----------------------------------------------------


def try_claim(reference: str, kind: str, **fields: object) -> PaymentWrite | None:
    """Insert-or-fail: returns the new claim, or None when another request holds it."""
    try:
        with transaction.atomic():
            return PaymentWrite.objects.create(
                reference=reference, kind=kind, outcome=PaymentWrite.SENDING, claimed_at=timezone.now(), **fields
            )
    except IntegrityError:
        return None


def complete(
    write: PaymentWrite,
    outcome: str,
    answer: Answer | None = None,
    *,
    error_code: str = "",
    error_message: str = "",
    release: bool = False,
) -> PaymentWrite:
    """
    Record the outcome. A refusal with nothing created at PayPal may ``release``
    the claim so a later request can take the step again.
    """
    if release and outcome == PaymentWrite.FAILED and not (answer and answer.provider_id):
        PaymentWrite.objects.filter(pk=write.pk).delete()
        write.outcome = outcome
        write.error_code = error_code
        write.error_message = error_message[:512]
        return write
    write.outcome = outcome
    if answer is not None:
        write.provider_id = answer.provider_id[:64]
        write.provider_status = answer.status[:64]
        if answer.provider_time is not None:
            write.provider_time = answer.provider_time
    write.error_code = error_code
    write.error_message = error_message[:512]
    write.save()
    return write


# --- the helper ----------------------------------------------------------------------------


def safe_write(
    *,
    reference: str,
    kind: str,
    send: Callable[[str], R],
    read: Callable[[R], Answer],
    outcome_of: Callable[[str], str],
    find: Callable[[str], R | None] | None = None,
    sent: tuple[Decimal, str] | None = None,
    claim: PaymentWrite | None = None,
    release_on_refusal: bool = True,
    claim_fields: dict[str, object] | None = None,
) -> WriteResult[R]:
    """
    Perform one PayPal write step at most once.

    ``send(reference)`` makes the call with ``reference`` as ``PayPal-Request-Id``;
    PayPal returns the original result for a repeat under the same id, so a
    re-send is also how an unknown outcome is checked.  ``find(reference)`` is an
    optional extra lookup used when the call returned nothing readable.
    """
    checking = False
    write = claim
    if write is None:
        write = try_claim(reference, kind, **(claim_fields or {}))
    if write is None:
        existing = PaymentWrite.objects.get(reference=reference)
        if existing.outcome == PaymentWrite.SENDING and existing.claimed_at > timezone.now() - send_window():
            return WriteResult(existing, None)  # in flight elsewhere: answer "in progress"
        if existing.outcome not in (PaymentWrite.SENDING, PaymentWrite.UNKNOWN):
            return WriteResult(existing, None)  # settled: answer from the record
        write = existing
        checking = True  # a stale sender or an unknown outcome: check, never create anew

    def refused(status: int, error: object) -> R:
        """A 4xx: the request was rejected and nothing was done."""
        if checking:
            # A check that is refused says nothing about the earlier attempt.
            return _find(find, reference, write)
        problem = provider_error(status, error)
        logger.info("PayPal refused %s %s: HTTP %s %s", kind, reference, status, paypal_issues(error))
        complete(
            write,
            PaymentWrite.FAILED,
            error_code=",".join(paypal_issues(error)) or f"http_{status}",
            error_message=problem.message,
            release=release_on_refusal or status in (401, 429),
        )
        raise problem

    result: R | None = None
    reset_last_status()
    try:
        result = send(reference)
    except NEVER_SENT:
        if checking:
            complete(write, PaymentWrite.UNKNOWN)
            raise OutcomeUnknown(reference) from None
        complete(write, PaymentWrite.FAILED, error_code="paypal_unreachable", release=True)
        raise ApiProblem(502, "paypal_unreachable", "PayPal could not be reached; nothing was sent.") from None
    except ApiError as exc:
        if exc.status_code < 500:
            result = refused(exc.status_code, exc.error)
        else:
            # A 5xx on a write may still have landed: look it up.
            result = _find(find, reference, write, resend=send)
    except ValueError:
        status = last_status()
        if status is not None and 400 <= status < 500:
            # A rejection whose error body did not match the SDK's model: still a rejection.
            result = refused(status, None)
        else:
            # A success we could not read: it may have landed.
            result = _find(find, reference, write, resend=send)
    except httpx.RequestError:  # sent, and no answer: may have landed
        result = _find(find, reference, write, resend=send)

    if result is None:
        result = _find(find, reference, write, resend=send)

    got = read(result)
    if not got.provider_id:
        # A 2xx with nothing we can name: it may have happened.
        complete(write, PaymentWrite.UNKNOWN)
        raise OutcomeUnknown(reference)

    if sent is not None:
        sent_amount, sent_currency = sent
        if not (same_money(got.amount, sent_amount) and got.currency == sent_currency):
            complete(write, PaymentWrite.NEEDS_REVIEW, got, error_code="amount_mismatch")
            raise AmountMismatch(reference, f"{sent_amount} {sent_currency}", f"{got.amount} {got.currency}")

    outcome = outcome_of(got.status)
    complete(write, outcome, got)
    return WriteResult(write, result)


def _find(
    find: Callable[[str], R | None] | None,
    reference: str,
    write: PaymentWrite,
    *,
    resend: Callable[[str], R] | None = None,
) -> R:
    """Settle a may-have-landed write by the same reference; unknown if it cannot be settled."""
    lookups = [f for f in (resend, find) if f is not None]
    for lookup in lookups:
        try:
            found = lookup(reference)
        except (ApiError, httpx.RequestError, ValueError):
            continue
        if found is not None:
            return found
    complete(write, PaymentWrite.UNKNOWN)
    raise OutcomeUnknown(reference)


def refresh(
    write: PaymentWrite,
    fetch: Callable[[], R],
    read: Callable[[R], Answer],
    outcome_of: Callable[[str], str],
) -> WriteResult[R]:
    """Re-read a ``pending`` write from PayPal and record what it says now."""
    result = fetch()
    got = read(result)
    complete(write, outcome_of(got.status), got)
    return WriteResult(write, result)


def answer_for(write: PaymentWrite, *, what: str) -> None:
    """Raise the API problem for any outcome that is not ``done``; return on done."""
    outcome = write.outcome
    if outcome == PaymentWrite.DONE:
        return
    if outcome in (PaymentWrite.SENDING, PaymentWrite.PENDING):
        # Signalled to the view as 202 via ApiProblem with status 202.
        raise ApiProblem(
            202,
            "in_progress" if outcome == PaymentWrite.SENDING else "pending",
            f"{what} is not finished at PayPal yet; repeat the request to check on it.",
            details={"reference": write.reference},
        )
    if outcome == PaymentWrite.UNKNOWN:
        raise OutcomeUnknown(write.reference)
    if outcome == PaymentWrite.NEEDS_REVIEW:
        raise ApiProblem(
            409,
            "needs_review",
            f"{what} happened at PayPal but not as requested; an operator must review it.",
            details={"reference": write.reference, "paypalStatus": write.provider_status},
        )
    raise ApiProblem(
        402 if write.kind == PaymentWrite.AUTHORIZE else 409,
        write.error_code or "failed",
        write.error_message or f"{what} failed at PayPal (status {write.provider_status or 'unknown'}).",
        details={"reference": write.reference, "paypalStatus": write.provider_status},
    )
