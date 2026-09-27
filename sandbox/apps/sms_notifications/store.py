"""The claim store behind ``provider.safe_write``, held in the application database.

The claim is a row keyed by a UNIQUE ``reference``: inserting it is the atomic insert-or-fail, so two
requests (in one process or in several) can never both make the same provider write.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone
from twilio_sdk.core import UnsetType

from . import provider


class DjangoClaimStore:
    def __init__(self, model, **defaults):
        self.model = model
        self.defaults = defaults
        self.row = None

    def try_claim(self, reference):
        now = timezone.now()
        try:
            with transaction.atomic():
                self.row = self.model.objects.create(
                    reference=reference, outcome=provider.SENDING, claimed_at=now, **self.defaults
                )
            return True
        except IntegrityError:
            pass
        # A failed write that the provider holds no record of released its claim: whoever flips it
        # back to "sending" first -- one conditional UPDATE, atomic in the database -- owns it now.
        reclaimed = self.model.objects.filter(
            reference=reference, outcome=provider.FAILED, provider_sid__isnull=True
        ).update(outcome=provider.SENDING, claimed_at=now, completed_at=None, error_code=None)
        return reclaimed == 1

    def load(self, reference):
        self.row = self.model.objects.get(reference=reference)
        return self._record(self.row)

    def complete(self, reference, outcome, answer=None, *, error_code=None):
        fields = {"outcome": outcome, "completed_at": timezone.now(), "error_code": error_code}
        if answer is not None:
            status = answer.status
            fields.update(
                provider_sid=answer.provider_id,
                provider_status="" if status is None or isinstance(status, UnsetType) else str(status)[:32],
                provider_time=answer.provider_time,
            )
        self.model.objects.filter(reference=reference).update(**fields)
        return self.load(reference)

    @staticmethod
    def _record(row):
        return provider.ClaimRecord(
            reference=row.reference, outcome=row.outcome, claimed_at=row.claimed_at, provider_id=row.provider_sid
        )
