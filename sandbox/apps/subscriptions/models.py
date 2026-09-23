"""
Integration ledger for Maxio subscription billing.

Maxio is the system of record for customers, plans and subscriptions; these rows only record that the
sandbox *asked* Maxio for something, under which reference, and what came of it. They make a
double-click (or a retry racing the original request) resolve to one Maxio customer and one
subscription, and they keep every write findable when its outcome could not be read.
"""
from django.db import models
from django.db.models import Q

from oscar.core import compat


class BillingCustomer(models.Model):
    """Links an Oscar user to their Maxio customer. The row is claimed before Maxio is called."""

    user = models.OneToOneField(compat.AUTH_USER_MODEL, related_name='billing_customer',
                                on_delete=models.CASCADE)
    # Sent to Maxio as the customer's reference; the key used to find the customer again.
    reference = models.CharField(max_length=64, unique=True)
    maxio_customer_id = models.BigIntegerField(null=True, blank=True, unique=True)
    # Set by the one request allowed to call create_customer; stale claims may be taken over.
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return '%s -> %s' % (self.reference, self.maxio_customer_id or 'unlinked')


class SubscriptionRequest(models.Model):
    """One request to subscribe a user to a plan, claimed before Maxio is called."""

    SENDING = 'sending'            # claimed; the create call is (or was) in flight
    PENDING = 'pending'            # Maxio accepted it and has not finished creating it
    ACTIVE = 'active'              # Maxio says it is active / trialing
    ATTENTION = 'attention'        # exists at Maxio but is not in good standing (past due, on hold, ...)
    FAILED = 'failed'              # definitely not created (rejected, never sent, failed_to_create)
    ENDED = 'ended'                # created, later canceled / expired
    NEEDS_REVIEW = 'needs_review'  # created, but not as asked (plan or price differ)
    UNKNOWN = 'unknown'            # may or may not exist at Maxio; reconciled by reference
    STATUS_CHOICES = [(s, s) for s in (SENDING, PENDING, ACTIVE, ATTENTION, FAILED, ENDED, NEEDS_REVIEW,
                                       UNKNOWN)]
    # Statuses that hold the (user, plan) claim: only FAILED and ENDED release it.
    HOLDING = (SENDING, PENDING, ACTIVE, ATTENTION, NEEDS_REVIEW, UNKNOWN)

    user = models.ForeignKey(compat.AUTH_USER_MODEL, related_name='subscription_requests',
                             on_delete=models.CASCADE)
    plan_handle = models.CharField(max_length=255)
    # Sent to Maxio as the subscription's reference; find_subscription looks it up by this value.
    reference = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    maxio_subscription_id = models.BigIntegerField(null=True, blank=True)
    maxio_state = models.CharField(max_length=32, blank=True)
    # The price we expected (from the plan list) — checked against what Maxio echoes back.
    expected_price_in_cents = models.BigIntegerField(null=True, blank=True)
    # Set just before the create call: a stale claim without it provably never reached Maxio.
    sent_at = models.DateTimeField(null=True, blank=True)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'plan_handle'],
                condition=Q(status__in=['sending', 'pending', 'active', 'attention', 'needs_review', 'unknown']),
                name='subscriptions_one_live_request_per_user_plan',
            ),
        ]

    def __str__(self) -> str:
        return '%s %s (%s)' % (self.user_id, self.plan_handle, self.status)
