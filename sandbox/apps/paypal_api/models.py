"""
PayPal-specific state that Oscar's own models have no room for.

Orders, order lines, payment sources/transactions and saved bankcards stay in
Oscar's models; these tables only hold what PayPal owns (ids, statuses, fees)
and the claims that keep every PayPal write from happening twice.
"""

from django.conf import settings
from django.db import models


class PayPalOperation(models.Model):
    """
    One PayPal write step, claimed before it is sent.

    ``ref`` is unique: inserting the row *is* the claim, so a second request for
    the same operation is rejected by the database rather than by a
    read-then-write check. The same ``ref`` travels to PayPal as the
    ``PayPal-Request-Id`` and is what an unknown outcome is looked up by.
    """

    SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN = (
        "sending",
        "done",
        "pending",
        "failed",
        "needs_review",
        "unknown",
    )
    OUTCOMES = [
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
    KINDS = [
        (AUTHORIZE, "Authorize"),
        (REAUTHORIZE, "Reauthorize"),
        (CAPTURE, "Capture"),
        (VOID, "Void"),
        (REFUND, "Refund"),
        (VAULT_CREATE, "Save card"),
        (VAULT_DELETE, "Delete saved card"),
    ]

    ref = models.CharField(max_length=190, unique=True)
    kind = models.CharField(max_length=32, choices=KINDS)
    outcome = models.CharField(max_length=16, choices=OUTCOMES, default=SENDING)
    order = models.ForeignKey(
        "order.Order",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="paypal_operations",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paypal_operations",
    )
    attempt = models.PositiveIntegerField(default=1)
    # What was asked for, recorded before the call
    amount_minor = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    fingerprint = models.CharField(max_length=128, blank=True)
    invoice_id = models.CharField(max_length=127, blank=True)
    # What PayPal said
    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=64, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    detail = models.JSONField(default=dict, blank=True)
    # Refunds: the amount this operation reserved against the capture (0 once released)
    reserved_minor = models.BigIntegerField(default=0)
    # Set exactly once, when the local effects of a done operation were recorded
    applied = models.BooleanField(default=False)

    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pk"]
        indexes = [models.Index(fields=["order", "kind"])]

    def __str__(self) -> str:
        return f"{self.kind} {self.ref} [{self.outcome}]"


class PayPalPayment(models.Model):
    """PayPal's view of an Oscar order's payment: the hold, the capture, the refunds."""

    AWAITING, AUTHORIZED, CAPTURING, CAPTURED, VOIDING, VOIDED = (
        "awaiting_payment",
        "authorized",
        "capturing",
        "captured",
        "voiding",
        "voided",
    )
    AUTHORIZATION_EXPIRED = "authorization_expired"
    CANCELLED = "cancelled"
    STATES = [
        (CANCELLED, "Cancelled before payment"),
        (AWAITING, "Awaiting payment"),
        (AUTHORIZED, "Authorized"),
        (CAPTURING, "Capturing"),
        (CAPTURED, "Captured"),
        (VOIDING, "Voiding"),
        (VOIDED, "Voided"),
        (AUTHORIZATION_EXPIRED, "Authorization expired"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.PROTECT, related_name="paypal_payment"
    )
    source = models.OneToOneField(
        "payment.Source",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paypal_payment",
    )
    state = models.CharField(max_length=32, choices=STATES, default=AWAITING)
    currency = models.CharField(max_length=3)
    amount_minor = models.BigIntegerField()

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    original_authorization_id = models.CharField(max_length=64, blank=True)
    reauthorized_at = models.DateTimeField(null=True, blank=True)
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)
    saved_card = models.ForeignKey(
        "payment.Bankcard", null=True, blank=True, on_delete=models.SET_NULL
    )

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_minor = models.BigIntegerField(default=0)
    paypal_fee_minor = models.BigIntegerField(null=True, blank=True)
    net_minor = models.BigIntegerField(null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    # Refunds: reserved grows before a refund is sent, so concurrent partial
    # refunds can never together exceed what was captured.
    refund_reserved_minor = models.BigIntegerField(default=0)
    refunded_minor = models.BigIntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(refund_reserved_minor__lte=models.F("captured_minor")),
                name="paypal_refunds_within_capture",
            ),
        ]

    def __str__(self) -> str:
        return f"PayPal payment for order {self.order_id} [{self.state}]"


class PayPalCustomer(models.Model):
    """The PayPal vault customer that holds a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer"
    )
    customer_id = models.CharField(max_length=64)

    def __str__(self) -> str:
        return f"PayPal customer for user {self.user_id}"
