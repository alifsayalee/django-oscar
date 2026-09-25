"""
Local records of the writes this site asks Maxio to make.

Maxio stays the system of record for what exists (customers, subscriptions,
their state). These rows record *that we asked*: each one is a claim taken
before the provider call, keyed by the reference sent with the write, so a
double-submit, a retry or a second worker can never send a second create.
"""
from django.db import models

from oscar.core.compat import AUTH_USER_MODEL


class Outcome:
    SENDING = 'sending'     # claimed, no answer yet
    DONE = 'done'           # the provider says it is in effect
    PENDING = 'pending'     # accepted; provider not finished, or something outstanding
    FAILED = 'failed'       # never sent, refused, or reported failed/undone
    UNKNOWN = 'unknown'     # may have happened; only the provider can settle it

    CHOICES = [(SENDING, 'Sending'), (DONE, 'Done'), (PENDING, 'Pending'),
               (FAILED, 'Failed'), (UNKNOWN, 'Unknown')]


class BillingInstall(models.Model):
    """One row: a random id naming this install in the references it sends to Maxio."""

    install_id = models.CharField(max_length=32, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.install_id


class ProviderClaim(models.Model):
    reference = models.CharField(max_length=255, unique=True)
    outcome = models.CharField(max_length=16, choices=Outcome.CHOICES, default=Outcome.SENDING)
    claimed_at = models.DateTimeField()
    # The provider's own clock for the record, for reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BillingCustomer(ProviderClaim):
    """Links an Oscar user to their Maxio customer."""

    user = models.OneToOneField(
        AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='billing_customer')
    provider_id = models.BigIntegerField(null=True, blank=True, unique=True)

    def __str__(self):
        return f'{self.user} -> Maxio customer {self.provider_id or "?"} ({self.outcome})'


class BillingSubscription(ProviderClaim):
    """A subscribe request made by an Oscar user, and the Maxio subscription it produced."""

    user = models.ForeignKey(
        AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='billing_subscriptions')
    plan_handle = models.CharField(max_length=255)
    provider_id = models.BigIntegerField(null=True, blank=True, unique=True)
    # Raw Maxio subscription state as last read (e.g. "active", "past_due").
    provider_state = models.CharField(max_length=32, blank=True, default='')

    class Meta:
        indexes = [models.Index(fields=['user', 'plan_handle'])]

    def __str__(self):
        return f'{self.user} {self.plan_handle} -> {self.provider_id or "?"} ({self.outcome})'
