"""
Claim rows for the writes this app makes to Maxio.

Maxio stays the record of *what exists*; these rows record *that we asked*.
Each one is written before the provider call, keyed by a reference we generate
and send, so a double-click, a retry or a crash mid-call can always be resolved
by looking the reference up instead of creating a second customer or
subscription.
"""

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from oscar.core.compat import AUTH_USER_MODEL


class BillingCustomer(models.Model):
    """The Maxio customer that bills one Oscar user."""

    SENDING, DONE, FAILED, UNKNOWN = "sending", "done", "failed", "unknown"
    STATUS_CHOICES = (
        (SENDING, _("Sending")),
        (DONE, _("Done")),
        (FAILED, _("Failed")),
        (UNKNOWN, _("Unknown")),
    )

    user = models.OneToOneField(
        AUTH_USER_MODEL,
        related_name="billing_customer",
        on_delete=models.CASCADE,
        verbose_name=_("User"),
    )
    reference = models.CharField(_("Maxio reference"), max_length=64, unique=True)
    maxio_customer_id = models.BigIntegerField(
        _("Maxio customer ID"), null=True, blank=True
    )
    status = models.CharField(
        _("Status"), max_length=16, choices=STATUS_CHOICES, default=SENDING
    )
    date_created = models.DateTimeField(_("Date created"), auto_now_add=True)
    date_updated = models.DateTimeField(_("Date updated"), auto_now=True, db_index=True)

    class Meta:
        verbose_name = _("Billing customer")
        verbose_name_plural = _("Billing customers")

    def __str__(self):
        return f"{self.user} -> {self.maxio_customer_id or self.reference} ({self.status})"


class SubscriptionEnrollment(models.Model):
    """One request to subscribe a user to a plan, and what Maxio made of it."""

    SENDING = "sending"
    DONE = "done"
    PENDING = "pending"
    NEEDS_ATTENTION = "needs_attention"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    FAILED = "failed"
    ENDED = "ended"
    STATUS_CHOICES = (
        (SENDING, _("Sending")),
        (DONE, _("Active")),
        (PENDING, _("Pending")),
        (NEEDS_ATTENTION, _("Needs attention")),
        (NEEDS_REVIEW, _("Needs review")),
        (UNKNOWN, _("Unknown")),
        (FAILED, _("Failed")),
        (ENDED, _("Ended")),
    )
    # Statuses that release the claim, so the user may subscribe to the plan again.
    RELEASED_STATUSES = (FAILED, ENDED)

    user = models.ForeignKey(
        AUTH_USER_MODEL,
        related_name="subscription_enrollments",
        on_delete=models.CASCADE,
        verbose_name=_("User"),
    )
    billing_customer = models.ForeignKey(
        BillingCustomer,
        related_name="enrollments",
        on_delete=models.PROTECT,
        verbose_name=_("Billing customer"),
    )
    plan_handle = models.CharField(_("Plan handle"), max_length=255)
    expected_price_in_cents = models.BigIntegerField(_("Expected price (cents)"))
    reference = models.CharField(_("Maxio reference"), max_length=64, unique=True)
    status = models.CharField(
        _("Status"), max_length=16, choices=STATUS_CHOICES, default=SENDING
    )
    maxio_subscription_id = models.BigIntegerField(
        _("Maxio subscription ID"), null=True, blank=True, db_index=True
    )
    maxio_state = models.CharField(_("Maxio state"), max_length=32, blank=True)
    # The provider's clock, for reconciliation (not our insert time).
    maxio_created_at = models.DateTimeField(_("Created in Maxio"), null=True, blank=True)
    status_detail = models.TextField(_("Status detail"), blank=True)
    date_created = models.DateTimeField(_("Date created"), auto_now_add=True)
    date_updated = models.DateTimeField(_("Date updated"), auto_now=True, db_index=True)

    class Meta:
        verbose_name = _("Subscription enrollment")
        verbose_name_plural = _("Subscription enrollments")
        ordering = ["-date_created"]
        constraints = [
            # One live claim per (user, plan) until it definitely failed or ended.
            models.UniqueConstraint(
                fields=["user", "plan_handle"],
                condition=~Q(status__in=["failed", "ended"]),
                name="subscriptions_one_live_enrollment_per_plan",
            ),
        ]

    def __str__(self):
        return f"{self.user} -> {self.plan_handle} ({self.status})"
