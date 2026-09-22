"""Sidecar models holding the PayPal-owned state that Oscar's own models cannot.

Orders and order lines are Oscar's ``order.Order`` / ``order.Line``; money
movement is mirrored into Oscar's ``payment.Source`` / ``payment.Transaction``.
These sidecars carry only what PayPal owns and Oscar has no field for: the
provider ids and current status for the hold / capture / refunds, the captured
fee and net proceeds, the vault token behind a saved card, and the refund
idempotency ledger. No PAN or CVV is ever stored here.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from . import money as money_utils


class SavedCard(models.Model):
    """A card a shopper vaulted with PayPal, described safely for recognition."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_saved_cards",
    )
    # PayPal vault payment-token id — the only handle we keep for the card.
    vault_id = models.CharField(max_length=255, unique=True)
    brand = models.CharField(max_length=64, blank=True)
    last4 = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # YYYY-MM
    label = models.CharField(max_length=128, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_api"
        ordering = ["-created"]

    def __str__(self):
        return self.label or f"{self.brand} ****{self.last4}"

    def describe(self) -> dict:
        return {
            "paymentMethodId": self.id,
            "brand": self.brand,
            "last4": self.last4,
            "expiry": self.expiry,
            "label": self.label or f"{self.brand} ending {self.last4}".strip(),
            "created": self.created.isoformat(),
        }


class PayPalPayment(models.Model):
    """Authoritative PayPal payment state for one Oscar order."""

    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    STATUS_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZED, "Authorized (funds held)"),
        (CAPTURED, "Captured (fulfilled)"),
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
    source = models.ForeignKey(
        "payment.Source",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    saved_card = models.ForeignKey(
        SavedCard,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )

    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=3)

    # PayPal-owned identifiers/state for a later request to act on.
    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)

    # Advances only when an authorization attempt is recorded as failed, so a
    # genuine re-attempt uses a fresh PayPal-Request-Id while concurrent
    # duplicates of the same attempt still dedupe to one authorization.
    pay_attempts = models.PositiveIntegerField(default=0)

    captured_amount = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "paypal_api"
        ordering = ["-created"]

    def __str__(self):
        return f"PayPalPayment(order={self.order_id}, status={self.status})"

    @property
    def refundable_amount(self) -> Decimal:
        """Amount still available to refund — never exceeds what was captured."""
        return (self.captured_amount or Decimal("0")) - (self.refunded_amount or Decimal("0"))

    def _fmt(self, value):
        return money_utils.format_amount(value, self.currency)

    def describe(self) -> dict:
        data = {
            "orderId": self.order_id,
            "orderNumber": self.order.number,
            "status": self.status,
            "currency": self.currency,
            "amount": self._fmt(self.amount),
            "capturedAmount": self._fmt(self.captured_amount),
            "refundedAmount": self._fmt(self.refunded_amount),
            "refundableAmount": self._fmt(self.refundable_amount),
            "paypal": {
                "orderId": self.paypal_order_id or None,
                "authorizationId": self.authorization_id or None,
                "authorizationStatus": self.authorization_status or None,
                "captureId": self.capture_id or None,
                "captureStatus": self.capture_status or None,
                "fee": self._fmt(self.paypal_fee) if self.paypal_fee is not None else None,
                "netAmount": self._fmt(self.net_amount) if self.net_amount is not None else None,
            },
            "createdAt": self.created.isoformat(),
            "updatedAt": self.updated.isoformat(),
        }
        if self.saved_card_id:
            data["paidWithSavedCardId"] = self.saved_card_id
        return data


class PayPalRefund(models.Model):
    """One refund against a capture — carries the caller's idempotency key."""

    payment = models.ForeignKey(
        PayPalPayment,
        on_delete=models.CASCADE,
        related_name="refunds",
    )
    refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32, blank=True)
    idempotency_key = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "paypal_api"
        ordering = ["-created"]
        # Repeating a request under the same key must not refund twice; two
        # distinct keys remain two legitimate partial refunds.
        unique_together = [("payment", "idempotency_key")]

    def __str__(self):
        return f"PayPalRefund({self.refund_id}, {self.amount} {self.currency})"

    def describe(self) -> dict:
        return {
            "refundId": self.id,
            "paypalRefundId": self.refund_id or None,
            "amount": money_utils.format_amount(self.amount, self.currency),
            "currency": self.currency,
            "status": self.status,
            "idempotencyKey": self.idempotency_key,
            "createdAt": self.created.isoformat(),
        }
