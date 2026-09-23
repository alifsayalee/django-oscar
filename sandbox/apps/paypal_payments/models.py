"""
PayPal state for Oscar orders.

These rows hold what PayPal owns (ids and current statuses of the hold, the
capture and the refunds) so any later request can act on a payment, and they
double as the durable claim that stops a double-click from moving money twice.
Oscar's own ``order.Order``, ``payment.Source``/``Transaction`` and
``order.PaymentEvent`` remain the record of the order and its bookkeeping.

No card number or security code is ever stored here.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class PaymentState(models.TextChoices):
    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZING = "authorizing"  # claimed; an authorize call may be in flight
    AUTHORIZATION_PENDING = "authorization_pending"  # PayPal accepted, not decided yet
    AUTHORIZATION_UNKNOWN = (
        "authorization_unknown"  # may have landed; re-send under the same ref
    )
    AUTHORIZED = "authorized"
    PAYMENT_FAILED = (
        "payment_failed"  # definitively declined/rejected; the shopper may pay again
    )
    CAPTURING = "capturing"
    CAPTURE_PENDING = "capture_pending"
    CAPTURE_UNKNOWN = "capture_unknown"
    CAPTURED = "captured"
    VOIDING = "voiding"
    VOID_UNKNOWN = "void_unknown"
    VOIDED = "voided"
    CANCELLED = "cancelled"  # cancelled before any money was held
    NEEDS_REVIEW = "needs_review"  # PayPal did something, but not what we asked


class PayPalPayment(models.Model):
    order = models.OneToOneField(
        "order.Order", on_delete=models.PROTECT, related_name="paypal_payment"
    )
    source = models.OneToOneField(
        "payment.Source",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    # Globally unique reference: order numbers repeat across shops sharing one
    # PayPal account, so every id we send PayPal is derived from this instead.
    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    state = models.CharField(
        max_length=32,
        choices=PaymentState.choices,
        default=PaymentState.AWAITING_PAYMENT,
    )
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    # Incremented only after a definitive failure, so a resend of an unknown
    # outcome reuses the same PayPal-Request-Id and PayPal collapses it.
    attempt = models.PositiveIntegerField(default=0)
    saved_card = models.ForeignKey(
        "SavedCard", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)

    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_amount = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )
    # PayPal's clock: when the *original* hold was placed (drives the 29-day
    # reauthorization limit) and when the current one was (3-day honor period).
    original_authorized_at = models.DateTimeField(null=True, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    previous_authorization_ids = models.JSONField(default=list, blank=True)
    reauthorization_count = models.PositiveIntegerField(default=0)

    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )
    paypal_fee = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )
    net_amount = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )
    captured_at = models.DateTimeField(null=True, blank=True)

    voided_at = models.DateTimeField(null=True, blank=True)

    # Money reserved by refunds that are sending, pending, unknown or completed.
    refund_reserved = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("0")
    )
    refunded_amount = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("0")
    )

    last_error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(refund_reserved__gte=0),
                name="paypal_payment_refund_reserved_non_negative",
            ),
        ]

    def __str__(self):
        return "PayPal payment for order %s (%s)" % (self.order.number, self.state)

    def request_ref(self, action):
        """Deterministic PayPal-Request-Id for one logical action on this payment."""
        return "oscar-%s-%s-%d" % (self.reference.hex, action, self.attempt)

    @property
    def custom_id(self):
        """Sent as the purchase unit's custom_id; PayPal reports it as custom_field."""
        return "%s-%s" % (self.order.number, self.reference.hex[:12])


class RefundState(models.TextChoices):
    SENDING = "sending"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PayPalRefund(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.PROTECT, related_name="refunds"
    )
    idempotency_key = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    reason = models.CharField(max_length=255, blank=True)
    state = models.CharField(
        max_length=16, choices=RefundState.choices, default=RefundState.SENDING
    )
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)  # PayPal's clock
    last_error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"], name="paypal_refund_unique_key"
            ),
        ]
        ordering = ["date_created"]

    @property
    def request_ref(self):
        return "oscar-refund-%s" % self.id.hex


class SavedCardStatus(models.TextChoices):
    SAVING = "saving"
    ACTIVE = "active"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    DELETING = "deleting"
    DELETED = "deleted"


class SavedCard(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_saved_cards",
    )
    request_ref = models.CharField(max_length=128)
    status = models.CharField(
        max_length=16, choices=SavedCardStatus.choices, default=SavedCardStatus.SAVING
    )
    paypal_token_id = models.CharField(
        max_length=64, null=True, blank=True, unique=True
    )
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    holder_name = models.CharField(max_length=255, blank=True)
    last_error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "request_ref"], name="saved_card_unique_ref"
            ),
        ]
        ordering = ["date_created"]


class PayPalCustomer(models.Model):
    """The PayPal vault customer that holds a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_customer",
    )
    paypal_customer_id = models.CharField(max_length=64)
