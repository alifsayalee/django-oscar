"""
PayPal state for Oscar orders.

Oscar's own models stay the record of the order (``order.Order`` and its
lines, status pipeline and status history) and of the money it has taken
(``payment.Source`` / ``payment.Transaction``). The models here hold only what
PayPal owns and Oscar has no place for: PayPal's ids and statuses for the
hold, the capture and the refunds, the vaulted cards, and one row per PayPal
transaction id this app created (the local side of reconciliation).

No card number or security code is ever stored.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


def _new_reference() -> str:
    return uuid.uuid4().hex[:20]


class PayPalCustomer(models.Model):
    """
    The PayPal vault customer id PayPal generated for a shopper's first saved card.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paypal_customer",
    )
    paypal_customer_id = models.CharField(max_length=64, unique=True)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.paypal_customer_id


class SavedCard(models.Model):
    """
    A card vaulted at PayPal for one shopper. Only the display details are kept.
    """

    SAVING, ACTIVE, DELETING, DELETED, FAILED, UNKNOWN = (
        "saving",
        "active",
        "deleting",
        "deleted",
        "failed",
        "unknown",
    )
    STATUS_CHOICES = [
        (SAVING, "Saving"),
        (ACTIVE, "Active"),
        (DELETING, "Deleting"),
        (DELETED, "Deleted"),
        (FAILED, "Failed"),
        (UNKNOWN, "Outcome unknown"),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards"
    )
    # The caller's idempotency key (or one we generated) for the save request
    request_key = models.CharField(max_length=100)
    vault_token_id = models.CharField(max_length=64, blank=True, db_index=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    cardholder_name = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SAVING)
    last_error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_created"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "request_key"], name="payments_savedcard_user_request_key"
            )
        ]

    def __str__(self) -> str:
        return "%s ending %s" % (self.brand or "Card", self.last_digits or "????")


class PayPalPayment(models.Model):
    """
    The PayPal side of one Oscar order: the hold, its capture and the refund totals.

    ``state`` is only ever moved by a single conditional UPDATE (see
    ``services``), so the row is also the claim that stops a double-click from
    authorizing, capturing or voiding twice.
    """

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZING = "authorizing"
    AUTHORIZATION_UNKNOWN = "authorization_unknown"
    AUTHORIZED = "authorized"
    AUTHORIZATION_PENDING = "authorization_pending"
    AUTHORIZATION_FAILED = "authorization_failed"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    CAPTURING = "capturing"
    CAPTURE_UNKNOWN = "capture_unknown"
    CAPTURE_PENDING = "capture_pending"
    CAPTURE_FAILED = "capture_failed"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDING = "voiding"
    VOID_UNKNOWN = "void_unknown"
    VOIDED = "voided"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"

    STATE_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZING, "Authorizing"),
        (AUTHORIZATION_UNKNOWN, "Authorization outcome unknown"),
        (AUTHORIZED, "Authorized (funds held)"),
        (AUTHORIZATION_PENDING, "Authorization pending at PayPal"),
        (AUTHORIZATION_FAILED, "Authorization declined"),
        (AUTHORIZATION_EXPIRED, "Authorization expired"),
        (CAPTURING, "Capturing"),
        (CAPTURE_UNKNOWN, "Capture outcome unknown"),
        (CAPTURE_PENDING, "Capture pending at PayPal"),
        (CAPTURE_FAILED, "Capture declined"),
        (CAPTURED, "Captured"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (VOIDING, "Releasing hold"),
        (VOID_UNKNOWN, "Release outcome unknown"),
        (VOIDED, "Hold released"),
        (CANCELLED, "Cancelled before payment"),
        (NEEDS_REVIEW, "Needs review"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    # Globally unique prefix for every PayPal-Request-Id and invoice id this payment
    # sends. Order numbers repeat when the sandbox database is rebuilt; PayPal's
    # idempotency and duplicate-invoice checks outlive the database.
    reference = models.CharField(max_length=32, unique=True, default=_new_reference, editable=False)
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    # When ``state`` last changed; in-flight claims older than the send window are stale
    state_changed_at = models.DateTimeField()
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=3)

    # Authorization attempts: bumped only after a definitive failure, so a retry
    # after an unknown outcome re-sends under the same PayPal-Request-Id.
    attempt = models.PositiveIntegerField(default=1)
    saved_card = models.ForeignKey(
        SavedCard, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments"
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized = models.BooleanField(default=False)

    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    # Completed refunds, and completed + in-flight + pending + unknown refunds.
    # ``refund_reserved`` never exceeds ``captured_amount``: it is only raised by
    # a conditional UPDATE that checks exactly that.
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))

    # Operator-facing explanation of the last failure
    last_error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return "PayPal payment for order %s (%s)" % (self.order.number, self.state)


class PayPalRefund(models.Model):
    SENDING, COMPLETED, PENDING, FAILED, UNKNOWN = (
        "sending",
        "completed",
        "pending",
        "failed",
        "unknown",
    )
    STATUS_CHOICES = [
        (SENDING, "Sending"),
        (COMPLETED, "Completed"),
        (PENDING, "Pending at PayPal"),
        (FAILED, "Failed"),
        (UNKNOWN, "Outcome unknown"),
    ]
    # Statuses whose amount is held against the refundable balance
    RESERVING = (SENDING, COMPLETED, PENDING, UNKNOWN)

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.CASCADE, related_name="refunds")
    idempotency_key = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    # Bumped when a failed refund is retried under the same idempotency key
    attempt = models.PositiveIntegerField(default=1)
    status_changed_at = models.DateTimeField()
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    last_error = models.TextField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date_created"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"], name="payments_refund_idempotency_key"
            )
        ]


class PayPalTransaction(models.Model):
    """
    One PayPal transaction id this app created, stamped with PayPal's own time.

    This is the local side of the reconciliation report: an authorization, its
    capture and every refund are separate PayPal transactions of one order.
    """

    AUTHORIZATION, CAPTURE, REFUND = "authorization", "capture", "refund"
    KIND_CHOICES = [
        (AUTHORIZATION, "Authorization"),
        (CAPTURE, "Capture"),
        (REFUND, "Refund"),
    ]

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name="paypal_transactions"
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    paypal_id = models.CharField(max_length=64, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["provider_time"]
