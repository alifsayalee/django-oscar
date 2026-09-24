from django.conf import settings
from django.db import models


class ContactNumber(models.Model):
    """
    A mobile number a shopper has put on file.

    ``phone_number`` holds the provider's canonical (E.164) form. Removing a
    number soft-deletes it: the row stays so that the notifications sent to it
    keep their history, but it is never listed or messaged again.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='sms_contact_numbers')
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'phone_number'],
                condition=models.Q(deleted_at__isnull=True),
                name='sms_contact_unique_active_number_per_user'),
        ]

    def __str__(self) -> str:
        return 'Contact number #%s' % self.pk


class Notification(models.Model):
    """
    One text message this application asked the provider to send.

    The row is written (``outcome='sending'``) and committed *before* the
    provider is called: it is the claim that stops the same message being sent
    twice, and the record an operator can find if the provider's answer is
    lost. ``reference`` is unique, so the database rejects a second claim.
    """
    KIND_ORDER_PLACED = 'order_placed'
    KIND_DISPATCHED = 'order_dispatched'
    KIND_DELIVERY_FOLLOWUP = 'delivery_followup'
    KIND_CANCELLED = 'order_cancelled'
    KIND_CHOICES = [
        (KIND_ORDER_PLACED, 'Order placed'),
        (KIND_DISPATCHED, 'Order dispatched'),
        (KIND_DELIVERY_FOLLOWUP, 'Delivery follow-up'),
        (KIND_CANCELLED, 'Order cancelled'),
    ]

    # Outcomes. Only the provider's word moves a message to done/pending/failed.
    SENDING = 'sending'            # claimed, no answer yet
    PENDING = 'pending'            # accepted by the provider, not delivered yet
    DONE = 'done'                  # delivered
    FAILED = 'failed'              # never sent, refused, undelivered or called off
    UNKNOWN = 'unknown'            # may have been sent; the provider has not said
    OUTCOME_CHOICES = [
        (SENDING, 'Sending'),
        (PENDING, 'Pending'),
        (DONE, 'Done'),
        (FAILED, 'Failed'),
        (UNKNOWN, 'Unknown'),
    ]

    # State of an attempt to call off a scheduled message.
    CANCEL_NONE = ''
    CANCEL_PENDING = 'pending'
    CANCEL_DONE = 'done'
    CANCEL_FAILED = 'failed'
    CANCEL_UNKNOWN = 'unknown'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='sms_notifications')
    order = models.ForeignKey(
        'order.Order', on_delete=models.CASCADE,
        related_name='sms_notifications')
    contact = models.ForeignKey(
        ContactNumber, on_delete=models.PROTECT, related_name='notifications')
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    reference = models.CharField(max_length=255, unique=True)
    ref_token = models.CharField(max_length=16, unique=True)
    body = models.TextField(blank=True)
    send_at = models.DateTimeField(null=True, blank=True)
    resend_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT,
        related_name='resends')
    idempotency_key = models.CharField(max_length=100, blank=True)

    outcome = models.CharField(
        max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    provider_sid = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    # The provider's own clock: when it sent the message, or when it created
    # it for a message that has not been sent.
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    cancel_state = models.CharField(max_length=16, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    claimed_at = models.DateTimeField()
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['pk']

    def __str__(self) -> str:
        return 'Notification #%s (%s, %s)' % (self.pk, self.kind, self.outcome)
