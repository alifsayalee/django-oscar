"""
Persistence for order SMS notifications.

The provider (Twilio) is the record of what exists; these rows record what
this application asked for, so every send, cancel and redaction can be found,
retried under the same claim and reported on by a later request.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical form."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="sms_contact_numbers", on_delete=models.CASCADE
    )
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Soft delete: notifications keep pointing at the number they went to,
    # but nothing is ever sent to a deleted number again.
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "phone_number"],
                condition=Q(deleted_at__isnull=True),
                name="sms_contact_unique_active_number",
            )
        ]
        ordering = ["created_at", "pk"]

    def __str__(self):
        # Never render the number itself - this ends up in logs and admin.
        return "ContactNumber #%s" % self.pk

    @property
    def masked(self):
        n = self.phone_number
        return n[:2] + "*" * max(len(n) - 4, 0) + n[-2:] if len(n) > 4 else "****"


class Notification(models.Model):
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

    # Local outcome. "sending" is our own in-flight claim; every other value is
    # derived from what the provider said (see status.py).
    SENDING = "sending"
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"
    STATUS_CHOICES = [
        (SENDING, "Sending"),
        (PENDING, "Pending"),
        (DELIVERED, "Delivered"),
        (FAILED, "Failed"),
        (CANCELED, "Canceled"),
        (UNKNOWN, "Unknown"),
    ]
    FINAL_STATUSES = (DELIVERED, FAILED, CANCELED)

    CANCEL_NONE = ""
    CANCEL_REQUESTED = "requested"
    CANCEL_DONE = "canceled"
    CANCEL_TOO_LATE = "too_late"
    CANCEL_CHOICES = [
        (CANCEL_NONE, "Not requested"),
        (CANCEL_REQUESTED, "Requested"),
        (CANCEL_DONE, "Canceled"),
        (CANCEL_TOO_LATE, "Too late - already sent"),
    ]

    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order = models.ForeignKey(
        "order.Order", related_name="sms_notifications", on_delete=models.CASCADE
    )
    contact_number = models.ForeignKey(
        ContactNumber, related_name="notifications", on_delete=models.PROTECT
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    resend_of = models.ForeignKey(
        "self", null=True, blank=True, related_name="resends", on_delete=models.PROTECT
    )
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    error_code = models.IntegerField(null=True, blank=True)
    # Why we could not settle (e.g. "provider_rejected", "outcome_unknown").
    failure_reason = models.CharField(max_length=64, blank=True)

    scheduled_for = models.DateTimeField(null=True, blank=True)
    provider_date_created = models.DateTimeField(null=True, blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True)

    cancel_state = models.CharField(
        max_length=16, choices=CANCEL_CHOICES, default=CANCEL_NONE, blank=True
    )
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One automatic message per order event per destination: the claim
            # that stops a double-submitted transition sending twice.
            models.UniqueConstraint(
                fields=["order", "kind", "contact_number"],
                condition=Q(resend_of__isnull=True),
                name="sms_notification_one_per_event",
            ),
            # One resend per caller-supplied idempotency key.
            models.UniqueConstraint(
                fields=["resend_of", "idempotency_key"],
                condition=Q(resend_of__isnull=False),
                name="sms_notification_resend_key",
            ),
        ]
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["provider_date_sent"])]

    def __str__(self):
        return "Notification #%s (%s, %s)" % (self.pk, self.kind, self.status)

    @property
    def short_ref(self):
        return self.reference.hex[:10].upper()


class OrderTransitionClaim(models.Model):
    """
    Written in the same transaction as the host's ``Order.set_status`` so that
    exactly one request performs a transition - and fires its messages.
    """

    order = models.ForeignKey(
        "order.Order", related_name="sms_transition_claims", on_delete=models.CASCADE
    )
    to_status = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "to_status"], name="sms_transition_claim_unique"
            )
        ]
