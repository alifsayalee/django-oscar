import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


def new_install_id():
    return uuid.uuid4().hex


class Installation(models.Model):
    """
    A single row holding an identifier unique to this install.

    It prefixes every provider reference we derive, so two installs sharing one
    Twilio account can never collide on a reference.
    """

    install_id = models.CharField(max_length=64, unique=True, default=new_install_id)
    created_at = models.DateTimeField(auto_now_add=True)


class ContactNumber(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sms_contact_numbers"
    )
    # The provider's canonical (E.164) form, never what the caller typed.
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Removed numbers are kept (notifications point at them) but never shown or messaged again.
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "phone_number"],
                condition=Q(removed_at__isnull=True),
                name="order_sms_one_active_number_per_user",
            )
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return "ContactNumber #%s" % self.pk  # never the number itself


class Notification(models.Model):
    PLACED, DISPATCHED, FOLLOWUP, CANCELLED, RESEND = (
        "order_placed", "order_dispatched", "delivery_followup", "order_cancelled", "resend")
    KIND_CHOICES = [(k, k) for k in (PLACED, DISPATCHED, FOLLOWUP, CANCELLED, RESEND)]

    # Outcome of the send, as the provider last told us.
    SENDING, PENDING, DONE, FAILED, UNKNOWN, NEEDS_REVIEW = (
        "sending", "pending", "done", "failed", "unknown", "needs_review")
    OUTCOME_CHOICES = [(o, o) for o in (SENDING, PENDING, DONE, FAILED, UNKNOWN, NEEDS_REVIEW)]

    # The claim: one row per provider write, keyed by a reference derived from the operation.
    reference = models.CharField(max_length=255, unique=True)
    ref_token = models.CharField(max_length=16, db_index=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    order = models.ForeignKey("order.Order", on_delete=models.CASCADE, related_name="sms_notifications")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sms_notifications"
    )
    contact_number = models.ForeignKey(
        ContactNumber, null=True, blank=True, on_delete=models.SET_NULL, related_name="notifications"
    )
    resend_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="resends"
    )

    # Message text without the reference suffix; cleared when content is disposed of.
    text = models.TextField(blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    failure_reason = models.CharField(max_length=255, blank=True)
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    # State the provider owns, stored so later requests can act and report on it.
    provider_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provider_status = models.CharField(max_length=32, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    provider_date_created = models.DateTimeField(null=True, blank=True)
    provider_date_sent = models.DateTimeField(null=True, blank=True)
    # The provider's event time (sent, else created) — the clock reconciliation filters on.
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)

    # Calling off a scheduled follow-up.
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_outcome = models.CharField(max_length=16, blank=True)

    # Disposal of the message content (redaction at the provider).
    content_disposal_requested_at = models.DateTimeField(null=True, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["claimed_at", "id"]

    def __str__(self):
        return "Notification #%s (%s)" % (self.pk, self.kind)
