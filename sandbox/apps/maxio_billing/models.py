from django.conf import settings
from django.db import models


class BillingInstall(models.Model):
    """
    Singleton row holding an id unique to this installation's database.

    Every reference sent to Maxio is prefixed with it (unless MAXIO_REFERENCE_PREFIX
    overrides it), so two installs sharing one Maxio site never collide on a
    reference derived from the same local user id.
    """

    install_id = models.CharField(max_length=32, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.install_id


class BillingWrite(models.Model):
    """
    The claim on one provider write, recorded *before* the write is sent.

    ``reference`` is sent to Maxio with the write and is what a write whose outcome
    is unknown is looked up by. Its UNIQUE constraint is what rejects a second
    request for the same write (a double-click, a retry, a second worker).
    Maxio stays the record of what exists; this records that we asked.
    """

    KIND_CUSTOMER = 'customer'
    KIND_SUBSCRIPTION = 'subscription'
    KIND_CHOICES = [
        (KIND_CUSTOMER, 'Customer'),
        (KIND_SUBSCRIPTION, 'Subscription'),
    ]

    # Claimed, no answer yet: nobody else calls the provider for it.
    SENDING = 'sending'
    # What the caller asked for is in effect.
    DONE = 'done'
    # The provider accepted it and has not finished (or it exists but needs attention).
    PENDING = 'pending'
    # Never sent, refused, or reported failed / ended by the provider.
    FAILED = 'failed'
    # It happened, but not as asked (e.g. a different price than the one shown).
    NEEDS_REVIEW = 'needs_review'
    # May have happened: only the provider's answer to a lookup settles it.
    UNKNOWN = 'unknown'
    OUTCOME_CHOICES = [
        (SENDING, 'Sending'),
        (DONE, 'Done'),
        (PENDING, 'Pending'),
        (FAILED, 'Failed'),
        (NEEDS_REVIEW, 'Needs review'),
        (UNKNOWN, 'Unknown'),
    ]

    reference = models.CharField(max_length=255, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='billing_writes')
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    outcome = models.CharField(max_length=32, choices=OUTCOME_CHOICES, default=SENDING)
    plan_handle = models.CharField(max_length=255, blank=True, default='')
    expected_price_in_cents = models.BigIntegerField(null=True, blank=True)

    provider_id = models.CharField(max_length=64, blank=True, default='')
    provider_state = models.CharField(max_length=64, blank=True, default='')
    # The provider's own clock (created_at of the record it holds), for reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True)
    # What the provider said, as shown to the caller (a summary, never credentials).
    snapshot = models.JSONField(default=dict, blank=True)
    detail = models.TextField(blank=True, default='')

    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-claimed_at']
        indexes = [models.Index(fields=['user', 'kind'])]

    def __str__(self) -> str:
        return f'{self.reference} ({self.outcome})'
