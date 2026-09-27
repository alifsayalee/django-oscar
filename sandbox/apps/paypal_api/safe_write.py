"""
The one path every PayPal write goes through: claim, call, check, verify, complete.

* The claim is a row in ``PayPalOperation`` with a unique ``ref``, committed
  before PayPal is called, so a double-click, a caller retry or a second worker
  can never send the same write twice.
* A write whose answer was lost is checked by the same reference - by a lookup,
  or by a resend where PayPal de-duplicates that reference - and stays
  ``unknown`` until PayPal's own answer settles it.
* The echoed amount is compared with what was asked for before anything is
  recorded as done.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal.core import ApiError
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CardVerificationStatus,
    OrderStatus,
    RefundStatus,
)

from .errors import AmountMismatch, OutcomeUnknown, ProviderError, provider_error
from .gateway import NEVER_SENT, last_status
from .models import PayPalOperation as Op

logger = logging.getLogger(__name__)

DONE, PENDING, FAILED, UNKNOWN = Op.DONE, Op.PENDING, Op.FAILED, Op.UNKNOWN


@dataclass(frozen=True)
class Answer:
    """What one step's response says, read the same way for every step."""

    provider_id: str
    status: Any
    provider_time: datetime | None
    amount: str | None = None
    currency: str | None = None
    detail: dict[str, Any] | None = None


def send_window() -> timedelta:
    # Longer than one attempt (the SDK never retries a write) plus margin.
    return timedelta(seconds=float(settings.PAYPAL_TIMEOUT_SECONDS) * 2 + 30)


def provider_time(value: object) -> datetime | None:
    """PayPal's RFC 3339 timestamps (``UNSET`` or malformed -> None)."""
    if not isinstance(value, str):
        return None
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        return None
    return parsed


# --- the claim store -------------------------------------------------------


def try_claim(ref: str, **fields: Any) -> bool:
    """Insert-or-fail. A ``failed`` claim with no provider id is released and retaken atomically."""
    now = timezone.now()
    try:
        with transaction.atomic():
            Op.objects.create(ref=ref, outcome=Op.SENDING, claimed_at=now, **fields)
        return True
    except IntegrityError:
        retake = {k: v for k, v in fields.items() if k not in ("order", "user", "kind")}
        taken = Op.objects.filter(ref=ref, outcome=Op.FAILED, provider_id="").update(
            outcome=Op.SENDING, claimed_at=now, provider_status="", detail={}, **retake
        )
        return taken == 1


def load_existing(ref: str) -> Op:
    return Op.objects.get(ref=ref)


def complete(
    ref: str,
    outcome: str,
    provider_id: str = "",
    when: datetime | None = None,
    *,
    status: object = "",
    detail: dict[str, Any] | None = None,
) -> Op:
    fields: dict[str, Any] = {"outcome": outcome, "updated_at": timezone.now()}
    if provider_id:
        fields["provider_id"] = provider_id
    if when is not None:
        fields["provider_time"] = when
    if status != "":
        parts = status if isinstance(status, tuple) else (status,)
        fields["provider_status"] = "/".join(str(p) for p in parts if p is not None)[:64]
    if detail is not None:
        fields["detail"] = detail
    Op.objects.filter(ref=ref).update(**fields)
    return load_existing(ref)


# --- the helper ------------------------------------------------------------


