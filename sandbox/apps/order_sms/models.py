"""
Models for order SMS notifications.

A ``ContactNumber`` is a shopper's mobile number, stored in the provider's canonical (E.164) form.
A ``Notification`` is one text message this application asked the provider to send about an
order. The row is written *before* the provider is called: its unique ``reference`` is the claim
that stops the same message being sent twice, and it carries the provider's identifier and delivery
outcome once known so any later request can report on it or act on it.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumber(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='sms_contact_numbers')
    # Canonical E.164 form as returned by the provider's lookup - never what the caller typed.
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Soft delete: notification history keeps pointing at the number, but nothing is sent to it.
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'phone_number'], condition=Q(deleted_at__isnull=True),
                name='order_sms_one_active_number_per_user'),
        ]

    def __str__(self) -> str:
        # Never render the number itself: str() ends up in logs and admin history.
        return f'ContactNumber #{self.pk}'

    @property
    def masked(self) -> str:
        number = self.phone_number
        return f'{number[:3]}{"*" * max(len(number) - 5, 0)}{number[-2:]}' if number else ''


class Notification(models.Model):
    KIND_PLACED = 'order_placed'
    KIND_DISPATCHED = 'order_dispatched'
    KIND_FOLLOWUP = 'delivery_followup'
    KIND_CANCELLED = 'order_cancelled'
    KIND_RESEND = 'resend'
    KIND_CHOICES = [
        (KIND_PLACED, 'Order placed'),
        (KIND_DISPATCHED, 'Order dispatched'),
        (KIND_FOLLOWUP, 'Delivery follow-up'),
        (KIND_CANCELLED, 'Order cancelled'),
        (KIND_RESEND, 'Operator re-send'),
    ]

    # Outcome of the send, in this application's terms (see gateway.status_from_provider).
    SENDING = 'sending'            # claimed, provider not answered yet
    PENDING = 'pending'            # provider accepted it and has not finished
    DONE = 'done'                  # provider reports it delivered
    FAILED = 'failed'              # never sent, refused, or reported failed/undone by the provider
    UNKNOWN = 'unknown'            # may have happened; settled only by asking the provider
    OUTCOME_CHOICES = [(v, v) for v in (SENDING, PENDING, DONE, FAILED, UNKNOWN)]

    # Outcome of a follow-up call-off / of content disposal.
    ACTION_REQUESTED = 'requested'
    ACTION_CHOICES = [(v, v) for v in (ACTION_REQUESTED, PENDING, DONE, FAILED, UNKNOWN)]

    order = models.ForeignKey(
        'order.Order', on_delete=models.CASCADE, related_name='sms_notifications')
    contact = models.ForeignKey(
        ContactNumber, on_delete=models.PROTECT, related_name='notifications')
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    resend_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT, related_name='resends')

    # The claim. Derived from the operation (install, order/source, step) - never random.
    reference = models.CharField(max_length=255, unique=True)
    idempotency_key_hash = models.CharField(max_length=64, blank=True)

    # Local copy of the text; cleared when the shopper asks for it to be disposed of.
    body = models.TextField(blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    provider_status = models.CharField(max_length=32, blank=True)
    message_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    error_code = models.IntegerField(null=True, blank=True)
    error_detail = models.CharField(max_length=255, blank=True)
    provider_date_created = models.DateTimeField(null=True, blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True)

    cancel_outcome = models.CharField(max_length=16, choices=ACTION_CHOICES, blank=True)
    content_disposal = models.CharField(max_length=16, choices=ACTION_CHOICES, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['claimed_at', 'pk']
        indexes = [
            models.Index(fields=['outcome']),
            models.Index(fields=['provider_date_sent']),
        ]

    def __str__(self) -> str:
        return f'Notification #{self.pk} ({self.kind}, {self.outcome})'
