"""
PayPal-side state for Oscar orders and saved cards.

Oscar's own models stay the system of record for what the shop sold and how much
money moved (``order.Order``, ``payment.Source``/``Transaction``,
``payment.Bankcard``).  The models here hold only what PayPal owns (its ids and
statuses) and the write claims that keep every PayPal write single-shot across
requests and processes.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class InstallIdentity(models.Model):
    """
    A random identifier generated once per database.

    It prefixes every reference sent to PayPal, so two installs sharing one
    merchant account never collide on ``PayPal-Request-Id`` or ``custom_id``.
    """

    prefix = models.CharField(max_length=32, unique=True)
    created = models.DateTimeField(auto_now_add=True)


class PaymentWrite(models.Model):
    """
    The claim for one PayPal write step, taken before the call is made.

    ``reference`` is unique: the database rejects a second claim for the same
    step, and the same value travels to PayPal as ``PayPal-Request-Id`` so an
    unknown outcome can be settled by resending under it.
    """

    SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN = (
        "sending",
        "done",
        "pending",
        "failed",
        "needs_review",
        "unknown",
    )
    OUTCOME_CHOICES = [
        (SENDING, "Sending"),
        (DONE, "Done"),
        (PENDING, "Pending"),
        (FAILED, "Failed"),
        (NEEDS_REVIEW, "Needs review"),
        (UNKNOWN, "Unknown"),
    ]

    AUTHORIZE, REAUTHORIZE, CAPTURE, VOID, REFUND, VAULT_CREATE, VAULT_DELETE = (
        "authorize",
        "reauthorize",
        "capture",
        "void",
        "refund",
        "vault_create",
        "vault_delete",
    )
    KIND_CHOICES = [
        (AUTHORIZE, "Authorize"),
        (REAUTHORIZE, "Reauthorize"),
        (CAPTURE, "Capture"),
        (VOID, "Void"),
        (REFUND, "Refund"),
        (VAULT_CREATE, "Save card"),
        (VAULT_DELETE, "Delete card"),
    ]

    reference = models.CharField(max_length=127, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    order = models.ForeignKey(
        "order.Order", null=True, blank=True, on_delete=models.SET_NULL
    )
    amount = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=12, blank=True)
    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=64, blank=True)
    # PayPal's own event time, used to reconcile on the provider's clock.
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=512, blank=True)
    claimed_at = models.DateTimeField()
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["kind", "outcome"])]

    def __str__(self) -> str:
        return f"{self.kind} {self.reference} ({self.outcome})"


class PayPalPayment(models.Model):
    """PayPal's view of the one card payment for an Oscar order."""

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZING = "authorizing"
    AUTHORIZATION_PENDING = "authorization_pending"
    AUTHORIZED = "authorized"
    REAUTHORIZING = "reauthorizing"
    CAPTURING = "capturing"
    CAPTURE_PENDING = "capture_pending"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDING = "voiding"
    VOIDED = "voided"
    NEEDS_REVIEW = "needs_review"
    STATE_CHOICES = [
        (s, s.replace("_", " ").capitalize())
        for s in (
            AWAITING_PAYMENT,
            AUTHORIZING,
            AUTHORIZATION_PENDING,
            AUTHORIZED,
            REAUTHORIZING,
            CAPTURING,
            CAPTURE_PENDING,
            CAPTURED,
            PARTIALLY_REFUNDED,
            REFUNDED,
            VOIDING,
            VOIDED,
            NEEDS_REVIEW,
        )
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    source = models.OneToOneField(
        "payment.Source", null=True, blank=True, on_delete=models.SET_NULL
    )
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=12)
    # Incremented after an authorization attempt that definitely did not hold money,
    # so the shopper can try again under a fresh reference.
    authorize_attempt = models.PositiveIntegerField(default=1)

    bankcard = models.ForeignKey(
        "payment.Bankcard", null=True, blank=True, on_delete=models.SET_NULL
    )
    card_label = models.CharField(max_length=64, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    original_authorization_id = models.CharField(max_length=64, blank=True)
    reauthorized_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    # Refundable balance is reserved before a refund is sent, so concurrent partial
    # refunds can never exceed the captured amount.
    refund_reserved = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    refunded_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=512, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"PayPal payment for order {self.order.number} ({self.state})"


class PayPalRefund(models.Model):
    """A refund of an order's capture, keyed by the caller's idempotency key."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name="refunds"
    )
    idempotency_key = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=12)
    reason = models.CharField(max_length=255, blank=True)
    write = models.OneToOneField(PaymentWrite, on_delete=models.PROTECT)
    outcome = models.CharField(
        max_length=16, choices=PaymentWrite.OUTCOME_CHOICES, default=PaymentWrite.SENDING
    )
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    # Whether this refund's amount is still held against the refundable balance.
    reserved = models.BooleanField(default=True)
    recorded = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"], name="uniq_refund_key_per_payment"
            )
        ]

    def __str__(self) -> str:
        return f"Refund {self.public_id} of {self.amount} {self.currency} ({self.outcome})"
