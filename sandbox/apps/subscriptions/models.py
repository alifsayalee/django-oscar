"""
Local records of what this site asked Maxio to do.

Maxio is the system of record for customers and subscriptions; these rows record *that we asked*,
under the deterministic reference we sent, so a repeated request (a double-click, a retry after a
timeout) checks the earlier attempt instead of creating a second customer or subscription.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class WriteStatus(models.TextChoices):
    SENDING = 'sending', _('Sending')      # saved before the call; no answer yet
    DONE = 'done', _('Done')               # Maxio holds the record
    FAILED = 'failed', _('Failed')         # never sent, or rejected: nothing exists at Maxio
    UNKNOWN = 'unknown', _('Unknown')      # may exist at Maxio; only a lookup settles it


class BillingCustomer(models.Model):
    """The Maxio customer for one of Oscar's users."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='billing_customer')
    reference = models.CharField(_('Maxio reference'), max_length=255, unique=True)
    maxio_customer_id = models.BigIntegerField(_('Maxio customer ID'), null=True, blank=True, unique=True)
    status = models.CharField(max_length=16, choices=WriteStatus.choices, default=WriteStatus.SENDING)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Billing customer')
        verbose_name_plural = _('Billing customers')

    def __str__(self):
        return '%s (%s)' % (self.reference, self.status)


class SubscriptionAttempt(models.Model):
    """One request to subscribe a user to a plan, keyed by the reference sent to Maxio."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='subscription_attempts')
    plan_handle = models.CharField(_('Plan handle'), max_length=255)
    # Increments only when the previous attempt for this plan failed or its subscription ended.
    sequence = models.PositiveIntegerField()
    reference = models.CharField(_('Maxio reference'), max_length=255, unique=True)
    status = models.CharField(max_length=16, choices=WriteStatus.choices, default=WriteStatus.SENDING)
    maxio_subscription_id = models.BigIntegerField(
        _('Maxio subscription ID'), null=True, blank=True, unique=True)
    # Last Maxio state seen for the subscription (e.g. "active"), for reference only.
    provider_state = models.CharField(max_length=64, blank=True)
    last_error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Subscription attempt')
        verbose_name_plural = _('Subscription attempts')
        ordering = ['-date_created']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'plan_handle', 'sequence'], name='unique_subscription_attempt_sequence'),
        ]

    def __str__(self):
        return '%s (%s)' % (self.reference, self.status)
