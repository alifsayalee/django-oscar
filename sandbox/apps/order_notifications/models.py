from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumber(models.Model):
    """
    A mobile number a shopper registered so the shop can text them.

    ``phone_number`` holds Twilio Lookup's canonical E.164 form, never the raw
    input. It is personal data: never log it.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='contact_numbers')
    phone_number = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'phone_number'],
                name='order_notifications_unique_number_per_user'),
        ]

    def __str__(self):
        return 'Contact number #%s' % self.pk

    @property
    def masked(self):
        return mask_number(self.phone_number)


def mask_number(number):
    if not number:
        return None
    return '*' * max(len(number) - 4, 0) + number[-4:]


class Notification(models.Model):
    """
    One SMS this application sent (or tried to send) about an order, with the
    provider's identifier and last known delivery outcome.
    """
    KIND_PLACED = 'order_placed'
    KIND_DISPATCHED = 'order_dispatched'
    KIND_FOLLOW_UP = 'delivery_follow_up'
    KIND_CANCELLED = 'order_cancelled'
    KIND_CHOICES = [
        (KIND_PLACED, 'Order placed'),
        (KIND_DISPATCHED, 'Order dispatched'),
        (KIND_FOLLOW_UP, 'Delivery follow-up'),
        (KIND_CANCELLED, 'Order cancelled'),
    ]

    # Where the send attempt got to on this application's side.
    STATE_PENDING = 'pending'        # claimed, not yet handed to Twilio
    STATE_SENDING = 'sending'        # being handed to Twilio right now
    STATE_SENT = 'sent'              # Twilio accepted it (see provider_status)
    STATE_REJECTED = 'rejected'      # Twilio refused it / never reached Twilio: nothing was sent
    STATE_UNKNOWN = 'unknown'        # may or may not have reached Twilio
    STATE_SUPPRESSED = 'suppressed'  # deliberately never sent (order cancelled, number removed)
    STATE_CHOICES = [(s, s) for s in (
        STATE_PENDING, STATE_SENDING, STATE_SENT, STATE_REJECTED, STATE_UNKNOWN, STATE_SUPPRESSED)]

    order = models.ForeignKey(
        'order.Order', on_delete=models.CASCADE, related_name='sms_notifications')
    contact_number = models.ForeignKey(
        ContactNumber, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='notifications')
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    body = models.TextField(blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    send_state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_PENDING)
    send_at = models.DateTimeField(
        null=True, blank=True, help_text='When Twilio is scheduled to send it (scheduled messages only).')

    # Provider-owned state, refreshed by asking Twilio.
    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provider_status = models.CharField(max_length=32, blank=True)
    from_number = models.CharField(max_length=32, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    cancel_failed = models.BooleanField(
        default=False, help_text='A scheduled message could not be confirmed as called off.')

    # Operator re-sends.
    resend_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT, related_name='resends')
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at', 'pk']
        constraints = [
            # One lifecycle message of each kind per order: the claim that stops
            # a repeated dispatch/cancel from texting the shopper twice.
            models.UniqueConstraint(
                fields=['order', 'kind'], condition=Q(resend_of__isnull=True),
                name='order_notifications_one_lifecycle_message_per_kind'),
            # One re-send per (original, idempotency key).
            models.UniqueConstraint(
                fields=['resend_of', 'idempotency_key'],
                name='order_notifications_unique_resend_key'),
        ]

    def __str__(self):
        return 'Notification #%s (%s)' % (self.pk, self.kind)
