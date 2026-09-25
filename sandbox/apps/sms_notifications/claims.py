"""
The claim store: ``SmsNotification.reference`` is UNIQUE in the database, so
inserting the row is an atomic insert-or-fail that outlives the request and the
process. Each claim is committed before the provider is called (the views run
outside ``ATOMIC_REQUESTS``), so a crash mid-call leaves a findable record.
"""
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import outcomes
from .gateway import MessageSnapshot
from .models import SmsNotification

# Longer than one attempt's worst case (10s client timeout, no retries) plus margin.
SEND_WINDOW = timedelta(seconds=60)


class ClaimStore:

    def try_claim(self, reference: str, fields: dict[str, Any]) -> bool:
        """Record ``sending`` under ``reference`` for exactly one caller."""
        now = timezone.now()
        try:
            with transaction.atomic():
                SmsNotification.objects.create(
                    reference=reference, outcome=outcomes.SENDING, claimed_at=now, **fields)
            return True
        except IntegrityError:
            pass
        # A 'failed' with no provider id means nothing happened at the provider:
        # the claim was released and may be retaken - just as atomically.
        with transaction.atomic():
            retaken = SmsNotification.objects.filter(
                reference=reference, outcome=outcomes.FAILED, provider_sid='',
            ).update(outcome=outcomes.SENDING, claimed_at=now, last_error='')
        return retaken == 1

    def load(self, reference: str) -> SmsNotification:
        return SmsNotification.objects.get(reference=reference)

    def is_in_flight(self, record: SmsNotification) -> bool:
        return record.outcome == outcomes.SENDING and record.claimed_at > timezone.now() - SEND_WINDOW

    def complete(self, reference: str, outcome: str, snap: MessageSnapshot | None = None,
                 error: str = '') -> SmsNotification:
        record = self.load(reference)
        record.outcome = outcome
        record.last_error = error[:255]
        record.last_checked_at = timezone.now()
        if snap is not None:
            apply_snapshot(record, snap)
        record.save()
        return record


def apply_snapshot(record: SmsNotification, snap: MessageSnapshot) -> None:
    """Store the provider's own fields as returned."""
    if snap.sid:
        record.provider_sid = snap.sid
    record.provider_status = snap.status or ''
    record.provider_error_code = snap.error_code
    if snap.date_created is not None:
        record.provider_date_created = snap.date_created
    record.provider_date_sent = snap.date_sent
