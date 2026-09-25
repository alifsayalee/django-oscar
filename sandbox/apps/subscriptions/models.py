"""
Local record of every write this site makes to Maxio Advanced Billing.

Maxio is the system of record for *what exists*; a ``ProviderWrite`` records
*that we asked*. The row is inserted (the "claim") before the provider call and
its unique ``reference`` is sent to Maxio with the write, so a double submit,
a second worker or an unanswered request can always be settled by that
reference instead of creating a second customer or subscription.
"""
from django.db import models

from oscar.core.compat import AUTH_USER_MODEL


class ProviderWrite(models.Model):
    KIND_CUSTOMER = 'customer'
    KIND_SUBSCRIPTION = 'subscription'
    KIND_CHOICES = (
        (KIND_CUSTOMER, 'Customer'),
        (KIND_SUBSCRIPTION, 'Subscription'),
    )

    # Claimed, request in flight: nobody else calls the provider for it.
    SENDING = 'sending'
    # The provider says what was asked for is in effect.
    DONE = 'done'
    # The provider accepted it and has not finished (or something is outstanding).
    PENDING = 'pending'
    # Never sent, refused, or reported failed/undone by the provider.
    FAILED = 'failed'
    # It happened, but not as asked.
    NEEDS_REVIEW = 'needs_review'
    # May have happened: only the provider's answer to a lookup settles it.
    UNKNOWN = 'unknown'
    OUTCOME_CHOICES = (
        (SENDING, 'Sending'),
        (DONE, 'Done'),
        (PENDING, 'Pending'),
        (FAILED, 'Failed'),
        (NEEDS_REVIEW, 'Needs review'),
        (UNKNOWN, 'Unknown'),
    )

    # The unique constraint on this column is the claim: the database rejects
    # a second insert for the same reference, in any process.
    reference = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    user = models.ForeignKey(
        AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='provider_writes')
    plan_handle = models.CharField(max_length=255, blank=True)
    outcome = models.CharField(max_length=32, choices=OUTCOME_CHOICES, default=SENDING)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_state = models.CharField(max_length=64, blank=True)
    # The provider's own clock (created_at), for reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True)
    # Snapshot of what the provider returned (plan name, price, next billing date).
    detail = models.JSONField(default=dict, blank=True)
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-claimed_at']
        indexes = [models.Index(fields=['user', 'kind'])]

    def __str__(self) -> str:
        return '%s %s (%s)' % (self.kind, self.reference, self.outcome)
