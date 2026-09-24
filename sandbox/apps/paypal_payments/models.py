"""
Local state for the PayPal integration.

PayPal stays the system of record for what money moved. These tables record
what this application *asked for* (``PaymentOperation`` — one row per provider
write, created and committed before the call), plus the PayPal ids and
statuses a later request needs in order to act on a payment.

Full card details are never stored here: a saved card is a PayPal vault token
plus the brand / last digits / expiry PayPal echoes back.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class IntegrationInstall(models.Model):
    """
    A random token generated once per database. It prefixes every reference
    sent to PayPal, so that two installs sharing one PayPal account (or a
    rebuilt sandbox reusing order numbers) never collide.
    """

    token = models.CharField(max_length=16, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return self.token


class SavedCard(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_saved_cards"
    )
    paypal_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    # Set before the provider delete is attempted: from that moment the card
    # is neither listed nor usable, whatever PayPal answers.
    deleted_at = models.DateTimeField(null=True, blank=True)
    provider_deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return "%s ending %s" % (self.brand or "card", self.last_digits)

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None


class PayPalPayment(models.Model):
    """PayPal payment state for one Oscar order."""

    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZING = "authorizing"
    PAYMENT_FAILED = "payment_failed"
    AUTHORIZED = "authorized"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    CAPTURING = "capturing"
    CAPTURE_PENDING = "capture_pending"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDING = "voiding"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"
    STATE_CHOICES = [
        (s, s)
        for s in (
            AWAITING_PAYMENT, AUTHORIZING, PAYMENT_FAILED, AUTHORIZED, AUTHORIZATION_EXPIRED,
            CAPTURING, CAPTURE_PENDING, CAPTURED, PARTIALLY_REFUNDED, REFUNDED, VOIDING,
            CANCELLED, NEEDS_REVIEW,
        )
    ]

    order = models.OneToOneField("order.Order", on_delete=models.CASCADE, related_name="paypal_payment")
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    state_detail = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)

    saved_card = models.ForeignKey(SavedCard, null=True, blank=True, on_delete=models.SET_NULL)
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized_from = models.CharField(max_length=64, blank=True)

    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Bumped by every locked section: the UPDATE takes the row's write lock
    # first, on SQLite as on PostgreSQL, so check-then-claim sequences
    # (refund headroom, next attempt number) are serialised per order.
    lock_version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return "PayPal payment for order %s (%s)" % (self.order_id, self.state)


class PaymentOperation(models.Model):
    """
    One provider write step, claimed before the call is made.

    ``reference`` is unique: inserting it is the claim, and the database
    rejects a second claim for the same step. It is also what PayPal receives
    as ``PayPal-Request-Id`` (and, for the authorization, as ``invoice_id``),
    so an outcome that was lost in transit can be looked up by it.
    """

    PAY = "pay"
    AUTHORIZE = "authorize"
    REAUTHORIZE = "reauthorize"
    CAPTURE = "capture"
    VOID = "void"
    REFUND = "refund"
    VAULT = "vault"
    KIND_CHOICES = [(k, k) for k in (PAY, AUTHORIZE, REAUTHORIZE, CAPTURE, VOID, REFUND, VAULT)]

    SENDING = "sending"
    DONE = "done"
    PENDING = "pending"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    OUTCOME_CHOICES = [(o, o) for o in (SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN)]
    # Outcomes that may hold (or will hold) money at PayPal.
    LIVE_OUTCOMES = (SENDING, DONE, PENDING, NEEDS_REVIEW, UNKNOWN)

    reference = models.CharField(max_length=127, unique=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    attempt = models.PositiveIntegerField(default=1)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    payment = models.ForeignKey(
        PayPalPayment, null=True, blank=True, on_delete=models.CASCADE, related_name="operations"
    )
    saved_card = models.ForeignKey(SavedCard, null=True, blank=True, on_delete=models.SET_NULL)
    # Hash of the caller's idempotency key (refunds) or of the card
    # fingerprint (vault) — never the raw value.
    request_key = models.CharField(max_length=64, blank=True, db_index=True)

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    # PayPal's own clock for the event: reconciliation filters on this.
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    detail = models.CharField(max_length=255, blank=True)

    claimed_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["claimed_at", "pk"]

    def __str__(self) -> str:
        return "%s %s (%s)" % (self.kind, self.reference, self.outcome)
