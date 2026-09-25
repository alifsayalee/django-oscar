"""
Local records for the Maxio integration.

Maxio is the system of record for plans, customers and subscriptions. These
rows only record what this site *asked* Maxio to do, so that a repeated or
concurrent request can be recognised (the unique constraints are the claim)
and an unanswered request can be looked up again by the reference we sent.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class Outcome(models.TextChoices):
    # Claimed, request in flight; nobody else calls Maxio for this record
    SENDING = 'sending', 'Sending'
    # Maxio confirmed what was asked is in effect
    DONE = 'done', 'Done'
    # Maxio accepted it but it is not (yet) in effect
    PENDING = 'pending', 'Pending'
    # Never sent, refused, or reported failed/undone by Maxio
    FAILED = 'failed', 'Failed'
    # Happened, but not as asked
    NEEDS_REVIEW = 'needs_review', 'Needs review'
    # May have happened; only Maxio's answer to a lookup settles it
    UNKNOWN = 'unknown', 'Unknown'


# Outcomes that still hold the (user, plan) claim for a subscription
LIVE_OUTCOMES = [
    Outcome.SENDING, Outcome.DONE, Outcome.PENDING, Outcome.UNKNOWN,
    Outcome.NEEDS_REVIEW,
]


class BillingCustomer(models.Model):
    """
    Link between an Oscar user and their Maxio customer.

    Inserted (as ``sending``) before the customer is created in Maxio; the
    one-to-one user and the unique reference make a second create impossible.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='billing_customer')
    reference = models.CharField(max_length=255, unique=True)
    maxio_customer_id = models.BigIntegerField(null=True, blank=True, unique=True)
    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    last_error = models.TextField(blank=True, default='')
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'billing customer'

    def __str__(self) -> str:
        return f'{self.reference} ({self.outcome})'


class SubscriptionEnrollment(models.Model):
    """
    One request to subscribe a user to a Maxio plan.

    Inserted (as ``sending``) before the subscription is created in Maxio.
    ``reference`` is sent to Maxio as the subscription's reference and is
    what an unanswered create is looked up by.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='subscription_enrollments')
    customer = models.ForeignKey(
        BillingCustomer, on_delete=models.PROTECT, related_name='enrollments')
    plan_handle = models.CharField(max_length=255)
    reference = models.CharField(max_length=255, unique=True)
    idempotency_key = models.CharField(max_length=255, blank=True, default='')
    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.SENDING)

    maxio_subscription_id = models.BigIntegerField(null=True, blank=True, unique=True)
    provider_state = models.CharField(max_length=64, blank=True, default='')
    price_in_cents = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True, default='')
    next_billing_at = models.DateTimeField(null=True, blank=True)
    # Maxio's own clock for the last state we recorded
    provider_updated_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')

    claimed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'subscription enrollment'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'plan_handle'],
                condition=Q(outcome__in=LIVE_OUTCOMES),
                name='subscriptions_one_live_enrollment_per_plan',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.reference} ({self.outcome})'
