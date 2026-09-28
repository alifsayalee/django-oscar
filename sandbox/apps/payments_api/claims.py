"""The Django-backed claim store behind ``safe_write`` and the per-install reference prefix."""

import secrets

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import InstallIdentity, ProviderWrite
from .paypal_gateway.safe_write import WriteRecord


def _record(row):
    return WriteRecord(
        reference=row.reference,
        outcome=row.outcome,
        provider_id=row.provider_id or None,
        provider_time=row.provider_time,
        claimed_at=row.claimed_at,
    )


class DatabaseClaimStore:
    """Each method commits on its own: a claim must be visible to every other worker at once and must
    survive whatever happens to the request after the provider call. Callers are never inside an
    atomic block when they reach here (the API views opt out of ATOMIC_REQUESTS)."""

    def try_claim(self, reference, operation):
        try:
            with transaction.atomic():
                ProviderWrite.objects.create(
                    reference=reference, operation=operation, outcome="sending", claimed_at=timezone.now()
                )
        except IntegrityError:
            return False  # the unique index rejected the second claim
        return True

    def load_existing(self, reference):
        return _record(ProviderWrite.objects.get(reference=reference))

    def complete(self, reference, outcome, provider_id=None, provider_time=None, detail=""):
        row = ProviderWrite.objects.get(reference=reference)
        if outcome == "failed" and not provider_id:
            # Never sent, or refused: nothing happened at PayPal, so release the claim.
            record = _record(row)
            row.delete()
            return WriteRecord(record.reference, "failed", None, None, record.claimed_at)
        row.outcome = outcome
        if provider_id:
            row.provider_id = provider_id
        if provider_time:
            row.provider_time = provider_time
        row.detail = detail[:255]
        row.save(update_fields=["outcome", "provider_id", "provider_time", "detail", "updated_at"])
        return _record(row)


def install_prefix():
    configured = getattr(settings, "PAYPAL_REFERENCE_PREFIX", "")
    if configured:
        return configured
    identity = InstallIdentity.objects.filter(key="install").first()
    if identity is None:
        try:
            with transaction.atomic():
                identity = InstallIdentity.objects.create(key="install", prefix="osc" + secrets.token_hex(4))
        except IntegrityError:
            identity = InstallIdentity.objects.get(key="install")
    return identity.prefix
