"""
Models for SMS order notifications.

A ``Notification`` row is written *before* its message is handed to Twilio and
its unique ``reference`` is the claim that stops the same message being sent
twice. Afterwards it carries the provider's own state for the message (its SID,
status and timestamps) so any later request can act on it and report on it.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumberQuerySet(models.QuerySet):
    def active(self):
        return self.filter(removed_at__isnull=True)


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical E.164 form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='sms_contact_numbers')
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Removal is a soft delete: past notifications keep pointing at the number
    # they went to, but nothing is sent to a removed number again.
    removed_at = models.DateTimeField(null=True, blank=True)

    objects = ContactNumberQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'phone_number'],
                condition=Q(removed_at__isnull=True),
                name='uniq_active_contact_number_per_user'),
        ]

    def __str__(self):
        # Never render the number itself: model reprs end up in logs.
        return f'ContactNumber #{self.pk}'


class Notification(models.Model):
    KIND_ORDER_PLACED = 'order_placed'
    KIND_ORDER_DISPATCHED = 'order_dispatched'
    KIND_DELIVERY_FOLLOWUP = 'delivery_followup'
    KIND_ORDER_CANCELLED = 'order_cancelled'
    KIND_CHOICES = [
        (KIND_ORDER_PLACED, 'Order placed'),
        (KIND_ORDER_DISPATCHED, 'Order dispatched'),
        (KIND_DELIVERY_FOLLOWUP, 'Delivery follow-up'),
        (KIND_ORDER_CANCELLED, 'Order cancelled'),
    ]

    # What this application knows about the outcome of sending the message.
    OUTCOME_SENDING = 'sending'        # claimed; no answer from the provider yet
    OUTCOME_PENDING = 'pending'        # provider accepted it and has not finished
    OUTCOME_DONE = 'done'              # delivered to the handset
    OUTCOME_FAILED = 'failed'          # never sent, refused, undeliverable or called off
    OUTCOME_UNKNOWN = 'unknown'        # may have been sent; only the provider can settle it
    OUTCOME_CHOICES = [(v, v) for v in (
        OUTCOME_SENDING, OUTCOME_PENDING, OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_UNKNOWN)]

    # Calling off a scheduled message (the delivery follow-up).
    CANCEL_NONE = 'none'
    CANCEL_REQUESTED = 'requested'     # claimed; the cancel call is in flight
    CANCEL_DONE = 'done'               # the provider says it is canceled
    CANCEL_PENDING = 'pending'         # provider still has it scheduled; retry
    CANCEL_TOO_LATE = 'too_late'       # the message had already gone out
    CANCEL_UNKNOWN = 'unknown'         # no readable answer; check with the provider
    CANCEL_CHOICES = [(v, v) for v in (
        CANCEL_NONE, CANCEL_REQUESTED, CANCEL_DONE, CANCEL_PENDING, CANCEL_TOO_LATE,
        CANCEL_UNKNOWN)]

    # Disposal of the message text, here and at the provider.
    CONTENT_RETAINED = 'retained'
    CONTENT_DISPOSING = 'disposing'    # claimed; the redaction call is in flight
    CONTENT_DISPOSED = 'disposed'      # provider confirmed an empty body; local copy wiped
    CONTENT_UNKNOWN = 'unknown'        # no confirmation yet; the request can be repeated
    CONTENT_CHOICES = [(v, v) for v in (
        CONTENT_RETAINED, CONTENT_DISPOSING, CONTENT_DISPOSED, CONTENT_UNKNOWN)]

    reference = models.CharField(max_length=200, unique=True)
    order = models.ForeignKey(
        'order.Order', on_delete=models.CASCADE, related_name='sms_notifications')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='sms_notifications')
    contact_number = models.ForeignKey(
        ContactNumber, on_delete=models.PROTECT, related_name='notifications')
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    body = models.TextField(blank=True)
    resend_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT, related_name='resends')
    idempotency_key = models.CharField(max_length=200, blank=True)

    outcome = models.CharField(
        max_length=16, choices=OUTCOME_CHOICES, default=OUTCOME_SENDING)
    claimed_at = models.DateTimeField()
    scheduled_for = models.DateTimeField(null=True, blank=True)

    # State owned by the provider, as last read from it.
    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_error_code = models.IntegerField(null=True, blank=True)
    provider_error_message = models.CharField(max_length=255, blank=True)
    # The provider's clock: when it sent the message, else when it created it.
    provider_time = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    cancel_state = models.CharField(
        max_length=16, choices=CANCEL_CHOICES, default=CANCEL_NONE)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)

    content_state = models.CharField(
        max_length=16, choices=CONTENT_CHOICES, default=CONTENT_RETAINED)
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['claimed_at', 'id']
        indexes = [
            models.Index(fields=['outcome']),
            models.Index(fields=['provider_time']),
        ]

    def __str__(self):
        return f'Notification #{self.pk} ({self.kind}, {self.outcome})'
