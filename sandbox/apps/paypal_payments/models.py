"""
PayPal state that Oscar's own models have no place for.

Oscar's models stay the record of the order (``order.Order``/``order.Line``),
of money movement (``payment.Source``/``payment.Transaction``) and of a
shopper's saved cards (``payment.Bankcard``, masked). These models add what
PayPal owns and a later request needs to act on: ids and statuses of the
PayPal order, authorization, capture and refunds, and one claim row per write
sent to PayPal so a repeated request never sends a second write.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class Outcome(models.TextChoices):
    # Claimed; the provider has not answered yet.
    SENDING = 'sending'
    # The provider says what was asked is in effect.
    DONE = 'done'
    # The provider accepted it and has not finished.
    PENDING = 'pending'
    # Never sent, refused, or reported failed/undone by the provider.
    FAILED = 'failed'
    # It happened, but not as asked (e.g. a different amount).
    NEEDS_REVIEW = 'needs_review'
    # May have happened; only the provider's answer can settle it.
    UNKNOWN = 'unknown'


class PaymentState(models.TextChoices):
    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZING = 'authorizing'
    AUTHORIZATION_FAILED = 'authorization_failed'
    AUTHORIZED = 'authorized'
    CAPTURING = 'capturing'
    CAPTURE_FAILED = 'capture_failed'
    CAPTURED = 'captured'
    PARTIALLY_REFUNDED = 'partially_refunded'
    REFUNDED = 'refunded'
    VOIDING = 'voiding'
    VOIDED = 'voided'
    NEEDS_REVIEW = 'needs_review'


class PayPalInstallation(models.Model):
    """
    A random key made once per database. Every reference sent to PayPal
    (PayPal-Request-Id, invoice id) carries it, because order numbers restart
    in every fresh install while the PayPal account - and its memory of
    request ids and invoice ids - is shared.
    """

    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.key)


class PayPalPayment(models.Model):
    """
    The PayPal side of one Oscar order: created with the order, in the
    awaiting-payment state.
    """

    order = models.OneToOneField(
        'order.Order', on_delete=models.PROTECT, related_name='paypal_payment')
    source = models.OneToOneField(
        'payment.Source', on_delete=models.PROTECT, related_name='paypal_payment')
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    state = models.CharField(
        max_length=32, choices=PaymentState.choices,
        default=PaymentState.AWAITING_PAYMENT)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    paypal_order_status = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=64, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorization_count = models.PositiveIntegerField(default=0)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=64, blank=True)
    captured_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))
    paypal_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    # Refund cap: every refund reserves its amount here (one conditional
    # UPDATE) before it is sent, and releases it only if it failed.
    refund_reserved = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))
    refunded_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))

    last_error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(refund_reserved__lte=models.F('captured_amount')),
                name='paypal_refunds_within_capture'),
        ]

    def __str__(self):
        return 'PayPal payment for order %s (%s)' % (self.order.number, self.state)


class PayPalOperation(models.Model):
    """
    The claim for one write sent to PayPal, recorded before the call.

    ``ref`` is derived from the operation (never random); ``request_id`` is
    sent as PayPal-Request-Id so a resend under it is de-duplicated by PayPal.
    The UNIQUE constraints are what reject a second claim for the same write,
    across threads, processes and workers.
    """

    class Kind(models.TextChoices):
        CREATE_ORDER = 'create_order'
        AUTHORIZE_ORDER = 'authorize_order'
        REAUTHORIZE = 'reauthorize'
        CAPTURE = 'capture'
        VOID = 'void'
        REFUND = 'refund'
        SETUP_TOKEN = 'setup_token'
        PAYMENT_TOKEN = 'payment_token'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    ref = models.CharField(max_length=255, unique=True)
    request_id = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    payment = models.ForeignKey(
        PayPalPayment, null=True, blank=True, on_delete=models.PROTECT,
        related_name='operations')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name='paypal_operations')
    seq = models.PositiveIntegerField(default=1)
    idempotency_key = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    # Non-sensitive inputs needed to resend the identical request later
    # (e.g. the saved card or setup token used). Never card details.
    inputs = models.JSONField(default=dict, blank=True)
    # Whether this refund holds a reservation on PayPalPayment.refund_reserved.
    reserved = models.BooleanField(default=False)

    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=64, blank=True)
    # The provider's own event time: reconciliation filters on this clock.
    provider_time = models.DateTimeField(null=True, blank=True)
    detail = models.TextField(blank=True)
    claimed_at = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['payment', 'kind', 'seq'],
                condition=models.Q(payment__isnull=False),
                name='paypal_one_claim_per_payment_step'),
        ]
        indexes = [models.Index(fields=['kind', 'provider_time'])]

    def __str__(self):
        return '%s %s (%s)' % (self.kind, self.ref, self.outcome)


class PayPalCustomer(models.Model):
    """The PayPal vault customer holding a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='paypal_customer')
    paypal_customer_id = models.CharField(max_length=64)

    def __str__(self):
        return self.paypal_customer_id


class PayPalSavedCard(models.Model):
    """
    Links an Oscar ``payment.Bankcard`` (masked display data only) to the
    PayPal payment token that can charge it.
    """

    bankcard = models.OneToOneField(
        'payment.Bankcard', on_delete=models.CASCADE, related_name='paypal_card')
    payment_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64)
    created = models.DateTimeField(auto_now_add=True)
    # Set before the vault delete is sent: from then on the card is hidden
    # and cannot pay, whatever PayPal answers.
    removed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return '%s (%s)' % (self.bankcard, self.payment_token_id)