def safe_write(
    ref: str,
    *,
    send: Callable[[str], Any],
    find: Callable[[str], Any],
    read: Callable[[Any], Answer],
    outcome_of: Callable[[Any], str],
    repeat_is_safe: bool = False,
    sent: tuple[int, str] | None = None,
    claim: dict[str, Any],
    on_claimed: Callable[[], None] | None = None,
    release: Callable[[], None] | None = None,
) -> tuple[Op, Any]:
    """
    Run one PayPal write step. Returns the operation record and PayPal's result
    (``None`` when the answer came from the stored record rather than PayPal).

    ref             from the operation and the step - the same on every attempt and every request
    send(key)       the provider call, with ``key`` as its PayPal-Request-Id
    find(ref)       the record carrying ``ref``, or None - a lookup, or ``send`` where PayPal de-duplicates
    read(result)    this step's response as an Answer
    outcome_of      this step's status mapper
    repeat_is_safe  PayPal will not make a second one for this reference
    sent            (amount in minor units, currency) asked for; None when no money moves
    on_claimed      runs once, right after this request wins a fresh claim and before the send;
                    an exception from it fails the claim (releasing it) and propagates
    release         undoes on_claimed's effect when a first send is refused (PayPal said no, or it
                    never left) - run BEFORE the claim is released, so a retake cannot race it
    """
    from .money import from_minor

    # 1. CLAIM FIRST.
    checking = False
    if try_claim(ref, **claim):
        if on_claimed is not None:
            try:
                on_claimed()
            except Exception:
                complete(ref, FAILED, detail={"error": "rejected before sending"})
                raise
    else:
        existing = load_existing(ref)
        if existing.outcome == Op.SENDING and existing.claimed_at > timezone.now() - send_window():
            return existing, None  # in flight elsewhere: answer "in progress"
        if existing.outcome not in (Op.SENDING, UNKNOWN):
            return existing, None  # settled: answer from the record
        checking = True  # stale sender or unresolved: look, never create

    resending = checking and repeat_is_safe

    def refused(detail: dict[str, Any]) -> None:
        if release is not None:
            release()
        complete(ref, FAILED, detail=detail)

    # 2. CALL - a first attempt, or a check by same-reference resend.
    result: Any = None
    if resending or not checking:
        try:
            result = send(ref)
        except NEVER_SENT as e:
            if resending:
                complete(ref, UNKNOWN)
                raise OutcomeUnknown(ref) from e
            refused({"error": "never_sent"})
            raise ProviderError(502, "paypal_unreachable", "PayPal could not be reached; nothing was sent.") from e
        except ApiError as e:
            if e.status_code < 500:
                mapped = provider_error(e.status_code, e.error)
                if resending:
                    complete(ref, UNKNOWN, detail={"check_error": mapped.message})
                    raise OutcomeUnknown(ref) from e
                refused({"error": mapped.message, "issue": mapped.issue})
                raise mapped from e
            # a 5xx on a write may still have landed: look it up
        except httpx.RequestError:
            pass  # sent, no readable answer: may have landed
        except ValueError as e:
            status = last_status.get()
            if status is not None and 400 <= status < 500:
                # rejected, only the detail was lost
                if resending:
                    complete(ref, UNKNOWN)
                    raise OutcomeUnknown(ref) from e
                refused({"error": f"rejected (HTTP {status})"})
                raise ProviderError(400, "paypal_rejected", f"PayPal rejected the request (HTTP {status}).") from e
            # a 2xx (or 5xx) we could not read: may have landed

    # 3. CHECK, by the reference we sent.
    if result is None:
        try:
            result = find(ref)
        except (ApiError, httpx.RequestError, ValueError, ProviderError) as e:
            complete(ref, UNKNOWN)
            raise OutcomeUnknown(ref) from e
        if result is None:
            complete(ref, UNKNOWN)
            raise OutcomeUnknown(ref)

    # 4. VERIFY the echoed money before keeping it.
    got = read(result)
    if sent is not None:
        sent_minor, sent_currency = sent
        echoed = (None if got.amount is None else Decimal(str(got.amount)), got.currency)
        if echoed != (from_minor(sent_minor, sent_currency), sent_currency):
            complete(ref, Op.NEEDS_REVIEW, got.provider_id, got.provider_time, status=got.status,
                     detail={"echoed_amount": got.amount, "echoed_currency": got.currency})
            logger.error("PayPal amount mismatch on %s: asked %s %s, got %s %s",
                         ref, sent_minor, sent_currency, got.amount, got.currency)
            raise AmountMismatch(ref, got.amount, got.currency)

    # 5. COMPLETE from what PayPal said.
    op = complete(ref, outcome_of(got.status), got.provider_id, got.provider_time,
                  status=got.status, detail=got.detail or {})
    return op, result


# --- per-step status mappers -----------------------------------------------
# Every enum member is listed by name; anything else is "unknown", never done.


def authorization_outcome(status: object) -> str:
    """The authorize and reauthorize steps: done means a hold is in place."""
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return FAILED
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return UNKNOWN  # no longer a hold - an operator must look
        case _:
            return UNKNOWN


def order_outcome(status: tuple[object, object]) -> str:
    """The single-step create order: the order envelope, then its authorization."""
    order_status, auth_status = status
    match order_status:
        case OrderStatus.COMPLETED:
            return authorization_outcome(auth_status) if auth_status is not None else UNKNOWN
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED | OrderStatus.PAYER_ACTION_REQUIRED:
            return PENDING
        case OrderStatus.VOIDED:
            return FAILED
        case _:
            return UNKNOWN


def void_outcome(status: object) -> str:
    """The cancel step: its done is the released hold."""
    match status:
        case AuthorizationStatus.VOIDED | AuthorizationStatus.DENIED:
            return DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED  # too late: the money was taken
        case AuthorizationStatus.CREATED | AuthorizationStatus.PENDING:
            return UNKNOWN  # not released (yet)
        case _:
            return UNKNOWN


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED | CaptureStatus.REFUNDED:
            return FAILED
        case CaptureStatus.PARTIALLY_REFUNDED:
            return UNKNOWN
        case _:
            return UNKNOWN


def refund_outcome(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return DONE
        case RefundStatus.PENDING:
            return PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return FAILED
        case _:
            return UNKNOWN


def vault_outcome(status: Any) -> str:
    """The saved-card step. The token response has no status member, so the
    status passed here is ``("TOKENIZED" | "INCOMPLETE", verification_status)``."""
    token_state, verification = status
    if token_state != "TOKENIZED":
        return UNKNOWN
    match verification:
        case None | CardVerificationStatus.VERIFIED:
            return DONE
        case CardVerificationStatus.FAILED:
            return FAILED
        case _:
            return UNKNOWN


def delete_outcome(status: object) -> str:
    return DONE if status == "deleted" else UNKNOWN
