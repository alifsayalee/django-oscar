"""
Durable claim rows for the two writes this app makes to Maxio.

Maxio is the system of record for customers and subscriptions; these rows only
record that *we asked*, under which reference, and what came of it. Each row is
written before the provider call so that a double submit, a crashed worker or a
timed-out request can always be matched back to what Maxio holds.
"""
import uuid

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from oscar.core.compat import AUTH_USER_MODEL


def new_customer_reference():
    return f'oscar-{uuid.uuid4().hex}'


def new_subscription_reference():
    return f'oscar-sub-{uuid.uuid4().hex}'


class BillingCustomer(models.Model):
    """Links an Oscar user to exactly one Maxio customer."""

    SENDING = 'sending'     # claimed; a create call may be in flight
    LINKED = 'linked'       # Maxio confirmed the customer
    UNKNOWN = 'unknown'     # may exist in Maxio; reconciled by reference
    FAILED = 'failed'       # definitely not created
    STATUS_CHOICES = [
        (SENDING, _('Sending')),
        (LINKED, _('Linked')),
        (UNKNOWN, _('Unknown')),
        (FAILED, _('Failed')),
    ]

    user = models.OneToOneField(
        AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='maxio_customer')
    # Sent to Maxio as the customer reference; Maxio enforces its uniqueness.
    reference = models.CharField(max_length=64, unique=True, default=new_customer_reference)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    maxio_customer_id = models.BigIntegerField(null=True, blank=True, unique=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Maxio customer')

    def __str__(self):
        return f'{self.user} -> {self.maxio_customer_id or self.reference} ({self.status})'


class SubscriptionEnrollment(models.Model):
    """One request to subscribe a user to a plan, and its outcome."""

    SENDING = 'sending'             # claimed; a create call may be in flight
    ACTIVE = 'active'               # Maxio says it is live and in good standing
    PENDING = 'pending'             # accepted by Maxio, not active yet
    ATTENTION = 'attention'         # exists, but not in good standing (past due, on hold, ...)
    NEEDS_REVIEW = 'needs_review'   # created, but not as requested (plan/price/customer mismatch)
    UNKNOWN = 'unknown'             # may exist in Maxio; reconciled by reference
    FAILED = 'failed'               # definitely not created
    ENDED = 'ended'                 # canceled or expired in Maxio
    STATUS_CHOICES = [
        (SENDING, _('Sending')),
        (ACTIVE, _('Active')),
        (PENDING, _('Pending')),
        (ATTENTION, _('Needs attention')),
        (NEEDS_REVIEW, _('Needs review')),
        (UNKNOWN, _('Unknown')),
        (FAILED, _('Failed')),
        (ENDED, _('Ended')),
    ]
    # Statuses that no longer hold the (user, plan) claim
    RELEASED = (FAILED, ENDED)

    user = models.ForeignKey(
        AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='maxio_subscriptions')
    customer = models.ForeignKey(
        BillingCustomer, on_delete=models.PROTECT, related_name='subscriptions')
    plan_handle = models.CharField(max_length=255)
    # Sent to Maxio as the subscription reference; Maxio enforces its uniqueness.
    reference = models.CharField(max_length=64, unique=True, default=new_subscription_reference)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    failure_reason = models.TextField(blank=True)

    # Snapshot of what Maxio last told us
    maxio_subscription_id = models.BigIntegerField(null=True, blank=True, unique=True)
    state = models.CharField(max_length=32, blank=True)
    plan_name = models.CharField(max_length=255, blank=True)
    price_in_cents = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    next_billing_at = models.DateTimeField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Maxio subscription enrollment')
        ordering = ['-created']
        constraints = [
            # One live request per user and plan: this constraint, not a prior
            # read, decides which of two concurrent submits calls Maxio.
            models.UniqueConstraint(
                fields=['user', 'plan_handle'],
                condition=~Q(status__in=['failed', 'ended']),
                name='maxio_one_live_enrollment_per_plan',
            ),
        ]

    def __str__(self):
        return f'{self.user} / {self.plan_handle} ({self.status})'
