"""Local durable record of subscribe *intents*.

Maxio is the record of what subscriptions exist; this table records *that we asked*, so a
double-click or overlapping retry cannot create two customers/subscriptions. One live claim
per (user, plan) is enforced by a partial-unique constraint, and the winning row is written
*before* the provider call (per python-configuration-resilience, "one operation, one durable
row"). This is not a copy of Maxio's data.
"""

from django.conf import settings
from django.db import models


class SubscriptionIntent(models.Model):
    STATUS_PENDING = "pending"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_UNKNOWN = "unknown"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_DONE, "Done"),
        (STATUS_FAILED, "Failed"),
        (STATUS_NEEDS_REVIEW, "Needs review"),
        (STATUS_UNKNOWN, "Unknown"),
    ]

    # A claim is "live" (blocks a duplicate) while pending or done.
    LIVE_STATUSES = (STATUS_PENDING, STATUS_DONE)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscription_intents",
    )
    plan_handle = models.CharField(max_length=255)
    # Client-chosen reference sent to Maxio as the subscription reference; also the key we
    # reconcile a may-have-landed write against.
    reference = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    maxio_customer_id = models.PositiveBigIntegerField(null=True, blank=True)
    maxio_subscription_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "subscriptions"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "plan_handle"],
                condition=models.Q(status__in=("pending", "done")),
                name="uniq_live_intent_per_user_plan",
            )
        ]

    def __str__(self):
        return f"{self.reference} ({self.status})"
