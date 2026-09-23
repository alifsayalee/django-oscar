"""Persistence for SMS order notifications.

These rows are the application's own record of *what it asked the provider to do* — the
provider (Twilio) stays the record of what actually exists. A ContactNumber holds the
provider's canonical E.164 form of a shopper's number; a Notification carries the provider
message SID and the last known delivery outcome so a later request can act on and report it.

Privacy: a destination number lives in the database (never in logs). It is not exposed to
another shopper — every query is scoped to the owning user.
"""
from django.conf import settings
from django.db import models


class Outcome(models.TextChoices):
    """Our own view of where a message got to, mapped from the provider's status."""

    PENDING = "pending", "Pending"
    SCHEDULED = "scheduled", "Scheduled"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    PARTIAL = "partial", "Partially delivered"
    FAILED = "failed", "Failed"
    CANCELED = "canceled", "Canceled"
    UNKNOWN = "unknown", "Unknown"


class NotificationKind(models.TextChoices):
    ORDER_PLACED = "order_placed", "Order placed"
    ORDER_DISPATCHED = "order_dispatched", "Order dispatched"
    DELIVERY_FOLLOWUP = "delivery_followup", "Delivery follow-up"
    ORDER_CANCELLED = "order_cancelled", "Order cancelled"
    RESEND = "resend", "Operator resend"


class ContactNumber(models.Model):
    """A mobile number a shopper has put on file, stored in the provider's canonical E.164 form."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_contact_numbers",
    )
    # Provider's canonical E.164 form (what fetch_phone_number2 returned), never the raw input.
    e164 = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "sms"
        ordering = ("-created_at",)
        # A shopper does not register the same canonical number twice.
        unique_together = (("owner", "e164"),)

    def __str__(self):  # pragma: no cover - admin/debug convenience, no number in repr
        return f"ContactNumber<{self.pk}> owner={self.owner_id}"


class Notification(models.Model):
    """One message the app sent (or tried to send) about an order, plus its provider state.

    The row is written *before* the provider is asked (for resend it is the idempotency claim),
    and settled from what the provider said afterwards. It survives content disposal — only the
    body text is removed, from the provider and here — so the fact a message was sent and what
    became of it endures.
    """

    order = models.ForeignKey(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="sms_notifications",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_notifications",
    )
    kind = models.CharField(max_length=32, choices=NotificationKind.choices)

    # Destination in canonical E.164 (needed for resend). Stored, never logged.
    to_number = models.CharField(max_length=32)
    body = models.TextField(blank=True, default="")
    content_disposed = models.BooleanField(default=False)

    # Provider-owned state.
    provider_sid = models.CharField(max_length=64, blank=True, default="", db_index=True)
    provider_status = models.CharField(max_length=32, blank=True, default="")
    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.PENDING
    )
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")
    # Whether the outcome may have taken effect but could not be confirmed (transport/decode).
    outcome_unknown = models.BooleanField(default=False)

    # The provider's own clock (parsed from Twilio's RFC-2822 timestamps), for reconciliation.
    provider_date_sent = models.DateTimeField(null=True, blank=True)
    provider_date_created = models.DateTimeField(null=True, blank=True)

    # Resend idempotency: this row is the durable claim. Only set for kind=resend.
    source_notification = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="resends",
    )
    idempotency_key = models.CharField(max_length=128, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "sms"
        ordering = ("-created_at",)
        constraints = [
            # A repeated resend under the same key must not send a second message.
            models.UniqueConstraint(
                fields=["source_notification", "idempotency_key"],
                name="uniq_resend_idempotency_key",
                condition=~models.Q(idempotency_key=""),
            ),
        ]

    def __str__(self):  # pragma: no cover
        return f"Notification<{self.pk}> order={self.order_id} kind={self.kind} outcome={self.outcome}"


class OrderTransitionClaim(models.Model):
    """A durable claim that makes an order status transition fire its side-effects exactly once.

    Inserted in the same transaction as the host's ``Order.set_status`` call; the unique
    constraint means only the request that inserted it proceeds to notify / schedule / cancel.
    A repeated transition (double-click, concurrent request) loses the insert and is a clean
    no-op — no second notification, no re-scheduled or re-cancelled follow-up.
    """

    order = models.ForeignKey(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="sms_transition_claims",
    )
    to_status = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "sms"
        unique_together = (("order", "to_status"),)
