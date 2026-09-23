from django.conf import settings
from django.db import models
from django.utils import timezone


class NotificationStatus(models.TextChoices):
    """Our own delivery outcome, mapped from Twilio's message status.

    Twilio owns the raw status; :func:`apps.smsnotify.status.map_status` folds it
    into exactly these values so the rest of the app never branches on a raw
    provider string. ``UNKNOWN`` is the default arm -- a value the SDK/enum did
    not list, which is neither delivered nor failed.
    """

    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"
    CANCELED = "canceled", "Canceled"
    PARTIAL = "partial", "Partially delivered"
    UNKNOWN = "unknown", "Unknown"


#: Statuses that mean the message demonstrably reached the handset. A resend is
#: refused for these; everything else is fair game for an operator resend.
REACHED_STATUSES = frozenset({NotificationStatus.DELIVERED, NotificationStatus.SENT})

#: Statuses that are still "live" at the provider (not a terminal outcome), so a
#: status refresh is worth a fetch_message round-trip.
NON_TERMINAL_STATUSES = frozenset({NotificationStatus.PENDING, NotificationStatus.UNKNOWN})


class ContactNumber(models.Model):
    """A mobile number a shopper has put on file, in the provider's canonical form.

    The stored value is always the E.164 form Twilio's Lookup returned -- never
    what the caller typed. A number belongs to exactly one shopper.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="sms_contact_numbers",
        on_delete=models.CASCADE,
    )
    phone_number = models.CharField(max_length=32, help_text="Canonical E.164 form from Twilio Lookup")
    created = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        app_label = "smsnotify"
        ordering = ("-created",)
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "phone_number"), name="uniq_owner_phone_number"
            )
        ]

    def __str__(self):
        # Deliberately does NOT include the number: shopper numbers are never logged.
        return f"ContactNumber #{self.pk} of user {self.owner_id}"


class NotificationCategory(models.TextChoices):
    PLACED = "placed", "Order placed"
    DISPATCHED = "dispatched", "Order dispatched"
    CANCELLED = "cancelled", "Order cancelled"
    DELIVERY_FOLLOWUP = "delivery_followup", "Delivery follow-up"
    RESEND = "resend", "Operator resend"


class Notification(models.Model):
    """One SMS the app tried to send about an order, and what became of it.

    Carries enough provider-owned state (the Message SID and the current delivery
    status/error) that a later request can act on it -- refresh it, resend it,
    cancel a scheduled follow-up, or redact its content -- not just the request
    that first sent it.
    """

    order = models.ForeignKey(
        "order.Order", related_name="sms_notifications", on_delete=models.CASCADE
    )
    category = models.CharField(max_length=32, choices=NotificationCategory.choices)

    #: E.164 snapshot of the destination at send time. Kept even if the
    #: ContactNumber row is later deleted, so the log survives.
    recipient = models.CharField(max_length=32)
    contact_number = models.ForeignKey(
        ContactNumber,
        related_name="notifications",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    #: The text that was sent. Nulled (and redacted provider-side) on content disposal.
    body = models.TextField(null=True, blank=True)

    #: Provider-owned identity + outcome.
    provider_sid = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    twilio_status = models.CharField(max_length=32, null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=NotificationStatus.choices,
        default=NotificationStatus.PENDING,
    )
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    #: A follow-up scheduled with the provider for later (not sent immediately).
    is_scheduled = models.BooleanField(default=False)
    send_at = models.DateTimeField(null=True, blank=True)
    canceled = models.BooleanField(default=False)

    content_redacted = models.BooleanField(default=False)

    #: Operator resend idempotency. Unique so a repeat under the same key cannot
    #: produce a second message (claim-first; see services.resend_notification).
    idempotency_key = models.CharField(
        max_length=128, null=True, blank=True, unique=True
    )
    resent_from = models.ForeignKey(
        "self", related_name="resends", null=True, blank=True, on_delete=models.SET_NULL
    )

    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "smsnotify"
        ordering = ("created", "id")

    def __str__(self):
        return f"Notification #{self.pk} ({self.category}) for order {self.order_id}"

    @property
    def reached(self):
        return self.status in REACHED_STATUSES
