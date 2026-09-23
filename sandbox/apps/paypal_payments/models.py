"""
PayPal-owned state for Oscar orders.

Orders, lines and the payment Source/Transaction ledger are Oscar's own models;
these tables only record what PayPal holds (ids and statuses) and the durable
claims that stop one action from reaching PayPal twice. No card number or
security code is ever stored here.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class PayPalCustomer(models.Model):
    """The PayPal vault customer that a shopper's saved cards belong to."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="paypal_customer")
    paypal_customer_id = models.CharField(max_length=64, unique=True)
    date_created = models.DateTimeField(auto_now_add=True)


class SavedCard(models.Model):
    ACTIVE, DELETING, DELETED = "active", "deleting", "deleted"
    STATUS_CHOICES = [(ACTIVE, "Active"), (DELETING, "Deleting"), (DELETED, "Deleted")]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards")
    # Caller-supplied (or generated) key: a repeated save with the same key returns the same card.
    idempotency_key = models.CharField(max_length=128)
    request_id = models.UUIDField(default=uuid.uuid4, editable=False)
    paypal_token_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    name = models.CharField(max_length=255, blank=True)
    # "sending" until PayPal confirms the token; then active/deleting/deleted.
    status = models.CharField(max_length=16, default="sending")
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_created"]
        constraints = [
            models.UniqueConstraint(fields=["user", "idempotency_key"], name="savedcard_user_key_uniq"),
        ]


class PayPalPayment(models.Model):
    """
    One attempt to pay for an order with PayPal.

    The row is inserted *before* PayPal is called; the partial unique constraint
    means an order has at most one live (not failed) payment, so a double-click
    cannot authorize twice.
    """

    SENDING = "sending"
    AUTHORIZED = "authorized"
    PENDING = "pending"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    VOIDED = "voided"
    CAPTURED = "captured"

    # Step states for capture / void: "" = not started.
    STEP_SENDING = "sending"
    STEP_DONE = "done"
    STEP_PENDING = "pending"
    STEP_FAILED = "failed"
    STEP_UNKNOWN = "unknown"
    STEP_NEEDS_REVIEW = "needs_review"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order = models.ForeignKey("order.Order", on_delete=models.PROTECT, related_name="paypal_payments")
    # PayPal-Request-Id for create-order, and the client reference sent in custom_id.
    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=16, default=SENDING, db_index=True)
    saved_card = models.ForeignKey(SavedCard, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="payments")
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorize_request_id = models.UUIDField(null=True, blank=True)
    reauthorized_at = models.DateTimeField(null=True, blank=True)

    capture_state = models.CharField(max_length=16, blank=True, default="")
    capture_request_id = models.UUIDField(null=True, blank=True)
    capture_claimed_at = models.DateTimeField(null=True, blank=True)
    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    void_state = models.CharField(max_length=16, blank=True, default="")
    void_request_id = models.UUIDField(null=True, blank=True)
    void_claimed_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)

    # Sum of refunds that are done, pending, in flight or unknown. Never exceeds captured_amount.
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=3, default=0)

    last_error = models.CharField(max_length=512, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_created"]
        constraints = [
            models.UniqueConstraint(fields=["order"], condition=~Q(status="failed"),
                                    name="paypalpayment_one_live_per_order"),
        ]

    @property
    def custom_id(self):
        return "%s:%s" % (self.order.number, self.request_id.hex)


class PayPalRefund(models.Model):
    SENDING, DONE, PENDING, FAILED, UNKNOWN, NEEDS_REVIEW = (
        "sending", "done", "pending", "failed", "unknown", "needs_review")

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.PROTECT, related_name="refunds")
    idempotency_key = models.CharField(max_length=128)
    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=16, default=SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=512, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date_created"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "idempotency_key"], name="paypalrefund_key_uniq"),
        ]
