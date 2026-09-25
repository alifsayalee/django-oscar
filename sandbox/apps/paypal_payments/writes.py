"""
The safe write: the one path every PayPal write that creates, charges or
sends goes through.

1. CLAIM  - a PayPalOperation row, inserted before the call. The database's
            UNIQUE constraint on ``ref`` decides the winner; a loser never
            makes a new write.
2. CALL   - with PayPal-Request-Id = the claim's ``request_id``.
3. CHECK  - an answer that may have landed (timeout after sending, 5xx,
            unreadable 2xx) is checked by resending under the SAME
            PayPal-Request-Id: PayPal de-duplicates it and returns the
            original result. Never under a new reference.
4. VERIFY - the echoed amount must equal what was asked, as Decimal.
5. COMPLETE - from the status PayPal reported, in the same DB transaction
            as the caller's own bookkeeping.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Any, Callable

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal.core import ApiError, OAuthProviderError

from .errors import NEVER_SENT, PaymentAPIError, summarize, translate
from .models import Outcome, PayPalInstallation, PayPalOperation

logger = logging.getLogger('apps.paypal_payments')

# How long PayPal keeps a PayPal-Request-Id (per operation, api-reference.md).
# A same-key resend is only a safe check inside this window.
RESEND_WINDOWS: dict[str, timedelta] = {
    PayPalOperation.Kind.CREATE_ORDER: timedelta(hours=6),
    PayPalOperation.Kind.AUTHORIZE_ORDER: timedelta(hours=6),
    PayPalOperation.Kind.REAUTHORIZE: timedelta(days=45),
    PayPalOperation.Kind.CAPTURE: timedelta(days=45),
    PayPalOperation.Kind.VOID: timedelta(days=45),
    PayPalOperation.Kind.REFUND: timedelta(days=45),
    PayPalOperation.Kind.SETUP_TOKEN: timedelta(hours=3),
    PayPalOperation.Kind.PAYMENT_TOKEN: timedelta(hours=3),
}


def send_window() -> timedelta:
    """A claim younger than this is still in flight: two attempts plus slack."""
    return timedelta(seconds=2 * float(getattr(settings, 'PAYPAL_TIMEOUT', 20.0)) + 10)


def install_key() -> str:
    """This database's key; created once, race-free (primary key 1)."""
    installation, _ = PayPalInstallation.objects.get_or_create(pk=1)
    return installation.key.hex


def make_ref(*parts: Any) -> str:
    """
    A reference unique to this install and operation: the configured prefix,
    this database's install key, then the operation's own parts.
    """
    return ':'.join([settings.PAYPAL_REFERENCE_PREFIX, install_key()] + [str(p) for p in parts])


def invoice_prefix() -> str:
    return '%s-%s-' % (settings.PAYPAL_REFERENCE_PREFIX, install_key()[:12])


def request_id_for(ref: str) -> str:
    # Deterministic: the same operation always sends the same PayPal-Request-Id.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ref))


def provider_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = parse_datetime(value)
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


@dataclass(frozen=True)
class Answer:
    """What one step's response says, read the same way for every step."""
    provider_id: str | None
    status: object
    provider_time: datetime | None
    amount: str | None = None
    currency: str | None = None


def try_claim(ref: str, kind: str, **fields: Any) -> tuple[PayPalOperation, bool]:
    """Insert-or-fail. Returns (operation, won)."""
    try:
        with transaction.atomic():
            op = PayPalOperation.objects.create(
                ref=ref, request_id=request_id_for(ref), kind=kind, **fields)
        return op, True
    except IntegrityError:
        return PayPalOperation.objects.get(ref=ref), False


def complete(op: PayPalOperation, outcome: str, *, answer: Answer | None = None,
             detail: str = '') -> PayPalOperation:
    op.outcome = outcome
    if answer is not None:
        op.provider_id = answer.provider_id or op.provider_id
        op.provider_status = str(answer.status) if answer.status is not None else ''
        op.provider_time = answer.provider_time or op.provider_time
    if detail:
        op.detail = detail
    op.save()
    return op


class OutcomeUnknown(PaymentAPIError):
    def __init__(self, op: PayPalOperation, message: str | None = None):
        super().__init__(
            504, 'outcome_unknown',
            message or ('PayPal did not confirm the %s; it may or may not have happened. '
                        'Repeat the same request to check.' % op.get_kind_display().lower()),
            outcome_unknown=True)
        self.op = op


class InProgress(PaymentAPIError):
    def __init__(self, op: PayPalOperation):
        super().__init__(
            409, 'in_progress', 'An identical %s request is already in progress.' % op.kind)
        self.op = op


