from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class PayPalPayment(models.Model):
    """PayPal-owned state for a single Oscar order.

    Holds the identifiers and current status that PayPal owns (the hold, the
    capture and the money breakdown) so a later request -- fulfil, cancel, refund,
    reconciliation -- can act on the order without re-deriving anything.
    """

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDED = "voided"
    FAILED = "failed"
    STATUS_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZED, "Authorized (funds held)"),
        (CAPTURED, "Captured (funds taken)"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (VOIDED, "Voided (hold released)"),
        (FAILED, "Failed"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    source = models.OneToOneField(
        "payment.Source",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paypal_payment",
    )
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default=AWAITING_PAYMENT
    )

    # Identifiers PayPal owns.
    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_expiry = models.DateTimeField(null=True, blank=True)
    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)

    # Money as PayPal reported it at capture time.
    gross_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    paypal_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    net_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    last_error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"PayPalPayment(order={self.order_id}, status={self.status})"

    @property
    def captured_amount(self):
        return self.gross_amount or Decimal("0.00")

    @property
    def total_refunded(self):
        total = self.refunds.filter(status__in=["COMPLETED", "PENDING"]).aggregate(
            total=models.Sum("amount")
        )["total"]
        return total or Decimal("0.00")

    @property
    def refundable_amount(self):
        """Amount still refundable -- never more than what was captured."""
        return self.captured_amount - self.total_refunded


class SavedCard(models.Model):
    """A card a shopper vaulted with PayPal for reuse.

    Only a safe descriptor is stored (brand, last four digits, expiry). The full
    card number and security code never touch this database.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_saved_cards",
    )
    vault_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # YYYY-MM
    cardholder_name = models.CharField(max_length=128, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"SavedCard(user={self.user_id}, {self.brand} ****{self.last_digits})"

    def as_dict(self):
        return {
            "paymentMethodId": self.vault_id,
            "brand": self.brand,
            "lastDigits": self.last_digits,
            "expiry": self.expiry,
            "cardholderName": self.cardholder_name,
            "created": self.created.isoformat(),
        }


class PayPalRefund(models.Model):
    """A refund against a captured payment, keyed by a caller idempotency key.

    The unique (payment, idempotency_key) constraint makes a repeated request under
    the same key return the existing refund rather than issuing a second one, while
    two distinct keys remain two legitimate partial refunds.
    """

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name="refunds"
    )
    idempotency_key = models.CharField(max_length=128)
    refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"],
                name="uniq_refund_idempotency_key",
            )
        ]

    def __str__(self):
        return f"PayPalRefund({self.refund_id or 'pending'}, {self.amount})"

    def as_dict(self):
        return {
            "refundId": self.refund_id,
            "amount": f"{self.amount:.2f}",
            "currency": self.currency,
            "status": self.status,
            "created": self.created.isoformat() if self.created else None,
        }
