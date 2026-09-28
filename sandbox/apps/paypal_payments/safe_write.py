"""
The ONE path every PayPal write goes through: claim, call, check, verify, complete.

* claim   — insert a ``ProviderWrite`` row keyed by the write's reference. The database's unique
            constraint decides the single winner, across threads, processes and hosts.
* call    — the reference is sent as ``PayPal-Request-Id``.
* check   — when the outcome is unknown (timeout, 5xx, unreadable body) the write is looked up by
            its own id, or re-sent under the SAME reference (PayPal returns the original).
* verify  — the amount PayPal echoes must equal the amount asked for, to the cent.
* complete— the outcome recorded is the one PayPal's status says, never "it answered".
"""
import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .gateway import ProviderError, translate
from .models import InstallIdentity, ProviderWrite

logger = logging.getLogger('apps.paypal_payments')

# Longer than one attempt (timeout, no retries) plus margin: a "sending" claim older than this
# belongs to a request that died, and the next request may check it.
SEND_WINDOW = timedelta(seconds=90)

_prefix_cache = {}


def install_prefix():
    configured = getattr(settings, 'PAYPAL_REFERENCE_PREFIX', '')
    if configured:
        return configured
    if 'prefix' not in _prefix_cache:
        identity = InstallIdentity.objects.order_by('pk').first()
        if identity is None:
            try:
                with transaction.atomic():
                    identity = InstallIdentity.objects.create(key=secrets.token_hex(6))
            except IntegrityError:
                identity = InstallIdentity.objects.order_by('pk').first()
        _prefix_cache['prefix'] = 'osc-%s' % identity.key
    return _prefix_cache['prefix']


def make_ref(*parts):
    """A reference derived from the operation and the step — the same on every attempt and every
    repeat of the request, never random."""
    return ':'.join([install_prefix()] + [str(p) for p in parts])


def _now():
    return timezone.now()


def try_claim(ref, *, kind, owner=None, order=None, sent=None):
    """Insert-or-fail: True for exactly one caller."""
    try:
        with transaction.atomic():
            ProviderWrite.objects.create(
                ref=ref, kind=kind, owner=owner, order=order, claimed_at=_now(),
                sent_amount=sent[0] if sent else None, currency=sent[1] if sent else '')
        return True
    except IntegrityError:
        return False


def load_existing(ref):
    return ProviderWrite.objects.filter(ref=ref).first()


def _take_over(record):
    """Only one request checks a stale or unresolved write at a time (compare-and-set)."""
    return ProviderWrite.objects.filter(pk=record.pk, claimed_at=record.claimed_at,
                                        outcome=record.outcome).update(claimed_at=_now()) == 1


def complete(ref, outcome, answer=None, error=None):
    """Record the outcome. A failure with no provider id (never sent, or refused) releases the
    claim: nothing happened, so the same reference may be sent again."""
    record = ProviderWrite.objects.get(ref=ref)
    if outcome == ProviderWrite.FAILED and (answer is None or not answer.provider_id):
        record.outcome = ProviderWrite.FAILED
        record.error = error or {}
        record.delete()
        return record
    record.outcome = outcome
    record.completed_at = _now()
    if error is not None:
        record.error = error
    if answer is not None:
        record.provider_id = answer.provider_id or record.provider_id
        record.provider_status = answer.status or ''
        record.provider_time = answer.provider_time or record.provider_time
        record.amount = answer.amount
        if answer.currency:
            record.currency = answer.currency
        record.data = _json_safe(answer.data)
    record.save()
    return record


def _json_safe(value):
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _amount_matches(answer, sent):
    if sent is None:
        return True
    if answer.amount is None or answer.currency is None:
        return False
    return (Decimal(str(answer.amount)), answer.currency) == (Decimal(str(sent[0])), sent[1])


