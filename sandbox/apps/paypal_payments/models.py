"""
PayPal-owned state that Oscar's own models have no place for.

Orders, lines and the money ledger stay in Oscar (``order.Order``,
``order.Line``, ``payment.Source``/``Transaction``); these models add the
PayPal ids and statuses a later request needs to act on, plus the claim
columns that stop the same operation from reaching PayPal twice.

No card number or security code is ever stored: a saved card is only the
PayPal vault token plus what a shopper needs to recognise it.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q


class PayPalCustomer(models.Model):
    """The PayPal vault customer that holds a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='paypal_customer')
    customer_id = models.CharField(max_length=64, unique=True)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.customer_id


class SavedCard(models.Model):
    SAVING, ACTIVE, DELETING, DELETED, FAILED = (
        'SAVING', 'ACTIVE', 'DELETING', 'DELETED', 'FAILED')
    STATES = [(s, s.title()) for s in (SAVING, ACTIVE, DELETING, DELETED, FAILED)]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='saved_cards')
    state = models.CharField(max_length=16, choices=STATES, default=SAVING)
    # Caller-supplied Idempotency-Key for the save request, when there was one.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    request_id = models.CharField(max_length=64)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    outcome_unknown = models.BooleanField(default=False)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.CharField(max_length=512, blank=True)

    vault_token_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    paypal_customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'idempotency_key'],
                condition=Q(idempotency_key__isnull=False),
                name='paypal_savedcard_unique_idempotency_key'),
        ]

    def __str__(self):
        return '%s ending %s' % (self.brand or 'Card', self.last_digits or '????')


class PayPalPayment(models.Model):
    """PayPal state for one Oscar order: the hold, the capture, the refunds."""

    AWAITING_PAYMENT = 'AWAITING_PAYMENT'
    AUTHORIZING = 'AUTHORIZING'
    AUTHORIZED = 'AUTHORIZED'
    FAILED = 'FAILED'
    CAPTURING = 'CAPTURING'
    CAPTURED = 'CAPTURED'
    VOIDING = 'VOIDING'
    VOIDED = 'VOIDED'
    CANCELLED = 'CANCELLED'
    STATES = [(s, s.replace('_', ' ').title()) for s in (
        AWAITING_PAYMENT, AUTHORIZING, AUTHORIZED, FAILED, CAPTURING,
        CAPTURED, VOIDING, VOIDED, CANCELLED)]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order = models.OneToOneField(
        'order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    state = models.CharField(max_length=24, choices=STATES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # Claim bookkeeping: a claim is a state transition; while it is held no
    # second request may reach PayPal for the same operation. An expired claim
    # (crash, or an outcome we could not read) may be re-taken, and the retry
    # re-sends the same PayPal-Request-Id so PayPal answers with the original
    # result instead of acting twice.
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    authorize_request_id = models.CharField(max_length=64, blank=True)
    capture_request_id = models.CharField(max_length=64, blank=True)
    void_request_id = models.CharField(max_length=64, blank=True)
    outcome_unknown = models.BooleanField(default=False)

    # The hold
    saved_card = models.ForeignKey(
        SavedCard, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='payments')
    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorization_count = models.PositiveIntegerField(default=0)
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    # The capture, as PayPal reported it
    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    # Refund accounting. ``refund_reserved`` covers every refund that is in
    # flight or done and is only ever raised by a conditional UPDATE that keeps
    # it <= captured_amount; ``refunded_amount`` is what PayPal confirmed.
    refund_reserved = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))
    refunded_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))

    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.CharField(max_length=512, blank=True)
    last_debug_id = models.CharField(max_length=64, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created']

    def __str__(self):
        return 'PayPal payment for order %s (%s)' % (self.order_id, self.state)

    @property
    def payment_status(self):
        """The state a caller sees, with refunds folded in."""
        if self.state == self.CAPTURED and self.refunded_amount > 0:
            if self.captured_amount is not None and self.refunded_amount >= self.captured_amount:
                return 'REFUNDED'
            return 'PARTIALLY_REFUNDED'
        return self.state

    @property
    def refundable_amount(self):
        if self.state != self.CAPTURED or self.captured_amount is None:
            return Decimal('0.00')
        return self.captured_amount - self.refund_reserved


class PayPalRefund(models.Model):
    REQUESTED = 'REQUESTED'    # claimed; PayPal's answer not yet recorded
    PENDING = 'PENDING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    STATES = [(s, s.title()) for s in (REQUESTED, PENDING, COMPLETED, FAILED, CANCELLED)]
    # States whose amount is held against the capture.
    HOLDING = (REQUESTED, PENDING, COMPLETED)

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name='refunds')
    idempotency_key = models.CharField(max_length=64)
    request_id = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=16, choices=STATES, default=REQUESTED)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    outcome_unknown = models.BooleanField(default=False)

    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.CharField(max_length=512, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created']
        constraints = [
            models.UniqueConstraint(
                fields=['payment', 'idempotency_key'],
                name='paypal_refund_unique_idempotency_key'),
        ]

    def __str__(self):
        return 'Refund %s of %s (%s)' % (self.public_id, self.amount, self.state)
