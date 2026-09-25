"""
The one path for every PayPal write that creates, charges, releases or
returns money: claim, call, check, verify, complete.

* The claim is a row in ``ProviderWrite`` whose ``ref`` is UNIQUE, inserted
  before PayPal is called. The database rejects a second claim, so a double
  click, a caller retry or a second worker never sends a second write.
* ``ref`` travels as ``PayPal-Request-Id``. PayPal keeps these keys (6 hours
  for Orders, 45 days for Payments, 3 hours for Vault) and answers a repeat
  with the original result, so when an earlier attempt's outcome is unknown
  the check is a resend under the same reference - never a new one.
* The echoed amount is compared (as ``Decimal``) before the outcome is kept.
* The outcome stored is what PayPal's status says, mapped by ``outcomes``.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from paypal.core import ApiError, OAuthProviderError

from . import outcomes
from .errors import NEVER_SENT, AmountMismatch, OutcomeUnknown, provider_message
from .models import InstallIdentity, Outcome, ProviderWrite

logger = logging.getLogger('apps.payments')

# PayPal's documented PayPal-Request-Id retention per API.
ORDERS_KEY_RETENTION = timedelta(hours=6)
PAYMENTS_KEY_RETENTION = timedelta(days=45)
VAULT_KEY_RETENTION = timedelta(hours=3)


def send_window() -> timedelta:
    """How long a claim may sit in ``sending`` before another request checks it."""
    return timedelta(seconds=settings.PAYPAL_TIMEOUT * 2 + 30)


def install_prefix() -> str:
    """This install's reference prefix: configured, or generated once and stored."""
    configured = getattr(settings, 'PAYPAL_REQUEST_PREFIX', '')
    if configured:
        return configured
    identity = InstallIdentity.objects.filter(pk=1).first()
    if identity is None:
        try:
            with transaction.atomic():
                identity = InstallIdentity.objects.create(pk=1, prefix='osc' + uuid.uuid4().hex[:10])
        except IntegrityError:
            identity = InstallIdentity.objects.get(pk=1)  # a concurrent first request made it
    return identity.prefix


def order_reference(order) -> str:
    """The order as PayPal sees it (``custom_id``): unique across installs."""
    return '%s-%s' % (install_prefix(), order.number)


def deterministic_ref(*parts: object) -> str:
    """A reference derived from this install and the operation step."""
    ref = '-'.join([install_prefix()] + [str(p) for p in parts])
    if len(ref) > 108:
        raise ValueError('PayPal request reference too long: %r' % ref)
    return ref


@dataclass(frozen=True)
class Answer:
    """What one step's response says, read the same way for every step."""

    provider_id: str
    status: str  # PayPal's status value as sent
    outcome: str  # outcomes.DONE / PENDING / FAILED / UNKNOWN
    provider_time: datetime | None
    amount: Decimal | None = None
    currency: str | None = None


@dataclass
class WriteResult:
    record: ProviderWrite
    result: Any = None  # the SDK model, when this request got one
    answer: Answer | None = None
    in_flight: bool = False  # another request holds a live claim

    @property
    def outcome(self) -> str:
        return self.record.outcome


# -- claim store -------------------------------------------------------------

def try_claim(ref: str, operation: str, order=None) -> bool:
    now = timezone.now()
    try:
        with transaction.atomic():
            ProviderWrite.objects.create(
                ref=ref, operation=operation, order=order, claimed_at=now)
        return True
    except IntegrityError:
        # A failure recorded with no provider id means nothing happened at
        # PayPal; its claim is released and may be re-taken - atomically.
        return ProviderWrite.objects.filter(
            ref=ref, outcome=Outcome.FAILED, provider_id='',
        ).update(outcome=Outcome.SENDING, claimed_at=now, detail='') == 1


def load_existing(ref: str) -> ProviderWrite:
    return ProviderWrite.objects.get(ref=ref)


def complete(ref: str, outcome: str, answer: Answer | None = None, detail: str = '') -> ProviderWrite:
    fields: dict[str, Any] = {'outcome': outcome, 'detail': detail[:500], 'updated_at': timezone.now()}
    if answer is not None:
        fields.update(provider_id=answer.provider_id, provider_status=answer.status[:64],
                      provider_time=answer.provider_time)
    ProviderWrite.objects.filter(ref=ref).update(**fields)
    return load_existing(ref)


