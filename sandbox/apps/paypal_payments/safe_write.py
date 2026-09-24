"""
The one path for every PayPal write that creates, charges, releases or
refunds money: claim, call, check, verify, complete.

* The claim is a ``PayPalOperation`` row whose ``reference`` is UNIQUE in the
  database, taken before PayPal is called. A second request for the same step
  (double click, retry, another worker) fails to insert it and answers from
  what is recorded; it never makes a new write.
* The reference is sent as ``PayPal-Request-Id``. PayPal returns the original
  result for a repeated key (verified in the sandbox for create order,
  capture and refund), so when an outcome is unknown the check is a resend
  under the same reference, inside PayPal's key retention window.
* The echoed amount is compared (as Decimal) before anything is recorded as done.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Callable, Generic, TypeVar, cast

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from paypal.core import ApiError, OAuthProviderError

from . import gateway
from .errors import NEVER_SENT, AmountMismatch, InProgress, OutcomeUnknown, ProviderUnavailable, translate
from .gateway import DONE, FAILED, NEEDS_REVIEW, PENDING, SENDING, UNKNOWN
from .models import PayPalOperation

logger = logging.getLogger(__name__)

T = TypeVar('T')

# PayPal-Request-Id retention, from each operation's documentation.
KEY_WINDOW_CREATE_ORDER = timedelta(hours=6)
KEY_WINDOW_PAYMENTS = timedelta(days=45)      # capture, reauthorize, void, refund
KEY_WINDOW_VAULT = timedelta(hours=3)
# Keep clear of the edge of a window: a resend just past it would be a new write.
_WINDOW_MARGIN = timedelta(minutes=10)


def send_window() -> timedelta:
    """How long a 'sending' claim is presumed in flight: one call's timeout plus margin (no retries)."""
    return timedelta(seconds=float(settings.PAYPAL_TIMEOUT) * 2 + 30)


@dataclass
class WriteResult(Generic[T]):
    operation: PayPalOperation
    # PayPal's answer, when this request obtained one; None when answered from the stored record.
    result: T | None


def try_claim(reference: str, kind: str, **fields: object) -> bool:
    """Insert-or-fail: True for exactly one caller per reference."""
    try:
        with transaction.atomic():
            PayPalOperation.objects.create(
                reference=reference, kind=kind, outcome=SENDING,
                claimed_at=timezone.now(), **fields)
    except IntegrityError:
        return False
    return True


def load_existing(reference: str) -> PayPalOperation:
    return cast(PayPalOperation, PayPalOperation.objects.get(reference=reference))


def complete(reference: str, outcome: str, answer: gateway.Answer | None = None,
             error_message: str = '') -> PayPalOperation:
    """
    Record what is known. A FAILED with no PayPal id (never sent, or refused)
    releases the claim so the same reference can be claimed again.
    """
    operation = cast(PayPalOperation, PayPalOperation.objects.select_for_update().get(reference=reference))
    if outcome == FAILED and not (answer and answer.provider_id):
        operation.outcome = FAILED
        operation.error_message = error_message[:512]
        operation.delete()
        return operation
    operation.outcome = outcome
    if answer is not None:
        operation.provider_id = answer.provider_id or operation.provider_id
        operation.provider_status = answer.status
        operation.provider_time = answer.provider_time or operation.provider_time
        if outcome == NEEDS_REVIEW:
            operation.provider_amount = answer.amount
    if error_message:
        operation.error_message = error_message[:512]
    operation.save()
    return operation


def _may_resend(operation: PayPalOperation, window: timedelta) -> bool:
    return bool(timezone.now() < operation.claimed_at + window - _WINDOW_MARGIN)


