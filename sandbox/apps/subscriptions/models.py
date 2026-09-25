import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _

from oscar.core.compat import AUTH_USER_MODEL


def new_install_id() -> str:
    return uuid.uuid4().hex[:12]


class MaxioInstall(models.Model):
    """
    A single row holding a random id for this database.

    Used as the prefix of every reference sent to Maxio (unless MAXIO_REFERENCE_PREFIX is set), so two
    installs sharing one Maxio site never claim each other's customers or subscriptions.
    """

    install_id = models.CharField(max_length=32, unique=True, default=new_install_id)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Maxio install")


class MaxioClaim(models.Model):
    """
    One row per provider write this site asked Maxio to make (create customer, create subscription).

    The row is inserted and committed *before* the provider call: the unique ``reference`` is what
    stops a double submit from reaching Maxio twice, and it is the same reference Maxio stores on the
    record, so a write whose answer was lost can be looked up. Maxio remains the record of what exists;
    this records that we asked, and what we last learned about the outcome.
    """

    KIND_CUSTOMER = "customer"
    KIND_SUBSCRIPTION = "subscription"
    KIND_CHOICES = (
        (KIND_CUSTOMER, _("Customer")),
        (KIND_SUBSCRIPTION, _("Subscription")),
    )

    SENDING = "sending"
    DONE = "done"
    PENDING = "pending"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    OUTCOME_CHOICES = (
        (SENDING, _("Sending")),
        (DONE, _("Done")),
        (PENDING, _("Pending")),
        (FAILED, _("Failed")),
        (NEEDS_REVIEW, _("Needs review")),
        (UNKNOWN, _("Unknown")),
    )

    reference = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    user = models.ForeignKey(AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="maxio_claims")
    plan_handle = models.CharField(max_length=255, blank=True)
    outcome = models.CharField(max_length=32, choices=OUTCOME_CHOICES, default=SENDING)
    provider_id = models.BigIntegerField(null=True, blank=True)
    provider_state = models.CharField(max_length=64, blank=True)
    # The provider's own clock (Maxio's created_at), for reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-claimed_at"]
        indexes = [models.Index(fields=["user", "kind", "plan_handle"])]
        verbose_name = _("Maxio write claim")

    def __str__(self) -> str:
        return f"{self.kind} {self.reference} ({self.outcome})"
