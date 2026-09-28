"""
Local records for PayPal payments and saved cards.

PayPal stays the record of what exists; these tables record what this site
asked for (``ProviderWrite``, the claim store) and the PayPal state a later
request needs to act on an order (``PayPalPayment``, ``PayPalRefund``).
No card number or security code is ever stored here.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class InstallIdentity(models.Model):
    """One row: an id generated once per database, used to prefix every
    reference this install sends to PayPal."""

    install_id = models.CharField(max_length=32, unique=True)
    created = models.DateTimeField(auto_now_add=True)


class ProviderWrite(models.Model):
    """
    The claim for one provider write step.

    ``ref`` is unique, so inserting it is insert-or-fail: the database decides
    which of two concurrent requests makes the PayPal call. The same value is
    sent to PayPal as ``PayPal-Request-Id``.
    """

    SENDING = "sending"
    DONE = "done"
    PENDING = "pending"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    OUTCOME_CHOICES = [
        (SENDING, "Sending"),
        (DONE, "Done"),
        (PENDING, "Pending"),
        (FAILED, "Failed"),
        (NEEDS_REVIEW, "Needs review"),
        (UNKNOWN, "Unknown"),
    ]

    ref = models.CharField(max_length=100, unique=True)
    operation = models.CharField(max_length=32)
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=64, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    # A short, card-data-free description of the last error PayPal returned.
    detail = models.TextField(blank=True)
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["provider_time"])]

    def __str__(self) -> str:
        return f"{self.ref} ({self.outcome})"


class PayPalCustomer(models.Model):
    """The PayPal vault customer the shopper's saved cards belong to."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer"
    )
    vault_customer_id = models.CharField(max_length=64)
    created = models.DateTimeField(auto_now_add=True)


class SavedCard(models.Model):
    """A card vaulted at PayPal. Only the token id and display details are kept."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards"
    )
    token_id = models.CharField(max_length=64, unique=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    cardholder_name = models.CharField(max_length=128, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    # Set before PayPal is asked to delete the token: from then on the card is
    # neither listed nor usable, whatever PayPal answers.
    deleted_at = models.DateTimeField(null=True, blank=True)
    provider_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"{self.brand} ending {self.last_digits}"


class PayPalPayment(models.Model):
    """PayPal state for one Oscar order."""

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZATION_PENDING = "authorization_pending"
    AUTHORIZED = "authorized"
    CAPTURE_PENDING = "capture_pending"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDED = "voided"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"
    STATUS_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZATION_PENDING, "Authorization pending"),
        (AUTHORIZED, "Authorized"),
        (CAPTURE_PENDING, "Capture pending"),
        (CAPTURED, "Captured"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (VOIDED, "Voided"),
        (CANCELLED, "Cancelled"),
        (NEEDS_REVIEW, "Needs review"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.PROTECT, related_name="paypal_payment"
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # Bumped after a definitive authorization failure so the shopper's next
    # attempt goes out under a new reference.
    pay_attempt = models.PositiveIntegerField(default=1)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    saved_card = models.ForeignKey(
        SavedCard, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments"
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    # When the current authorization (or its latest reauthorization) was made:
    # the start of its honor period.
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Refund amounts claimed by in-flight or completed refunds; never exceeds
    # captured_amount (enforced by a conditional UPDATE).
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount_refunded = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    last_error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"PayPal payment for order {self.order_id} ({self.status})"


class PayPalRefund(models.Model):
    SENDING = "sending"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NEEDS_REVIEW = "needs_review"
    STATUS_CHOICES = [
        (SENDING, "Sending"),
        (PENDING, "Pending"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
        (UNKNOWN, "Unknown"),
        (NEEDS_REVIEW, "Needs review"),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.PROTECT, related_name="refunds")
    # SHA-256 of the caller's idempotency key; the key itself is not kept.
    key_hash = models.CharField(max_length=64)
    ref = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    # Whether this refund still holds part of PayPalPayment.refund_reserved.
    reservation_held = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["payment", "key_hash"], name="uniq_refund_key_per_payment")
        ]
        ordering = ["created"]

    def __str__(self) -> str:
        return f"Refund {self.public_id} ({self.status})"
