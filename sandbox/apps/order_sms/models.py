import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from . import twilio_gateway as gateway


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in Twilio's canonical E.164 form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sms_contact_numbers"
    )
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "phone_number"], name="order_sms_unique_user_number"),
        ]

    def __str__(self):
        # Never render the number itself: str() ends up in logs and admin history.
        return f"ContactNumber #{self.pk}"


class OrderTransitionClaim(models.Model):
    """
    Inserted in the same transaction as ``Order.set_status``: only the request
    that inserts it performs the transition's side effects (messages).
    """

    order = models.ForeignKey("order.Order", on_delete=models.CASCADE, related_name="sms_transition_claims")
    to_status = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "to_status"], name="order_sms_unique_transition"),
        ]


class Notification(models.Model):
    ORDER_PLACED = "order_placed"
    ORDER_DISPATCHED = "order_dispatched"
    DELIVERY_FOLLOWUP = "delivery_followup"
    ORDER_CANCELLED = "order_cancelled"
    KIND_CHOICES = [
        (ORDER_PLACED, "Order placed"),
        (ORDER_DISPATCHED, "Order dispatched"),
        (DELIVERY_FOLLOWUP, "Delivery follow-up"),
        (ORDER_CANCELLED, "Order cancelled"),
    ]

    # Local-only states; every other value of ``status`` is Twilio's own.
    CLAIMED = "claimed"  # claimed locally, provider has not answered yet
    REJECTED = "rejected"  # Twilio refused it, or it never left: nothing was sent
    UNKNOWN = "unknown"  # may have reached Twilio; could not confirm

    order = models.ForeignKey("order.Order", on_delete=models.CASCADE, related_name="sms_notifications")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    contact = models.ForeignKey(
        ContactNumber, null=True, blank=True, on_delete=models.SET_NULL, related_name="notifications"
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    ref = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    body = models.TextField(blank=True)
    status = models.CharField(max_length=32, default=CLAIMED)
    provider_sid = models.CharField(max_length=64, unique=True, null=True, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    send_at = models.DateTimeField(null=True, blank=True)
    provider_date_created = models.DateTimeField(null=True, blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    content_redacted_at = models.DateTimeField(null=True, blank=True)
    resend_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="resends"
    )
    idempotency_key = models.CharField(max_length=200, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            # One automatic message per order event: the durable send claim.
            models.UniqueConstraint(
                fields=["order", "kind"],
                condition=Q(resend_of__isnull=True),
                name="order_sms_unique_event_message",
            ),
            # One re-send per (source message, caller idempotency key).
            models.UniqueConstraint(
                fields=["resend_of", "idempotency_key"],
                condition=Q(resend_of__isnull=False),
                name="order_sms_unique_resend_key",
            ),
        ]

    def __str__(self):
        return f"Notification #{self.pk} ({self.kind}, {self.status})"

    @property
    def outcome(self):
        if self.status == self.CLAIMED:
            return gateway.PENDING
        if self.status == self.REJECTED:
            return gateway.FAILED
        if self.status == self.UNKNOWN:
            return gateway.UNKNOWN
        return gateway.outcome_for(self.status)

    @property
    def is_final(self):
        return self.outcome in (gateway.DELIVERED, gateway.FAILED, gateway.CANCELED)

    @property
    def provider_time(self):
        return self.provider_date_sent or self.provider_date_created

    @property
    def ref_tag(self):
        return f"[ref {self.ref.hex[:10]}]"
