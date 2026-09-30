from django.conf import settings
from django.db import models
from django.db.models import Q

from . import gateway


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical (E.164) form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sms_contact_numbers")
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    national_format = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "phone_number"], name="sms_contact_number_unique_per_user"),
        ]

    def __str__(self):
        return "ContactNumber #%s" % self.pk


class Notification(models.Model):
    """
    One text message about an order, and what the provider says became of it.

    ``state`` is this application's view of the send attempt; ``provider_*``
    mirrors the provider-owned state (message sid, delivery status) so any
    later request can act on it and report on it.
    """

    class Kind(models.TextChoices):
        ORDER_PLACED = "order_placed", "Order placed"
        DISPATCHED = "dispatched", "Dispatched"
        DELIVERY_FOLLOWUP = "delivery_followup", "Delivery follow-up"
        CANCELLED = "cancelled", "Cancelled"

    class State(models.TextChoices):
        PENDING = "pending", "Claimed, not yet handed to the provider"
        SUBMITTED = "submitted", "Accepted by the provider"
        UNKNOWN = "unknown", "Sent, but the provider's answer was lost"
        SEND_FAILED = "send_failed", "Not accepted by the provider"
        SKIPPED = "skipped", "Not sent (no number on file, or no longer wanted)"

    class ActionState(models.TextChoices):
        NONE = "", "Not requested"
        REQUESTED = "requested", "In progress"
        DONE = "done", "Done"
        UNKNOWN = "unknown", "Outcome unknown, will be re-checked"
        FAILED = "failed", "Failed, can be retried"
        TOO_LATE = "too_late", "Message had already been sent"
        NOT_NEEDED = "not_needed", "Nothing at the provider to act on"

    order = models.ForeignKey("order.Order", on_delete=models.CASCADE, related_name="sms_notifications")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    contact_number = models.ForeignKey(
        ContactNumber, null=True, blank=True, on_delete=models.SET_NULL, related_name="notifications")
    # Snapshot of the destination, needed to find the message at the provider.
    to_number = models.CharField(max_length=32, blank=True)
    body = models.TextField(blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_error_code = models.IntegerField(null=True, blank=True)
    provider_date_created = models.DateTimeField(null=True, blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True)
    provider_checked_at = models.DateTimeField(null=True, blank=True)

    scheduled_for = models.DateTimeField(null=True, blank=True)
    attempt_started_at = models.DateTimeField(null=True, blank=True)

    cancel_state = models.CharField(max_length=16, choices=ActionState.choices, blank=True, default="")
    cancel_requested_at = models.DateTimeField(null=True, blank=True)

    disposal_state = models.CharField(max_length=16, choices=ActionState.choices, blank=True, default="")
    disposal_requested_at = models.DateTimeField(null=True, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    resend_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="resends")
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, unique=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            # One original message of each kind per order; resends are extra rows.
            models.UniqueConstraint(
                fields=["order", "kind"], condition=Q(resend_of__isnull=True),
                name="sms_notification_one_per_order_kind"),
        ]
        indexes = [models.Index(fields=["provider_date_sent"])]

    def __str__(self):
        return "Notification #%s (%s)" % (self.pk, self.kind)

    @property
    def is_final(self):
        return self.provider_status in gateway.FINAL_STATUSES

    @property
    def delivery_outcome(self):
        """A caller-facing summary of where this message got to."""
        if self.state == self.State.SKIPPED:
            return "not_sent"
        if self.state == self.State.SEND_FAILED:
            return "failed"
        if self.state in (self.State.PENDING, self.State.UNKNOWN):
            return "unknown" if self.state == self.State.UNKNOWN else "pending"
        status = self.provider_status
        if status in gateway.DELIVERED_STATUSES:
            return "delivered"
        if status in gateway.FAILED_STATUSES:
            return "failed"
        if status == "canceled":
            return "canceled"
        if status == "scheduled":
            return "scheduled"
        if status in gateway.IN_FLIGHT_STATUSES:
            return "in_progress"
        return "unknown"
