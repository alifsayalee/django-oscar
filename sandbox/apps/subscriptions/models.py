from django.db import models

from oscar.core.compat import AUTH_USER_MODEL


class BillingClaim(models.Model):
    """
    A write this site asked Maxio to make, recorded *before* the request is sent.

    Maxio is the system of record for customers and subscriptions; this table
    only records that we asked, so that a double submit or two workers racing
    make one provider write, and so that a write whose answer was lost can be
    looked up again by the reference it carried.
    """

    CUSTOMER = 'customer'
    SUBSCRIPTION = 'subscription'
    KIND_CHOICES = (
        (CUSTOMER, 'Customer'),
        (SUBSCRIPTION, 'Subscription'),
    )

    # Claimed, no answer yet - nobody else calls the provider for it.
    SENDING = 'sending'
    # What the caller asked for is in effect.
    DONE = 'done'
    # The provider accepted it and has not finished.
    PENDING = 'pending'
    # Never sent, refused, or made and then undone.
    FAILED = 'failed'
    # Happened, but not as asked.
    NEEDS_REVIEW = 'needs_review'
    # May have happened: only the provider's answer settles it.
    UNKNOWN = 'unknown'
    OUTCOME_CHOICES = (
        (SENDING, 'Sending'),
        (DONE, 'Done'),
        (PENDING, 'Pending'),
        (FAILED, 'Failed'),
        (NEEDS_REVIEW, 'Needs review'),
        (UNKNOWN, 'Unknown'),
    )

    # The local identity of the operation, e.g. "customer:12". Unique, so the
    # database rejects a second claim on the same operation.
    key = models.CharField(max_length=255, unique=True)
    # The reference sent to Maxio with the write, fixed when the claim is
    # first inserted and reused on every attempt.
    reference = models.CharField(max_length=128, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    user = models.ForeignKey(
        AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='billing_claims')
    plan_handle = models.CharField(max_length=255, blank=True)

    outcome = models.CharField(max_length=32, choices=OUTCOME_CHOICES, default=SENDING)
    provider_id = models.CharField(max_length=64, blank=True)
    # The provider's clock for the write (for reconciliation).
    provider_time = models.DateTimeField(null=True, blank=True)
    # What the caller is shown for this write (plan, price, state, ...).
    snapshot = models.JSONField(default=dict, blank=True)

    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-claimed_at',)

    def __str__(self) -> str:
        return '%s (%s)' % (self.key, self.outcome)
