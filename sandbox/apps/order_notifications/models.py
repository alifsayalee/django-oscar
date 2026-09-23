import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q

from . import status as st


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical (E.164) form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="contact_numbers", on_delete=models.CASCADE
    )
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Removal is recorded rather than the row deleted, so that past notifications
    # keep pointing at it; a removed number is never listed or messaged again.
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "phone_number"],
                condition=Q(removed_at__isnull=True),
                name="uniq_active_contact_number",
            )
        ]

    @property
    def is_active(self):
        return self.removed_at is None

    def __str__(self):
        return "ContactNumber #%s" % self.pk


def new_ref():
    return secrets.token_hex(4).upper()


class Notification(models.Model):
    """One text message this app asked the provider to send (or tried to)."""

    PLACED, DISPATCHED, FOLLOW_UP, CANCELLED = "placed", "dispatched", "follow_up", "cancelled"
    KIND_CHOICES = [
        (PLACED, "Order placed"),
        (DISPATCHED, "Order dispatched"),
        (FOLLOW_UP, "Delivery follow-up"),
        (CANCELLED, "Order cancelled"),
    ]

    order = models.ForeignKey(
        "order.Order", related_name="sms_notifications", on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="sms_notifications", on_delete=models.CASCADE
    )
    contact_number = models.ForeignKey(
        ContactNumber, related_name="notifications", null=True, on_delete=models.SET_NULL
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    # Our own reference, embedded in the message text so a send whose outcome
    # is unknown can be found at the provider.
    ref = models.CharField(max_length=16, unique=True, default=new_ref)
    body = models.TextField(blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    outcome = models.CharField(max_length=16, default=st.SENDING, db_index=True)
    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provider_status = models.CharField(max_length=32, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    # The provider's own clock, used by reconciliation.
    provider_created_at = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_sent_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    scheduled_for = models.DateTimeField(null=True, blank=True)
    # Set when the order is cancelled; a follow-up carrying it must not go out.
    cancel_requested_at = models.DateTimeField(null=True, blank=True)

    resend_of = models.ForeignKey(
        "self", related_name="resends", null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return "Notification #%s (%s)" % (self.pk, self.kind)

    @property
    def is_terminal(self):
        return self.outcome in st.TERMINAL


class ResendRequest(models.Model):
    """Claims an operator's idempotency key before anything is sent under it."""

    idempotency_key = models.CharField(max_length=128, unique=True)
    original = models.ForeignKey(
        Notification, related_name="resend_requests", on_delete=models.CASCADE
    )
    result = models.OneToOneField(
        Notification, related_name="resend_request", on_delete=models.CASCADE
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)


class OrderTransitionClaim(models.Model):
    """Only the request that inserts this row performs the transition and messages."""

    order = models.ForeignKey("order.Order", related_name="+", on_delete=models.CASCADE)
    to_status = models.CharField(max_length=64)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "to_status"], name="uniq_order_transition")
        ]
