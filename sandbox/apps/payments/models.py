"""
Provider state for PayPal payments.

The order itself, its lines, its payment source and transactions are Oscar's
own models (``order.Order``, ``payment.Source``, ``payment.Transaction``).
These tables only hold what PayPal owns and what a later request needs to act
on it: ids and statuses of the hold, the capture and the refunds, the vaulted
card tokens, and one claim row per provider write.

No card number or security code is ever stored here.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class InstallIdentity(models.Model):
    """
    A random prefix, generated once per database, that makes every reference
    this install sends to PayPal distinct from other installs sharing the
    same PayPal account.
    """

    prefix = models.CharField(max_length=24, unique=True)

    def __str__(self):
        return self.prefix


class Outcome(models.TextChoices):
    """What is known about one provider write (see ``ProviderWrite``)."""

    SENDING = 'sending', 'Sending'  # claimed, no answer yet
    DONE = 'done', 'Done'
    PENDING = 'pending', 'Pending'  # provider accepted it and has not finished
    FAILED = 'failed', 'Failed'  # never sent, refused, or failed/undone at the provider
    NEEDS_REVIEW = 'needs_review', 'Needs review'  # happened, but not as asked
    UNKNOWN = 'unknown', 'Unknown'  # may have happened


class ProviderWrite(models.Model):
    """
    One row per provider write step that creates, charges or sends.

    ``ref`` is sent to PayPal as ``PayPal-Request-Id``. Its UNIQUE constraint
    is the claim: a second request for the same step fails to insert and is
    answered from this row instead of calling PayPal again.
    """

    ref = models.CharField(max_length=128, unique=True)
    operation = models.CharField(max_length=32)
    order = models.ForeignKey(
        'order.Order', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='paypal_writes')
    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=64, blank=True)
    # The provider's own event time, used by reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True)
    detail = models.CharField(max_length=500, blank=True)
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['provider_time'])]

    def __str__(self):
        return '%s %s (%s)' % (self.operation, self.ref, self.outcome)


class PaymentState(models.TextChoices):
    AWAITING_PAYMENT = 'awaiting_payment', 'Awaiting payment'
    AUTHORIZING = 'authorizing', 'Authorizing'
    AUTH_PENDING = 'auth_pending', 'Authorization pending'
    AUTH_FAILED = 'auth_failed', 'Authorization failed'
    AUTH_UNKNOWN = 'auth_unknown', 'Authorization outcome unknown'
    AUTHORIZED = 'authorized', 'Authorized'
    AUTHORIZATION_EXPIRED = 'authorization_expired', 'Authorization expired'
    CAPTURING = 'capturing', 'Capturing'
    CAPTURE_PENDING = 'capture_pending', 'Capture pending'
    CAPTURE_FAILED = 'capture_failed', 'Capture failed'
    CAPTURE_UNKNOWN = 'capture_unknown', 'Capture outcome unknown'
    CAPTURED = 'captured', 'Captured'
    VOIDING = 'voiding', 'Voiding'
    VOID_PENDING = 'void_pending', 'Void pending'
    VOID_UNKNOWN = 'void_unknown', 'Void outcome unknown'
    VOIDED = 'voided', 'Voided'
    CANCELLED = 'cancelled', 'Cancelled (no payment taken)'
    NEEDS_REVIEW = 'needs_review', 'Needs review'


class OrderPayment(models.Model):
    """The PayPal side of one Oscar order."""

    order = models.OneToOneField(
        'order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    source = models.ForeignKey(
        'payment.Source', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+')
    state = models.CharField(
        max_length=32, choices=PaymentState.choices,
        default=PaymentState.AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # Each /pay that starts over after a definite failure is a new attempt,
    # sent under a new reference.
    attempt = models.PositiveIntegerField(default=0)
    saved_card = models.ForeignKey(
        'SavedCard', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+')
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)

    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    # When the hold currently in use was created, and when the first one of
    # this attempt was (the 29-day renewal limit counts from that one).
    authorized_at = models.DateTimeField(null=True, blank=True)
    original_authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))
    paypal_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    # Confirmed refunds, and everything reserved for refunds that are
    # confirmed, pending or of unknown outcome. The reservation is what
    # guards the captured amount.
    refunded_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))
    refund_reserved = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))

    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'PayPal payment for order %s (%s)' % (self.order_id, self.state)


class RefundState(models.TextChoices):
    SENDING = 'sending', 'Sending'
    DONE = 'done', 'Refunded'
    PENDING = 'pending', 'Pending'
    FAILED = 'failed', 'Failed'
    UNKNOWN = 'unknown', 'Outcome unknown'
    NEEDS_REVIEW = 'needs_review', 'Needs review'


class PaymentRefund(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(
        OrderPayment, on_delete=models.CASCADE, related_name='refunds')
    idempotency_key = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    state = models.CharField(
        max_length=16, choices=RefundState.choices, default=RefundState.SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['payment', 'idempotency_key'],
                name='uniq_refund_key_per_payment'),
        ]

    def __str__(self):
        return 'Refund %s of %s (%s)' % (self.public_id, self.amount, self.state)


class PayPalCustomer(models.Model):
    """The PayPal vault customer id PayPal generated for a shopper."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='paypal_customer')
    vault_customer_id = models.CharField(max_length=64, unique=True)


class CardState(models.TextChoices):
    SENDING = 'sending', 'Saving'
    ACTIVE = 'active', 'Active'
    FAILED = 'failed', 'Failed'
    UNKNOWN = 'unknown', 'Outcome unknown'
    DELETED = 'deleted', 'Deleted'


class SavedCard(models.Model):
    """
    A card in PayPal's vault. Only PayPal's token id and a safe description
    (brand, last digits, expiry) are kept.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='saved_cards')
    request_ref = models.CharField(max_length=128, unique=True)
    state = models.CharField(
        max_length=16, choices=CardState.choices, default=CardState.SENDING)
    paypal_token_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    cardholder_name = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return '%s ending %s' % (self.brand or 'Card', self.last_digits or '????')
