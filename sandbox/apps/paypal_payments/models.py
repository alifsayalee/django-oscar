"""
Local records of the PayPal state this site depends on.

Nothing here ever holds card numbers or security codes: a one-off card goes
straight from the request to PayPal, and a saved card is known only by the
vault token PayPal returned plus the brand / last digits PayPal echoed.

Every provider write is claimed on one of these rows *before* the call is made
(a conditional UPDATE or a unique insert, decided by the database), and the
reference sent to PayPal is derived from the row, so a repeated request never
becomes a second authorization, capture, void, refund or vaulted card.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models


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
    SENDING = 'sending'
    ACTIVE = 'active'
    FAILED = 'failed'
    UNKNOWN = 'unknown'
    DELETING = 'deleting'
    DELETE_UNKNOWN = 'delete_unknown'
    DELETED = 'deleted'
    STATE_CHOICES = [(s, s) for s in (
        SENDING, ACTIVE, FAILED, UNKNOWN, DELETING, DELETE_UNKNOWN, DELETED)]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='saved_cards')
    # Sent as PayPal-Request-Id; derived from the caller's idempotency key (or
    # a keyed fingerprint of the card) so a repeated save is the same write.
    reference = models.CharField(max_length=100, unique=True)
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=SENDING)
    paypal_token_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.CharField(max_length=255, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created']

    def __str__(self):
        return '%s ending %s' % (self.brand or 'card', self.last_digits or '????')


class OrderPayment(models.Model):
    """PayPal's side of one Oscar order: the hold, the capture and the refunds."""

    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZING = 'authorizing'
    AUTHORIZATION_PENDING = 'authorization_pending'
    AUTHORIZATION_UNKNOWN = 'authorization_unknown'
    AUTHORIZATION_FAILED = 'authorization_failed'
    AUTHORIZED = 'authorized'
    AUTHORIZATION_EXPIRED = 'authorization_expired'
    CAPTURING = 'capturing'
    CAPTURE_PENDING = 'capture_pending'
    CAPTURE_UNKNOWN = 'capture_unknown'
    CAPTURED = 'captured'
    VOIDING = 'voiding'
    VOID_UNKNOWN = 'void_unknown'
    VOIDED = 'voided'
    CANCELLED = 'cancelled'
    NEEDS_REVIEW = 'needs_review'
    STATE_CHOICES = [(s, s) for s in (
        AWAITING_PAYMENT, AUTHORIZING, AUTHORIZATION_PENDING, AUTHORIZATION_UNKNOWN,
        AUTHORIZATION_FAILED, AUTHORIZED, AUTHORIZATION_EXPIRED, CAPTURING,
        CAPTURE_PENDING, CAPTURE_UNKNOWN, CAPTURED, VOIDING, VOID_UNKNOWN, VOIDED,
        CANCELLED, NEEDS_REVIEW)]

    # States from which the shopper may (re)try paying.
    PAYABLE_STATES = (AWAITING_PAYMENT, AUTHORIZATION_FAILED, AUTHORIZATION_EXPIRED)

    order = models.OneToOneField(
        'order.Order', on_delete=models.PROTECT, related_name='paypal_payment')
    # Base of every reference sent to PayPal for this order; the attempt
    # number is appended for the authorization (it is also the invoice id).
    reference = models.CharField(max_length=80, unique=True)
    attempt = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    # The provider step an in-flight or unresolved claim belongs to:
    # authorize / authorize_order / reauthorize / capture / void.
    step = models.CharField(max_length=32, blank=True)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    saved_card = models.ForeignKey(
        SavedCard, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    original_authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    # PayPal's clock, as reported.
    authorized_at = models.DateTimeField(null=True, blank=True)
    original_authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    voided_at = models.DateTimeField(null=True, blank=True)

    # Sum of refunds claimed and not failed; guards against refunding more
    # than was captured, even across concurrent requests.
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.CharField(max_length=500, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    source = models.ForeignKey(
        'payment.Source', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    def __str__(self):
        return '%s (%s)' % (self.reference, self.state)

    @property
    def attempt_reference(self):
        return '%s-%d' % (self.reference, self.attempt)

    def reference_for(self, step):
        return '%s-%s' % (self.attempt_reference, step)

    @property
    def refundable_amount(self):
        if self.captured_amount is None:
            return Decimal('0.00')
        return self.captured_amount - self.refund_reserved


class PaymentRefund(models.Model):
    SENDING = 'sending'
    DONE = 'done'
    PENDING = 'pending'
    FAILED = 'failed'
    UNKNOWN = 'unknown'
    NEEDS_REVIEW = 'needs_review'
    STATE_CHOICES = [(s, s) for s in (SENDING, DONE, PENDING, FAILED, UNKNOWN, NEEDS_REVIEW)]

    payment = models.ForeignKey(OrderPayment, on_delete=models.PROTECT, related_name='refunds')
    idempotency_key = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.CharField(max_length=500, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created']
        constraints = [
            models.UniqueConstraint(
                fields=['payment', 'idempotency_key'], name='paypal_refund_unique_key'),
        ]

    def __str__(self):
        return '%s %s (%s)' % (self.payment.reference, self.amount, self.state)

    @property
    def reference(self):
        return '%s-refund-%d' % (self.payment.reference, self.pk)
