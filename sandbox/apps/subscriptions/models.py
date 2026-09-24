import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from oscar.core.compat import AUTH_USER_MODEL


def new_reference():
    return 'oscar-sub-' + uuid.uuid4().hex


def new_customer_reference():
    return settings.MAXIO_CUSTOMER_REFERENCE_PREFIX + uuid.uuid4().hex


class BillingAccount(models.Model):
    """
    Links an Oscar user to their customer record at Maxio.

    The reference is random and fixed at creation rather than derived from the
    user id, so two installations billing through the same Maxio site can never
    resolve to each other's customers. Maxio allows one customer per reference,
    which makes creating the customer idempotent.
    """
    user = models.OneToOneField(AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='billing_account',
                                verbose_name=_('User'))
    reference = models.CharField(_('Maxio customer reference'), max_length=128, unique=True,
                                 default=new_customer_reference, editable=False)
    maxio_customer_id = models.BigIntegerField(_('Maxio customer id'), null=True, blank=True)
    date_created = models.DateTimeField(_('Date created'), auto_now_add=True)

    class Meta:
        verbose_name = _('Billing account')
        verbose_name_plural = _('Billing accounts')

    def __str__(self):
        return '%s → %s' % (self.user_id, self.reference)


class SubscriptionEnrollment(models.Model):
    """
    One attempt by a user to subscribe to a Maxio plan.

    Maxio is the system of record for the subscription itself. This row exists so
    that a repeated subscribe request (a double-click, a client retry) resolves
    to the subscription the first request created instead of a second one: at
    most one *open* enrollment per user and plan is allowed, and its reference is
    sent to Maxio so that an attempt with an unknown outcome can be looked up.
    """
    PENDING, ACTIVE, UNKNOWN, FAILED, ENDED = 'pending', 'active', 'unknown', 'failed', 'ended'
    STATUS_CHOICES = (
        (PENDING, _('Pending')),
        (ACTIVE, _('Active')),
        (UNKNOWN, _('Outcome unknown')),
        (FAILED, _('Failed')),
        (ENDED, _('Ended')),
    )
    OPEN_STATUSES = (PENDING, ACTIVE, UNKNOWN)

    user = models.ForeignKey(AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='subscription_enrollments',
                             verbose_name=_('User'))
    plan_handle = models.CharField(_('Plan handle'), max_length=255)
    reference = models.CharField(_('Maxio subscription reference'), max_length=64, unique=True,
                                 default=new_reference, editable=False)
    status = models.CharField(_('Status'), max_length=16, choices=STATUS_CHOICES, default=PENDING)
    maxio_customer_id = models.BigIntegerField(_('Maxio customer id'), null=True, blank=True)
    maxio_subscription_id = models.BigIntegerField(_('Maxio subscription id'), null=True, blank=True)
    date_created = models.DateTimeField(_('Date created'), auto_now_add=True)
    date_updated = models.DateTimeField(_('Date updated'), auto_now=True)

    class Meta:
        ordering = ['-date_created']
        verbose_name = _('Subscription enrollment')
        verbose_name_plural = _('Subscription enrollments')
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'plan_handle'],
                condition=models.Q(status__in=('pending', 'active', 'unknown')),
                name='subscriptions_one_open_enrollment_per_plan',
            ),
        ]

    def __str__(self):
        return '%s → %s (%s)' % (self.user_id, self.plan_handle, self.status)

    def mark(self, status, **fields):
        self.status = status
        for name, value in fields.items():
            setattr(self, name, value)
        self.save(update_fields=['status', 'date_updated', *fields])
