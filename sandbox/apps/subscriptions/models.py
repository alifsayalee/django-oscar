"""
Local records of the Maxio customers and subscriptions this site created.

Maxio is the system of record for billing state. These rows exist to claim a
write *before* it is sent (so a double-submit cannot reach Maxio twice) and to
remember the reference each write carried (so a write whose outcome is unknown
can be looked up and settled afterwards).
"""

from django.db import models
from django.db.models import Q

from oscar.core.compat import AUTH_USER_MODEL


class MaxioCustomer(models.Model):
    """One Maxio customer per sandbox user; the row is the claim on creating it."""

    PENDING, CREATED = "pending", "created"
    STATUS_CHOICES = ((PENDING, "Pending"), (CREATED, "Created"))

    user = models.OneToOneField(AUTH_USER_MODEL, related_name="maxio_customer", on_delete=models.CASCADE)
    reference = models.CharField(max_length=255, unique=True)
    maxio_customer_id = models.BigIntegerField(null=True, blank=True, unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    # When the current holder took the claim; a stale pending claim may be taken over.
    lease_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.reference} ({self.status})"


class MaxioSubscription(models.Model):
    """A subscription this site asked Maxio to create, and what became of it."""

    # Claimed, create call in flight.
    PENDING = "pending"
    # The create call was sent but no readable answer came back; settle by reference.
    UNKNOWN = "unknown"
    # Exists in Maxio and is not in a terminal state.
    LIVE = "live"
    # Exists in Maxio in a terminal state (canceled, expired, failed_to_create).
    ENDED = "ended"
    # Maxio refused the create; nothing exists there.
    REJECTED = "rejected"
    STATUS_CHOICES = (
        (PENDING, "Pending"),
        (UNKNOWN, "Unknown"),
        (LIVE, "Live"),
        (ENDED, "Ended"),
        (REJECTED, "Rejected"),
    )
    # A user holds at most one of these per plan at a time.
    OPEN_STATUSES = (PENDING, UNKNOWN, LIVE)

    user = models.ForeignKey(AUTH_USER_MODEL, related_name="maxio_subscriptions", on_delete=models.CASCADE)
    plan_handle = models.CharField(max_length=255)
    # Sent to Maxio as the subscription's own reference.
    reference = models.CharField(max_length=64, unique=True)
    maxio_subscription_id = models.BigIntegerField(null=True, blank=True, unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    lease_at = models.DateTimeField()

    # Snapshot of Maxio's view, refreshed whenever it is read.
    state = models.CharField(max_length=32, blank=True)
    plan_name = models.CharField(max_length=255, blank=True)
    price_in_cents = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    next_billing_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("user", "plan_handle"),
                condition=Q(status__in=("pending", "unknown", "live")),
                name="subscriptions_one_open_per_user_plan",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.plan_handle} for user {self.user_id} ({self.status})"
