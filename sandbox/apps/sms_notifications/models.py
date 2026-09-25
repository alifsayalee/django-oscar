from django.conf import settings
from django.db import models
from django.db.models import Q


def mask_number(number: str) -> str:
    """Show only the last few digits of a phone number, e.g. for responses about messages."""
    return f"***{number[-3:]}" if len(number) > 3 else "***"


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical (E.164) form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sms_contact_numbers"
    )
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    national_format = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Removed numbers are kept (notifications reference them) but never messaged again.
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "phone_number"],
                condition=Q(deleted_at__isnull=True),
                name="sms_unique_active_number_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"ContactNumber #{self.pk} ({mask_number(self.phone_number)})"


class Notification(models.Model):
    """
    One SMS this app asked the provider to send, and what became of it.

    The row is the claim for the send: it is committed (outcome ``sending``)
    before the provider is called, under a ``reference`` unique to the
    operation, so a repeated request can never send a second message.
    """

    class Kind(models.TextChoices):
        PLACED = "placed", "Order placed"
        DISPATCHED = "dispatched", "Order dispatched"
        FOLLOWUP = "followup", "Delivery follow-up"
        CANCELLED = "cancelled", "Order cancelled"
        RESEND = "resend", "Operator resend"

    class Outcome(models.TextChoices):
        SENDING = "sending", "Claimed, no provider answer yet"
        PENDING = "pending", "Accepted by the provider, not delivered yet"
        DONE = "done", "Delivered"
        FAILED = "failed", "Not delivered"
        NEEDS_REVIEW = "needs_review", "Sent, but not as requested"
        UNKNOWN = "unknown", "May have been sent; not confirmed"

    reference = models.CharField(max_length=200, unique=True)
    ref_token = models.CharField(max_length=16, db_index=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    order = models.ForeignKey("order.Order", on_delete=models.CASCADE, related_name="sms_notifications")
    contact_number = models.ForeignKey(ContactNumber, on_delete=models.PROTECT, related_name="notifications")
    to_number = models.CharField(max_length=32)
    body = models.TextField(blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    resend_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="resends")
    idempotency_key_hash = models.CharField(max_length=64, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    outcome_detail = models.CharField(max_length=255, blank=True)
    claimed_at = models.DateTimeField()

    # State owned by the provider, as last reported by it.
    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_error_code = models.IntegerField(null=True, blank=True)
    provider_error_message = models.CharField(max_length=255, blank=True)
    provider_date_created = models.DateTimeField(null=True, blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True, db_index=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_outcome = models.CharField(max_length=16, choices=Outcome.choices, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)

    content_disposal_requested_at = models.DateTimeField(null=True, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"Notification #{self.pk} ({self.kind}, {self.outcome})"