def safe_write(ref, *, kind, send, read, find=None, repeat_is_safe=True, sent=None, owner=None,
               order=None):
    """Run one provider write step; returns its ``ProviderWrite`` record.

    send(ref)      makes the provider call with ``ref`` as PayPal-Request-Id; returns the SDK model
    read(result)   the step's response as a provider.Answer (outcome already mapped by the step's mapper)
    find(record)   lookup by the provider's own id for this write (record.provider_id, or the
                   record being called off); returns None when it has nothing to look up by
    repeat_is_safe PayPal will not make a second one for this reference (PayPal-Request-Id)
    sent           (amount, currency) asked for; the echoed amount must match

    Raises ProviderError only when nothing happened at PayPal (never sent, or refused on a first
    send) — the claim is then released. Every other result, including "unknown", is a record.
    """
    checking = False
    existing = None
    if not try_claim(ref, kind=kind, owner=owner, order=order, sent=sent):
        existing = load_existing(ref)
        if existing is None:            # released between our insert and our read: claim again
            return safe_write(ref, kind=kind, send=send, read=read, find=find,
                              repeat_is_safe=repeat_is_safe, sent=sent, owner=owner, order=order)
        if existing.outcome == ProviderWrite.SENDING and existing.claimed_at > _now() - SEND_WINDOW:
            return existing             # in flight: answer "in progress", no provider call
        if existing.outcome in (ProviderWrite.DONE, ProviderWrite.FAILED, ProviderWrite.NEEDS_REVIEW):
            return existing             # settled: answer from it
        if not _take_over(existing):
            return load_existing(ref) or existing   # another request is checking it right now
        checking = True                 # stale sender, unknown or pending: LOOK, never a new write

    result = None
    if checking and existing.outcome == ProviderWrite.PENDING:
        # PayPal holds it and has not finished: only a re-read by its own id can move it on.
        if find is None:
            return existing
        try:
            result = find(existing)
        except Exception as exc:        # a failed re-read settles nothing
            logger.warning('Re-read of pending %s failed: %s', ref, translate(exc).code)
            return existing
        if result is None:
            return existing
    else:
        if not checking or repeat_is_safe:
            try:
                result = send(ref)
            except Exception as exc:
                err = translate(exc)
                if err.never_sent and not checking:
                    complete(ref, ProviderWrite.FAILED, error=err.as_dict())
                    raise err
                if err.refused and not checking:
                    complete(ref, ProviderWrite.FAILED, error=err.as_dict())
                    raise err
                logger.warning('PayPal write %s has no readable outcome (%s); checking', ref, err.code)
                result = None
                if checking:
                    # the resend WAS the check, and it settled nothing
                    return complete(ref, ProviderWrite.UNKNOWN, error=err.as_dict())
        if result is None:
            result = _check(ref, find, send, repeat_is_safe, checking)
            if result is None:
                return complete(ref, ProviderWrite.UNKNOWN,
                                error={'code': 'outcome_unknown',
                                       'message': 'PayPal has not confirmed this request either way.'})

    answer = read(result)
    if not _amount_matches(answer, sent) and answer.outcome in ('done', 'pending'):
        logger.error('PayPal write %s echoed %s %s, asked for %s %s', ref, answer.amount,
                     answer.currency, sent[0], sent[1])
        return complete(ref, ProviderWrite.NEEDS_REVIEW, answer,
                        error={'code': 'amount_mismatch',
                               'message': 'PayPal recorded %s %s; this app asked for %s %s.' % (
                                   answer.amount, answer.currency, sent[0], sent[1])})
    return complete(ref, answer.outcome, answer)


def _check(ref, find, send, repeat_is_safe, checking):
    """Look the write up after a send with no readable answer: by the provider's own id when we
    have one, else by re-sending under the same reference. A failed lookup is not an absence."""
    record = load_existing(ref)
    try:
        if find is not None and record is not None:
            found = find(record)
            if found is not None:
                return found
        if repeat_is_safe:
            return send(ref)
    except Exception as exc:
        logger.warning('Check of %s failed: %s', ref, translate(exc).code)
    return None
