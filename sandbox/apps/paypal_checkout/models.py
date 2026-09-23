from decimal import Decimal

from django.db import models
from django.utils import timezone


class PayPalPayment(models.Model):
    """PayPal-specific state for a single Oscar order.

    Oscar's ``order.Order`` and ``payment.Source``/``Transaction`` own the order
    and the money ledger; this model only holds the identifiers and statuses that
    PayPal owns (the hold, the capture and the fee/net breakdown) so that a later
    request can act on the payment, not only the one that started it.
    """

    # Payment lifecycle states.
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    STATE_CHOICES = [
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
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # PayPal-owned identifiers and statuses.
    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_expiry = models.DateTimeField(null=True, blank=True)
    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)

    # Money movement reported by PayPal at capture time.
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Which saved card (if any) funded the authorization.
    bankcard = models.ForeignKey(
        "payment.Bankcard",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paypal_payments",
    )

    # Stored idempotency keys (PayPal-Request-Id) so a retried request reuses the
    # same key instead of authorizing/capturing twice.
    authorize_request_id = models.CharField(max_length=64, blank=True)
    capture_request_id = models.CharField(max_length=64, blank=True)

    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "paypal_checkout"
        ordering = ["-date_created"]

    def __str__(self):
        return "PayPalPayment(order=%s, state=%s)" % (self.order_id, self.state)

    @property
    def total_refunded(self):
        agg = self.refunds.filter(status__in=("COMPLETED", "PENDING")).aggregate(
            total=models.Sum("amount")
        )
        return agg["total"] or Decimal("0.00")

    @property
    def refundable_amount(self):
        """Amount still available to refund: captured minus what is already refunded."""
        if self.captured_amount is None:
            return Decimal("0.00")
        return self.captured_amount - self.total_refunded

    def recompute_refund_state(self):
        refunded = self.total_refunded
        if self.captured_amount is not None and refunded >= self.captured_amount:
            self.state = self.REFUNDED
        elif refunded > Decimal("0.00"):
            self.state = self.PARTIALLY_REFUNDED


class PayPalRefund(models.Model):
    """A single refund against a captured PayPal payment.

    ``idempotency_key`` is the caller-supplied key: repeating a request under the
    same key must not refund twice, while two distinct keys are two legitimate
    partial refunds of the same capture.
    """

    payment = models.ForeignKey(
        PayPalPayment,
        on_delete=models.CASCADE,
        related_name="refunds",
    )
    refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32, blank=True)
    idempotency_key = models.CharField(max_length=128)
    date_created = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "paypal_checkout"
        ordering = ["date_created"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"],
                name="uniq_refund_idempotency_per_payment",
            )
        ]

    def __str__(self):
        return "PayPalRefund(%s, %s %s)" % (self.refund_id, self.amount, self.currency)
