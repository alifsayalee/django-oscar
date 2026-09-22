"""Database models for the PayPal payments API.

These models sit *alongside* Oscar's own models. ``OrderPayment`` links a real
Oscar ``order.Order`` (reused, not duplicated) to the state PayPal owns — the
authorization, the capture and the refunds — so a later request can act on a
payment, not only the one that created it.

Full card numbers are never stored here: only PayPal's vault token id plus the
safe descriptors PayPal returns (brand + last four digits).
"""
from decimal import Decimal

from django.conf import settings
from django.db import models


class PayPalCustomer(models.Model):
    """Maps a local user to their PayPal-generated vault customer id.

    All of a shopper's saved cards are grouped under one PayPal customer id so
    they can be listed and reused consistently.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_customer",
    )
    paypal_customer_id = models.CharField(max_length=64)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PayPalCustomer<{self.user_id}={self.paypal_customer_id}>"


class SavedPaymentMethod(models.Model):
    """A card a shopper vaulted for reuse. Belongs to exactly one shopper."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_payment_methods",
    )
    paypal_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # "YYYY-MM"
    label = models.CharField(max_length=128, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_created",)

    def __str__(self):
        return f"{self.brand} ****{self.last_digits} (user {self.user_id})"

    def describe(self):
        """A safe, shopper-recognisable description — never full card details."""
        return {
            "paymentMethodId": self.pk,
            "brand": self.brand,
            "last_digits": self.last_digits,
            "expiry": self.expiry,
            "label": self.label,
            "created": self.date_created.isoformat(),
        }


class OrderPayment(models.Model):
    """The PayPal payment state attached to an Oscar order."""

    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDED = "voided"
    FAILED = "failed"
    STATUS_CHOICES = [
        (PENDING, "Awaiting payment"),
        (AUTHORIZED, "Authorized (funds held)"),
        (CAPTURED, "Captured (funds taken)"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Fully refunded"),
        (VOIDED, "Voided (hold released)"),
        (FAILED, "Failed"),
    ]

    order = models.OneToOneField(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="paypal_payment",
    )
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=PENDING)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_expiry = models.CharField(max_length=40, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    paypal_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    net_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    last_error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"OrderPayment<order={self.order_id} {self.status}>"

    @property
    def total_refunded(self):
        agg = self.refunds.aggregate(total=models.Sum("amount"))
        return agg["total"] or Decimal("0.00")

    @property
    def refundable_amount(self):
        """How much of the capture can still be refunded."""
        if self.captured_amount is None:
            return Decimal("0.00")
        return self.captured_amount - self.total_refunded

    def describe(self):
        return {
            "status": self.status,
            "currency": self.currency,
            "amount": f"{self.amount:.2f}",
            "paypal_order_id": self.paypal_order_id,
            "authorization": {
                "id": self.authorization_id,
                "status": self.authorization_status,
                "expiry": self.authorization_expiry,
            }
            if self.authorization_id
            else None,
            "capture": {
                "id": self.capture_id,
                "status": self.capture_status,
                "captured_amount": None
                if self.captured_amount is None
                else f"{self.captured_amount:.2f}",
                "paypal_fee": None
                if self.paypal_fee is None
                else f"{self.paypal_fee:.2f}",
                "net_amount": None
                if self.net_amount is None
                else f"{self.net_amount:.2f}",
            }
            if self.capture_id
            else None,
            "refunds": [r.describe() for r in self.refunds.all()],
            "total_refunded": f"{self.total_refunded:.2f}",
        }


class PaymentRefund(models.Model):
    """A single refund against a captured payment.

    ``idempotency_key`` is caller-supplied: repeating a request under the same
    key returns the same refund, while two distinct keys are two real refunds.
    """

    payment = models.ForeignKey(
        OrderPayment, on_delete=models.CASCADE, related_name="refunds"
    )
    refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=32, blank=True)
    idempotency_key = models.CharField(max_length=128)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("payment", "idempotency_key")
        ordering = ("date_created",)

    def __str__(self):
        return f"PaymentRefund<{self.refund_id} {self.amount}>"

    def describe(self):
        return {
            "refundId": self.refund_id,
            "amount": f"{self.amount:.2f}",
            "status": self.status,
            "created": self.date_created.isoformat(),
        }
