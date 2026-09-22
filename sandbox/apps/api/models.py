"""PayPal-specific persisted state.

The order itself is Oscar's ``order.Order`` (+ ``order.Line``); these models only hold
the state PayPal owns — the ids and current status of the hold, the capture and each
refund — plus the shopper's vaulted cards. Full card numbers are NEVER stored here.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models


class PayPalPayment(models.Model):
    """The PayPal money-movement state for a single Oscar order (one-to-one)."""

    # Lifecycle states this app tracks (distinct from raw PayPal statuses, which we also keep).
    PENDING = "pending"                     # order placed, no hold yet
    AUTHORIZED = "authorized"               # money held, not taken
    CAPTURED = "captured"                   # money taken (fulfilled)
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDED = "voided"                       # hold released (cancelled)
    FAILED = "failed"
    STATE_CHOICES = [
        (PENDING, "Awaiting payment"),
        (AUTHORIZED, "Authorized"),
        (CAPTURED, "Captured"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (VOIDED, "Voided"),
        (FAILED, "Failed"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment")
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=PENDING)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # Globally-unique, stable-per-order reference used as the PayPal invoice_id and as the
    # seed for idempotency keys. The PayPal account requires unique invoice ids across ALL
    # transactions, and a reused PayPal-Request-Id returns a cached response — so this must
    # never repeat, even across database resets, yet stay fixed for a given order.
    reference = models.CharField(max_length=64, unique=True)

    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Deterministic idempotency keys (PayPal-Request-Id) so a double-click cannot
    # authorize or capture the shopper twice.
    authorize_request_id = models.CharField(max_length=100, blank=True)
    capture_request_id = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "paypal_api"

    def __str__(self) -> str:
        return f"PayPalPayment(order={self.order_id}, state={self.state})"

    @property
    def total_refunded(self) -> Decimal:
        agg = self.refunds.filter(
            status__in=[PayPalRefund.COMPLETED, PayPalRefund.PENDING]
        ).aggregate(total=models.Sum("amount"))
        return (agg["total"] or Decimal("0")).quantize(Decimal("0.01"))

    @property
    def refundable_amount(self) -> Decimal:
        """Never more than what was captured minus what has already been refunded."""
        remaining = (self.captured_amount or Decimal("0.00")) - self.total_refunded
        return remaining.quantize(Decimal("0.01"))


class PayPalRefund(models.Model):
    COMPLETED = "COMPLETED"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name="refunds")
    refund_id = models.CharField(max_length=64, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32)
    # Caller-supplied key: the same key must not refund twice; distinct keys may.
    idempotency_key = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_api"
        unique_together = [("payment", "idempotency_key")]

    def __str__(self) -> str:
        return f"PayPalRefund({self.refund_id}, {self.amount} {self.currency})"


class PayPalCustomer(models.Model):
    """Maps a shopper to their PayPal Vault customer id, reused across saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer")
    paypal_customer_id = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_api"


class SavedCard(models.Model):
    """A shopper's vaulted card. Only safe descriptors are stored — never the PAN."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards")
    vault_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # YYYY-MM
    cardholder_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_api"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"SavedCard({self.brand} ****{self.last_digits}, user={self.user_id})"

    def describe(self) -> dict:
        return {
            "paymentMethodId": self.id,
            "brand": self.brand,
            "last4": self.last_digits,
            "expiry": self.expiry,
            "cardholderName": self.cardholder_name,
            "label": f"{self.brand or 'Card'} ending {self.last_digits}",
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }
