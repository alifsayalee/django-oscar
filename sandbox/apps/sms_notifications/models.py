from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumber(models.Model):
    """
    A shopper's mobile number, stored in the provider's canonical (E.164) form.

    Removal is a soft delete so the history of what was sent survives; a
    removed number is never listed and never messaged again.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='sms_contact_numbers')
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'phone_number'],
                condition=Q(deleted_at__isnull=True),
                name='sms_unique_active_number_per_user'),
        ]

    def __str__(self):
        # Never render the number itself: model reprs end up in logs.
        return 'ContactNumber #%s' % self.pk


class ProviderWrite(models.Model):
    """
    The claim ledger: one row per provider write step (a send, a cancel, a
    redaction), keyed by a reference derived from the operation.

    The unique ``reference`` column is what rejects a second claim, across
    processes. It records that *we asked*; the provider stays the record of
    what exists.
    """
    SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN = (
        'sending', 'done', 'pending', 'failed', 'needs_review', 'unknown')
    OUTCOMES = [(o, o) for o in (SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN)]

    SEND, CANCEL, REDACT = 'send', 'cancel', 'redact'
    OPERATIONS = [(o, o) for o in (SEND, CANCEL, REDACT)]

    reference = models.CharField(max_length=255, unique=True)
    operation = models.CharField(max_length=16, choices=OPERATIONS)
    outcome = models.CharField(max_length=16, choices=OUTCOMES, default=SENDING)
    provider_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    error_code = models.IntegerField(null=True, blank=True)
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return '%s %s (%s)' % (self.operation, self.reference, self.outcome)


class Notification(models.Model):
    """
    One text message about an order, and where it got to.

    Delivery state lives on ``send_write`` (the provider's message SID, its
    status and the provider's clock), so any later request can act on it.
    """
    PLACED, DISPATCHED, FOLLOW_UP, CANCELLED = (
        'order_placed', 'order_dispatched', 'delivery_follow_up', 'order_cancelled')
    KINDS = [(k, k) for k in (PLACED, DISPATCHED, FOLLOW_UP, CANCELLED)]

    order = models.ForeignKey(
        'order.Order', on_delete=models.CASCADE, related_name='sms_notifications')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='sms_notifications')
    contact_number = models.ForeignKey(
        ContactNumber, null=True, on_delete=models.SET_NULL,
        related_name='notifications')
    kind = models.CharField(max_length=32, choices=KINDS)
    body = models.TextField(blank=True)
    ref_token = models.CharField(max_length=16)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    resend_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='resends')
    send_write = models.OneToOneField(
        ProviderWrite, on_delete=models.PROTECT, related_name='notification')
    cancel_write = models.OneToOneField(
        ProviderWrite, null=True, blank=True, on_delete=models.PROTECT,
        related_name='cancelled_notification')
    redact_write = models.OneToOneField(
        ProviderWrite, null=True, blank=True, on_delete=models.PROTECT,
        related_name='redacted_notification')
    content_redacted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return 'Notification #%s (%s)' % (self.pk, self.kind)
