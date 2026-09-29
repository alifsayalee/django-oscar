"""
Local records for Maxio-billed subscriptions.

Maxio is the billing system of record; these rows link Oscar's own user model to
Maxio's customer and subscription ids, and double as the duplicate-request
claims: each row is inserted and committed *before* the corresponding Maxio
write, so a database uniqueness constraint refuses a second concurrent request.
"""

import uuid

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from oscar.core.compat import AUTH_USER_MODEL


def new_customer_reference() -> str:
    return f"oscar-cus-{uuid.uuid4().hex}"


def new_subscription_reference() -> str:
    return f"oscar-sub-{uuid.uuid4().hex}"


class BillingCustomer(models.Model):
    """The Maxio customer that bills an Oscar user (one per user)."""

    PENDING, READY = "pending", "ready"
    STATUS_CHOICES = ((PENDING, _("Pending")), (READY, _("Ready")))

    user = models.OneToOneField(
        AUTH_USER_MODEL, related_name="billing_customer", on_delete=models.CASCADE
    )
    # The reference Maxio stores on the customer. Generated when the claim is taken
    # and committed before the create, so a lost response is always recoverable
    # with a lookup by reference; random so installs sharing a Maxio site never collide.
    reference = models.CharField(max_length=64, unique=True, default=new_customer_reference, editable=False)
    maxio_customer_id = models.BigIntegerField(null=True, blank=True, unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    outcome_unknown = models.BooleanField(default=False)
    claimed_at = models.DateTimeField()
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Billing customer")
        verbose_name_plural = _("Billing customers")

    def __str__(self) -> str:
        return f"{self.reference} -> {self.maxio_customer_id or 'pending'}"


class Subscription(models.Model):
    """A user's subscription to a Maxio plan, with a snapshot of its last known state."""

    PENDING, CONFIRMED, RELEASED = "pending", "confirmed", "released"
    CLAIM_CHOICES = (
        (PENDING, _("Pending")),
        (CONFIRMED, _("Confirmed")),
        (RELEASED, _("Released")),
    )

    user = models.ForeignKey(
        AUTH_USER_MODEL, related_name="maxio_subscriptions", on_delete=models.CASCADE
    )
    product_family = models.CharField(max_length=255)
    plan_handle = models.CharField(max_length=255)
    claim_state = models.CharField(max_length=16, choices=CLAIM_CHOICES, default=PENDING)
    # A live row blocks a second subscription for the same user and product family.
    is_live = models.BooleanField(default=True)
    outcome_unknown = models.BooleanField(default=False)
    claimed_at = models.DateTimeField()
    # Sent to Maxio with the create; finds the subscription again if the reply is lost.
    reference = models.CharField(max_length=64, unique=True, default=new_subscription_reference, editable=False)

    maxio_subscription_id = models.BigIntegerField(null=True, blank=True, unique=True)
    state = models.CharField(max_length=32, blank=True)
    plan_name = models.CharField(max_length=255, blank=True)
    price_in_cents = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    interval = models.PositiveIntegerField(null=True, blank=True)
    interval_unit = models.CharField(max_length=16, blank=True)
    next_billing_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_created"]
        verbose_name = _("Subscription")
        verbose_name_plural = _("Subscriptions")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "product_family"],
                condition=Q(is_live=True),
                name="subscriptions_one_live_per_family",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.plan_handle} ({self.state or self.claim_state})"
