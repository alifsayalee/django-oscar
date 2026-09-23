"""Durable rows for the SMS notification app.

These are NOT a parallel copy of Twilio's records or of Oscar's order model. Twilio stays the system
of record for *what messages exist*; Oscar stays the system of record for *what orders exist*. These
rows record *that this app asked* -- the shopper's number on file, each message this app tried to
send (with the provider's identifier and last-known delivery outcome), and the claims that keep
dispatch / cancel / resend from firing their side effects twice.
"""

from django.conf import settings
from django.db import models


class ContactNumber(models.Model):
    """A mobile number a shopper has put on file, in the provider's canonical E.164 form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_contact_numbers",
    )
    # Twilio Lookup's canonical E.164 form -- never whatever the caller happened to type.
    canonical_number = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "sms_notifications"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "canonical_number"],
                name="uniq_user_contact_number",
            )
        ]
        ordering = ["-created_at"]

    def __str__(self):  # pragma: no cover - admin/debug convenience
        return f"ContactNumber(user={self.user_id})"


class OrderNotification(models.Model):
    """One message this app tried to send for an order, plus the state the provider owns for it."""

    KIND_PLACED = "placed"
    KIND_DISPATCHED = "dispatched"
    KIND_DISPATCHED_FOLLOWUP = "dispatched_followup"
    KIND_CANCELLED = "cancelled"
    KIND_RESEND = "resend"
    KIND_CHOICES = [
        (KIND_PLACED, "Order placed"),
        (KIND_DISPATCHED, "Order dispatched"),
        (KIND_DISPATCHED_FOLLOWUP, "Delivery follow-up"),
        (KIND_CANCELLED, "Order cancelled"),
        (KIND_RESEND, "Operator resend"),
    ]

    # Our own mapped outcome. Distinct from the raw provider status, which we also keep.
    OUTCOME_SENDING = "sending"      # claimed, no answer yet
    OUTCOME_PENDING = "pending"      # provider accepted, not finished
    OUTCOME_DONE = "done"            # delivered / sent / received / read
    OUTCOME_FAILED = "failed"        # failed / undelivered / canceled, or provider rejected the send
    OUTCOME_PARTIAL = "partial"      # partially_delivered
    OUTCOME_UNKNOWN = "unknown"      # may have happened, or a status we do not map
    OUTCOME_SKIPPED = "skipped"      # no number on file -> nothing was attempted

    order = models.ForeignKey(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="sms_notifications",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_order_notifications",
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    to_number = models.CharField(max_length=32, blank=True)
    # The text we sent. Cleared locally when content is disposed of (redacted at the provider too).
    body = models.TextField(blank=True, default="")

    provider_sid = models.CharField(max_length=64, blank=True, default="")
    provider_status = models.CharField(max_length=32, blank=True, default="")
    outcome = models.CharField(max_length=16, default=OUTCOME_SENDING)
    error_code = models.IntegerField(null=True, blank=True)

    is_followup = models.BooleanField(default=False)
    canceled = models.BooleanField(default=False)
    content_redacted = models.BooleanField(default=False)
    scheduled_send_at = models.DateTimeField(null=True, blank=True)
    # The provider's own clock (date_sent). Used for reconciliation -- NOT created_at.
    provider_time = models.DateTimeField(null=True, blank=True)

    # Caller-supplied idempotency key for resend. Unique so a repeat under the same key cannot send
    # a second message; a fresh key is a legitimate new attempt.
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "sms_notifications"
        ordering = ["-created_at"]

    def __str__(self):  # pragma: no cover - admin/debug convenience
        return f"OrderNotification(order={self.order_id}, kind={self.kind}, outcome={self.outcome})"


class OrderTransition(models.Model):
    """A claim row: inserting it is how a request wins the right to run a transition's side effects.

    The unique constraint on (order, to_status) closes the check-then-act race that a "read status,
    then write if different" would leave open -- across processes, not just threads.
    """

    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"

    order = models.ForeignKey(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="sms_transitions",
    )
    to_status = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "sms_notifications"
        constraints = [
            models.UniqueConstraint(
                fields=["order", "to_status"],
                name="uniq_order_transition",
            )
        ]

    def __str__(self):  # pragma: no cover - admin/debug convenience
        return f"OrderTransition(order={self.order_id}, to={self.to_status})"
