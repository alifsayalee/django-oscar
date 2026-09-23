"""
Claim rows for writes sent to Maxio.

Maxio is the system of record for customers and subscriptions. These rows only
record that *we asked*, under which reference, and what we know about the
outcome - so a double-submit never produces a second customer or subscription,
and an outcome we could not read can be reconciled by that reference later.
"""

from django.db import models
from django.db.models import Q

from oscar.core.compat import AUTH_USER_MODEL


class MaxioCustomer(models.Model):
    SENDING, DONE, UNKNOWN, FAILED = 'sending', 'done', 'unknown', 'failed'
    STATUS_CHOICES = (
        (SENDING, 'Sending'),
        (DONE, 'Done'),
        (UNKNOWN, 'Outcome unknown'),
        (FAILED, 'Failed'),
    )

    # The one-to-one is the claim: one Maxio customer per shopper.
    user = models.OneToOneField(AUTH_USER_MODEL, related_name='maxio_customer', on_delete=models.CASCADE)
    reference = models.CharField(max_length=128, unique=True)
    maxio_customer_id = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    # Set just before the create is sent; until then nothing can exist at Maxio.
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user_id} -> {self.maxio_customer_id or self.reference} ({self.status})'


class SubscriptionRequest(models.Model):
    SENDING, PENDING, DONE = 'sending', 'pending', 'done'
    UNKNOWN, NEEDS_REVIEW, FAILED, ENDED = 'unknown', 'needs_review', 'failed', 'ended'
    STATUS_CHOICES = (
        (SENDING, 'Sending'),
        (PENDING, 'Pending at Maxio'),
        (DONE, 'Subscribed'),
        (UNKNOWN, 'Outcome unknown'),
        (NEEDS_REVIEW, 'Needs review'),
        (FAILED, 'Failed'),
        (ENDED, 'Ended'),
    )
    # Statuses that no longer occupy the plan: the shopper may subscribe to it again.
    RELEASED = (FAILED, ENDED)

    user = models.ForeignKey(AUTH_USER_MODEL, related_name='subscription_requests', on_delete=models.CASCADE)
    plan_handle = models.CharField(max_length=255)
    reference = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    state = models.CharField(max_length=32, blank=True)
    maxio_subscription_id = models.BigIntegerField(null=True, blank=True)
    plan_name = models.CharField(max_length=255, blank=True)
    price_in_cents = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    next_billing_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'plan_handle'],
                condition=~Q(status__in=['failed', 'ended']),
                name='subscriptions_one_live_request_per_plan',
            ),
        ]

    def __str__(self):
        return f'{self.user_id} / {self.plan_handle} ({self.status})'
