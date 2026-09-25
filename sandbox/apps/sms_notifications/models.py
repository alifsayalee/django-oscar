from django.conf import settings
from django.db import models


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical (E.164) form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sms_contact_numbers')
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date_created', '-id']
        constraints = [
            models.UniqueConstraint(fields=['user', 'phone_number'], name='sms_contact_unique_per_user'),
        ]

    def __str__(self) -> str:
        return 'Contact number #%s' % self.pk

    @property
    def masked(self) -> str:
        return mask_number(self.phone_number)


def mask_number(number: str) -> str:
    """Show only the last two digits, e.g. ``+1********88``."""
    if len(number) <= 4:
        return '*' * len(number)
    return number[:2] + '*' * (len(number) - 4) + number[-2:]


class SmsNotification(models.Model):
    """
    One message this application asked the provider to send.

    The row is created - and committed - *before* the provider call: it is the
    claim. ``reference`` is unique, so a second request for the same message
    cannot take it. The provider's identifier, status and clock are recorded
    when it answers.
    """

    KIND_ORDER_PLACED = 'order_placed'
    KIND_ORDER_DISPATCHED = 'order_dispatched'
    KIND_DELIVERY_FOLLOWUP = 'delivery_followup'
    KIND_ORDER_CANCELLED = 'order_cancelled'
    KIND_RESEND = 'resend'
    KIND_CHOICES = [
        (KIND_ORDER_PLACED, 'Order placed'),
        (KIND_ORDER_DISPATCHED, 'Order dispatched'),
        (KIND_DELIVERY_FOLLOWUP, 'Delivery follow-up'),
        (KIND_ORDER_CANCELLED, 'Order cancelled'),
        (KIND_RESEND, 'Operator resend'),
    ]

    reference = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    order = models.ForeignKey(
        'order.Order', on_delete=models.CASCADE, related_name='sms_notifications')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sms_notifications')
    contact_number = models.ForeignKey(
        ContactNumber, null=True, blank=True, on_delete=models.SET_NULL, related_name='notifications')
    to_number = models.CharField(max_length=32)
    body = models.TextField(blank=True)
    resend_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='resends')

    # Our view of the provider's state. ``outcome`` is one of outcomes.*.
    outcome = models.CharField(max_length=16, db_index=True)
    provider_sid = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_error_code = models.IntegerField(null=True, blank=True)
    provider_date_created = models.DateTimeField(null=True, blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)

    claimed_at = models.DateTimeField()
    date_updated = models.DateTimeField(auto_now=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    # Calling off a scheduled message.
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_outcome = models.CharField(max_length=16, blank=True)

    # Content disposal.
    redaction_requested_at = models.DateTimeField(null=True, blank=True)
    content_redacted_at = models.DateTimeField(null=True, blank=True)
    redaction_outcome = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ['claimed_at', 'id']

    def __str__(self) -> str:
        return 'SMS notification #%s (%s)' % (self.pk, self.kind)
