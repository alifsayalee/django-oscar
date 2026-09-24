"""
Models for PayPal state that Oscar has no home for.

Card numbers, expiry dates and security codes are never stored here: a saved
card is a PayPal vault token plus the display details PayPal returned for it.
"""

import uuid

from django.conf import settings
from django.db import models


class ProviderWrite(models.Model):
    """
    One row per PayPal write (authorize, reauthorize, capture, void, refund,
    vault). ``reference`` is unique and is also sent to PayPal as the
    ``PayPal-Request-Id``: inserting the row is the claim that stops the same
    operation being sent twice, and it is committed before PayPal is called.
    """

    AUTHORIZE, REAUTHORIZE, CAPTURE, VOID, REFUND, VAULT = (
        "authorize",
        "reauthorize",
        "capture",
        "void",
        "refund",
        "vault",
    )
    KIND_CHOICES = [
        (k, k) for k in (AUTHORIZE, REAUTHORIZE, CAPTURE, VOID, REFUND, VAULT)
    ]

    # Outcomes. ``sending`` is our in-flight marker; ``pending`` is only ever
    # PayPal's own word that it accepted the request and has not finished.
    SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN = (
        "sending",
        "done",
        "pending",
        "failed",
        "needs_review",
        "unknown",
    )
    OUTCOME_CHOICES = [
        (o, o) for o in (SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN)
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    reference = models.CharField(max_length=128, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    order = models.ForeignKey(
        "order.Order",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="paypal_writes",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=64, blank=True)
    # PayPal's own event time, used by reconciliation (never our created-at).
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    detail = models.CharField(max_length=512, blank=True)
    claimed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["claimed_at", "pk"]

    def __str__(self) -> str:
        return f"{self.kind} {self.reference} ({self.outcome})"


class PaypalCustomer(models.Model):
    """The PayPal vault customer id that holds a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer"
    )
    paypal_customer_id = models.CharField(max_length=64)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.paypal_customer_id


class SavedCard(models.Model):
    """A card vaulted at PayPal for one shopper. Only display details are kept."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards"
    )
    vault_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    # HMAC-SHA256 of the card keyed with SECRET_KEY: lets the same card saved
    # twice resolve to one saved card without keeping anything reversible.
    fingerprint = models.CharField(max_length=64, db_index=True)
    created = models.DateTimeField(auto_now_add=True)
    # Removed from the shopper's cards (and unusable) from this moment on ...
    deleted_at = models.DateTimeField(null=True, blank=True)
    # ... and removed from PayPal's vault once PayPal confirms it.
    provider_deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created"]

    def __str__(self) -> str:
        return f"{self.brand} ending {self.last_digits}"

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None


class PaypalPayment(models.Model):
    """
    PayPal-side state of an order's payment: the hold, the capture and what
    PayPal reported for it. Money movements are also mirrored onto Oscar's
    ``payment.Source`` / ``payment.Transaction`` for the dashboard.
    """

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZED = "authorized"
    AUTHORIZATION_PENDING = "authorization_pending"
    CAPTURED = "captured"
    CAPTURE_PENDING = "capture_pending"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDED = "voided"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"
    STATE_CHOICES = [
        (s, s)
        for s in (
            AWAITING_PAYMENT,
            AUTHORIZED,
            AUTHORIZATION_PENDING,
            CAPTURED,
            CAPTURE_PENDING,
            PARTIALLY_REFUNDED,
            REFUNDED,
            VOIDED,
            CANCELLED,
            NEEDS_REVIEW,
        )
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.CASCADE, related_name="paypal_payment"
    )
    source = models.OneToOneField(
        "payment.Source",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paypal_payment",
    )
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)

    saved_card = models.ForeignKey(
        SavedCard, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    card_label = models.CharField(max_length=64, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    original_authorization_id = models.CharField(max_length=64, blank=True)
    original_authorization_created_at = models.DateTimeField(null=True, blank=True)
    reauthorized_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    detail = models.CharField(max_length=512, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"PayPal payment for order {self.order_id} ({self.state})"
