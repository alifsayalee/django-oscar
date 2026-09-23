"""Persistent state the checkout API owns.

Oscar already models the order (``order.Order``/``order.Line``), the money movement
(``payment.Source``/``payment.Transaction``) and the saved card (``payment.Bankcard``).
What Oscar has no place for is the *PayPal-owned* state that a later request must act
on: the ids and current status of the hold (authorization), the capture and each
refund. That -- and only that -- lives here, hanging off the Oscar order rather than
duplicating it.

These rows are also the durable idempotency claim (``python-configuration-resilience``):
one ``PayPalPayment`` per order and one ``PayPalRefund`` per (payment, idempotency key),
so a double-click can never authorize, capture or refund twice.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class PayPalPayment(models.Model):
    """PayPal payment state for a single Oscar order (one-to-one)."""

    # Lifecycle of the money at PayPal.
    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    VOIDED = "voided"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    FAILED = "failed"
    STATE_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZED, "Authorized (funds held)"),
        (CAPTURED, "Captured (money taken)"),
        (VOIDED, "Voided (hold released)"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (FAILED, "Failed"),
    ]

    order = models.OneToOneField(
        "order.Order",
        on_delete=models.CASCADE,
        related_name="paypal_payment",
    )
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    # The order total we hold/capture, to the cent.
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # A globally-unique invoice reference we send to PayPal as invoice_id. The Oscar
    # order number restarts at 100001 on a fresh database, and PayPal enforces
    # invoice_id uniqueness per merchant account, so the raw order number would
    # collide across runs. We keep the order number as custom_id for reconciliation
    # and use this unique value for invoice_id.
    invoice_reference = models.CharField(max_length=64, blank=True, db_index=True)

    # PayPal-owned identifiers and statuses.
    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_expiry = models.DateTimeField(null=True, blank=True)
    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)

    # The settlement breakdown PayPal reports at capture.
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # PayPal's own event time for the capture, recorded so reconciliation filters on
    # the provider's clock rather than our created_at.
    captured_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "api"
        ordering = ["-created_at"]

    def __str__(self):
        return "PayPalPayment(order=%s, state=%s)" % (self.order_id, self.state)

    @property
    def amount_refunded(self):
        total = Decimal("0.00")
        for refund in self.refunds.all():
            if refund.status not in (PayPalRefund.FAILED, PayPalRefund.CANCELLED):
                total += refund.amount
        return total

    @property
    def amount_refundable(self):
        """How much of the captured payment can still be refunded."""
        if self.gross_amount is None:
            return Decimal("0.00")
        return self.gross_amount - self.amount_refunded

    def is_authorization_stale(self):
        if not self.authorization_expiry:
            return False
        return self.authorization_expiry <= timezone.now()


class PayPalRefund(models.Model):
    """A single refund against a captured PayPal payment.

    ``idempotency_key`` is the caller-supplied key; ``unique_together`` with the
    payment makes a repeat under the same key return the same refund rather than
    refunding twice, while two distinct keys remain two legitimate partial refunds.
    """

    COMPLETED = "COMPLETED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    payment = models.ForeignKey(
        PayPalPayment,
        on_delete=models.CASCADE,
        related_name="refunds",
    )
    refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32, blank=True)
    idempotency_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "api"
        ordering = ["-created_at"]
        unique_together = ("payment", "idempotency_key")

    def __str__(self):
        return "PayPalRefund(%s, %s %s)" % (self.refund_id, self.amount, self.currency)
