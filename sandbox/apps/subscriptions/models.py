"""
Local records of the writes this app makes to Maxio.

Maxio stays the system of record for what exists; these rows record that we
asked, under which reference, and what the provider answered. Each row is
created (committed) *before* its provider call, and the unique constraints on
it are the claim that stops a double-click or a second worker from creating
a second customer or subscription.
"""
from django.db import models

from oscar.core.compat import AUTH_USER_MODEL


class Outcome(models.TextChoices):
    # Claimed, no answer yet. Nobody else calls the provider for it.
    SENDING = 'sending', 'Sending'
    # What the caller asked for is in effect.
    DONE = 'done', 'Done'
    # The provider accepted it and has not finished (or something is outstanding).
    PENDING = 'pending', 'Pending'
    # Refused, or reported failed or undone by the provider.
    FAILED = 'failed', 'Failed'
    # It happened, but not as asked (e.g. a different price).
    NEEDS_REVIEW = 'needs_review', 'Needs review'
    # May have happened; only a lookup by reference can settle it.
    UNKNOWN = 'unknown', 'Unknown'


class MaxioCustomer(models.Model):
    """
    The claim on, and outcome of, creating the Maxio customer for a user.
    """
    user = models.OneToOneField(
        AUTH_USER_MODEL, related_name='maxio_customer', on_delete=models.PROTECT)
    reference = models.CharField(max_length=255, unique=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    maxio_customer_id = models.BigIntegerField(null=True, blank=True, unique=True)
    claimed_at = models.DateTimeField()
    # The provider's own clock (customer.created_at), for reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'maxio_subscriptions'

    def __str__(self) -> str:
        return f'{self.reference} ({self.outcome})'


class SubscriptionEnrollment(models.Model):
    """
    The claim on, and outcome of, subscribing a user to a plan.

    ``attempt`` only advances once an earlier attempt for the same plan is
    settled as failed at the provider (e.g. canceled), so a repeated request
    always lands on the same claim and the same reference.
    """
    user = models.ForeignKey(
        AUTH_USER_MODEL, related_name='maxio_subscriptions', on_delete=models.PROTECT)
    plan_handle = models.CharField(max_length=255)
    attempt = models.PositiveIntegerField(default=1)
    reference = models.CharField(max_length=255, unique=True)
    # The catalogue price the caller was shown, checked against Maxio's echo.
    expected_price_in_cents = models.BigIntegerField()
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    claimed_at = models.DateTimeField()

    # Filled from the provider's answer.
    maxio_subscription_id = models.BigIntegerField(null=True, blank=True, unique=True)
    state = models.CharField(max_length=32, blank=True)
    plan_name = models.CharField(max_length=255, blank=True)
    price_in_cents = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    next_billing_at = models.DateTimeField(null=True, blank=True)
    # The provider's own clock (subscription.created_at), for reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'maxio_subscriptions'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'plan_handle', 'attempt'],
                name='maxio_enrollment_one_claim_per_attempt'),
        ]

    def __str__(self) -> str:
        return f'{self.reference} ({self.outcome})'
