"""PayPal-owned state for the checkout API.

Orders and their lines are Oscar's own models (``order.Order`` / ``order.Line``);
money movement is also mirrored onto Oscar's ``payment.Source`` /
``payment.Transaction``. These models hold only what PayPal owns and Oscar has
nowhere to keep: the PayPal order/authorization/capture/refund ids and statuses,
and the vaulted-card references. No full card number, CVV or expiry-with-PAN is
ever stored here.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

ZERO = Decimal("0.00")


class PayPalPayment(models.Model):
    """One-to-one companion to an Oscar order holding its PayPal payment state."""

    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"
    FAILED = "failed"
    STATUS_CHOICES = [
        (PENDING, "Awaiting payment"),
        (AUTHORIZED, "Authorized (funds held)"),
        (CAPTURED, "Captured (funds taken)"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Fully refunded"),
        (CANCELLED, "Cancelled (authorization voided)"),
        (FAILED, "Failed"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=PENDING)
    currency = models.CharField(max_length=12)

    # PayPal-owned identifiers.
    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    capture_id = models.CharField(max_length=64, blank=True)

    # Amounts, kept to the cent.
    order_total = models.DecimalField(max_digits=12, decimal_places=2)
    authorized_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    amount_refunded = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    authorization_expires_at = models.DateTimeField(null=True, blank=True)

    # Idempotency: a stable PayPal-Request-Id base per payment so a retried
    # authorize/capture is idempotent on PayPal's side as well as ours.
    request_id_base = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "paypal_checkout"

    def __str__(self):
        return f"PayPalPayment(order={self.order_id}, status={self.status})"

    @property
    def amount_available_for_refund(self):
        return self.captured_amount - self.amount_refunded


class PayPalCustomer(models.Model):
    """PayPal customer id for a shopper, so vaulted cards group under one customer.

    Populated from PayPal's response the first time the shopper vaults a card,
    then reused for every subsequent vault/list call.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer"
    )
    paypal_customer_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_checkout"

    def __str__(self):
        return f"PayPalCustomer(user={self.user_id}, id={self.paypal_customer_id})"


class SavedCard(models.Model):
    """A card the shopper vaulted with PayPal, described safely (never the PAN)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards"
    )
    paypal_vault_id = models.CharField(max_length=128, unique=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # "YYYY-MM"
    cardholder_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_checkout"
        ordering = ["-created_at"]

    def __str__(self):
        return f"SavedCard({self.brand} ****{self.last_digits}, user={self.user_id})"

    @property
    def label(self):
        parts = [p for p in [self.brand, f"ending {self.last_digits}" if self.last_digits else ""] if p]
        return " ".join(parts) or "Card"


class PayPalRefund(models.Model):
    """A refund against a captured PayPal payment.

    ``idempotency_key`` is caller-supplied; a repeated request under the same key
    returns the existing refund rather than issuing a second one, while two
    distinct keys are two legitimate partial refunds.
    """

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name="refunds"
    )
    idempotency_key = models.CharField(max_length=128)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=12)
    status = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_checkout"
        unique_together = ("payment", "idempotency_key")
        ordering = ["-created_at"]

    def __str__(self):
        return f"PayPalRefund({self.paypal_refund_id}, {self.amount} {self.currency})"
