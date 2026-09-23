"""Persistence for the PayPal integration.

These models do NOT duplicate Oscar's order/line/payment models -- they record
the state PayPal owns (vault ids, authorization/capture/refund ids and their
current status) and act as the durable idempotency claim for each payment
action. Full card details (PAN/CVV) are never stored.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models


class PayPalCustomer(models.Model):
    """One PayPal customer id per shopper, so their vaulted cards group together."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_customer",
    )
    paypal_customer_id = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_api"

    def __str__(self):
        return f"PayPalCustomer<{self.user_id}={self.paypal_customer_id}>"


class PaymentMethod(models.Model):
    """A saved (vaulted) card belonging to a shopper. Safe display data only."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_payment_methods",
    )
    paypal_vault_id = models.CharField(max_length=255, unique=True)
    paypal_customer_id = models.CharField(max_length=255, blank=True)
    brand = models.CharField(max_length=64, blank=True)
    last_digits = models.CharField(max_length=8, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # "YYYY-MM"
    cardholder_name = models.CharField(max_length=255, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_api"
        ordering = ["-created"]

    def __str__(self):
        return f"{self.brand} ****{self.last_digits} ({self.expiry})"

    def as_dict(self):
        return {
            "paymentMethodId": self.pk,
            "brand": self.brand,
            "last4": self.last_digits,
            "expiry": self.expiry,
            "cardholderName": self.cardholder_name,
            "created": self.created.isoformat(),
        }


class OrderPayment(models.Model):
    """The PayPal payment state for one Oscar order, and the idempotency claim.

    ``status`` is our own coarse lifecycle; the PayPal-owned ids and their
    reported statuses are stored alongside so a later request can act on them.
    """

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"
    FAILED = "failed"
    STATUS_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZED, "Authorized"),
        (CAPTURED, "Captured"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (CANCELLED, "Cancelled"),
        (FAILED, "Failed"),
    ]

    order = models.OneToOneField(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="paypal_payment",
    )
    currency = models.CharField(max_length=12)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=AWAITING_PAYMENT)

    paypal_order_id = models.CharField(max_length=255, blank=True)
    authorization_id = models.CharField(max_length=255, blank=True)
    auth_status = models.CharField(max_length=64, blank=True)
    auth_expiry = models.CharField(max_length=64, blank=True)

    capture_id = models.CharField(max_length=255, blank=True)
    capture_status = models.CharField(max_length=64, blank=True)
    captured_value = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # PayPal's own clock for the capture event, for reconciliation.
    paypal_event_time = models.DateTimeField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "paypal_api"

    def __str__(self):
        return f"OrderPayment<{self.order_id} {self.status}>"

    @property
    def amount_refunded(self):
        total = Decimal("0.00")
        for refund in self.refunds.all():
            if refund.is_settled:
                total += refund.amount
        return total

    @property
    def amount_available_for_refund(self):
        if self.captured_value is None:
            return Decimal("0.00")
        return self.captured_value - self.amount_refunded

    def as_dict(self):
        return {
            "orderId": self.order_id,
            "orderNumber": self.order.number,
            "status": self.status,
            "currency": self.currency,
            "amount": str(self.amount),
            "paypalOrderId": self.paypal_order_id,
            "authorizationId": self.authorization_id,
            "authorizationStatus": self.auth_status,
            "captureId": self.capture_id,
            "captureStatus": self.capture_status,
            "capturedAmount": None if self.captured_value is None else str(self.captured_value),
            "paypalFee": None if self.paypal_fee is None else str(self.paypal_fee),
            "netAmount": None if self.net_value is None else str(self.net_value),
            "amountRefunded": str(self.amount_refunded),
            "amountAvailableForRefund": str(self.amount_available_for_refund),
        }


class PaymentRefund(models.Model):
    """A refund against a captured payment, keyed by the caller's idempotency key."""

    payment = models.ForeignKey(
        OrderPayment,
        on_delete=models.CASCADE,
        related_name="refunds",
    )
    idempotency_key = models.CharField(max_length=255)
    paypal_refund_id = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=64, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_api"
        ordering = ["created"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"],
                name="uniq_refund_idempotency_key_per_payment",
            )
        ]

    def __str__(self):
        return f"PaymentRefund<{self.paypal_refund_id or 'pending'} {self.amount}>"

    @property
    def is_settled(self):
        # COMPLETED and PENDING both ring-fence money against the capture;
        # only an outright failure/cancellation frees it.
        return self.status.upper() not in ("FAILED", "CANCELLED")

    def as_dict(self):
        return {
            "refundId": self.pk,
            "paypalRefundId": self.paypal_refund_id,
            "amount": str(self.amount),
            "status": self.status,
            "created": self.created.isoformat(),
        }
