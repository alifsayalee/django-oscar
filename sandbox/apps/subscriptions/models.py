"""
Local claim rows for the Maxio integration.

Maxio is the system of record for what exists (customers, subscriptions,
plans). These rows record *that we asked*: each one is written before the
provider call it guards, keyed by a reference we generate and send, so a
double-click, an overlapping retry or a second worker can never create a second
customer or subscription, and a write whose outcome is unknown can always be
found and reconciled.
"""

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from oscar.core.compat import AUTH_USER_MODEL


class ClaimStatus(models.TextChoices):
    # Claimed; the provider has not answered yet. Nobody else calls it.
    SENDING = "sending", _("Sending")
    # The provider's word: accepted, not finished yet.
    PENDING = "pending", _("Pending")
    # The provider's word: done / live and in good standing.
    ACTIVE = "active", _("Active")
    # Exists at the provider, but not in good standing (past due, on hold...).
    PROBLEM = "problem", _("Problem")
    # Existed and has ended (cancelled, expired). Releases the claim.
    ENDED = "ended", _("Ended")
    # Definitely did not happen (never sent, or rejected). Releases the claim.
    FAILED = "failed", _("Failed")
    # It happened, but not as asked (echoed plan/price/customer differ).
    NEEDS_REVIEW = "needs_review", _("Needs review")
    # May have happened; reconcile by reference before doing anything else.
    UNKNOWN = "unknown", _("Unknown")


# Statuses that no longer hold a claim: a new request may start over.
RELEASED_STATUSES = (ClaimStatus.FAILED, ClaimStatus.ENDED)


class BillingCustomer(models.Model):
    """
    Links an Oscar user to their Maxio customer. The one-to-one on ``user`` is
    the claim: exactly one request per user ever calls Maxio's create.
    """

    user = models.OneToOneField(
        AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="billing_customer",
        verbose_name=_("User"),
    )
    reference = models.CharField(_("Maxio reference"), max_length=255, unique=True)
    maxio_customer_id = models.BigIntegerField(
        _("Maxio customer ID"), null=True, blank=True, unique=True
    )
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=ClaimStatus.choices,
        default=ClaimStatus.SENDING,
        db_index=True,
    )
    last_error = models.TextField(_("Last error"), blank=True)
    date_created = models.DateTimeField(_("Date created"), auto_now_add=True)
    date_updated = models.DateTimeField(_("Date updated"), auto_now=True)

    class Meta:
        app_label = "subscriptions"
        verbose_name = _("Billing customer")
        verbose_name_plural = _("Billing customers")

    def __str__(self) -> str:
        return f"{self.reference} ({self.status})"


class SubscriptionEnrollment(models.Model):
    """
    One request to enroll a user on a plan. The partial unique constraint means
    a user holds at most one live enrollment per plan: a second concurrent
    subscribe loses the insert and answers from the winner's row instead of
    calling Maxio.
    """

    user = models.ForeignKey(
        AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="subscription_enrollments",
        verbose_name=_("User"),
    )
    billing_customer = models.ForeignKey(
        BillingCustomer,
        on_delete=models.PROTECT,
        related_name="enrollments",
        verbose_name=_("Billing customer"),
    )
    plan_handle = models.CharField(_("Plan handle"), max_length=255)
    reference = models.CharField(_("Maxio reference"), max_length=255, unique=True)
    maxio_subscription_id = models.BigIntegerField(
        _("Maxio subscription ID"), null=True, blank=True, unique=True
    )
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=ClaimStatus.choices,
        default=ClaimStatus.SENDING,
        db_index=True,
    )
    # What we asked for, so the provider's echo can be verified against it.
    expected_price_in_cents = models.BigIntegerField(_("Expected price (cents)"))
    # Last state Maxio reported, verbatim, and when Maxio said it happened.
    maxio_state = models.CharField(_("Maxio state"), max_length=50, blank=True)
    maxio_created_at = models.DateTimeField(_("Maxio created at"), null=True, blank=True)
    last_error = models.TextField(_("Last error"), blank=True)
    date_created = models.DateTimeField(_("Date created"), auto_now_add=True)
    date_updated = models.DateTimeField(_("Date updated"), auto_now=True)

    class Meta:
        app_label = "subscriptions"
        ordering = ["-date_created"]
        verbose_name = _("Subscription enrollment")
        verbose_name_plural = _("Subscription enrollments")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "plan_handle"],
                condition=~Q(status__in=RELEASED_STATUSES),
                name="subscriptions_one_live_enrollment_per_plan",
            )
        ]

    def __str__(self) -> str:
        return f"{self.reference} {self.plan_handle} ({self.status})"
