from django.conf import settings
from django.db import models
from django.db.models import Q

from oscar.core.loading import get_model

Order = get_model("order", "Order")


class Outcome(models.TextChoices):
    """What we know about a provider write or a message's delivery.

    Only ``DONE`` is success. ``PENDING`` is the provider's word that it has not
    finished; ``SENDING`` is our own in-flight claim; ``UNKNOWN`` means it may or
    may not have happened and only the provider's answer can settle it.
    """

    SENDING = "sending"
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical E.164 form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="contact_numbers",
    )
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    national_format = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Set when the shopper removes the number. From then on it is hidden and
    # never messaged; the row itself is erased once every follow-up queued for
    # it with the provider has been called off.
    disabled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "phone_number"],
                condition=Q(disabled_at__isnull=True),
                name="order_notifications_unique_active_number",
            )
        ]

    def __str__(self) -> str:
        return "Contact number #%s" % self.pk


class Notification(models.Model):
    PLACED = "placed"
    DISPATCHED = "dispatched"
    FOLLOW_UP = "follow_up"
    CANCELLED = "cancelled"
    KIND_CHOICES = [
        (PLACED, "Order placed"),
        (DISPATCHED, "Order dispatched"),
        (FOLLOW_UP, "Delivery follow-up"),
        (CANCELLED, "Order cancelled"),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="sms_notifications")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_notifications",
    )
    contact_number = models.ForeignKey(
        ContactNumber,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    to_number = models.CharField(max_length=32)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    # Unique per triggering event: the database rejects a second notification
    # for the same order/kind/number, or for the same resend idempotency key.
    trigger_key = models.CharField(max_length=191, unique=True)
    resend_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="resends"
    )
    # Local copy of the text; cleared when the content is disposed of.
    body = models.TextField(blank=True)
    # Token carried in the message text so a send whose answer was lost can be
    # found again at the provider.
    reference_token = models.CharField(max_length=16)
    scheduled_for = models.DateTimeField(null=True, blank=True)

    message_sid = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    provider_created_at = models.DateTimeField(null=True, blank=True)
    provider_sent_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    call_off_outcome = models.CharField(max_length=16, choices=Outcome.choices, blank=True)
    disposal_outcome = models.CharField(max_length=16, choices=Outcome.choices, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self) -> str:
        return "Notification #%s (%s)" % (self.pk, self.kind)


class ProviderWrite(models.Model):
    """The claim for one provider write step, taken before the provider call.

    ``ref`` is unique, so the database - not a lock in this process - decides
    which request makes the call.
    """

    SEND = "send"
    CANCEL = "cancel"
    REDACT = "redact"
    STEP_CHOICES = [(SEND, "Send"), (CANCEL, "Cancel"), (REDACT, "Redact")]

    ref = models.CharField(max_length=191, unique=True)
    step = models.CharField(max_length=16, choices=STEP_CHOICES)
    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="provider_writes"
    )
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return "Provider write %s (%s)" % (self.step, self.outcome)
