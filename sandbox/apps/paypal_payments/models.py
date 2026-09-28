"""
Local records for PayPal money movement.

Nothing here ever holds card details: a one-off card is passed straight
through to PayPal, and a saved card is only PayPal's vault token plus the
brand / last digits / expiry PayPal reports back.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class InstallIdentity(models.Model):
    """
    One row holding the prefix this install puts on every reference it sends
    to PayPal, so two installs sharing a merchant account never collide.
    """

    key = models.CharField(max_length=16, unique=True)
    prefix = models.CharField(max_length=16)

    def __str__(self):
        return self.prefix


class PayPalWrite(models.Model):
    """
    The claim for one provider write step (authorize, capture, refund, ...).

    It is inserted - and committed - before PayPal is called. The UNIQUE
    constraint on ``ref`` is what rejects a second request for the same step,
    across processes. ``ref`` is also sent to PayPal as ``PayPal-Request-Id``.
    """

    SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN = (
        'sending', 'done', 'pending', 'failed', 'needs_review', 'unknown')
    OUTCOME_CHOICES = [(o, o) for o in (SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN)]

    AUTHORIZE, REAUTHORIZE, CAPTURE, VOID, REFUND, SAVE_CARD, DELETE_CARD = (
        'authorize', 'reauthorize', 'capture', 'void', 'refund', 'save_card', 'delete_card')

    ref = models.CharField(max_length=128, unique=True)
    step = models.CharField(max_length=32)
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SENDING)
    order = models.ForeignKey(
        'order.Order', null=True, blank=True, on_delete=models.SET_NULL, related_name='paypal_writes')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='paypal_writes')
    amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=64, blank=True)
    # PayPal's own event time, used to reconcile on PayPal's clock
    provider_time = models.DateTimeField(null=True, blank=True)
    # Operator-facing explanation of a failed / unknown / needs_review outcome
    detail = models.TextField(blank=True)
    claimed_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['pk']

    def __str__(self):
        return '%s %s' % (self.ref, self.outcome)


class SavedCard(models.Model):
    """A card saved in PayPal's vault for one shopper."""

    ACTIVE, DELETING, DELETED = 'active', 'deleting', 'deleted'
    STATE_CHOICES = [(s, s) for s in (ACTIVE, DELETING, DELETED)]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='paypal_saved_cards')
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=ACTIVE)
    paypal_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    save_ref = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['pk']

    def __str__(self):
        return '%s ending %s' % (self.brand or 'Card', self.last_digits)


class OrderPayment(models.Model):
    """PayPal payment state for one Oscar order."""

    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZATION_PENDING = 'authorization_pending'
    AUTHORIZED = 'authorized'
    CAPTURE_PENDING = 'capture_pending'
    CAPTURED = 'captured'
    PARTIALLY_REFUNDED = 'partially_refunded'
    REFUNDED = 'refunded'
    CANCELLED = 'cancelled'
    NEEDS_REVIEW = 'needs_review'
    STATE_CHOICES = [(s, s) for s in (
        AWAITING_PAYMENT, AUTHORIZATION_PENDING, AUTHORIZED, CAPTURE_PENDING, CAPTURED,
        PARTIALLY_REFUNDED, REFUNDED, CANCELLED, NEEDS_REVIEW)]

    order = models.OneToOneField('order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='paypal_payments')
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=3)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    # PayPal's create_time of the ORIGINAL authorization (the 29-day window runs from it)
    original_authorized_at = models.DateTimeField(null=True, blank=True)
    # PayPal's create_time of the current authorization (honor period runs from it)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized_at = models.DateTimeField(null=True, blank=True)
    payment_method = models.ForeignKey(
        SavedCard, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    refunded_amount = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0'))
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['pk']

    def __str__(self):
        return '%s %s' % (self.order_id, self.state)


class PayPalRefund(models.Model):
    """
    One refund of a capture. Created as a reservation (before PayPal is
    called) so the sum of refunds can never exceed what was captured.
    """

    payment = models.ForeignKey(OrderPayment, on_delete=models.CASCADE, related_name='refunds')
    idempotency_key = models.CharField(max_length=255)
    ref = models.CharField(max_length=128, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    outcome = models.CharField(max_length=16, choices=PayPalWrite.OUTCOME_CHOICES, default=PayPalWrite.SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=32, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['pk']
        constraints = [
            models.UniqueConstraint(fields=['payment', 'idempotency_key'], name='paypal_refund_unique_key'),
        ]

    def __str__(self):
        return '%s %s' % (self.ref, self.outcome)