class AmountMismatch(PaymentAPIError):
    def __init__(self, op: PayPalOperation, echoed: tuple[Decimal | None, str | None]):
        super().__init__(
            502, 'amount_mismatch',
            'PayPal processed the %s for %s %s instead of %s %s; it needs review.'
            % (op.kind, echoed[0], echoed[1], op.amount, op.currency))
        self.op = op


def _may_have_landed(e: BaseException) -> bool:
    if isinstance(e, ApiError):
        return e.status_code >= 500 and not isinstance(e.error, OAuthProviderError)
    if isinstance(e, NEVER_SENT):
        return False
    return isinstance(e, (httpx.RequestError, ValueError))


def safe_write(
    op: PayPalOperation,
    won: bool,
    *,
    send: Callable[[str], Any],
    read: Callable[[Any], Answer],
    outcome_of: Callable[[object], str],
    apply: Callable[[PayPalOperation, Any, Answer], None] | None = None,
    action: str,
) -> tuple[PayPalOperation, Any]:
    """
    Run one claimed write. Returns (operation, provider result) - the result
    is None when the answer comes from an earlier, settled attempt.
    ``apply`` runs in the same DB transaction that records the outcome.
    """
    checking = False
    if not won:
        if op.outcome == Outcome.SENDING and op.claimed_at > timezone.now() - send_window():
            raise InProgress(op)
        if op.outcome not in (Outcome.SENDING, Outcome.UNKNOWN):
            return op, None
        checking = True
        if op.claimed_at < timezone.now() - RESEND_WINDOWS[op.kind]:
            # PayPal no longer de-duplicates this reference: a resend could make a
            # second one. Only an operator can settle it now.
            complete(op, Outcome.UNKNOWN)
            raise OutcomeUnknown(op, 'PayPal did not confirm the %s and it is too old to check '
                                     'automatically; an operator must look it up by reference %s.'
                                 % (op.kind, op.request_id))

    resending = checking
    result: Any = None
    last_error: BaseException | None = None
    for _attempt in range(2):
        try:
            result = send(op.request_id)
            last_error = None
            break
        except Exception as e:  # noqa: BLE001 - classified below, re-raised when not ours
            if not isinstance(e, (ApiError, httpx.RequestError, ValueError)):
                raise
            if _may_have_landed(e):
                logger.warning('PayPal %s %s may have landed (%s); resending under the same reference',
                               op.kind, op.ref, type(e).__name__)
                last_error = e
                resending = True
                continue
            if isinstance(e, NEVER_SENT) or (
                    isinstance(e, ApiError) and isinstance(e.error, OAuthProviderError)):
                # This attempt never reached PayPal.
                if resending:
                    complete(op, Outcome.UNKNOWN)
                    raise OutcomeUnknown(op) from e
                complete(op, Outcome.FAILED, detail='not sent: %s' % type(e).__name__)
                raise translate(e, action=action) from e
            assert isinstance(e, ApiError)
            refused = e.status_code in (400, 422) or not resending
            if refused:
                complete(op, Outcome.FAILED, detail=summarize(e))
                raise translate(e, action=action) from e
            # A 401/403/404/409/429 to a check says nothing about the original.
            complete(op, Outcome.UNKNOWN, detail=summarize(e))
            raise OutcomeUnknown(op) from e
    if last_error is not None:
        detail = summarize(last_error) if isinstance(last_error, ApiError) else type(last_error).__name__
        complete(op, Outcome.UNKNOWN, detail=detail)
        raise OutcomeUnknown(op) from last_error

    answer = read(result)
    # Recorded first, raised after: the record must survive the error.
    error: PaymentAPIError | None = None
    with transaction.atomic():
        if not answer.provider_id:
            # Accepted, but we cannot name what was created: not success.
            complete(op, Outcome.UNKNOWN, answer=answer, detail='response carried no id')
            error = OutcomeUnknown(op)
        else:
            echoed_amount = None if answer.amount is None else Decimal(str(answer.amount))
            if op.amount is not None and (echoed_amount, answer.currency) != (Decimal(op.amount), op.currency):
                complete(op, Outcome.NEEDS_REVIEW, answer=answer,
                         detail='echoed %s %s' % (answer.amount, answer.currency))
                error = AmountMismatch(op, (echoed_amount, answer.currency))
            else:
                complete(op, outcome_of(answer.status), answer=answer)
            if apply is not None:
                apply(op, result, answer)
    if error is not None:
        raise error
    return op, result