# -- the safe write ----------------------------------------------------------

def safe_write(
    ref: str,
    *,
    operation: str,
    send: Callable[[str], Any],
    read: Callable[[Any], Answer],
    retention: timedelta,
    order=None,
    sent: tuple[Decimal, str] | None = None,
    apply: Callable[[ProviderWrite, Any, Answer], None] | None = None,
) -> WriteResult:
    """
    Make one PayPal write step at most once.

    ``send(key)`` makes the SDK call with ``key`` as ``PayPal-Request-Id``.
    ``read(result)`` turns the SDK response into an ``Answer``; it raises
    ``ValueError`` when the response lacks the id or status we depend on.
    ``apply(record, result, answer)`` updates the app's own records in the
    same transaction that stores the outcome.
    """
    checking = False
    if not try_claim(ref, operation, order):
        existing = load_existing(ref)
        if existing.outcome == Outcome.SENDING and existing.claimed_at > timezone.now() - send_window():
            return WriteResult(existing, in_flight=True)
        if existing.outcome not in (Outcome.SENDING, Outcome.UNKNOWN):
            return WriteResult(existing)
        # A stale sender, or an unresolved outcome: check, never create anew.
        if existing.claimed_at < timezone.now() - retention:
            # PayPal no longer holds this request id, so a resend could make
            # a second one. Leave it for an operator.
            raise OutcomeUnknown(ref, 'The outcome of this PayPal request is unknown and its '
                                      'reference has expired at PayPal; an operator must check it '
                                      'in the PayPal dashboard.')
        checking = True
        ProviderWrite.objects.filter(ref=ref).update(outcome=Outcome.SENDING, claimed_at=timezone.now())

    # CALL - a first attempt, or the check: a resend under the SAME reference.
    result = None
    try:
        result = send(ref)
    except NEVER_SENT:
        if checking:
            complete(ref, Outcome.UNKNOWN, detail='check could not reach PayPal')
            raise OutcomeUnknown(ref) from None
        complete(ref, Outcome.FAILED, detail='never sent: PayPal unreachable')
        raise
    except ApiError as e:
        if e.status_code < 500:
            refused = ((e.status_code in (400, 422) and not isinstance(e.error, OAuthProviderError))
                       or not checking)
            message, issues = provider_message(e)
            detail = '%s %s %s' % (e.status_code, message, ' '.join(issues))
            complete(ref, Outcome.FAILED if refused else Outcome.UNKNOWN, detail=detail)
            if not refused:
                raise OutcomeUnknown(ref) from e
            raise
        logger.warning('PayPal %s answered HTTP %s for %s; outcome unknown', operation, e.status_code, ref)
    except (httpx.RequestError, ValueError) as e:
        logger.warning('No readable answer from PayPal %s for %s: %s', operation, ref, type(e).__name__)

    if result is None:
        # PayPal de-duplicates by request id, so a repeat of this request
        # (same reference) is the lookup that settles it.
        complete(ref, Outcome.UNKNOWN, detail='no readable answer')
        raise OutcomeUnknown(ref)

    try:
        answer = read(result)
    except ValueError as e:
        complete(ref, Outcome.UNKNOWN, detail='response missing %s' % e)
        raise OutcomeUnknown(ref) from e

    # VERIFY before keeping it: PayPal is authoritative for what happened,
    # not for what was asked.
    if sent is not None and answer.outcome in (outcomes.DONE, outcomes.PENDING):
        sent_amount, sent_currency = sent
        if (answer.amount, answer.currency) != (Decimal(sent_amount), sent_currency):
            with transaction.atomic():
                record = complete(ref, Outcome.NEEDS_REVIEW, answer,
                                  detail='echoed %s %s' % (answer.amount, answer.currency))
                if apply is not None:
                    apply(record, result, answer)
            logger.error('PayPal %s %s echoed %s %s, expected %s %s', operation, ref,
                         answer.amount, answer.currency, sent_amount, sent_currency)
            raise AmountMismatch(ref, answer.amount, answer.currency)

    # COMPLETE from what PayPal said.
    with transaction.atomic():
        record = complete(ref, answer.outcome, answer)
        if apply is not None:
            apply(record, result, answer)
    return WriteResult(record, result, answer)
