"""
Local records for the PayPal integration.

Nothing here ever holds a full card number or security code: saved cards keep
only PayPal's vault token id plus the brand/last digits/expiry PayPal reports.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class Outcome(models.TextChoices):
    """What one provider write step is known to have done."""

    SENDING = "sending"  # claimed, no answer yet
    DONE = "done"
    PENDING = "pending"  # the provider accepted it and has not finished
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"  # happened, but not as asked
    UNKNOWN = "unknown"  # may have happened


class InstallIdentity(models.Model):
    """
    A random id generated once per database. It prefixes every reference sent
    to PayPal, so two installs (or a rebuilt database) sharing one PayPal
    account never collide on ``PayPal-Request-Id``.
    """

    value = models.CharField(max_length=32, unique=True)


class ProviderWrite(models.Model):
    """
    The claim for one provider write step.

    ``ref`` is derived from the operation (never random) and is also sent to
    PayPal as ``PayPal-Request-Id``; the UNIQUE constraint is what rejects a
    second request for the same step, across processes.
    """

    ref = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=32, db_index=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    provider_id = models.CharField(max_length=128, blank=True)
    provider_status = models.CharField(max_length=64, blank=True)
    # PayPal's own event time for this write, used for reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    # A short, card-free description of the last provider error (name/message/debug id).
    detail = models.CharField(max_length=500, blank=True)
    claimed_at = models.DateTimeField(db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self) -> str:
        return f"{self.ref} [{self.outcome}]"


class PayPalCustomer(models.Model):
    """The PayPal vault customer id that holds a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer"
    )
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    # Bumped on every completed card deletion so re-saving the same card is a new write.
    times_deleted = models.PositiveIntegerField(default=0)


class SavedCard(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards"
    )
    ref = models.CharField(max_length=200, unique=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    paypal_token_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Set the moment a delete is requested: from then on the card is neither
    # listed nor usable, whatever PayPal's answer to the delete turns out to be.
    deleted_at = models.DateTimeField(null=True, blank=True)
    delete_outcome = models.CharField(max_length=16, choices=Outcome.choices, blank=True)

    class Meta:
        ordering = ["pk"]

    @property
    def is_usable(self) -> bool:
        return self.deleted_at is None and self.outcome == Outcome.DONE and bool(self.paypal_token_id)


class PaymentState(models.TextChoices):
    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZING = "authorizing"
    AUTHORIZED = "authorized"
    PAYMENT_FAILED = "payment_failed"
    CAPTURING = "capturing"
    CAPTURE_PENDING = "capture_pending"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"


class OrderPayment(models.Model):
    """PayPal-side state for one Oscar order: the hold, the capture, the refunds."""

    order = models.OneToOneField("order.Order", on_delete=models.CASCADE, related_name="paypal_payment")
    source = models.OneToOneField(
        "payment.Source", on_delete=models.SET_NULL, null=True, blank=True, related_name="paypal_payment"
    )
    state = models.CharField(max_length=32, choices=PaymentState.choices, default=PaymentState.AWAITING_PAYMENT)
    # Each new pay attempt (after a declined/failed one) gets fresh provider references.
    attempt = models.PositiveIntegerField(default=1)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    saved_card = models.ForeignKey(SavedCard, on_delete=models.SET_NULL, null=True, blank=True)
    card_label = models.CharField(max_length=64, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)

    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    # When the original hold was placed: the reauthorization window counts from here.
    authorized_at = models.DateTimeField(null=True, blank=True)
    # When the current (possibly renewed) hold was placed.
    honor_period_start = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorizations = models.PositiveIntegerField(default=0)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    last_error = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class PaymentRefund(models.Model):
    """
    One refund request. Creating the row reserves its amount against the
    capture, so concurrent partial refunds can never exceed what was captured.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(OrderPayment, on_delete=models.CASCADE, related_name="refunds")
    idempotency_key = models.CharField(max_length=128)
    ref = models.CharField(max_length=200, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    booked = models.BooleanField(default=False)  # recorded on the Oscar payment Source
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "idempotency_key"], name="unique_refund_key_per_payment"),
        ]

    @property
    def reserves_funds(self) -> bool:
        # A failed refund returned nothing; everything else may have (or did) move money.
        return self.outcome != Outcome.FAILED
