"""
PayPal-side state for Oscar orders and saved cards.

Oscar's own ``order.Order``/``order.Line`` hold the order and
``payment.Source``/``payment.Transaction`` hold the money ledger. These models
only record what PayPal owns (its ids, statuses and timestamps) plus the claim
rows that make every PayPal write happen once. No card number or security code
is ever stored here.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q


class PayPalCustomer(models.Model):
    """The PayPal vault customer id PayPal assigned to a shopper."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer"
    )
    paypal_customer_id = models.CharField(max_length=64)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return "%s -> %s" % (self.user_id, self.paypal_customer_id)


class SavedCard(models.Model):
    """A card vaulted at PayPal; only PayPal's token id and a safe description are kept."""

    CREATING = "creating"  # claimed, PayPal call in flight
    ACTIVE = "active"
    UNKNOWN = "unknown"  # PayPal may have vaulted it; resend under the same request id
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards"
    )
    # Caller-supplied Idempotency-Key (or a fresh uuid) - one vaulting per key.
    client_key = models.CharField(max_length=128)
    state = models.CharField(max_length=16, default=CREATING, db_index=True)
    paypal_token_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_created"]
        constraints = [
            models.UniqueConstraint(fields=["user", "client_key"], name="payments_savedcard_user_key"),
        ]

    def __str__(self):
        return "%s ****%s (%s)" % (self.brand, self.last_digits, self.state)

    @property
    def request_id(self):
        return "card-%s" % self.public_id


class OrderRequestKey(models.Model):
    """Makes ``POST /api/orders`` idempotent under a caller-supplied Idempotency-Key."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=128)
    order = models.ForeignKey("order.Order", on_delete=models.CASCADE, null=True, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="payments_orderkey_user_key"),
        ]


class PayPalPayment(models.Model):
    """
    One payment attempt for an Oscar order, and the PayPal state behind it.

    At most one attempt per order is live at a time (the partial unique
    constraint below): an attempt that definitely failed releases the order for
    a new one; everything else - including ``unknown`` - holds it.
    """

    # Authorization phase
    CREATING = "creating"  # claimed; create/authorize in flight
    AUTH_PENDING = "authorization_pending"
    AUTHORIZED = "authorized"
    FAILED = "failed"
    UNKNOWN = "unknown"  # PayPal may have acted; re-invoke to resume under the same request ids
    NEEDS_REVIEW = "needs_review"  # PayPal acted, but not as asked (amount mismatch)
    # Void phase
    VOIDING = "voiding"
    VOID_UNKNOWN = "void_unknown"
    VOIDED = "voided"
    # Capture phase
    CAPTURING = "capturing"
    CAPTURE_UNKNOWN = "capture_unknown"
    CAPTURE_PENDING = "capture_pending"
    CAPTURED = "captured"

    ref = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order = models.ForeignKey("order.Order", on_delete=models.CASCADE, related_name="paypal_payments")
    source = models.ForeignKey(
        "payment.Source", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    saved_card = models.ForeignKey(
        SavedCard, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    attempt = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=32, default=CREATING, db_index=True)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    card_label = models.CharField(max_length=64, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)

    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_time = models.DateTimeField(null=True, blank=True)  # PayPal's clock
    authorization_expires = models.DateTimeField(null=True, blank=True)
    reauthorization_count = models.PositiveIntegerField(default=0)

    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    capture_time = models.DateTimeField(null=True, blank=True)  # PayPal's clock
    captured_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    # Sum of every refund that is not definitely failed - the refund cap.
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))

    failure_reason = models.CharField(max_length=255, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_created"]
        constraints = [
            models.UniqueConstraint(
                fields=["order"], condition=~Q(state="failed"), name="payments_one_live_payment_per_order"
            ),
            models.UniqueConstraint(fields=["order", "attempt"], name="payments_payment_order_attempt"),
        ]

    def __str__(self):
        return "PayPal payment %s for order %s (%s)" % (self.ref, self.order_id, self.state)

    def request_id(self, action):
        return "%s-%s" % (self.ref, action)


class PayPalRefund(models.Model):
    SENDING = "sending"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.CASCADE, related_name="refunds")
    idempotency_key = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    reason = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=16, default=SENDING, db_index=True)
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    refund_time = models.DateTimeField(null=True, blank=True)  # PayPal's clock
    failure_reason = models.CharField(max_length=255, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date_created"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "idempotency_key"], name="payments_refund_idempotency"),
        ]

    def __str__(self):
        return "Refund %s of %s (%s)" % (self.public_id, self.amount, self.state)

    @property
    def request_id(self):
        return "refund-%s" % self.public_id
