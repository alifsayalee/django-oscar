"""Local persistence for the Maxio integration.

Maxio Advanced Billing remains the system of record for *what exists* (customers,
subscriptions). These two tables record *that we asked* — durable claim rows that make the
subscribe flow idempotent across a double-click, a client retry, or two worker processes.

Per ``python-configuration-resilience`` (the "one operation, one durable row" rule), the claim
must be written *before* the Maxio call and be keyed by a reference we generate and send, so a
provider-side record created before anything local points at it can always be reconciled by that
reference.  An in-process lock cannot close a check-then-act race between processes; a unique
constraint can.
"""

from django.conf import settings
from django.db import models


class ClaimStatus(models.TextChoices):
    """Status of *our request*, not of the Maxio record.

    ``done`` / ``pending`` / ``failed`` / ``unknown`` mirror the outcome vocabulary in
    ``python-calling-endpoints``.  ``needs_review`` is for a landing that did not match what we
    asked.  ``sending`` is the in-flight marker held between claiming and settling.
    """

    SENDING = "sending", "Sending"
    PENDING = "pending", "Pending"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    UNKNOWN = "unknown", "Unknown"
    NEEDS_REVIEW = "needs_review", "Needs review"


class MaxioCustomer(models.Model):
    """One-to-one link between a Django user and their Maxio customer.

    The row (unique on ``user``) is the claim: ``get_or_create`` guarantees exactly one per user,
    and ``reference`` is the stable idempotency key sent to Maxio, so a customer created out of
    band can still be found by reference.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="maxio_customer",
    )
    reference = models.CharField(max_length=100, unique=True)
    customer_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "maxio_subscriptions"
        verbose_name = "Maxio customer"

    def __str__(self):
        return f"{self.reference} -> maxio#{self.customer_id}"


class MaxioSubscription(models.Model):
    """Durable claim row for a subscribe request.

    The partial-unique constraint on ``(user, plan_handle)`` — excluding claims that definitely
    failed — is what makes the subscribe endpoint idempotent: a second concurrent request for the
    same plan raises ``IntegrityError`` on insert and never reaches ``create_subscription``.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="maxio_subscriptions",
    )
    plan_handle = models.CharField(max_length=100)
    # The idempotency key we send to Maxio. NOT globally unique: a genuinely failed attempt must be
    # retryable under the same reference (safe — Maxio dedupes by it, and the pre-create guard looks
    # it up). Uniqueness that matters is the partial constraint on (user, plan_handle) below.
    reference = models.CharField(max_length=150, db_index=True)
    customer_id = models.BigIntegerField(null=True, blank=True)
    subscription_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    status = models.CharField(
        max_length=20, choices=ClaimStatus.choices, default=ClaimStatus.SENDING
    )
    # The raw Maxio subscription state (e.g. "active", "canceled"), stored verbatim for display
    # and reconciliation. Never folded into ``status``.
    maxio_state = models.CharField(max_length=40, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "maxio_subscriptions"
        verbose_name = "Maxio subscription"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "plan_handle"],
                condition=~models.Q(status=ClaimStatus.FAILED),
                name="uniq_live_subscription_claim_per_plan",
            )
        ]

    def __str__(self):
        return f"{self.reference} -> maxio#{self.subscription_id} ({self.status})"
