from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumber(models.Model):
    """
    A shopper's mobile number, stored in the provider's canonical (E.164) form.

    Removing a number soft-deletes it: the row stays so that notifications
    already sent to it keep their history, but it is never messaged again.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_contact_numbers",
    )
    e164 = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "e164"],
                condition=Q(deleted_at__isnull=True),
                name="order_sms_one_active_number_per_user",
            )
        ]

    def __str__(self):
        # Never render the number itself: str() ends up in logs.
        return "ContactNumber #%s" % self.pk

    @property
    def masked(self):
        return mask_number(self.e164)


class Notification(models.Model):
    """
    One SMS this application asked the provider to send.

    The row is written *before* the provider is called and is the claim that
    stops the same message being sent twice: ``reference`` is unique, and
    derived from the operation (order + kind, or original + idempotency key).
    """

    KIND_PLACED = "placed"
    KIND_DISPATCHED = "dispatched"
    KIND_FOLLOWUP = "followup"
    KIND_CANCELLED = "cancelled"
    KIND_CHOICES = [
        (KIND_PLACED, "Order placed"),
        (KIND_DISPATCHED, "Order dispatched"),
        (KIND_FOLLOWUP, "Delivery follow-up"),
        (KIND_CANCELLED, "Order cancelled"),
    ]

    # Our reading of the provider's answer. "sending" is our own in-flight
    # marker: claimed, no answer yet.
    OUTCOME_SENDING = "sending"
    OUTCOME_DONE = "done"
    OUTCOME_PENDING = "pending"
    OUTCOME_FAILED = "failed"
    OUTCOME_UNKNOWN = "unknown"
    OUTCOME_CHOICES = [
        (OUTCOME_SENDING, "Sending"),
        (OUTCOME_DONE, "Delivered"),
        (OUTCOME_PENDING, "In progress"),
        (OUTCOME_FAILED, "Not delivered"),
        (OUTCOME_UNKNOWN, "Unknown"),
    ]

    CANCEL_DONE = "done"
    CANCEL_PENDING = "pending"
    CANCEL_FAILED = "failed"
    CANCEL_CHOICES = [
        (CANCEL_DONE, "Called off"),
        (CANCEL_PENDING, "Cancel not confirmed yet"),
        (CANCEL_FAILED, "Too late to call off"),
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
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notifications",
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    resend_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="resends",
    )
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    reference = models.CharField(max_length=255, unique=True)
    destination = models.CharField(max_length=32)
    body = models.TextField(blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)
    content_disposal_requested_at = models.DateTimeField(null=True, blank=True)

    scheduled_for = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField()
    outcome = models.CharField(
        max_length=16, choices=OUTCOME_CHOICES, default=OUTCOME_SENDING
    )
    failure_reason = models.CharField(max_length=255, blank=True)

    # State the provider owns, as last read from it.
    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_error_code = models.IntegerField(null=True, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_state = models.CharField(
        max_length=16, choices=CANCEL_CHOICES, blank=True
    )

    class Meta:
        ordering = ["claimed_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["resend_of", "idempotency_key"],
                name="order_sms_one_resend_per_key",
            )
        ]

    def __str__(self):
        return "Notification #%s (%s)" % (self.pk, self.kind)


def mask_number(number):
    """Render a phone number safely for display: keep only the last 2 digits."""
    if not number:
        return ""
    return "*" * max(len(number) - 2, 0) + number[-2:]
