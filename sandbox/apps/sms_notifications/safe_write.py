"""
The safe write: every message this app sends goes through ``safe_send``.

    claim -> call -> check -> complete

* The claim is a committed ``SmsNotification`` row with a unique ``reference``,
  inserted before the provider call. The database's unique constraint decides
  the winner, so two requests (or two workers) cannot both send.
* Twilio's Messages API has no idempotency key and no reference field, so the
  reference travels as a short token in the message text (``Ref <token>``) and a
  may-have-landed send is settled by listing our messages to that destination
  and matching the token (``gateway.find_by_reference``).
* Only the provider's answer settles an unknown. Nothing turns "unknown" into
  "failed" by time, and nothing re-sends under a new reference.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import timedelta
from typing import Any

import httpx
from django.db import IntegrityError, transaction
from django.utils import timezone
from twilio_sdk.core import ApiError

from . import gateway
from .gateway import MessageSnapshot
from .models import SmsNotification

logger = logging.getLogger("apps.sms_notifications")

# Longer than one provider call can take (timeout, no retries), so a claim older
# than this with no answer belongs to a request that died.
SEND_WINDOW = timedelta(minutes=2)

NEVER_SENT = gateway.NEVER_SENT


def ref_token(reference: str) -> str:
    digest = hashlib.sha256(reference.encode()).digest()
    return base64.b32encode(digest).decode()[:10]


def try_claim(reference: str, fields: dict[str, Any]) -> tuple[SmsNotification, bool]:
    """Insert-or-fail. Returns (row, True) for the one caller that owns the send."""
    now = timezone.now()
    try:
        with transaction.atomic():
            row = SmsNotification.objects.create(
                reference=reference, ref_token=ref_token(reference),
                outcome=SmsNotification.SENDING, claimed_at=now, **fields)
        return row, True
    except IntegrityError:
        pass
    # A failed send that never reached the provider released its claim: take it
    # again atomically (compare-and-set), so exactly one caller re-sends.
    retaken = SmsNotification.objects.filter(
        reference=reference, outcome=SmsNotification.FAILED, provider_sid__isnull=True,
    ).update(outcome=SmsNotification.SENDING, claimed_at=now, error_code=None, provider_status="")
    return SmsNotification.objects.get(reference=reference), retaken == 1


def complete(row: SmsNotification, outcome: str, answer: MessageSnapshot | None = None,
             error_code: int | None = None) -> SmsNotification:
    """Record what is known. Provider fields come from the provider's answer only."""
    row.outcome = outcome
    fields = ["outcome"]
    if answer is not None:
        row.provider_sid = answer.sid
        row.provider_status = answer.status or ""
        row.error_code = answer.error_code
        row.provider_sent_at = answer.date_sent
        row.provider_created_at = answer.date_created
        row.last_checked_at = timezone.now()
        fields += ["provider_sid", "provider_status", "error_code", "provider_sent_at",
                   "provider_created_at", "last_checked_at"]
    elif error_code is not None:
        row.error_code = error_code
        fields.append("error_code")
    row.save(update_fields=fields)
    return row


def settle_by_lookup(row: SmsNotification) -> SmsNotification:
    """Ask the provider whether the send carrying this row's reference landed."""
    try:
        found = gateway.find_by_reference(row.to_number, row.ref_token)
    except (ApiError, httpx.RequestError, ValueError, gateway.ProviderError) as exc:
        logger.warning("notification %s: lookup by reference failed (%s); outcome stays unknown",
                       row.pk, type(exc).__name__)
        return complete(row, SmsNotification.UNKNOWN)
    if found is None or found.sid is None:
        # Not found (yet): a timed-out send can still land, so this proves nothing.
        return complete(row, SmsNotification.UNKNOWN)
    return complete(row, gateway.send_outcome(found.status), found)


def safe_send(reference: str, fields: dict[str, Any]) -> SmsNotification:
    """Send the message described by ``fields`` at most once for ``reference``."""
    row, claimed = try_claim(reference, fields)
    if not claimed:
        if row.outcome == SmsNotification.SENDING and row.claimed_at > timezone.now() - SEND_WINDOW:
            return row  # in flight in another request: answer "in progress", no provider call
        if row.outcome not in (SmsNotification.SENDING, SmsNotification.UNKNOWN):
            return row  # settled: answer from the stored outcome
        # A stale sender or an unresolved outcome: LOOK, never create.
        return settle_by_lookup(row)

    try:
        answer = gateway.create_message(row.to_number, row.body, row.scheduled_for)
    except gateway.NotConfigured:
        logger.error("notification %s: Twilio is not configured; nothing sent", row.pk)
        return complete(row, SmsNotification.FAILED)  # never sent: claim released
    except NEVER_SENT as exc:
        logger.warning("notification %s: send never left (%s)", row.pk, type(exc).__name__)
        return complete(row, SmsNotification.FAILED)  # never sent: claim released
    except ApiError as exc:
        if exc.status_code < 500:
            code = gateway.provider_code(exc.error)
            logger.warning("notification %s: provider refused the send (HTTP %s, code %s)",
                           row.pk, exc.status_code, code)
            return complete(row, SmsNotification.FAILED, error_code=code)  # refused: nothing created
        logger.warning("notification %s: provider %s on send; checking by reference",
                       row.pk, exc.status_code)
        return settle_by_lookup(row)  # a 5xx may still have landed
    except (httpx.RequestError, ValueError) as exc:
        logger.warning("notification %s: no readable answer (%s); checking by reference",
                       row.pk, type(exc).__name__)
        return settle_by_lookup(row)  # sent, no readable answer: may have landed

    if answer.sid is None:
        # Accepted but we cannot name what was created: same as no answer.
        return settle_by_lookup(row)
    return complete(row, gateway.send_outcome(answer.status), answer)

