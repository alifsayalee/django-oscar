from django.conf import settings
from django.db import models


class ContactNumber(models.Model):
    """A mobile number a shopper has put on file so the shop can text them.

    The stored value is the provider's own canonical (E.164) form of the
    number, established at registration time via a Twilio lookup. The number
    belongs to the shopper who registered it and must never be exposed to,
    used by, or deleted by another shopper. It is never written to logs.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_contact_numbers",
    )
    # Provider-canonical E.164 form. Not the raw input the caller typed.
    e164 = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "sms"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "e164"], name="uniq_owner_e164"
            )
        ]

    def __str__(self):
        # Deliberately does not reveal the number.
        return f"ContactNumber #{self.pk}"


class OrderNotification(models.Model):
    """One SMS this app sent (or tried to send) about an order.

    It carries enough of the state the provider owns — its message identifier
    and the current delivery outcome — that a later request can act on it
    (resend, cancel a scheduled follow-up, dispose of the content) and report
    on it, not only the request that first sent it.
    """

    KIND_ORDER_PLACED = "order_placed"
    KIND_ORDER_DISPATCHED = "order_dispatched"
    KIND_DELIVERY_FOLLOWUP = "delivery_followup"
    KIND_ORDER_CANCELLED = "order_cancelled"
    KIND_RESEND = "resend"
    KIND_CHOICES = [
        (KIND_ORDER_PLACED, "Order placed"),
        (KIND_ORDER_DISPATCHED, "Order dispatched"),
        (KIND_DELIVERY_FOLLOWUP, "Delivery follow-up"),
        (KIND_ORDER_CANCELLED, "Order cancelled"),
        (KIND_RESEND, "Operator resend"),
    ]

    # Local outcome used when the message never reached the provider at all
    # (so there is no provider SID and no provider-side status to report).
    LOCAL_STATUS_SEND_FAILED = "send_failed"

    order = models.ForeignKey(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="sms_notifications",
    )
    # The registered number this was aimed at. SET_NULL so the notification
    # history (and its provider outcome) survives if the shopper later removes
    # the number; resend, however, is refused once this is null.
    recipient = models.ForeignKey(
        ContactNumber,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    # E.164 snapshot of the destination, kept for history and reconciliation
    # matching even after the ContactNumber row is removed.
    to_number = models.CharField(max_length=32)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)

    # State the provider owns.
    provider_sid = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="")
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    # The message text. Cleared (and redacted at the provider) on content disposal.
    body = models.TextField(blank=True, default="")
    content_redacted = models.BooleanField(default=False)

    # A scheduled follow-up still held by the provider (not yet sent).
    is_scheduled = models.BooleanField(default=False)

    # Caller-supplied idempotency key for operator resend. Unique so a repeat
    # under the same key returns the existing message rather than sending again.
    idempotency_key = models.CharField(
        max_length=128, null=True, blank=True, unique=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "sms"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"OrderNotification #{self.pk} ({self.kind})"

    @property
    def reached(self):
        """Whether the provider considers the message to have reached the handset."""
        return self.status in ("delivered", "read")

    @property
    def resend_eligible(self):
        """A message that did not reach the shopper (operator may re-send)."""
        return self.status in (
            "failed",
            "undelivered",
            "canceled",
            self.LOCAL_STATUS_SEND_FAILED,
        )
