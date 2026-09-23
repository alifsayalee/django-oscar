from django.conf import settings
from django.db import models
from django.utils import timezone


class ContactNumber(models.Model):
    """A mobile number a shopper has put on file so the shop can text them.

    ``e164`` holds the *provider's* canonical form of the number (as returned by
    the Twilio lookup), not whatever the caller typed. A number belongs to the
    shopper who registered it; it must never be written to logs.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_contact_numbers",
    )
    e164 = models.CharField(max_length=20)
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        # One canonical number per shopper; re-registering the same number is a no-op
        # match rather than a duplicate.
        unique_together = ("user", "e164")
        ordering = ("-created",)

    def __str__(self):
        # Deliberately does not include the number itself (never log a shopper's number).
        return "ContactNumber #%s for user %s" % (self.pk, self.user_id)


class OrderNotification(models.Model):
    """One SMS the shop sent (or tried to send) about an order.

    Carries enough of the state Twilio owns — the message ``provider_sid`` and
    the last known ``status`` — that a later request can act on it (resend,
    cancel a scheduled follow-up, dispose of content) and report on it, not only
    the request that first sent it.
    """

    KIND_PLACED = "placed"
    KIND_DISPATCHED = "dispatched"
    KIND_CANCELLED = "cancelled"
    KIND_DELIVERY_SURVEY = "delivery_survey"
    KIND_RESEND = "resend"
    KIND_CHOICES = [
        (KIND_PLACED, "Order placed"),
        (KIND_DISPATCHED, "Order dispatched"),
        (KIND_CANCELLED, "Order cancelled"),
        (KIND_DELIVERY_SURVEY, "Delivery survey (follow-up)"),
        (KIND_RESEND, "Operator re-send"),
    ]

    # Local outcome, distinct from the provider's own status string:
    #   pending  - we are about to talk to the provider / accepted, not yet resolved
    #   sent     - the provider accepted the message (has a sid)
    #   failed   - the provider rejected it, or it never left
    #   unknown  - we could not tell whether it landed
    OUTCOME_PENDING = "pending"
    OUTCOME_SENT = "sent"
    OUTCOME_FAILED = "failed"
    OUTCOME_UNKNOWN = "unknown"

    order = models.ForeignKey(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="sms_notifications",
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    # Destination number (canonical E.164). Stored, never logged.
    to_number = models.CharField(max_length=20)

    # Provider-owned state.
    provider_sid = models.CharField(max_length=64, blank=True, default="")
    provider_status = models.CharField(max_length=32, blank=True, default="")
    provider_error_code = models.IntegerField(null=True, blank=True)
    provider_error_message = models.TextField(blank=True, default="")
    provider_date_sent = models.DateTimeField(null=True, blank=True)

    # Local bookkeeping.
    local_outcome = models.CharField(max_length=16, default=OUTCOME_PENDING)
    detail = models.TextField(blank=True, default="")

    is_followup = models.BooleanField(default=False)
    scheduled_send_at = models.DateTimeField(null=True, blank=True)
    followup_cancelled = models.BooleanField(default=False)
    content_disposed = models.BooleanField(default=False)

    resend_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resends",
    )
    # Caller-supplied idempotency key for resend. Unique so a repeat under the same
    # key cannot create a second message.
    idempotency_key = models.CharField(
        max_length=200, null=True, blank=True, unique=True
    )

    created = models.DateTimeField(default=timezone.now)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created",)

    def __str__(self):
        return "OrderNotification #%s (%s) for order %s" % (
            self.pk,
            self.kind,
            self.order_id,
        )
