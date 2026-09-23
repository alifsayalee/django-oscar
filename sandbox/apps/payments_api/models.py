"""Persistence for the PayPal integration.

These models hold the PayPal-owned state (ids and current status for the hold,
the capture and the refunds) so that a later request can act on a payment, not
only the one that started it. Full card details are **never** stored here — card
numbers and CVVs flow straight through into the PayPal SDK request and are dropped.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models


class PaymentStatus(models.TextChoices):
    PENDING_PAYMENT = "PENDING_PAYMENT", "Awaiting payment"
    AUTHORIZED = "AUTHORIZED", "Authorized (funds held)"
    CAPTURED = "CAPTURED", "Captured (funds taken)"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
    REFUNDED = "REFUNDED", "Fully refunded"
    VOIDED = "VOIDED", "Voided (hold released)"
    FAILED = "FAILED", "Failed"


class PayPalCustomer(models.Model):
    """The stable PayPal Vault customer id for a shopper.

    PayPal generates a customer id the first time a card is vaulted; we store it
    so every subsequent saved card for the same shopper is filed under the same
    PayPal customer, and so we can list that customer's tokens.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer"
    )
    customer_id = models.CharField(max_length=64, unique=True)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PayPalCustomer<{self.user_id}={self.customer_id}>"


class SavedPaymentMethod(models.Model):
    """A card a shopper has vaulted for reuse. Holds only a safe description."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_payment_methods"
    )
    payment_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # ISO-8601 YYYY-MM
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.brand} ****{self.last_digits}"

    def describe(self):
        return {
            "paymentMethodId": self.payment_token_id,
            "brand": self.brand or None,
            "last_digits": self.last_digits or None,
            "expiry": self.expiry or None,
            "created": self.created.isoformat(),
        }


class PayPalPayment(models.Model):
    """The PayPal payment state attached to an Oscar order."""

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_payments"
    )
    status = models.CharField(
        max_length=32, choices=PaymentStatus.choices, default=PaymentStatus.PENDING_PAYMENT
    )
    currency = models.CharField(max_length=3)

    # PayPal-owned identifiers.
    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    capture_id = models.CharField(max_length=64, blank=True)

    # Amounts, all to the currency's own precision.
    order_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    authorized_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Idempotency keys we send to PayPal (PayPal-Request-Id) so a retried request
    # is de-duplicated on PayPal's side as well as ours.
    authorize_request_id = models.CharField(max_length=64, blank=True)
    capture_request_id = models.CharField(max_length=64, blank=True)

    last_error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"PayPalPayment<order={self.order_id} status={self.status}>"

    @property
    def refundable_remaining(self) -> Decimal:
        """How much of the captured amount can still be refunded."""
        captured = self.captured_amount or Decimal("0.00")
        return captured - self.refunded_amount

    def describe(self):
        return {
            "status": self.status,
            "currency": self.currency,
            "order_total": str(self.order_total),
            "paypal_order_id": self.paypal_order_id or None,
            "authorization_id": self.authorization_id or None,
            "capture_id": self.capture_id or None,
            "authorized_amount": _dec(self.authorized_amount),
            "captured_amount": _dec(self.captured_amount),
            "paypal_fee": _dec(self.paypal_fee),
            "net_amount": _dec(self.net_amount),
            "refunded_amount": str(self.refunded_amount),
            "refundable_remaining": str(self.refundable_remaining),
        }


class PayPalRefund(models.Model):
    """A single refund against a captured payment.

    ``idempotency_key`` is caller-supplied: repeating a refund request under the
    same key returns this same row rather than refunding twice, while two distinct
    keys are two legitimate partial refunds.
    """

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name="refunds"
    )
    idempotency_key = models.CharField(max_length=128)
    refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=32, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("payment", "idempotency_key")]
        ordering = ["created"]

    def describe(self):
        return {
            "refundId": self.refund_id or None,
            "amount": str(self.amount),
            "status": self.status or None,
            "idempotency_key": self.idempotency_key,
            "created": self.created.isoformat(),
        }


def _dec(value):
    return str(value) if value is not None else None
