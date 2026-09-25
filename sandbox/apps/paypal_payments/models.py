"""
Local records for the PayPal integration.

Oscar's own ``order.Order`` and ``payment.Source``/``payment.Transaction`` stay
the records of what was ordered and what money moved. These models hold the
state PayPal owns (ids and statuses) that a later request needs in order to act,
plus the claims that keep a provider write from being made twice.

No card number or security code is ever stored here.
"""

import uuid

from django.conf import settings
from django.db import models


class Outcome(models.TextChoices):
    SENDING = "sending", "Claimed, no answer yet"
    DONE = "done", "Done"
    PENDING = "pending", "Accepted by PayPal, not finished"
    FAILED = "failed", "Failed, refused or undone"
    NEEDS_REVIEW = "needs_review", "Happened, but not as asked"
    UNKNOWN = "unknown", "May have happened"


class InstallIdentity(models.Model):
    """A random token created once per database, so that references sent to
    PayPal never collide across a rebuilt database or another install that
    shares the same PayPal account."""

    token = models.CharField(max_length=32, unique=True)
    date_created = models.DateTimeField(auto_now_add=True)


class PayPalOperation(models.Model):
    """One provider write step, claimed before the call is made.

    ``reference`` is unique: inserting it is the claim, and the database rejects
    a second claim for the same operation. It is also sent to PayPal as the
    ``PayPal-Request-Id``, so an unknown outcome is checked by that reference.
    """

    CREATE_ORDER = "create_order"
    AUTHORIZE = "authorize"
    REAUTHORIZE = "reauthorize"
    CAPTURE = "capture"
    VOID = "void"
    REFUND = "refund"
    VAULT = "vault"
    KIND_CHOICES = [
        (CREATE_ORDER, "Create PayPal order"),
        (AUTHORIZE, "Authorize"),
        (REAUTHORIZE, "Reauthorize"),
        (CAPTURE, "Capture"),
        (VOID, "Void"),
        (REFUND, "Refund"),
        (VAULT, "Save card"),
    ]

    reference = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    order = models.ForeignKey(
        "order.Order",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="paypal_operations",
    )
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=64, blank=True)
    # PayPal's own event time, used by reconciliation (not our created-at).
    provider_time = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)
    claimed_at = models.DateTimeField()
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pk"]
        indexes = [models.Index(fields=["outcome"]), models.Index(fields=["provider_time"])]

    def __str__(self) -> str:
        return f"{self.kind} {self.reference} [{self.outcome}]"


class PayPalPayment(models.Model):
    """The PayPal side of one Oscar order."""

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZING = "authorizing"
    PAYER_ACTION_REQUIRED = "payer_action_required"
    AUTHORIZATION_PENDING = "authorization_pending"
    AUTHORIZED = "authorized"
    DECLINED = "declined"
    CAPTURING = "capturing"
    CAPTURE_PENDING = "capture_pending"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDING = "voiding"
    VOIDED = "voided"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"
    STATE_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZING, "Authorizing"),
        (PAYER_ACTION_REQUIRED, "Payer action required"),
        (AUTHORIZATION_PENDING, "Authorization pending at PayPal"),
        (AUTHORIZED, "Authorized (funds held)"),
        (DECLINED, "Declined"),
        (CAPTURING, "Capturing"),
        (CAPTURE_PENDING, "Capture pending at PayPal"),
        (CAPTURED, "Captured"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (VOIDING, "Voiding"),
        (VOIDED, "Voided (hold released)"),
        (CANCELLED, "Cancelled before payment"),
        (NEEDS_REVIEW, "Needs operator review"),
    ]

    order = models.OneToOneField("order.Order", on_delete=models.PROTECT, related_name="paypal_payment")
    source = models.OneToOneField(
        "payment.Source", null=True, blank=True, on_delete=models.PROTECT, related_name="paypal_payment"
    )
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    attempt = models.PositiveIntegerField(default=0)
    saved_card = models.ForeignKey(
        "paypal_payments.SavedCard", null=True, blank=True, on_delete=models.SET_NULL, related_name="payments"
    )
    card_label = models.CharField(max_length=64, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized = models.BooleanField(default=False)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    last_error = models.CharField(max_length=255, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"PayPal payment for order {self.order_id} [{self.state}]"


class PayPalRefund(models.Model):
    """A refund of the captured payment. ``(payment, idempotency_key)`` is
    unique, so a repeated request under the same key never refunds twice."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.PROTECT, related_name="refunds")
    idempotency_key = models.CharField(max_length=255)
    reference = models.CharField(max_length=120, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "idempotency_key"], name="paypal_refund_unique_key"),
        ]


class PayPalCustomer(models.Model):
    """The PayPal vault customer that groups a shopper's saved cards."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer")
    customer_id = models.CharField(max_length=64)


class SavedCard(models.Model):
    """A card saved in PayPal's vault. Only PayPal's token and a safe description
    are kept: brand, last digits, expiry and cardholder name."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards")
    vault_token_id = models.CharField(max_length=64, unique=True)
    reference = models.CharField(max_length=120, unique=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    name = models.CharField(max_length=128, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    provider_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["pk"]

    @property
    def label(self) -> str:
        return f"{self.brand or 'Card'} ending {self.last_digits}"
