import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

OUTCOMES = [
    ("sending", "Sending"),
    ("done", "Done"),
    ("pending", "Pending at PayPal"),
    ("failed", "Failed"),
    ("needs_review", "Needs review"),
    ("unknown", "Outcome unknown"),
]


class InstallIdentity(models.Model):
    """A random prefix generated once per database.

    Every PayPal reference starts with it, so two installs sharing one PayPal account (and both
    numbering orders from 100001) never collide on a PayPal-Request-Id or an invoice id.
    """

    key = models.CharField(max_length=16, unique=True, default="install")
    prefix = models.CharField(max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)


class ProviderWrite(models.Model):
    """The claim store: one row per PayPal write step, inserted BEFORE the call.

    The unique index on ``reference`` is what rejects a second attempt at the same step.
    """

    reference = models.CharField(max_length=100, unique=True)
    operation = models.CharField(max_length=32)
    outcome = models.CharField(max_length=16, choices=OUTCOMES, default="sending")
    provider_id = models.CharField(max_length=64, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    detail = models.CharField(max_length=255, blank=True)
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["outcome", "claimed_at"])]

    def __str__(self):
        return f"{self.reference} ({self.outcome})"


class SavedCard(models.Model):
    ACTIVE, DELETING, DELETED = "active", "deleting", "deleted"
    STATUSES = [(ACTIVE, "Active"), (DELETING, "Deleting"), (DELETED, "Deleted")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards")
    # PayPal's vault payment-token id. The card itself lives only in PayPal's vault.
    paypal_token_id = models.CharField(max_length=64, unique=True)
    vault_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    # HMAC of number+expiry under SECRET_KEY: lets a double-submit of the same card find its claim,
    # without being card data.
    fingerprint = models.CharField(max_length=64, db_index=True)
    reference = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=16, choices=STATUSES, default=ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class PayPalPayment(models.Model):
    """PayPal's side of an Oscar order: the hold, the capture and the refunds."""

    AWAITING, AUTH_PENDING, AUTHORIZED = "awaiting_payment", "authorization_pending", "authorized"
    CAPTURE_PENDING, CAPTURED, VOIDED = "capture_pending", "captured", "voided"
    CANCELLED, NEEDS_REVIEW, UNKNOWN = "cancelled", "needs_review", "outcome_unknown"
    STATUSES = [(s, s) for s in (AWAITING, AUTH_PENDING, AUTHORIZED, CAPTURE_PENDING, CAPTURED, VOIDED,
                                 CANCELLED, NEEDS_REVIEW, UNKNOWN)]

    order = models.OneToOneField("order.Order", on_delete=models.CASCADE, related_name="paypal_payment")
    source = models.OneToOneField("payment.Source", null=True, blank=True, on_delete=models.SET_NULL)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=32, choices=STATUSES, default=AWAITING)
    # Bumped each time a pay attempt definitively fails, so the next attempt gets a new reference.
    attempt = models.PositiveIntegerField(default=0)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    saved_card = models.ForeignKey(SavedCard, null=True, blank=True, on_delete=models.SET_NULL)
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    # PayPal's clock: when the ORIGINAL hold was made (drives the 29-day limit) and when the
    # current one was (re)made (drives the 3-day honor period).
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_renewed_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    # Money promised to refunds that are done, pending, or not yet settled; never exceeds captured_amount.
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(refund_reserved__lte=models.F("captured_amount")),
                name="paypal_refund_reserved_within_captured",
            ),
        ]


class PayPalRefund(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.CASCADE, related_name="refunds")
    idempotency_key = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=16, choices=OUTCOMES, default="sending")
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "idempotency_key"], name="paypal_refund_key_unique"),
        ]
