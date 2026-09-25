"""The one path every PayPal write that creates, charges, holds, releases or refunds goes through.

claim → call → check → verify → complete, with the claim held in the database (UNIQUE ``ref`` on
``PaymentOperation``) so it outlives the request and the process. See pay-pal-server-sdk-plan.md.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone

from paypal.core import ApiError, OAuthProviderError

from . import paypal_gateway as gw
from .models import PaymentOperation

log = logging.getLogger(__name__)

# Longer than one attempt can take (client timeout + margin): a younger "sending" claim is in flight.
SEND_WINDOW = timedelta(minutes=2)


class IdempotencyConflict(gw.ProviderError):
    def __init__(self) -> None:
        super().__init__(
            422,
            "idempotency_key_reused",
            "This idempotency key was already used for a different request.",
        )


def request_id(ref: str) -> str:
    """The PayPal-Request-Id for a ref: the same on every attempt and every repeat of the request."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ref))


@dataclass
class Write:
    ref: str
    kind: str
    send: Callable[[str], Any]
    read: Callable[[Any], gw.Answer]
    # Lookup by the reference when the outcome is unknown; None when the API offers none.
    find: Callable[[], Any | None] | None = None
    # The provider will not make a second one for this ref (it de-duplicates the key), so a resend
    # under the SAME key is itself the check — only inside the provider's key-retention window.
    repeat_is_safe: bool = False
    resend_window: timedelta = timedelta(0)
    # (amount, currency) the write moves; verified against what PayPal echoes before "done".
    sent: tuple[Decimal, str] | None = None
    # For a lookup against a record that is complete after a documented lag (the reporting API): once the
    # claim is older than this, "not found" is PayPal's answer that the write never happened.
    absent_after: timedelta | None = None
    # An error body that means an earlier attempt already landed ("reference already exists").
    already_landed: Callable[[ApiError[Any]], bool] = lambda e: False
    fingerprint: str = ""
    claim_fields: dict[str, Any] = field(default_factory=dict)
    # Custom claim (e.g. refunds: ceiling check under a row lock). Must insert the row or return False.
    claim: Callable[[], bool] | None = None
    # Domain updates, run in the same DB transaction as the outcome; answer is None on error paths.
    on_complete: Callable[[PaymentOperation, gw.Answer | None], None] | None = None


def insert_claim(w: Write) -> bool:
    """Insert-or-fail. A ``failed`` claim with no provider id (never sent / refused) is re-taken atomically."""
    now = timezone.now()
    try:
        with transaction.atomic():
            PaymentOperation.objects.create(
                ref=w.ref,
                kind=w.kind,
                outcome=PaymentOperation.SENDING,
                fingerprint=w.fingerprint,
                claimed_at=now,
                amount=w.sent[0] if w.sent else None,
                currency=w.sent[1] if w.sent else "",
                **w.claim_fields,
            )
        return True
    except IntegrityError:
        retaken = PaymentOperation.objects.filter(
            ref=w.ref, outcome=PaymentOperation.FAILED, provider_id="", fingerprint=w.fingerprint
        ).update(outcome=PaymentOperation.SENDING, claimed_at=now, detail={})
        return bool(retaken == 1)


def _settled(op: PaymentOperation) -> bool:
    """An outcome PayPal's word has fixed: nothing a later check says may move it back."""
    return op.outcome in (PaymentOperation.DONE, PaymentOperation.NEEDS_REVIEW) or (
        op.outcome == PaymentOperation.FAILED and bool(op.provider_id)
    )


def complete(w: Write, outcome: str, answer: gw.Answer | None = None, **detail: Any) -> PaymentOperation:
    with transaction.atomic():
        op: PaymentOperation = PaymentOperation.objects.select_for_update().get(ref=w.ref)
        if _settled(op):
            # A concurrent check already settled it (e.g. two repeats resending the same key): keep that,
            # and never apply its domain effects twice.
            if op.outcome != outcome:
                log.info("paypal write %s %s: keeping %s over %s", w.kind, w.ref, op.outcome, outcome)
            return op
        # on_complete may run again on a later re-check: it acts on a transition, not on a state.
        op.previous_outcome = op.outcome
        op.outcome = outcome
        if answer is not None:
            op.provider_id = answer.provider_id or op.provider_id
            op.provider_status = answer.provider_status
            op.provider_time = answer.provider_time or op.provider_time
            op.provider_amount = answer.amount
            op.detail = {**op.detail, **answer.detail}
        if detail:
            op.detail = {**op.detail, **detail}
        op.save()
        if w.on_complete is not None:
            w.on_complete(op, answer)
    log.info("paypal write %s %s -> %s %s", w.kind, w.ref, outcome, op.provider_id or "")
    return op