def safe_write(
    reference: str,
    *,
    kind: str,
    send: Callable[[str], T],
    read: Callable[[T], gateway.Answer],
    key_window: timedelta,
    sent: tuple[Decimal, str] | None,
    on_complete: Callable[[PayPalOperation, T], None] | None = None,
    refresh: Callable[[PayPalOperation], T] | None = None,
    **claim_fields: object,
) -> WriteResult[T]:
    """
    reference    derived from the operation and the step; the same on every attempt and every repeat
    send(key)    makes the PayPal call with ``key`` as its PayPal-Request-Id
    read(result) this step's answer as an Answer (status already mapped to an outcome)
    key_window   how long PayPal de-duplicates the key; a resend is only a check inside it
    sent         (amount, currency) asked for; None for a write that moves no money
    on_complete  applies the answer to the app's own records, in the same transaction as the outcome
    refresh      a READ of the provider record by its id, used to settle a stored 'pending'
    """
    checking = False
    if not try_claim(reference, kind, **claim_fields):
        existing = load_existing(reference)
        if existing.outcome == SENDING and existing.claimed_at > timezone.now() - send_window():
            raise InProgress(reference)
        if existing.outcome == PENDING and refresh is not None and existing.provider_id:
            return _settle_from_read(reference, existing, refresh, read, sent, on_complete)
        if existing.outcome not in (SENDING, UNKNOWN):
            return WriteResult(existing, None)
        checking = True  # stale sender or unresolved: look, never create anew
        if not _may_resend(existing, key_window):
            if refresh is not None and existing.provider_id:
                return _settle_from_read(reference, existing, refresh, read, sent, on_complete)
            logger.error('PayPal write %s is unresolved outside the key window; needs an operator', reference)
            with transaction.atomic():
                complete(reference, UNKNOWN)
            raise OutcomeUnknown(reference)

    result: T | None = None
    try:
        result = send(reference)
    except ApiError as exc:
        if isinstance(exc.error, OAuthProviderError):  # the token fetch failed: nothing was sent
            with transaction.atomic():
                complete(reference, UNKNOWN if checking else FAILED)
            raise translate(exc) from exc
        if exc.status_code < 500:
            refused = exc.status_code in (400, 422) or not checking
            with transaction.atomic():
                if refused:
                    complete(reference, FAILED, error_message=str(translate(exc).message))
                else:
                    complete(reference, UNKNOWN)
            if refused:
                raise translate(exc) from exc
            raise OutcomeUnknown(reference) from exc
        logger.warning('PayPal write %s answered HTTP %s; checking by resend', reference, exc.status_code)
    except NEVER_SENT as exc:
        with transaction.atomic():
            complete(reference, UNKNOWN if checking else FAILED)
        if checking:
            raise OutcomeUnknown(reference) from exc
        raise translate(exc) from exc
    except (httpx.RequestError, ValueError) as exc:
        logger.warning('PayPal write %s got no readable answer (%s); checking by resend',
                       reference, type(exc).__name__)

    if result is None:
        # May have landed. The check is a resend under the SAME reference: PayPal
        # answers with the original if it landed and performs it once if it did not.
        try:
            result = send(reference)
        except (ApiError, httpx.RequestError, ValueError) as exc:
            with transaction.atomic():
                complete(reference, UNKNOWN)
            raise OutcomeUnknown(reference) from exc

    return _record(reference, result, read, sent, on_complete)


def _settle_from_read(reference: str, existing: PayPalOperation, refresh: Callable[[PayPalOperation], T],
                      read: Callable[[T], gateway.Answer], sent: tuple[Decimal, str] | None,
                      on_complete: Callable[[PayPalOperation, T], None] | None) -> WriteResult[T]:
    try:
        result = refresh(existing)
    except (ApiError, httpx.RequestError, ValueError) as exc:
        # Only the status read failed; the write itself is still pending at PayPal.
        failure = translate(exc)
        raise ProviderUnavailable(failure.status, failure.code, failure.message, outcome_unknown=True) from exc
    return _record(reference, result, read, sent, on_complete)


def _record(reference: str, result: T, read: Callable[[T], gateway.Answer],
            sent: tuple[Decimal, str] | None,
            on_complete: Callable[[PayPalOperation, T], None] | None) -> WriteResult[T]:
    answer = read(result)
    mismatch = False
    if sent is not None and answer.outcome in (DONE, PENDING):
        sent_amount, sent_currency = sent
        mismatch = (answer.amount, answer.currency) != (Decimal(sent_amount), sent_currency)
    with transaction.atomic():
        operation = complete(reference, NEEDS_REVIEW if mismatch else answer.outcome, answer)
        if on_complete is not None:
            on_complete(operation, result)
    if mismatch:
        logger.error('PayPal write %s echoed %s %s, asked %s', reference, answer.amount, answer.currency, sent)
        raise AmountMismatch(reference, answer.amount, answer.currency)
    return WriteResult(operation, result)
