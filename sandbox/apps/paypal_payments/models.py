"""
PayPal state kept beside Oscar's own records.

Oscar's ``order.Order`` / ``order.Line`` hold the order, ``payment.Source`` /
``payment.Transaction`` hold the money ledger and ``payment.Bankcard`` holds a
saved card (masked). These models carry what Oscar has no place for: the ids
and statuses PayPal owns, and the claims that keep a repeated request from
reaching PayPal twice. No card number or security code is ever stored.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class PayPalPayment(models.Model):
    # Status doubles as the claim: every transition into an in-flight status is a
    # single conditional UPDATE, so a second concurrent request is refused by the DB.
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    AUTHORIZING = "AUTHORIZING"
    AUTHORIZED = "AUTHORIZED"
    DECLINED = "DECLINED"
    CAPTURING = "CAPTURING"
    CAPTURE_PENDING = "CAPTURE_PENDING"
    CAPTURED = "CAPTURED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    VOIDING = "VOIDING"
    VOIDED = "VOIDED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNKNOWN = "UNKNOWN"

    STATUS_CHOICES = [(s, s) for s in (
        AWAITING_PAYMENT, AUTHORIZING, AUTHORIZED, DECLINED, CAPTURING, CAPTURE_PENDING, CAPTURED,
        PARTIALLY_REFUNDED, REFUNDED, VOIDING, VOIDED, CANCELLED, EXPIRED, NEEDS_REVIEW, UNKNOWN,
    )]

    order = models.OneToOneField("order.Order", on_delete=models.CASCADE, related_name="paypal_payment")
    source = models.OneToOneField(
        "payment.Source", on_delete=models.SET_NULL, null=True, blank=True, related_name="paypal_payment"
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=AWAITING_PAYMENT, db_index=True)
    # The operation that was in flight when its outcome became unknown.
    pending_operation = models.CharField(max_length=32, blank=True)
    currency = models.CharField(max_length=12)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # Namespace for PayPal-Request-Id values; stable for this payment, unique across databases.
    request_ns = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    attempt = models.PositiveIntegerField(default=0)

    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)
    bankcard = models.ForeignKey(
        "payment.Bankcard", on_delete=models.SET_NULL, null=True, blank=True, related_name="paypal_payments"
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)  # PayPal's create_time
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorization_count = models.PositiveIntegerField(default=0)

    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)  # PayPal's create_time

    # Refund accounting: reserved includes in-flight refunds, so the cap holds under concurrency.
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    last_error = models.CharField(max_length=255, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(refund_reserved__gte=0), name="paypal_payment_refund_reserved_non_negative"
            ),
        ]

    def request_id(self, operation, attempt=None):
        return f"{self.request_ns}-{operation}-{self.attempt if attempt is None else attempt}"

    def __str__(self):
        return f"PayPal payment for order {self.order.number} ({self.status})"


class PayPalRefund(models.Model):
    SUBMITTING = "SUBMITTING"
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    STATUS_CHOICES = [(s, s) for s in (SUBMITTING, PENDING, COMPLETED, FAILED, CANCELLED, UNKNOWN)]
    # Refunds in these states still hold their share of the refundable amount.
    HOLDING = (SUBMITTING, PENDING, COMPLETED, UNKNOWN)

    payment = models.ForeignKey(PayPalPayment, on_delete=models.CASCADE, related_name="refunds")
    idempotency_key = models.CharField(max_length=255)
    # Our reference, sent to PayPal as custom_id: finds the refund when its outcome is unknown.
    reference = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SUBMITTING)
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)  # PayPal's create_time
    last_error = models.CharField(max_length=255, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["payment", "idempotency_key"], name="paypal_refund_unique_key"),
        ]

    def request_id(self):
        return f"{self.payment.request_ns}-refund-{self.reference}"


class PayPalCustomer(models.Model):
    """PayPal's vault customer id for a shopper; all their saved cards live under it."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer")
    customer_id = models.CharField(max_length=64, unique=True)
    created = models.DateTimeField(auto_now_add=True)


class PayPalVaultCard(models.Model):
    """Links Oscar's masked ``payment.Bankcard`` to the PayPal vault token that can charge it."""

    ACTIVE = "ACTIVE"
    DELETING = "DELETING"
    STATE_CHOICES = [(ACTIVE, ACTIVE), (DELETING, DELETING)]

    bankcard = models.OneToOneField("payment.Bankcard", on_delete=models.CASCADE, related_name="paypal_vault")
    token_id = models.CharField(max_length=64, unique=True)
    customer_id = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=ACTIVE, db_index=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)


class CardSaveClaim(models.Model):
    """One save-card request; the unique key stops a double-submit vaulting the card twice."""

    IN_PROGRESS = "IN_PROGRESS"
    SAVED = "SAVED"
    UNKNOWN = "UNKNOWN"
    STATE_CHOICES = [(s, s) for s in (IN_PROGRESS, SAVED, UNKNOWN)]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="card_save_claims")
    idempotency_key = models.CharField(max_length=255)
    request_ns = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=IN_PROGRESS)
    # What we can match an unknown vault write on later (never the card number).
    card_last_digits = models.CharField(max_length=4, blank=True)
    card_expiry = models.CharField(max_length=7, blank=True)
    bankcard = models.ForeignKey("payment.Bankcard", on_delete=models.SET_NULL, null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "idempotency_key"], name="card_save_claim_unique_key"),
        ]
