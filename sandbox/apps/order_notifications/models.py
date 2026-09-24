from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="sms_contact_numbers", on_delete=models.CASCADE
    )
    # E.164, exactly as the provider's lookup returned it.
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Removed numbers are kept (notifications reference them) but are never
    # listed or messaged again.
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "phone_number"],
                condition=Q(deleted_at__isnull=True),
                name="order_notifications_unique_active_number",
            )
        ]

    def __str__(self):
        # Deliberately does not render the number.
        return "ContactNumber #%s" % self.pk


class Notification(models.Model):
    """One text message about an order, and what became of it."""

    ORDER_PLACED = "order_placed"
    DISPATCHED = "dispatched"
    DELIVERY_FOLLOWUP = "delivery_followup"
    CANCELLED = "cancelled"
    KIND_CHOICES = [
        (ORDER_PLACED, "Order placed"),
        (DISPATCHED, "Order dispatched"),
        (DELIVERY_FOLLOWUP, "Delivery follow-up"),
        (CANCELLED, "Order cancelled"),
    ]

    # submit_state: did the provider accept the message?
    SUBMIT_SENDING = "sending"      # request about to go / in flight
    SUBMIT_ACCEPTED = "accepted"    # provider returned an identifier
    SUBMIT_NOT_SENT = "not_sent"    # definitely did not reach the provider, or was refused
    SUBMIT_UNKNOWN = "unknown"      # may have reached the provider; not resolved yet
    SUBMIT_CHOICES = [
        (SUBMIT_SENDING, "Sending"),
        (SUBMIT_ACCEPTED, "Accepted by provider"),
        (SUBMIT_NOT_SENT, "Not sent"),
        (SUBMIT_UNKNOWN, "Unknown"),
    ]

    order = models.ForeignKey("order.Order", related_name="sms_notifications", on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="sms_notifications", on_delete=models.CASCADE
    )
    contact_number = models.ForeignKey(
        ContactNumber, related_name="notifications", null=True, blank=True, on_delete=models.SET_NULL
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    resend_of = models.ForeignKey(
        "self", related_name="resends", null=True, blank=True, on_delete=models.SET_NULL
    )
    # Our own reference, carried in the message text so a send whose outcome
    # is unknown can be found at the provider.
    reference = models.CharField(max_length=32, unique=True)
    body = models.TextField(blank=True)
    content_redacted_at = models.DateTimeField(null=True, blank=True)

    submit_state = models.CharField(max_length=16, choices=SUBMIT_CHOICES, default=SUBMIT_SENDING)
    # Provider-owned state: its identifier and its latest delivery outcome.
    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provider_status = models.CharField(max_length=32, blank=True)
    # Our reading of provider_status (see gateway.outcome_from_status), or
    # "not_sent" / "unknown" when nothing was accepted.
    outcome = models.CharField(max_length=16, default="pending")
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)

    scheduled_for = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    # A cancellation that has not been confirmed by the provider yet; it is
    # retried whenever the message is looked at again.
    cancel_requested = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return "Notification #%s (%s)" % (self.pk, self.kind)


class ResendRequest(models.Model):
    """An operator's resend, keyed by the caller-supplied idempotency key."""

    STATE_SENDING = "sending"
    STATE_DONE = "done"
    STATE_FAILED = "failed"
    STATE_UNKNOWN = "unknown"
    STATE_CHOICES = [
        (STATE_SENDING, "Sending"),
        (STATE_DONE, "Done"),
        (STATE_FAILED, "Failed"),
        (STATE_UNKNOWN, "Unknown"),
    ]

    key = models.CharField(max_length=128, unique=True)
    notification = models.ForeignKey(Notification, related_name="resend_requests", on_delete=models.CASCADE)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="+", null=True, on_delete=models.SET_NULL
    )
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_SENDING)
    result = models.ForeignKey(
        Notification, related_name="+", null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "ResendRequest #%s" % self.pk
