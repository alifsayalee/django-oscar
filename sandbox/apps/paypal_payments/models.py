"""
Local state for PayPal payments.

Nothing here ever holds card numbers or security codes: a saved card is the
PayPal vault token plus the brand/last digits PayPal reports back.
"""
import uuid

from django.conf import settings
from django.db import models


class Outcome(models.TextChoices):
    """What is known about one provider write (see ``safe_write``)."""

    SENDING = 'sending', 'Sending'            # claimed, no answer yet
    DONE = 'done', 'Done'
    PENDING = 'pending', 'Pending'            # PayPal accepted it and has not finished
    FAILED = 'failed', 'Failed'               # never sent, refused, or reported failed/undone
    NEEDS_REVIEW = 'needs_review', 'Needs review'  # happened, but not as asked
    UNKNOWN = 'unknown', 'Unknown'            # may have happened; only PayPal can settle it


class InstallReference(models.Model):
    """
    A random prefix unique to this database, put in front of every reference
    sent to PayPal, so two installs sharing one PayPal account (or a rebuilt
    sandbox reusing order numbers) never send the same PayPal-Request-Id.
    """

    prefix = models.CharField(max_length=32, unique=True)


class PayPalOperation(models.Model):
    """
    The claim for one provider write step. ``reference`` is sent to PayPal as
    the PayPal-Request-Id; the unique constraint is what rejects a second
    claim for the same step, across requests and processes.
    """

    KIND_AUTHORIZE = 'authorize'
    KIND_REAUTHORIZE = 'reauthorize'
    KIND_CAPTURE = 'capture'
    KIND_VOID = 'void'
    KIND_REFUND = 'refund'
    KIND_VAULT = 'vault'
    KIND_CHOICES = [
        (KIND_AUTHORIZE, 'Authorize'),
        (KIND_REAUTHORIZE, 'Reauthorize'),
        (KIND_CAPTURE, 'Capture'),
        (KIND_VOID, 'Void'),
        (KIND_REFUND, 'Refund'),
        (KIND_VAULT, 'Save card'),
    ]

    reference = models.CharField(max_length=128, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    order_payment = models.ForeignKey(
        'OrderPayment', null=True, blank=True, on_delete=models.PROTECT,
        related_name='operations')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name='+')
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    amount = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    # PayPal's own event time, for reconciliation on PayPal's clock.
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    # Echoed amount when it differs from what was asked (needs_review).
    provider_amount = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    error_message = models.CharField(max_length=512, blank=True)
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['claimed_at']

    def __str__(self):
        return '%s (%s)' % (self.reference, self.outcome)


class OrderPayment(models.Model):
    """The PayPal payment for one Oscar order."""

    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZING = 'authorizing'
    AUTHORIZED = 'authorized'
    CAPTURING = 'capturing'
    CAPTURED = 'captured'
    PARTIALLY_REFUNDED = 'partially_refunded'
    REFUNDED = 'refunded'
    VOIDING = 'voiding'
    VOIDED = 'voided'
    CANCELLED = 'cancelled'
    NEEDS_REVIEW = 'needs_review'
    STATE_CHOICES = [(s, s.replace('_', ' ').capitalize()) for s in (
        AWAITING_PAYMENT, AUTHORIZING, AUTHORIZED, CAPTURING, CAPTURED,
        PARTIALLY_REFUNDED, REFUNDED, VOIDING, VOIDED, CANCELLED, NEEDS_REVIEW)]

    order = models.OneToOneField(
        'order.Order', on_delete=models.PROTECT, related_name='paypal_payment')
    state = models.CharField(max_length=24, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    # Bumped each time a payment attempt definitively fails, so the next
    # attempt goes to PayPal under a new reference.
    attempt = models.PositiveIntegerField(default=1)
    # Bumped to take a write lock before reserving refund amounts.
    lock_version = models.PositiveIntegerField(default=0)

    saved_card = models.ForeignKey(
        'SavedCard', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    void_status = models.CharField(max_length=32, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)

    last_error = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'PayPal payment for order %s (%s)' % (self.order.number, self.state)


class PayPalRefund(models.Model):
    """
    One refund request. Inserting it reserves ``amount`` against the capture
    (see ``services.refund``); a failed refund releases its reservation.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order_payment = models.ForeignKey(
        OrderPayment, on_delete=models.PROTECT, related_name='refunds')
    idempotency_key = models.CharField(max_length=255)
    reference = models.CharField(max_length=128, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['order_payment', 'idempotency_key'], name='paypal_refund_unique_key'),
        ]


class SavedCardQuerySet(models.QuerySet):
    def active(self):
        return self.filter(deleted_at__isnull=True)


class SavedCard(models.Model):
    """A card saved in the PayPal vault. Only the token and a safe description live here."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='paypal_saved_cards')
    paypal_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    # The PayPalOperation reference that created it, to answer a repeated save.
    operation_reference = models.CharField(max_length=128, unique=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    name = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    provider_deleted = models.BooleanField(default=False)

    objects = SavedCardQuerySet.as_manager()

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return '%s ending %s' % (self.brand or 'Card', self.last_digits)