def run(w: Write) -> PaymentOperation:
    """Claim, call, check, verify, complete. Returns the operation record; raises ProviderError."""
    # 1. CLAIM FIRST — the database decides the winner.
    checking = False
    claimed = (w.claim or (lambda: insert_claim(w)))()
    if not claimed:
        existing: PaymentOperation = PaymentOperation.objects.get(ref=w.ref)
        if w.fingerprint and existing.fingerprint and existing.fingerprint != w.fingerprint:
            raise IdempotencyConflict()
        in_flight = existing.claimed_at > timezone.now() - SEND_WINDOW
        if existing.outcome == PaymentOperation.SENDING and in_flight:
            return existing  # someone else's call is in flight: answer "in progress", call nothing
        if existing.outcome not in (PaymentOperation.SENDING, PaymentOperation.UNKNOWN):
            return existing  # settled (or pending/needs_review): answer from the record
        checking = True  # stale sender or unresolved: LOOK, never create anew
        claimed_at = existing.claimed_at
    else:
        claimed_at = timezone.now()

    try:
        return _call_check_verify(w, checking=checking, claimed_at=claimed_at)
    except gw.ProviderError:
        raise
    except Exception as e:
        # Anything unforeseen after the claim (an unreadable amount, a DB error while recording, a domain
        # callback failing): the write may have happened. Record UNKNOWN — never leave the claim "sending"
        # without saying so — and let the next repeat re-check it under the same reference.
        log.exception("paypal write %s %s: unexpected failure after the claim", w.kind, w.ref)
        try:
            op = complete(w, PaymentOperation.UNKNOWN, error=type(e).__name__)
            if op.outcome != PaymentOperation.UNKNOWN:
                return op
        except Exception:  # the store itself is failing; the stale "sending" claim is re-checked later
            log.exception("paypal write %s %s: could not record UNKNOWN", w.kind, w.ref)
        raise gw.OutcomeUnknown(w.ref) from e


def _unknown(w: Write, cause: BaseException | None = None, **detail: Any) -> PaymentOperation:
    """Record UNKNOWN and raise — unless a concurrent check already settled it, then answer from that."""
    op = complete(w, PaymentOperation.UNKNOWN, **detail)
    if op.outcome != PaymentOperation.UNKNOWN:
        return op
    raise gw.OutcomeUnknown(w.ref) from cause


def _refused(w: Write, refusal: gw.ProviderError, cause: BaseException, **detail: Any) -> PaymentOperation:
    """A first send that never left or was refused: nothing happened, the claim is released."""
    op = complete(w, PaymentOperation.FAILED, **detail)
    if op.outcome != PaymentOperation.FAILED or op.provider_id:
        return op  # settled by someone else meanwhile
    raise refusal from cause


def _call_check_verify(w: Write, *, checking: bool, claimed_at: Any) -> PaymentOperation:
    resending = checking and w.repeat_is_safe and claimed_at > timezone.now() - w.resend_window

    # 2. CALL, sending the reference — a first attempt, or a check by same-key resend.
    result: Any = None
    if resending or not checking:
        try:
            result = w.send(request_id(w.ref))
        except gw.ConfigurationError as e:  # no client could be built: nothing was sent
            if resending:
                return _unknown(w, e)
            complete(w, PaymentOperation.FAILED, error="not_configured")
            raise
        except gw.NEVER_SENT as e:
            if resending:  # a check that never left says nothing
                return _unknown(w, e)
            return _refused(w, gw.translate(e), e, error="never_sent")
        except ApiError as e:
            issues = gw.error_issues(e)
            if isinstance(e.error, OAuthProviderError):  # the token fetch failed: nothing was sent
                if resending:
                    return _unknown(w, e)
                return _refused(w, gw.translate(e), e, error="provider_auth")
            if w.already_landed(e):
                pass  # an earlier attempt landed: look it up below
            elif e.status_code < 500:
                if resending:  # a check never fails the write
                    return _unknown(w, e, issues=issues)
                return _refused(w, gw.translate(e), e, issues=issues, http_status=e.status_code)
            # a 5xx may still have landed: fall through to the lookup
        except (httpx.RequestError, ValueError):  # sent, no readable answer (ValidationError is a ValueError)
            pass

    # 3. CHECK, here — by the reference sent. Where PayPal de-duplicates the key (repeat_is_safe), a
    # resend under the SAME key is the lookup, inside its retention window; otherwise the write's own find.
    if result is None:
        find = w.find
        if find is None and w.repeat_is_safe and claimed_at > timezone.now() - w.resend_window:
            find = lambda: w.send(request_id(w.ref))  # noqa: E731
        if find is None:
            return _unknown(w)
        try:
            result = find()
        except (ApiError, httpx.RequestError, ValueError, gw.ProviderError) as e:
            return _unknown(w, e)
        if result is None:
            if w.absent_after is not None and claimed_at < timezone.now() - w.absent_after:
                # PayPal's own record, past its documented lag, has no such write: that IS its answer.
                return complete(w, PaymentOperation.FAILED, error="not_found_at_paypal")
            return _unknown(w)  # not found YET: a lookup inside the window cannot prove it did not happen

    # 4. VERIFY before keeping it: PayPal is authoritative for what happened, not for what we asked.
    answer = w.read(result)
    if w.sent is not None and answer.outcome != PaymentOperation.FAILED and answer.amount is not None:
        sent_amount, sent_currency = w.sent
        if (answer.amount, answer.currency) != (Decimal(sent_amount), sent_currency):
            op = complete(w, PaymentOperation.NEEDS_REVIEW, answer)
            if op.outcome == PaymentOperation.NEEDS_REVIEW:
                raise gw.AmountMismatch(w.ref, f"{sent_amount} {sent_currency}", f"{answer.amount} {answer.currency}")
            return op
    elif w.sent is not None and answer.outcome == PaymentOperation.DONE and answer.amount is None:
        op = complete(w, PaymentOperation.NEEDS_REVIEW, answer, error="amount_not_echoed")
        if op.outcome == PaymentOperation.NEEDS_REVIEW:
            raise gw.AmountMismatch(w.ref, f"{w.sent[0]} {w.sent[1]}", "no amount")
        return op

    # 5. COMPLETE from what PayPal SAID.
    return complete(w, answer.outcome, answer)
