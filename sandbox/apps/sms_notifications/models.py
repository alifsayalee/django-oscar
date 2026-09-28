"""
Models for SMS order notifications.

A ``Notification`` row is also the *claim* for the provider write that sends
it: its ``reference`` is unique, so the database rejects a second attempt to
send the same message. ``ProviderAction`` plays the same role for the writes
that act on an existing message (cancelling a scheduled follow-up, redacting
its text).
"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class Outcome(models.TextChoices):
    # Claimed, no answer from the provider yet.
    SENDING = "sending", "Sending"
    # What the caller asked for is in effect.
    DONE = "done", "Done"
    # The provider accepted it and has not finished.
    PENDING = "pending", "Pending"
    # Never sent, refused, or reported failed/undone by the provider.
    FAILED = "failed", "Failed"
    # It happened, but not as asked.
    NEEDS_REVIEW = "needs_review", "Needs review"
    # May have happened; only the provider's answer settles it.
    UNKNOWN = "unknown", "Unknown"


class ContactNumber(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_contact_numbers",
    )
    # The provider's canonical (E.164) form, never what the caller typed.
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Removed numbers are kept for the notification history but are never
    # listed or messaged again.
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "phone_number"),
                condition=Q(removed_at__isnull=True),
                name="sms_contact_number_unique_active",
            )
        ]

    def __str__(self) -> str:
        return "Contact number #%s" % self.pk


class Notification(models.Model):
    PLACED, DISPATCHED, FOLLOW_UP, CANCELLED = (
        "placed",
        "dispatched",
        "follow_up",
        "cancelled",
    )
    KIND_CHOICES = (
        (PLACED, "Order placed"),
        (DISPATCHED, "Order dispatched"),
        (FOLLOW_UP, "Delivery follow-up"),
        (CANCELLED, "Order cancelled"),
    )

    # Claim key for the send: derived from the operation, never random.
    reference = models.CharField(max_length=255, unique=True)
    order = models.ForeignKey(
        "order.Order", on_delete=models.CASCADE, related_name="sms_notifications"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_notifications",
    )
    contact_number = models.ForeignKey(
        ContactNumber, on_delete=models.PROTECT, related_name="notifications"
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    resend_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="resends",
    )
    # Message text without the reference suffix. Cleared on content disposal.
    text = models.TextField(blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)
    # For a follow-up queued with the provider.
    send_at = models.DateTimeField(null=True, blank=True)

    # The send write's outcome (see ``Outcome``).
    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.SENDING
    )
    claimed_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    # State the provider owns, refreshed from it.
    provider_sid = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_error_code = models.IntegerField(null=True, blank=True)
    provider_error_message = models.CharField(max_length=255, blank=True)
    provider_date_created = models.DateTimeField(null=True, blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_checked_at = models.DateTimeField(null=True, blank=True)

    # Set when the order is cancelled (or the number removed) so a follow-up
    # whose send is still in flight is called off as soon as it resolves.
    cancel_requested_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")

    def __str__(self) -> str:
        return "Notification #%s (%s)" % (self.pk, self.kind)


class ProviderAction(models.Model):
    CANCEL, REDACT = "cancel", "redact"
    ACTION_CHOICES = ((CANCEL, "Cancel scheduled message"), (REDACT, "Redact content"))

    # Claim key for the write: derived from the notification and the action.
    reference = models.CharField(max_length=255, unique=True)
    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="actions"
    )
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.SENDING
    )
    claimed_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    provider_sid = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ("claimed_at", "id")

    def __str__(self) -> str:
        return "%s of notification #%s" % (self.action, self.notification_id)
