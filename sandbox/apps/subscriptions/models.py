from django.conf import settings
from django.db import models


class MaxioInstall(models.Model):
    """
    A single row identifying this installation. Its id prefixes every
    reference we send to Maxio, so two installs sharing one Maxio site never
    claim each other's customers or subscriptions.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    install_id = models.CharField(max_length=32, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.install_id


class MaxioWrite(models.Model):
    """
    The claim for one write to Maxio, keyed by the reference sent with it.

    The row is inserted *before* the provider call; the unique constraint on
    ``reference`` is what rejects a second request for the same write, in
    any process. It then records what Maxio said happened.
    """

    CUSTOMER, SUBSCRIPTION = 'customer', 'subscription'
    KIND_CHOICES = [(CUSTOMER, 'Customer'), (SUBSCRIPTION, 'Subscription')]

    SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN = (
        'sending', 'done', 'pending', 'failed', 'needs_review', 'unknown')
    OUTCOME_CHOICES = [
        (SENDING, 'Sending'),
        (DONE, 'Done'),
        (PENDING, 'Pending'),
        (FAILED, 'Failed'),
        (NEEDS_REVIEW, 'Needs review'),
        (UNKNOWN, 'Unknown'),
    ]

    reference = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name='maxio_writes')
    plan_handle = models.CharField(max_length=255, blank=True)
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES)
    provider_id = models.CharField(max_length=64, null=True, blank=True)
    provider_state = models.CharField(max_length=64, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['user', 'kind', 'outcome'])]

    def __str__(self) -> str:
        return f'{self.kind} {self.reference} ({self.outcome})'
