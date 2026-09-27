from django.conf import settings
from django.db import models
from django.utils import timezone


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical (E.164) form."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="sms_contact_numbers", on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "phone_number"], name="sms_contact_number_unique_per_user"),
        ]
        ordering = ["created_at", "id"]

    def __str__(self):
        # Never render the number itself: model reprs end up in logs and tracebacks.
        return f"ContactNumber #{self.pk}"

    @property
    def masked(self):
        return f"{self.phone_number[:3]}••••{self.phone_number[-2:]}"


class ProviderWrite(models.Model):
    """The claim a provider write holds, and what became of it.

    ``reference`` is derived from the operation (never random) and is unique, so the database -- not a
    lock in this process -- decides which request gets to make the provider call.
    """

    SENDING = "sending"
    DONE = "done"
    PENDING = "pending"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    OUTCOMES = [(o, o) for o in (SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN)]

    reference = models.CharField(max_length=200, unique=True)
    outcome = models.CharField(max_length=16, choices=OUTCOMES, default=SENDING)
    provider_sid = models.CharField(max_length=64, null=True, blank=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    claimed_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class Notification(ProviderWrite):
    """One text message this app sent (or tried to send) about an order."""

    PLACED = "placed"
    DISPATCHED = "dispatched"
    FOLLOW_UP = "delivery_follow_up"
    CANCELLED = "cancelled"
    KINDS = [(k, k) for k in (PLACED, DISPATCHED, FOLLOW_UP, CANCELLED)]

    order = models.ForeignKey("order.Order", related_name="sms_notifications", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="sms_notifications", on_delete=models.CASCADE)
    # SET_NULL: deleting a number must stop every future send to it, including resends.
    contact_number = models.ForeignKey(
        ContactNumber, related_name="notifications", null=True, blank=True, on_delete=models.SET_NULL
    )
    kind = models.CharField(max_length=32, choices=KINDS)
    resend_of = models.ForeignKey("self", related_name="resends", null=True, blank=True, on_delete=models.SET_NULL)
    body = models.TextField(blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    date_sent = models.DateTimeField(null=True, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    status_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["provider_sid"]), models.Index(fields=["date_sent"])]

    def __str__(self):
        return f"Notification #{self.pk} ({self.kind}, {self.outcome})"


class NotificationAction(ProviderWrite):
    """A provider write that acts on an existing message: calling it off, or redacting its text."""

    CANCEL = "cancel"
    REDACT = "redact"
    KINDS = [(CANCEL, CANCEL), (REDACT, REDACT)]

    notification = models.ForeignKey(Notification, related_name="actions", on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, choices=KINDS)
    target_sid = models.CharField(max_length=64)

    class Meta:
        ordering = ["claimed_at", "id"]

    def __str__(self):
        return f"NotificationAction #{self.pk} ({self.kind}, {self.outcome})"
