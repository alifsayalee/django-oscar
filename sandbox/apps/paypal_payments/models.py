import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


def new_request_id():
    return str(uuid.uuid4())


class OperationClaim(models.Model):
    """
    A claim on a payment operation, taken before PayPal is called.

    The unique ``key`` is what stops the same operation being requested twice
    (a double click, a client retry, two operators): the second insert fails
    with an ``IntegrityError``. A claim whose PayPal call was refused is
    deleted so the operation can be tried again; one whose outcome could not
    be read stays behind as ``OUTCOME_UNKNOWN`` and is resumed with the same
    ``request_id``, which PayPal de-duplicates on.
    """

    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    STATE_CHOICES = [
        (IN_PROGRESS, "In progress"),
        (SUCCEEDED, "Succeeded"),
        (OUTCOME_UNKNOWN, "Outcome unknown"),
    ]

    key = models.CharField(max_length=255, unique=True)
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=IN_PROGRESS)
    # Sent to PayPal as the PayPal-Request-Id header of the claimed call.
    request_id = models.CharField(max_length=64, default=new_request_id)
    # What a repeated request needs to answer without calling PayPal again.
    result = models.JSONField(default=dict, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key} ({self.state})"


class PayPalCustomer(models.Model):
    """The PayPal vault customer that a shopper's saved cards are grouped under."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="paypal_customer"
    )
    paypal_customer_id = models.CharField(max_length=64)

    def __str__(self):
        return self.paypal_customer_id


class SavedCardQuerySet(models.QuerySet):
    def active(self):
        return self.filter(deleted_at__isnull=True)


class SavedCard(models.Model):
    """
    A card vaulted at PayPal for a shopper.

    Only PayPal's token id and the non-sensitive description PayPal returns
    (brand, last digits, expiry) are kept - never the card number or CVC.
    Deleting a card tombstones it (``deleted_at``) at once, so it stops being
    listed or usable, and then removes the token from PayPal's vault.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_cards"
    )
    paypal_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    cardholder_name = models.CharField(max_length=255, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    # False while a deleted card's token still has to be removed from PayPal.
    vault_token_deleted = models.BooleanField(default=False)

    objects = SavedCardQuerySet.as_manager()

    class Meta:
        ordering = ["-date_created"]

    def __str__(self):
        return f"{self.brand} ending {self.last_digits}"


class PayPalPayment(models.Model):
    """The PayPal side of an Oscar order: the hold, the capture and the refunds."""

    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    VOIDED = "VOIDED"
    STATE_CHOICES = [
        (AUTHORIZED, "Authorized"),
        (CAPTURED, "Captured"),
        (PARTIALLY_REFUNDED, "Partially refunded"),
        (REFUNDED, "Refunded"),
        (VOIDED, "Voided"),
    ]

    order = models.OneToOneField(
        "order.Order", on_delete=models.PROTECT, related_name="paypal_payment"
    )
    state = models.CharField(max_length=32, choices=STATE_CHOICES)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    paypal_order_id = models.CharField(max_length=64)
    invoice_id = models.CharField(max_length=127)
    saved_card = models.ForeignKey(
        SavedCard, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments"
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    # The current authorization (a reauthorization replaces it).
    authorization_id = models.CharField(max_length=64)
    authorization_status = models.CharField(max_length=32)
    authorized_at = models.DateTimeField()
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorization_count = models.PositiveSmallIntegerField(default=0)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    # Refundable balance bookkeeping, in integer minor units (cents) so that
    # the database can compare it exactly. A refund reserves its amount with a
    # conditional UPDATE before PayPal is called, so concurrent partial refunds
    # can never add up past what was captured.
    captured_minor = models.BigIntegerField(default=0)
    refund_reserved_minor = models.BigIntegerField(default=0)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    source = models.ForeignKey(
        "payment.Source", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(refund_reserved_minor__gte=0)
                & models.Q(refund_reserved_minor__lte=models.F("captured_minor")),
                name="paypal_refund_reserved_within_captured",
            ),
        ]

    def __str__(self):
        return f"PayPal payment for order {self.order.number} ({self.state})"


class PayPalRefund(models.Model):
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    COMPLETED = "COMPLETED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    STATUS_CHOICES = [
        (PENDING_SUBMISSION, "Being submitted"),
        (OUTCOME_UNKNOWN, "Outcome unknown"),
        (COMPLETED, "Completed"),
        (PENDING, "Pending at PayPal"),
        (FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.PROTECT, related_name="refunds")
    idempotency_key = models.CharField(max_length=255)
    # Sent to PayPal as the PayPal-Request-Id header.
    request_id = models.CharField(max_length=64, default=new_request_id)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=PENDING_SUBMISSION)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    note = models.CharField(max_length=255, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date_created"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"], name="paypal_refund_unique_idempotency_key"
            ),
        ]

    def __str__(self):
        return f"Refund {self.amount} ({self.status})"
