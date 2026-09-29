from django.db import models
from django.utils.translation import gettext_lazy as _

from oscar.core.compat import AUTH_USER_MODEL


class MaxioSubscriptionEnrollment(models.Model):
    """
    Local ledger of a shopper's enrollment in a Maxio plan.

    Maxio Advanced Billing is the system of record for the subscription itself;
    this row exists so that a subscribe request can be claimed atomically (one
    in-flight or active enrollment per user and plan), which is what makes a
    double-submitted subscribe safe.
    """
    PENDING, ACTIVE, FAILED, ENDED = 'pending', 'active', 'failed', 'ended'
    STATUS_CHOICES = (
        (PENDING, _('Pending')),
        (ACTIVE, _('Active')),
        (FAILED, _('Failed')),
        (ENDED, _('Ended')),
    )
    #: Statuses that hold the (user, plan) claim.
    CLAIMING_STATUSES = (PENDING, ACTIVE)

    user = models.ForeignKey(
        AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='maxio_subscription_enrollments', verbose_name=_('User'))
    plan_handle = models.CharField(_('Plan handle'), max_length=255)
    status = models.CharField(_('Status'), max_length=16, choices=STATUS_CHOICES, default=PENDING)
    maxio_customer_id = models.BigIntegerField(_('Maxio customer ID'), null=True, blank=True)
    maxio_subscription_id = models.BigIntegerField(
        _('Maxio subscription ID'), null=True, blank=True, unique=True)
    last_error = models.TextField(_('Last error'), blank=True)
    date_created = models.DateTimeField(_('Date created'), auto_now_add=True)
    date_updated = models.DateTimeField(_('Date updated'), auto_now=True)

    class Meta:
        ordering = ['-date_created']
        verbose_name = _('Maxio subscription enrollment')
        verbose_name_plural = _('Maxio subscription enrollments')
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'plan_handle'],
                condition=models.Q(status__in=['pending', 'active']),
                name='maxio_one_open_enrollment_per_user_plan',
            ),
        ]

    def __str__(self):
        return '%s → %s (%s)' % (self.user_id, self.plan_handle, self.status)
