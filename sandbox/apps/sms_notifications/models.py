from django.conf import settings
from django.db import models
from django.db.models import Q


class ContactNumber(models.Model):
    """A shopper's mobile number, stored in the provider's canonical (E.164) form."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="sms_contact_numbers")
    phone_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Soft delete: the number stops being listed and used, while the history of
    # what was sent to it survives.
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["user", "phone_number"], condition=Q(deleted_at__isnull=True),
                                    name="sms_contact_number_active_unique"),
        ]

    def __str__(self):
        return "ContactNumber #%s" % self.pk  # never the number itself


class SmsNotification(models.Model):
    """One message this app asked the provider to send - and the claim that it asked.

    The row is committed (``outcome=sending``) *before* the provider call, under a
    unique ``reference``; a second request for the same reference loses the insert
    and is answered from this row instead of sending again.
    """

    KIND_PLACED, KIND_DISPATCHED, KIND_FOLLOWUP, KIND_CANCELLED = "placed", "dispatched", "followup", "cancelled"
    KIND_CHOICES = [(KIND_PLACED, "Order placed"), (KIND_DISPATCHED, "Order dispatched"),
                    (KIND_FOLLOWUP, "Delivery follow-up"), (KIND_CANCELLED, "Order cancelled")]

    SENDING, PENDING, DONE, FAILED, UNKNOWN = "sending", "pending", "done", "failed", "unknown"
    OUTCOME_CHOICES = [(SENDING, "Claimed, no provider answer yet"), (PENDING, "Accepted, not delivered yet"),
                       (DONE, "Delivered"), (FAILED, "Failed"), (UNKNOWN, "Unknown - may have been sent")]

    order = models.ForeignKey("order.Order", on_delete=models.CASCADE, related_name="sms_notifications")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="sms_notifications")
    contact_number = models.ForeignKey(ContactNumber, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="notifications")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    resend_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="resends")

    reference = models.CharField(max_length=255, unique=True)
    ref_token = models.CharField(max_length=16)
    to_number = models.CharField(max_length=32)
    body = models.TextField(blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    claimed_at = models.DateTimeField()

    # State the provider owns, as last read from it.
    provider_sid = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    error_code = models.IntegerField(null=True, blank=True)
    provider_sent_at = models.DateTimeField(null=True, blank=True)
    provider_created_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    # Calling off a scheduled follow-up: "", pending, done, failed, unknown.
    cancel_state = models.CharField(max_length=16, blank=True)
    content_disposed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return "SmsNotification #%s (%s)" % (self.pk, self.kind)
