"""
Records of the SMS messages this shop sends about orders.

Each row that asks Twilio to do something (send a message, call a scheduled
one off, redact one) is written *before* the provider call and is keyed by a
unique ``reference``. That unique constraint is the claim that stops the same
write from reaching the provider twice, whichever process the second request
lands on.
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Outcome(models.TextChoices):
    # Claimed locally, no answer from the provider yet.
    SENDING = 'sending', _('Sending')
    # The provider accepted it and has not finished.
    PENDING = 'pending', _('Pending')
    DONE = 'done', _('Done')
    FAILED = 'failed', _('Failed')
    # It happened, but not as asked.
    NEEDS_REVIEW = 'needs_review', _('Needs review')
    # It may have happened: only the provider's answer settles it.
    UNKNOWN = 'unknown', _('Unknown')


class ContactNumber(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='sms_contact_numbers')
    # The provider's canonical E.164 form, never what the caller typed.
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'phone_number'],
                name='order_notifications_unique_user_number'),
        ]

    def __str__(self):
        # Deliberately no digits: this string ends up in logs and the admin.
        return 'ContactNumber #%s' % self.pk


class Notification(models.Model):
    PLACED, DISPATCHED, FOLLOW_UP, CANCELLED = (
        'placed', 'dispatched', 'follow_up', 'cancelled')
    KIND_CHOICES = [
        (PLACED, _('Order placed')),
        (DISPATCHED, _('Order dispatched')),
        (FOLLOW_UP, _('Delivery follow-up')),
        (CANCELLED, _('Order cancelled')),
    ]

    order = models.ForeignKey(
        'order.Order', on_delete=models.CASCADE,
        related_name='sms_notifications')
    contact_number = models.ForeignKey(
        ContactNumber, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='notifications')
    to_number = models.CharField(max_length=32)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    resend_of = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='resends')

    reference = models.CharField(max_length=200, unique=True)
    body = models.TextField(blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)

    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    provider_sid = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_error_code = models.IntegerField(null=True, blank=True)
    # When the provider says it sent the message (its clock, not ours).
    provider_time = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    claimed_at = models.DateTimeField()
    last_checked_at = models.DateTimeField(null=True, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return 'Notification #%s (%s)' % (self.pk, self.kind)


class ProviderAction(models.Model):
    """
    A write against a message that already exists at the provider: calling a
    scheduled message off, or redacting its text.
    """
    CANCEL, REDACT = 'cancel', 'redact'
    KIND_CHOICES = [
        (CANCEL, _('Call off a scheduled message')),
        (REDACT, _('Redact message content')),
    ]

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name='actions')
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    reference = models.CharField(max_length=200, unique=True)
    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    provider_status = models.CharField(max_length=32, blank=True)
    detail = models.CharField(max_length=255, blank=True)
    claimed_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return 'ProviderAction #%s (%s)' % (self.pk, self.kind)
