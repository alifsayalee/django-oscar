from django.conf import settings
from django.db import models


class MaxioWrite(models.Model):
    """
    A claim on one provider write (create customer / create subscription).

    This is not a copy of Maxio's data - Maxio stays the record of what
    exists. A row records that *we asked*, under which reference, and what we
    know about the outcome. The UNIQUE ``reference`` column is what stops the
    same write being requested twice: whichever request inserts the row first
    owns the write; every other request answers from the row.
    """

    KIND_CUSTOMER = "customer"
    KIND_SUBSCRIPTION = "subscription"
    KIND_CHOICES = (
        (KIND_CUSTOMER, "Customer"),
        (KIND_SUBSCRIPTION, "Subscription"),
    )

    # Claimed, no answer yet - nobody else calls the provider for it.
    SENDING = "sending"
    # The provider says what was asked for is in effect.
    DONE = "done"
    # The provider accepted it and has not finished (or it needs attention).
    PENDING = "pending"
    # Never sent, refused, or reported failed/undone by the provider.
    FAILED = "failed"
    # Happened, but not as asked.
    NEEDS_REVIEW = "needs_review"
    # May have happened; only a lookup by reference can settle it.
    UNKNOWN = "unknown"
    OUTCOME_CHOICES = (
        (SENDING, "Sending"),
        (DONE, "Done"),
        (PENDING, "Pending"),
        (FAILED, "Failed"),
        (NEEDS_REVIEW, "Needs review"),
        (UNKNOWN, "Unknown"),
    )

    reference = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="maxio_writes",
    )
    plan_handle = models.CharField(max_length=255, blank=True, default="")
    outcome = models.CharField(max_length=32, choices=OUTCOME_CHOICES, default=SENDING)
    provider_id = models.BigIntegerField(null=True, blank=True)
    provider_state = models.CharField(max_length=64, blank=True, default="")
    # The provider's own clock (e.g. the subscription's created_at), for
    # reconciliation - distinct from when we wrote the row.
    provider_time = models.DateTimeField(null=True, blank=True)
    detail = models.TextField(blank=True, default="")
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-claimed_at",)
        indexes = [
            models.Index(fields=("user", "kind", "plan_handle")),
            models.Index(fields=("outcome",)),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.reference} [{self.outcome}]"
