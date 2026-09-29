from django.conf import settings
from django.db import models
from django.utils import timezone


class ContactNumberQuerySet(models.QuerySet['ContactNumber']):

    def active(self) -> 'ContactNumberQuerySet':
        return self.filter(deleted_at__isnull=True)


class ContactNumber(models.Model):
    """
    A shopper's mobile number, stored in the provider's canonical (E.164)
    form. Removal is a soft delete so that the history of what was sent to
    the number survives; a removed number is never selected for sending.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name='sms_contact_numbers',
        on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ContactNumberQuerySet.as_manager()

    class Meta:
        ordering = ['-created', '-pk']

    def __str__(self) -> str:
        return 'Contact number #%s' % self.pk

    @property
    def masked(self) -> str:
        """The number with all but its last two digits hidden - safe to show and log."""
        return mask_number(self.phone_number)


def mask_number(number: str) -> str:
    if not number:
        return ''
    return '*' * max(len(number) - 2, 0) + number[-2:]


class Outcome:
    """
    What a provider write, or a message's delivery, came to.

    ``sending`` is our own in-flight marker; ``pending`` is only ever the
    provider's word that it accepted the message and has not finished.
    """

    SKIPPED = 'skipped'
    SENDING = 'sending'
    PENDING = 'pending'
    DONE = 'done'
    FAILED = 'failed'
    NEEDS_REVIEW = 'needs_review'
    UNKNOWN = 'unknown'

    choices = [
        (SKIPPED, 'Skipped - no number on file'),
        (SENDING, 'Sending'),
        (PENDING, 'Pending at provider'),
        (DONE, 'Done'),
        (FAILED, 'Failed'),
        (NEEDS_REVIEW, 'Needs review'),
        (UNKNOWN, 'Unknown'),
    ]


class Notification(models.Model):
    """
    One text message about an order: what we meant to send, to whom, and the
    provider's state for it (its sid and latest status).
    """

    PLACED, DISPATCHED, FOLLOWUP, CANCELLED = 'placed', 'dispatched', 'followup', 'cancelled'
    KIND_CHOICES = [
        (PLACED, 'Order placed'),
        (DISPATCHED, 'Order dispatched'),
        (FOLLOWUP, 'Delivery follow-up'),
        (CANCELLED, 'Order cancelled'),
    ]

    reference = models.CharField(max_length=255, unique=True)
    order = models.ForeignKey(
        'order.Order', related_name='sms_notifications', on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name='sms_notifications',
        null=True, blank=True, on_delete=models.SET_NULL)
    contact_number = models.ForeignKey(
        ContactNumber, related_name='notifications', null=True, blank=True,
        on_delete=models.SET_NULL)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    resend_of = models.ForeignKey(
        'self', related_name='resends', null=True, blank=True, on_delete=models.SET_NULL)
    body = models.TextField(blank=True)
    send_at = models.DateTimeField(null=True, blank=True)

    # The provider's state for the message
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    provider_sid = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    provider_sent_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    # Calling off a scheduled follow-up, and disposing of the content
    call_off_outcome = models.CharField(max_length=16, choices=Outcome.choices, blank=True)
    content_redacted_at = models.DateTimeField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created', 'pk']

    def __str__(self) -> str:
        return 'Notification #%s (%s, order %s)' % (self.pk, self.kind, self.order_id)


class ProviderWrite(models.Model):
    """
    The claim and ledger for one write to the provider (a send, a call-off or
    a redaction). The row is committed *before* the provider is called; the
    unique ``reference`` is what stops the same write being made twice.
    """

    SEND, CANCEL, REDACT = 'send', 'cancel', 'redact'
    STEP_CHOICES = [(SEND, 'Send'), (CANCEL, 'Call off'), (REDACT, 'Redact content')]

    reference = models.CharField(max_length=255, unique=True)
    notification = models.ForeignKey(
        Notification, related_name='writes', on_delete=models.CASCADE)
    step = models.CharField(max_length=16, choices=STEP_CHOICES)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    claimed_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['claimed_at', 'pk']

    def __str__(self) -> str:
        return '%s: %s' % (self.reference, self.outcome)
