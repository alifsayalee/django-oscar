"""Local persistence for the PayPal checkout API.

These rows are *additive*. PayPal remains the record of what exists (holds,
captures, refunds, vaulted cards); these rows record *that we asked* and carry
the provider ids/statuses a later request needs to act on. They also form the
durable claim that makes the payment operations idempotent under a double-click.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models


class PayPalPayment(models.Model):
    """PayPal payment state for a single Oscar order (one-to-one)."""

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDED = "voided"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    STATUS_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZED, "Authorized (funds held)"),
        (CAPTURED, "Captured (funds taken)"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (VOIDED, "Voided (hold released)"),
        (FAILED, "Failed"),
        (NEEDS_REVIEW, "Needs review"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment",
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=AWAITING_PAYMENT)

    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # A globally-unique invoice id we send to PayPal (the account requires
    # invoice ids to be unique per transaction). Stored so an idempotent retry
    # reuses the same value, and so reconciliation can match on it.
    paypal_invoice_id = models.CharField(max_length=127, blank=True)

    # Ids and current status of the state PayPal owns.
    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_expiry = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # The provider's own clock for the capture, recorded for reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "paypal_checkout"
        ordering = ["-created"]

    def __str__(self):
        return f"PayPalPayment(order={self.order_id}, status={self.status})"

    @property
    def refunded_total(self) -> Decimal:
        """Sum of all non-failed refunds against this payment's capture."""
        total = Decimal("0")
        for refund in self.refunds.all():
            if refund.status != PayPalRefund.FAILED:
                total += refund.amount
        return total

    @property
    def refundable_remaining(self) -> Decimal:
        if self.captured_value is None:
            return Decimal("0")
        return self.captured_value - self.refunded_total


class PayPalRefund(models.Model):
    """A single refund against a captured payment.

    ``(payment, idempotency_key)`` is unique: it is the durable claim that makes
    a refund idempotent under a repeated caller-supplied key, while two distinct
    keys remain two legitimate partial refunds.
    """

    COMPLETED = "completed"
    PENDING = "pending"
    FAILED = "failed"

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name="refunds",
    )
    idempotency_key = models.CharField(max_length=128)
    refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32, default=PENDING)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_checkout"
        ordering = ["created"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"],
                name="uniq_refund_idempotency_key",
            )
        ]

    def __str__(self):
        return f"PayPalRefund(payment={self.payment_id}, amount={self.amount})"


class SavedCard(models.Model):
    """A shopper's vaulted card. Full card details are never stored here — only a
    safe description (brand + last digits + expiry) and the PayPal token id."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards",
    )
    paypal_token_id = models.CharField(max_length=64)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # YYYY-MM
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_checkout"
        ordering = ["-created"]

    def __str__(self):
        return f"SavedCard(user={self.user_id}, {self.brand} ****{self.last_digits})"
