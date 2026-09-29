"""
PayPal state that Oscar's own models have no field for.

Orders, lines, payment sources/transactions and saved cards are Oscar's
(``order.Order``, ``payment.Source``, ``payment.Transaction``,
``payment.Bankcard``). These models only carry the identifiers and statuses
PayPal owns, so a later request can act on what an earlier one started.
No card number or security code is ever stored here.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models


class PayPalCustomer(models.Model):
    """The PayPal vault customer that holds a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_customer",
    )
    paypal_customer_id = models.CharField(max_length=64, unique=True)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return "PayPal customer %s" % self.paypal_customer_id


class PayPalPayment(models.Model):
    """The PayPal side of one Oscar order's payment."""

    # Our own payment state machine
    PENDING = "PENDING"  # a pay attempt is in flight
    AUTHORIZED = "AUTHORIZED"
    DECLINED = "DECLINED"
    CAPTURED = "CAPTURED"
    CAPTURE_PENDING = "CAPTURE_PENDING"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    VOIDED = "VOIDED"
    EXPIRED = "EXPIRED"
    STATUS_CHOICES = [
        (s, s)
        for s in (
            PENDING,
            AUTHORIZED,
            DECLINED,
            CAPTURED,
            CAPTURE_PENDING,
            PARTIALLY_REFUNDED,
            REFUNDED,
            VOIDED,
            EXPIRED,
        )
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    source = models.OneToOneField(
        "payment.Source",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paypal_payment",
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=PENDING)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # PayPal-Request-Id keys. Persisted before the call so a repeat (double
    # click, retry after a timeout) replays the same key and PayPal answers
    # with the original result instead of acting twice.
    authorize_request_id = models.CharField(max_length=64, blank=True)
    reauthorize_request_id = models.CharField(max_length=64, blank=True)
    capture_request_id = models.CharField(max_length=64, blank=True)
    void_request_id = models.CharField(max_length=64, blank=True)
    invoice_id = models.CharField(max_length=127, blank=True)

    # What paid: a one-off card or a saved one (never the card number)
    bankcard = models.ForeignKey(
        "payment.Bankcard",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paypal_payments",
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    # The hold
    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    # When the current honor period started (original auth or last reauthorization)
    honor_period_started_at = models.DateTimeField(null=True, blank=True)

    # The capture
    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
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
    captured_at = models.DateTimeField(null=True, blank=True)

    last_error = models.CharField(max_length=255, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "PayPal payment for order %s (%s)" % (self.order.number, self.status)

    @property
    def refunded_amount(self):
        """Refunds that happened or may have happened (in flight) — never refundable again."""
        total = self.refunds.exclude(status=PayPalRefund.FAILED).aggregate(
            total=models.Sum("amount")
        )["total"]
        return total or Decimal("0.00")

    @property
    def refundable_amount(self):
        if self.captured_amount is None:
            return Decimal("0.00")
        return max(self.captured_amount - self.refunded_amount, Decimal("0.00"))


class PayPalRefund(models.Model):
    REQUESTED = "REQUESTED"  # reserved; PayPal outcome not yet known
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STATUS_CHOICES = [(s, s) for s in (REQUESTED, PENDING, COMPLETED, FAILED)]

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name="refunds"
    )
    idempotency_key = models.CharField(max_length=128)
    paypal_request_id = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=REQUESTED)
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"], name="paypal_refund_idempotency"
            )
        ]
