"""
Local state for PayPal payments.

PayPal remains the record of what exists; these rows record what this app
asked for (so a repeated request never reaches PayPal twice) and enough of
PayPal's ids and statuses that a later request can act on a payment.
Full card details are never stored here - only PayPal's token id and the
brand/last digits PayPal reports back.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


def new_request_id() -> str:
    """A fresh PayPal-Request-Id (a string: it is sent as a header)."""
    return str(uuid.uuid4())


class PayPalCustomer(models.Model):
    """The PayPal vault customer that holds a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_customer",
    )
    customer_id = models.CharField(max_length=64, unique=True)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.customer_id


class SavedCard(models.Model):
    """A card vaulted at PayPal for one shopper."""

    ACTIVE, DELETING, DELETED = "active", "deleting", "deleted"
    STATE_CHOICES = [(ACTIVE, "Active"), (DELETING, "Deleting"), (DELETED, "Deleted")]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_saved_cards",
    )
    token_id = models.CharField(max_length=64, unique=True)
    customer_id = models.CharField(max_length=64)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    cardholder_name = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=ACTIVE)
    date_created = models.DateTimeField(auto_now_add=True)
    date_deleted = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date_created"]

    def __str__(self) -> str:
        return f"{self.brand} ending {self.last_digits}"


class OrderPayment(models.Model):
    """PayPal payment state for one Oscar order."""

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZING = "authorizing"
    AUTHORIZED = "authorized"
    AUTHORIZATION_PENDING = "authorization_pending"
    CAPTURING = "capturing"
    CAPTURE_PENDING = "capture_pending"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDING = "voiding"
    VOIDED = "voided"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    NEEDS_REVIEW = "needs_review"

    # A new authorization may be attempted from these states.
    PAYABLE_STATES = (AWAITING_PAYMENT, FAILED, EXPIRED)
    # Money has been taken; refunds apply.
    CAPTURED_STATES = (CAPTURED, PARTIALLY_REFUNDED, REFUNDED)

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    state = models.CharField(max_length=32, default=AWAITING_PAYMENT, db_index=True)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    attempt = models.PositiveIntegerField(default=0)
    saved_card = models.ForeignKey(
        SavedCard, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    # PayPal's own clock: when the (current) authorization was created and
    # when PayPal will stop honouring it.
    authorization_time = models.DateTimeField(null=True, blank=True)
    authorization_expires = models.DateTimeField(null=True, blank=True)
    reauthorized = models.BooleanField(default=False)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    capture_time = models.DateTimeField(null=True, blank=True)

    # Refunds: ``refund_reserved`` is claimed before a refund is sent and
    # released only if PayPal definitely refused it, so concurrent partial
    # refunds can never add up to more than was captured.
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    last_error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(refund_reserved__gte=0), name="paypal_refund_reserved_non_negative"
            ),
        ]

    def __str__(self) -> str:
        return f"PayPal payment for order {self.order_id} ({self.state})"


class PaymentOperation(models.Model):
    """
    A durable claim on one PayPal write, written before the call is made.

    ``key`` identifies the logical operation (order + step + target), so a
    repeated request finds the existing claim instead of calling PayPal again.
    ``request_id`` is sent as ``PayPal-Request-Id``; resending under the same
    id is how an unknown outcome is looked up.
    """

    AUTHORIZE, REAUTHORIZE, CAPTURE, VOID, REFUND, VAULT = (
        "authorize",
        "reauthorize",
        "capture",
        "void",
        "refund",
        "vault",
    )
    SENDING, DONE, PENDING, FAILED, UNKNOWN, NEEDS_REVIEW = (
        "sending",
        "done",
        "pending",
        "failed",
        "unknown",
        "needs_review",
    )

    key = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=16)
    order_payment = models.ForeignKey(
        OrderPayment,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="operations",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    request_id = models.CharField(max_length=64, default=new_request_id)
    fingerprint = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, default=SENDING, db_index=True)
    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date_created"]

    def __str__(self) -> str:
        return f"{self.key} ({self.status})"


class PayPalRefund(models.Model):
    """One refund against a captured payment."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order_payment = models.ForeignKey(
        OrderPayment, on_delete=models.CASCADE, related_name="refunds"
    )
    operation = models.OneToOneField(
        PaymentOperation, on_delete=models.CASCADE, related_name="refund"
    )
    idempotency_key = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    refund_id = models.CharField(max_length=64, blank=True)
    # Our outcome: pending / done / failed / unknown / needs_review.
    status = models.CharField(max_length=16, default=PaymentOperation.SENDING)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date_created"]

    def __str__(self) -> str:
        return f"Refund {self.public_id} ({self.status})"
