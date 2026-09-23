from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical (E.164) form."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="contact_numbers", on_delete=models.CASCADE)
    e164 = models.CharField(max_length=20)
    country_code = models.CharField(max_length=2, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["user", "e164"], name="uniq_contact_number_per_user")]

    def masked(self):
        return "***" + self.e164[-4:]

    def __str__(self):
        # Never render the full number in logs/admin lists.
        return f"ContactNumber#{self.pk} ({self.masked()})"


class Notification(models.Model):
    """One message this app asked the provider to send, and what became of it.

    The row is written (status ``sending``) *before* the provider is called, so
    a request that dies mid-call still leaves a record an operator can find.
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

    # Local-only states; every other value is a provider outcome (see provider.outcome_from_status).
    SENDING = "sending"      # claimed, provider call in flight
    NOT_SENT = "not_sent"    # definitely never reached the provider / refused by it
    STATUS_CHOICES = [(s, s) for s in (
        "sending", "not_sent", "pending", "sent", "delivered", "scheduled", "failed", "canceled", "unknown")]
    TERMINAL = ("delivered", "failed", "canceled", "not_sent")

    order = models.ForeignKey("order.Order", related_name="sms_notifications", on_delete=models.CASCADE)
    contact_number = models.ForeignKey(
        ContactNumber, related_name="notifications", null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    body = models.TextField(blank=True, default="")
    resend_of = models.ForeignKey(
        "self", related_name="resends", null=True, blank=True, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, unique=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provider_status = models.CharField(max_length=32, blank=True, default="")
    error_code = models.IntegerField(null=True, blank=True)
    error_detail = models.CharField(max_length=255, blank=True, default="")
    scheduled_for = models.DateTimeField(null=True, blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            # One original message of each kind per order; resends are separate rows.
            models.UniqueConstraint(
                fields=["order", "kind"], condition=Q(resend_of__isnull=True), name="uniq_original_notification"),
        ]

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL

    def __str__(self):
        return f"Notification#{self.pk} {self.kind} [{self.status}]"


class OrderTransition(models.Model):
    """Claim row for an operator status change. Exactly one request can insert
    it, so only that request changes the order and sends messages."""

    order = models.ForeignKey("order.Order", related_name="sms_transitions", on_delete=models.CASCADE)
    to_status = models.CharField(max_length=64)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+", on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["order", "to_status"], name="uniq_order_transition")]
