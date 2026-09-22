from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class PayPalCustomer(models.Model):
    """Maps an Oscar user to the PayPal-generated customer id.

    Vaulted cards are attached to this customer id so PayPal keeps a single
    profile per shopper.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_customer",
    )
    customer_id = models.CharField(_("PayPal customer id"), max_length=64)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return "PayPalCustomer<%s=%s>" % (self.user_id, self.customer_id)


class SavedCard(models.Model):
    """A card a shopper vaulted with PayPal for reuse.

    Full card details never touch this database — only the PayPal vault token
    and a safe, human-recognisable description (brand + last four + expiry).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_cards",
    )
    vault_id = models.CharField(_("PayPal vault token id"), max_length=128, unique=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True, help_text="YYYY-MM")
    label = models.CharField(max_length=64, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return "%s ****%s" % (self.brand or "Card", self.last_digits or "????")

    def as_dict(self):
        return {
            "paymentMethodId": self.id,
            "brand": self.brand,
            "lastDigits": self.last_digits,
            "expiry": self.expiry,
            "label": self.label,
            "created": self.created.isoformat(),
        }


class PayPalPayment(models.Model):
    """PayPal-owned state for a single Oscar order.

    Holds enough of what PayPal owns (ids and current status for the hold, the
    capture and — via related refunds — the refunds) for a later request to act
    on it, not only the one that started it.
    """

    PENDING_PAYMENT = "PENDING_PAYMENT"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    STATUS_CHOICES = [
        (PENDING_PAYMENT, "Awaiting payment"),
        (AUTHORIZED, "Authorized (funds held)"),
        (CAPTURED, "Captured (funds taken)"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Fully refunded"),
        (CANCELLED, "Cancelled (hold released)"),
        (FAILED, "Failed"),
    ]

    order = models.OneToOneField(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="paypal_payment",
    )
    # Denormalised owner so shopper-scoping never depends on order joins.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_payments",
    )
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default=PENDING_PAYMENT
    )

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_expiry = models.DateTimeField(null=True, blank=True)

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

    # Idempotency keys (PayPal-Request-Id) persisted so a retry reuses them.
    auth_request_id = models.CharField(max_length=64, blank=True)
    capture_request_id = models.CharField(max_length=64, blank=True)

    last_error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "PayPalPayment<order=%s status=%s>" % (self.order_id, self.status)

    @property
    def total_refunded(self):
        agg = self.refunds.filter(status__in=["COMPLETED", "PENDING"]).aggregate(
            total=models.Sum("amount")
        )
        return agg["total"] or Decimal("0.00")

    @property
    def refundable_amount(self):
        if not self.captured_amount:
            return Decimal("0.00")
        return self.captured_amount - self.total_refunded

    def as_dict(self):
        return {
            "orderId": self.order_id,
            "orderNumber": self.order.number,
            "status": self.status,
            "currency": self.currency,
            "amount": str(self.amount),
            "paypalOrderId": self.paypal_order_id,
            "authorizationId": self.authorization_id,
            "authorizationStatus": self.authorization_status,
            "authorizationExpiry": (
                self.authorization_expiry.isoformat()
                if self.authorization_expiry
                else None
            ),
            "captureId": self.capture_id,
            "captureStatus": self.capture_status,
            "capturedAmount": (
                str(self.captured_amount) if self.captured_amount is not None else None
            ),
            "paypalFee": str(self.paypal_fee) if self.paypal_fee is not None else None,
            "netAmount": (
                str(self.net_amount) if self.net_amount is not None else None
            ),
            "totalRefunded": str(self.total_refunded),
            "refundableAmount": str(self.refundable_amount),
            "refunds": [r.as_dict() for r in self.refunds.all()],
        }


class PayPalRefund(models.Model):
    """A single refund against a captured PayPal payment.

    The caller-supplied idempotency key is unique per payment, so replaying a
    request under the same key never refunds twice while two distinct partial
    refunds remain legitimate.
    """

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name="refunds"
    )
    refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32, blank=True)
    idempotency_key = models.CharField(max_length=128)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created"]
        unique_together = ("payment", "idempotency_key")

    def __str__(self):
        return "PayPalRefund<%s %s>" % (self.refund_id, self.amount)

    def as_dict(self):
        return {
            "refundId": self.id,
            "paypalRefundId": self.refund_id,
            "amount": str(self.amount),
            "currency": self.currency,
            "status": self.status,
            "idempotencyKey": self.idempotency_key,
            "created": self.created.isoformat(),
        }
