from django.conf import settings
from django.db import models


class MaxioSubscription(models.Model):
    """Durable idempotency ledger for a subscribe operation.

    Maxio Advanced Billing is the system of record for *what exists*; this row
    records *that we asked*. One row per (user, plan) keyed by a deterministic
    ``reference`` with a unique constraint, so a double-click or a retry from a
    second worker can never create two subscriptions -- the constraint decides
    the single winner that calls Maxio, and everyone else reads the outcome
    back off the row (python-configuration-resilience: one operation, one
    durable row).
    """

    STATUS_SENDING = 'sending'
    STATUS_DONE = 'done'
    STATUS_PENDING = 'pending'
    STATUS_FAILED = 'failed'
    STATUS_NEEDS_REVIEW = 'needs_review'
    STATUS_UNKNOWN = 'unknown'
    STATUS_CHOICES = [
        (STATUS_SENDING, 'Sending'),
        (STATUS_DONE, 'Done'),
        (STATUS_PENDING, 'Pending'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_NEEDS_REVIEW, 'Needs review'),
        (STATUS_UNKNOWN, 'Unknown'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='maxio_subscriptions',
    )
    plan_handle = models.CharField(max_length=255)
    # The reference we generate and send to Maxio; also how we look a
    # may-have-landed write back up. Globally unique -> the cross-process claim.
    reference = models.CharField(max_length=255, unique=True)
    maxio_customer_id = models.PositiveBigIntegerField(null=True, blank=True)
    maxio_subscription_id = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_SENDING)
    # Last state string Maxio reported for the subscription (e.g. "active").
    state = models.CharField(max_length=50, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'subscriptions'
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.reference} ({self.status})'
