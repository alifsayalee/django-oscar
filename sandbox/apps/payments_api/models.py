"""Durable rows for the PayPal integration.

These records never hold full card details (PAN/CVV) and are never the record of *what
exists* at PayPal — PayPal stays authoritative for money movement. They record *that we
asked*, plus the PayPal-owned identifiers and current statuses a later request needs to act
on (hold, capture, refunds), and they act as the idempotency claim for each operation.
"""
from django.conf import settings
from django.db import models


class PayPalCustomer(models.Model):
    """One PayPal customer id per shopper, so a shopper's vaulted cards group together."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer"
    )
    customer_id = models.CharField(max_length=64, unique=True)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"PayPalCustomer(user={self.user_id}, customer_id={self.customer_id})"


class SavedCard(models.Model):
    """A vaulted card belonging to one shopper. Full card details are never stored here."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards"
    )
    paypal_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # 'YYYY-MM'
    name = models.CharField(max_length=128, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]

    def as_dict(self) -> dict:
        return {
            "paymentMethodId": self.id,
            "brand": self.brand,
            "lastDigits": self.last_digits,
            "expiry": self.expiry,
            "cardholderName": self.name,
            "created": self.created.isoformat(),
        }


class OrderPayment(models.Model):
    """One durable payment row per Oscar order. Also the claim for authorize/capture/void."""

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZING = "authorizing"
    AUTHORIZED = "authorized"
    CAPTURING = "capturing"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CANCELLING = "cancelling"
    VOIDED = "voided"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"

    STATUS_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZING, "Authorizing"),
        (AUTHORIZED, "Authorized (funds held)"),
        (CAPTURING, "Capturing"),
        (CAPTURED, "Captured"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (CANCELLING, "Cancelling"),
        (VOIDED, "Voided (hold released)"),
        (FAILED, "Failed"),
        (NEEDS_REVIEW, "Needs review"),
        (UNKNOWN, "Unknown"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="order_payments"
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    invoice_id = models.CharField(max_length=127, unique=True)

    # Authorization (the hold)
    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    auth_status = models.CharField(max_length=32, blank=True)
    auth_expiry = models.CharField(max_length=40, blank=True)
    authorize_request_id = models.CharField(max_length=64, blank=True, null=True, unique=True)

    # Capture
    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    capture_request_id = models.CharField(max_length=64, blank=True)
    capture_time = models.DateTimeField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created"]

    @property
    def refunded_total(self):
        from decimal import Decimal

        agg = self.refunds.filter(status__in=["COMPLETED", "PENDING"]).aggregate(
            models.Sum("amount")
        )
        return agg["amount__sum"] or Decimal("0.00")

    def payment_state(self) -> dict:
        return {
            "status": self.status,
            "currency": self.currency,
            "amount": str(self.amount),
            "invoiceId": self.invoice_id,
            "paypalOrderId": self.paypal_order_id or None,
            "authorizationId": self.authorization_id or None,
            "authorizationStatus": self.auth_status or None,
            "authorizationExpiry": self.auth_expiry or None,
            "captureId": self.capture_id or None,
            "captureStatus": self.capture_status or None,
            "capturedAmount": str(self.captured_value) if self.captured_value is not None else None,
            "paypalFee": str(self.paypal_fee) if self.paypal_fee is not None else None,
            "netAmount": str(self.net_amount) if self.net_amount is not None else None,
            "refundedTotal": str(self.refunded_total),
        }


class PaymentRefund(models.Model):
    """A refund against a captured payment. The idempotency key makes a repeat a no-op."""

    order_payment = models.ForeignKey(
        OrderPayment, on_delete=models.CASCADE, related_name="refunds"
    )
    idempotency_key = models.CharField(max_length=128)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=32, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("order_payment", "idempotency_key")]
        ordering = ["-created"]

    def as_dict(self) -> dict:
        return {
            "refundId": self.id,
            "paypalRefundId": self.paypal_refund_id or None,
            "amount": str(self.amount),
            "status": self.status,
            "idempotencyKey": self.idempotency_key,
            "created": self.created.isoformat(),
        }
