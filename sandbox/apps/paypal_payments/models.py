"""
PayPal state kept alongside Oscar's own order, payment-source and bankcard
models.

Oscar remains the record of the order (``order.Order``/``order.Line``), of the
money accounting (``payment.Source``/``payment.Transaction``) and of the
shopper's saved card as they see it (``payment.Bankcard``, masked). These
models carry what PayPal owns and a later request needs to act on it: PayPal's
ids and statuses, and a durable claim row written *before* each PayPal call so
that a double-click or an overlapping retry never authorizes, captures or
refunds twice.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q


class PayPalPayment(models.Model):
    """The PayPal authorization/capture of one Oscar order."""

    # Claimed, request to PayPal in flight (or its sender died).
    SENDING = "sending"
    # PayPal holds the order total on the card.
    AUTHORIZED = "authorized"
    # PayPal accepted the authorization but has not finished it.
    PENDING = "pending"
    # Definitely no hold: declined, rejected, or never sent.
    FAILED = "failed"
    # PayPal's answer could not be obtained; may have happened.
    UNKNOWN = "unknown"
    # PayPal did it, but not as asked (e.g. a different amount).
    NEEDS_REVIEW = "needs_review"
    # Capture claimed / in flight.
    CAPTURING = "capturing"
    CAPTURE_PENDING = "capture_pending"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    # Void claimed / in flight, and done.
    VOIDING = "voiding"
    VOIDED = "voided"
    # The authorization ran out before capture and could not be renewed.
    EXPIRED = "expired"

    STATUS_CHOICES = [
        (s, s)
        for s in (
            SENDING, AUTHORIZED, PENDING, FAILED, UNKNOWN, NEEDS_REVIEW, CAPTURING, CAPTURE_PENDING,
            CAPTURED, PARTIALLY_REFUNDED, REFUNDED, VOIDING, VOIDED, EXPIRED,
        )
    ]
    # Statuses that release the order for a fresh payment attempt.
    CLOSED_STATUSES = (FAILED, VOIDED, EXPIRED)

    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order = models.ForeignKey("order.Order", on_delete=models.PROTECT, related_name="paypal_payments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=SENDING)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    saved_card = models.ForeignKey(
        "PayPalSavedCard", null=True, blank=True, on_delete=models.SET_NULL, related_name="payments"
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    # Sent as the purchase unit's invoice_id. PayPal accounts can require it to be
    # unique across all their transactions, and Oscar order numbers are only
    # unique within one database, so each attempt gets its own.
    invoice_id = models.CharField(max_length=127, blank=True, db_index=True)
    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    # Authorization ids replaced by a reauthorization, oldest first.
    previous_authorization_ids = models.JSONField(default=list, blank=True)

    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    # Refunds: reserved grows before a refund is sent; refunded when PayPal confirms.
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))

    voided_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True)
    failure_message = models.TextField(blank=True)
    paypal_debug_id = models.CharField(max_length=64, blank=True)

    source = models.ForeignKey(
        "payment.Source", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One live payment per order: the claim that makes /pay idempotent.
            models.UniqueConstraint(
                fields=["order"],
                condition=~Q(status__in=["failed", "voided", "expired"]),
                name="paypal_one_live_payment_per_order",
            ),
        ]

    def __str__(self):
        return "PayPal payment %s for order %s (%s)" % (self.reference, self.order_id, self.status)

    @property
    def refundable_amount(self) -> Decimal:
        if self.captured_amount is None:
            return Decimal("0")
        return self.captured_amount - self.refund_reserved


class PayPalRefund(models.Model):
    """One refund of a captured payment, keyed by the caller's idempotency key."""

    SENDING = "sending"
    DONE = "done"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"
    STATUS_CHOICES = [(s, s) for s in (SENDING, DONE, PENDING, FAILED, UNKNOWN)]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.PROTECT, related_name="refunds")
    idempotency_key = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True)
    failure_message = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "idempotency_key"], name="paypal_refund_key_per_payment"),
        ]

    def __str__(self):
        return "PayPal refund %s (%s %s, %s)" % (self.public_id, self.amount, self.currency, self.status)


class PayPalSavedCard(models.Model):
    """
    A card vaulted at PayPal for a shopper. Only PayPal's token id and the
    masked description PayPal returned are stored; never the card itself.
    """

    SENDING = "sending"
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"
    UNKNOWN = "unknown"
    STATUS_CHOICES = [(s, s) for s in (SENDING, ACTIVE, DELETING, DELETED, FAILED, UNKNOWN)]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_cards")
    idempotency_key = models.CharField(max_length=128)
    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    bankcard = models.OneToOneField(
        "payment.Bankcard", null=True, blank=True, on_delete=models.SET_NULL, related_name="paypal_card"
    )
    paypal_token_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    failure_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "idempotency_key"], name="paypal_card_key_per_user"),
        ]

    def __str__(self):
        return "%s ending %s (%s)" % (self.brand or "Card", self.last_digits, self.status)
