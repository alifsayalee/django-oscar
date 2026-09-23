"""Local persistence for the Maxio subscription capability.

This is an **operation-tracking** table, not a parallel copy of Oscar's or Maxio's
domain models. Maxio stays the record of *what exists*; this row records *that we asked*
to create a subscription, so that a double-click or a concurrent retry cannot create two
subscriptions for the same (user, plan). See ``services.subscribe`` for how the row is
claimed before the provider call and settled after it.
"""

from django.conf import settings
from django.db import models


class SubscriptionStatus(models.TextChoices):
    # The row is claimed and the provider call has not returned yet. Holds the claim so
    # nobody else calls Maxio for the same (user, plan).
    SENDING = "sending", "Sending"
    # Live states — the subscription is usable or on its way there.
    ACTIVE = "active", "Active"
    PENDING = "pending", "Pending"
    # Current but in a problem state (past_due, paused, ...). Still a live claim.
    PROBLEM = "problem", "Problem"
    # End-of-life — frees the (user, plan) pair to be subscribed again.
    ENDED = "ended", "Ended"
    # The create was rejected or never left; nothing exists provider-side.
    FAILED = "failed", "Failed"
    # The create may or may not have landed and we could not reconcile it.
    UNKNOWN = "unknown", "Unknown"


# The pair is free to re-subscribe only once the previous claim is ended or failed.
LIVE_STATUSES = frozenset(
    {
        SubscriptionStatus.SENDING,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.PENDING,
        SubscriptionStatus.PROBLEM,
        SubscriptionStatus.UNKNOWN,
    }
)


class MaxioSubscription(models.Model):
    """One durable row per logical subscribe of a user to a plan."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="maxio_subscriptions",
    )
    plan_handle = models.CharField(max_length=255)

    # References we generate and send to Maxio, used for lookups/reconciliation.
    customer_reference = models.CharField(max_length=255)
    subscription_reference = models.CharField(max_length=255)

    # Provider identifiers, populated once known.
    provider_customer_id = models.BigIntegerField(null=True, blank=True)
    provider_subscription_id = models.BigIntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.SENDING,
    )
    # The raw provider state string (e.g. "active", "past_due") for observability.
    state_raw = models.CharField(max_length=64, blank=True, default="")

    # A snapshot of confirmation fields, refreshed from the provider on each read.
    plan_name = models.CharField(max_length=255, blank=True, default="")
    price_in_cents = models.IntegerField(null=True, blank=True)
    current_period_ends_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "subscriptions"
        constraints = [
            # At most one live claim per (user, plan). A double-click's second insert
            # violates this and is caught as the idempotency signal.
            models.UniqueConstraint(
                fields=["user", "plan_handle"],
                condition=~models.Q(status__in=["ended", "failed"]),
                name="uniq_live_subscription_per_user_plan",
            ),
        ]
        indexes = [
            models.Index(fields=["subscription_reference"]),
            models.Index(fields=["provider_subscription_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.plan_handle} -> {self.provider_subscription_id} ({self.status})"
