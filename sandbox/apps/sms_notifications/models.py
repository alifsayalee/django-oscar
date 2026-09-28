"""
Order SMS notifications.

The provider (Twilio) owns what happened to each message; these models hold
what this application asked for, the claim that stops one write being made
twice, and the last thing the provider said about it.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_contact_numbers",
    )
    # E.164, as returned by the provider's lookup - never what the caller typed.
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Removal is recorded rather than deleting the row, so notifications that
    # went to this number keep their history.
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "phone_number"],
                condition=Q(deleted_at__isnull=True),
                name="sms_unique_active_contact_number",
            )
        ]

    def __str__(self) -> str:
        # Never render the number itself: model reprs end up in logs.
        return "ContactNumber #%s" % self.pk

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None


class Notification(models.Model):
    """One text message this application sent (or tried to send) about an order."""

    KIND_PLACED = "placed"
    KIND_DISPATCHED = "dispatched"
    KIND_FOLLOW_UP = "follow_up"
    KIND_CANCELLED = "cancelled"
    KIND_CHOICES = [
        (KIND_PLACED, "Order placed"),
        (KIND_DISPATCHED, "Order dispatched"),
        (KIND_FOLLOW_UP, "Delivery follow-up"),
        (KIND_CANCELLED, "Order cancelled"),
    ]

    order = models.ForeignKey(
        "order.Order", on_delete=models.CASCADE, related_name="sms_notifications"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_notifications",
    )
    contact_number = models.ForeignKey(
        ContactNumber,
        on_delete=models.PROTECT,
        related_name="notifications",
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    # The text sent, including the reference tag. Wiped once the provider has
    # disposed of its copy (content_redacted_at).
    body = models.TextField(blank=True)
    content_redacted_at = models.DateTimeField(null=True, blank=True)
    # Set for scheduled messages: when the provider will send it.
    send_at = models.DateTimeField(null=True, blank=True)
    resend_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="resends",
    )

    # What the provider last said about this message.
    provider_sid = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_error_code = models.IntegerField(null=True, blank=True)
    # The provider's clock: when it sent the message (RFC 2822 date_sent).
    provider_date_sent = models.DateTimeField(null=True, blank=True, db_index=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return "Notification #%s (%s)" % (self.pk, self.kind)


class ProviderWrite(models.Model):
    """
    The claim on one write to the provider: a send, a call-off or a redaction.

    ``reference`` is unique, so the database - not a lock in this process -
    decides which request makes the call.
    """

    OP_SEND = "send"
    OP_CANCEL = "cancel"
    OP_REDACT = "redact"
    OP_CHOICES = [
        (OP_SEND, "Send"),
        (OP_CANCEL, "Call off a scheduled message"),
        (OP_REDACT, "Redact message content"),
    ]

    SENDING = "sending"
    DONE = "done"
    PENDING = "pending"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    OUTCOME_CHOICES = [
        (SENDING, "Claimed, no answer yet"),
        (DONE, "Done"),
        (PENDING, "Accepted by the provider, not finished"),
        (FAILED, "Failed"),
        (NEEDS_REVIEW, "Happened, but not as asked"),
        (UNKNOWN, "May have happened"),
    ]

    reference = models.CharField(max_length=255, unique=True)
    operation = models.CharField(max_length=16, choices=OP_CHOICES)
    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="writes"
    )
    # For a resend: the caller's idempotency key, hashed (the raw key is theirs).
    idempotency_key_hash = models.CharField(max_length=64, blank=True)
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    provider_sid = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["claimed_at", "id"]

    def __str__(self) -> str:
        return "ProviderWrite %s (%s)" % (self.reference, self.outcome)
