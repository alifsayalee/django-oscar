"""Only what Oscar lacks: PayPal's state for an order's payment, the vault customer, and the claim ledger.

Orders, lines, payment sources/transactions and saved cards are Oscar's own models
(``order.Order``, ``payment.Source``/``Transaction``, ``payment.Bankcard``).
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class PayPalPayment(models.Model):
    """PayPal's view of one order's payment, beside Oscar's ``payment.Source`` for it."""

    # Lifecycle doubles as the order-level mutex: every transition is a conditional UPDATE, so pay,
    # fulfil and cancel on one order cannot race each other into two provider writes.
    AWAITING_PAYMENT = "awaiting_payment"
    AUTHORIZING = "authorizing"
    AUTHORIZED = "authorized"
    CAPTURING = "capturing"
    CAPTURED = "captured"
    VOIDING = "voiding"
    VOIDED = "voided"
    CANCELLED = "cancelled"
    LIFECYCLE_CHOICES = [
        (AWAITING_PAYMENT, "Awaiting payment"),
        (AUTHORIZING, "Authorizing"),
        (AUTHORIZED, "Authorized (funds held)"),
        (CAPTURING, "Capturing"),
        (CAPTURED, "Captured"),
        (VOIDING, "Voiding"),
        (VOIDED, "Voided (hold released)"),
        (CANCELLED, "Cancelled before payment"),
    ]

    source = models.OneToOneField("payment.Source", on_delete=models.PROTECT, related_name="paypal")
    order = models.OneToOneField("order.Order", on_delete=models.PROTECT, related_name="paypal_payment")
    lifecycle = models.CharField(max_length=32, choices=LIFECYCLE_CHOICES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=12)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    bankcard = models.ForeignKey(
        "payment.Bankcard", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized = models.BooleanField(default=False)

    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    # Bumped inside the refund claim so concurrent refunds serialize on this row.
    refund_lock = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "PayPal payment"

    def __str__(self):
        return f"PayPal payment for order {self.order.number} ({self.lifecycle})"

    @property
    def refundable_amount(self):
        if self.captured_amount is None:
            return Decimal("0.00")
        return self.captured_amount - self.refunded_amount


class PayPalCustomer(models.Model):
    """The PayPal vault customer a shopper's saved cards belong to."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer")
    customer_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PayPal customer {self.customer_id}"


class PaymentOperation(models.Model):
    """One provider write step: the claim taken before the call, and the outcome PayPal reported.

    ``ref`` is derived from the operation and the step, is UNIQUE, and is what the write is sent and
    later checked by. The row exists before PayPal is called and is never deleted.
    """

    PLACE_ORDER = "place_order"
    CREATE_ORDER = "create_order"
    AUTHORIZE = "authorize"
    REAUTHORIZE = "reauthorize"
    CAPTURE = "capture"
    VOID = "void"
    REFUND = "refund"
    VAULT_CREATE = "vault_create"
    VAULT_DELETE = "vault_delete"
    KIND_CHOICES = [
        (PLACE_ORDER, "Place order (local)"),
        (CREATE_ORDER, "Create PayPal order / authorize card"),
        (AUTHORIZE, "Authorize PayPal order"),
        (REAUTHORIZE, "Reauthorize"),
        (CAPTURE, "Capture"),
        (VOID, "Void"),
        (REFUND, "Refund"),
        (VAULT_CREATE, "Save card"),
        (VAULT_DELETE, "Delete saved card"),
    ]

    SENDING = "sending"
    DONE = "done"
    PENDING = "pending"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    OUTCOME_CHOICES = [
        (SENDING, "Sending (claimed, no answer yet)"),
        (DONE, "Done"),
        (PENDING, "Pending at PayPal"),
        (FAILED, "Failed / refused / undone"),
        (NEEDS_REVIEW, "Happened, but not as asked"),
        (UNKNOWN, "Unknown — may have happened"),
    ]
    UNSETTLED = (SENDING, UNKNOWN, NEEDS_REVIEW)

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    ref = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    order = models.ForeignKey(
        "order.Order", null=True, blank=True, on_delete=models.PROTECT, related_name="paypal_operations"
    )
    payment = models.ForeignKey(
        PayPalPayment, null=True, blank=True, on_delete=models.PROTECT, related_name="operations"
    )

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=12, blank=True)
    # What the caller asked for under this ref; a repeat that asks for something else is refused.
    fingerprint = models.CharField(max_length=128, blank=True)

    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Provider facts and error codes only — never card data.
    detail = models.JSONField(default=dict, blank=True)

    claimed_at = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pk"]
        verbose_name = "PayPal payment operation"

    def __str__(self):
        return f"{self.kind} {self.ref} ({self.outcome})"
