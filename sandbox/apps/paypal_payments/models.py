"""
Payment state for the PayPal API.

Money bookkeeping reuses Oscar's own models (``payment.Source`` /
``payment.Transaction`` and ``order.PaymentEvent``); these models only hold
what Oscar has no place for: the PayPal ids and statuses a later request needs
to act on, the claims that stop a write being made twice, and vaulted cards.
No card number or security code is ever stored here.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, models, transaction


class InstallIdentity(models.Model):
    """
    A random token unique to this database, used to prefix every reference
    sent to PayPal so two installs sharing one PayPal account never collide.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    token = models.CharField(max_length=32, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @classmethod
    def prefix(cls) -> str:
        configured: str = getattr(settings, "PAYPAL_REFERENCE_PREFIX", "")
        if configured:
            return configured
        identity = cls.objects.filter(pk=1).first()
        if identity is None:
            try:
                with transaction.atomic():
                    identity = cls.objects.create(pk=1, token=uuid.uuid4().hex[:12])
            except IntegrityError:
                identity = cls.objects.get(pk=1)
        return "osc-%s" % identity.token


class ProviderWrite(models.Model):
    """
    The claim taken before any PayPal write that creates, charges or cancels
    something, and the outcome PayPal reported for it.

    ``ref`` is unique: inserting it is how one request - and only one - wins
    the right to make the write. ``request_id`` is sent to PayPal as the
    ``PayPal-Request-Id`` header, so a resend under the same reference is
    de-duplicated by PayPal rather than repeated.
    """

    AUTHORIZE, ORDER_AUTHORIZE, REAUTHORIZE, CAPTURE, VOID, REFUND, VAULT = (
        "authorize",
        "order_authorize",
        "reauthorize",
        "capture",
        "void",
        "refund",
        "vault",
    )
    OPERATION_CHOICES = [
        (AUTHORIZE, "Create order and authorize"),
        (ORDER_AUTHORIZE, "Authorize order"),
        (REAUTHORIZE, "Reauthorize"),
        (CAPTURE, "Capture"),
        (VOID, "Void"),
        (REFUND, "Refund"),
        (VAULT, "Save card"),
    ]

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
        (PENDING, "Pending at PayPal"),
        (FAILED, "Failed"),
        (NEEDS_REVIEW, "Needs review"),
        (UNKNOWN, "Unknown"),
    ]

    ref = models.CharField(max_length=255, unique=True)
    request_id = models.CharField(max_length=36, unique=True)
    operation = models.CharField(max_length=32, choices=OPERATION_CHOICES)
    order = models.ForeignKey(
        "order.Order",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="paypal_writes",
    )
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=64, blank=True)
    # PayPal's own event time - reconciliation filters on this clock
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    # Operator-readable reason for a failure (PayPal's issue and description)
    detail = models.CharField(max_length=1000, blank=True)
    # Non-sensitive fields PayPal returned (ids, expiry, fee breakdown, card brand/last digits)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return "%s %s (%s)" % (self.operation, self.ref, self.outcome)


class OrderPayment(models.Model):
    """PayPal state for one Oscar order."""

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZATION_PENDING = "authorization_pending"
    AUTHORIZED = "authorized"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    CAPTURE_PENDING = "capture_pending"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDED = "voided"
    CANCELLED = "cancelled"
    STATE_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZATION_PENDING, "Authorization pending at PayPal"),
        (AUTHORIZED, "Authorized (funds held)"),
        (AUTHORIZATION_EXPIRED, "Authorization expired"),
        (CAPTURE_PENDING, "Capture pending at PayPal"),
        (CAPTURED, "Captured"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (VOIDED, "Voided (hold released)"),
        (CANCELLED, "Cancelled before payment"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    source = models.OneToOneField(
        "payment.Source", null=True, blank=True, on_delete=models.SET_NULL
    )
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    attempt = models.PositiveIntegerField(default=1)
    currency = models.CharField(max_length=3)
    saved_card = models.ForeignKey(
        "paypal_payments.SavedCard", null=True, blank=True, on_delete=models.SET_NULL
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # PayPal's creation time of the current authorization, and of the original one
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    original_authorization_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized = models.BooleanField(default=False)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    last_error = models.CharField(max_length=1000, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "PayPal payment for order %s (%s)" % (self.order.number, self.state)


class PayPalRefund(models.Model):
    """One refund of a capture, keyed by the caller's idempotency key."""

    RESERVED = "reserved"
    # Outcomes that do not hold any of the refundable balance
    RELEASED_OUTCOMES = (ProviderWrite.FAILED,)

    payment = models.ForeignKey(OrderPayment, on_delete=models.CASCADE, related_name="refunds")
    idempotency_key = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    outcome = models.CharField(max_length=16, default=RESERVED)
    write = models.OneToOneField(
        ProviderWrite, null=True, blank=True, on_delete=models.PROTECT, related_name="refund"
    )
    refund_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    applied = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"], name="paypal_refund_unique_key"
            )
        ]


class SavedCard(models.Model):
    """
    A card vaulted at PayPal for one shopper. Only PayPal's token id and a
    safe description (brand, last digits, expiry) are kept.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_saved_cards"
    )
    write = models.OneToOneField(
        ProviderWrite, on_delete=models.PROTECT, related_name="saved_card"
    )
    vault_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    provider_deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return "%s ending %s" % (self.brand or "Card", self.last_digits)
