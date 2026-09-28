from django.conf import settings
from django.db import models
from oscar.core.loading import get_model

from .outcomes import OUTCOME_CHOICES, SENDING

Order = get_model("order", "Order")


class ContactNumber(models.Model):
    """
    A shopper's mobile number, stored in the provider's canonical (E.164) form.

    Removing the row is what stops further messages: sends only ever pick a number that still exists.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sms_contact_numbers"
    )
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["user", "phone_number"], name="sms_contact_number_unique_per_user"),
        ]

    def __str__(self):
        # Never render the number itself: model reprs end up in logs.
        return "ContactNumber #%s" % self.pk


class Notification(models.Model):
    """
    One text message about an order, and the claim that guards sending it.

    ``reference`` is derived from the operation (order + kind, or original + caller's idempotency key) and is unique,
    so the database rejects a second send of the same message from any process. The row is written *before* the
    provider is called and carries the provider's own state (sid, status, times) once it answers.
    """

    ORDER_PLACED = "order_placed"
    ORDER_DISPATCHED = "order_dispatched"
    DELIVERY_FOLLOW_UP = "delivery_follow_up"
    ORDER_CANCELLED = "order_cancelled"
    KIND_CHOICES = [
        (ORDER_PLACED, "Order placed"),
        (ORDER_DISPATCHED, "Order dispatched"),
        (DELIVERY_FOLLOW_UP, "Delivery follow-up"),
        (ORDER_CANCELLED, "Order cancelled"),
    ]

    reference = models.CharField(max_length=255, unique=True)
    ref_token = models.CharField(max_length=16, db_index=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="sms_notifications")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="sms_notifications"
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    resend_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="resends"
    )
    contact_number = models.ForeignKey(
        ContactNumber, null=True, blank=True, on_delete=models.SET_NULL, related_name="notifications"
    )
    to_number = models.CharField(max_length=32, blank=True)
    body = models.TextField(blank=True)
    send_at = models.DateTimeField(null=True, blank=True)
    skip_reason = models.CharField(max_length=64, blank=True)

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    detail = models.CharField(max_length=255, blank=True)
    claimed_at = models.DateTimeField()

    provider_sid = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_error_code = models.IntegerField(null=True, blank=True)
    provider_created_at = models.DateTimeField(null=True, blank=True)
    provider_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    content_disposed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return "Notification #%s (%s)" % (self.pk, self.kind)


class ProviderAction(models.Model):
    """
    A write against a message the provider already holds: calling off a queued follow-up, or redacting its text.

    Its own unique ``reference`` is the claim for that write, held here before the provider is called.
    """

    CANCEL = "cancel"
    REDACT = "redact"
    ACTION_CHOICES = [(CANCEL, "Call off"), (REDACT, "Redact content")]

    reference = models.CharField(max_length=255, unique=True)
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="actions")
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    detail = models.CharField(max_length=255, blank=True)
    claimed_at = models.DateTimeField()
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "pk"]

    @property
    def provider_sid(self):
        # The provider id this write acts on (the message's): a call-off or redaction is keyed by it.
        return self.notification.provider_sid

    def __str__(self):
        return "ProviderAction #%s (%s)" % (self.pk, self.action)
