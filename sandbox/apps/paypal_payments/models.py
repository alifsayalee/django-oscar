"""
PayPal-side state for Oscar orders.

Oscar already owns the order (``order.Order``), the money ledger
(``payment.Source`` / ``payment.Transaction``) and the saved-card record
(``payment.Bankcard``). These models hold only what PayPal owns and Oscar has
no place for: the PayPal ids and statuses a later request needs to act on the
payment, plus the bookkeeping that makes each PayPal call idempotent.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


def _new_request_key():
    return uuid.uuid4().hex


class PayPalPayment(models.Model):
    """The PayPal payment behind one Oscar order."""

    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZING = 'authorizing'
    AUTHORIZED = 'authorized'
    CAPTURING = 'capturing'
    CAPTURED = 'captured'
    PARTIALLY_REFUNDED = 'partially_refunded'
    REFUNDED = 'refunded'
    VOIDING = 'voiding'
    CANCELLED = 'cancelled'
    STATE_CHOICES = [
        (AWAITING_PAYMENT, 'Awaiting payment'),
        (AUTHORIZING, 'Authorizing'),
        (AUTHORIZED, 'Authorized (funds held)'),
        (CAPTURING, 'Capturing'),
        (CAPTURED, 'Captured'),
        (PARTIALLY_REFUNDED, 'Partially refunded'),
        (REFUNDED, 'Refunded'),
        (VOIDING, 'Releasing held funds'),
        (CANCELLED, 'Cancelled'),
    ]
    # States that mean a PayPal call is in flight for this payment.
    IN_FLIGHT_STATES = (AUTHORIZING, CAPTURING, VOIDING)

    order = models.OneToOneField(
        'order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # Prefix of every PayPal-Request-Id sent for this payment. Random rather
    # than derived from the order number, so a rebuilt database can never
    # replay another install's PayPal responses.
    request_key = models.CharField(max_length=32, unique=True, default=_new_request_key, editable=False)
    # Bumped after a definitive authorization failure, so the next attempt
    # (e.g. with another card) is a new request rather than a PayPal replay.
    authorization_attempt = models.PositiveIntegerField(default=1)
    # When the in-flight state was claimed; a claim older than the lease is
    # treated as abandoned and may be resumed with the same request id.
    claimed_at = models.DateTimeField(null=True, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)
    saved_card = models.ForeignKey(
        'payment.Bankcard', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    card_label = models.CharField(max_length=64, blank=True)

    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # PayPal's create_time of the current (re)authorization: its 3-day honor
    # period runs from here.
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorization_count = models.PositiveIntegerField(default=0)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    voided_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return 'PayPal payment for order %s (%s)' % (self.order.number, self.state)

    @property
    def refunded_amount(self):
        """Money PayPal has returned (or is returning) to the shopper."""
        total = Decimal('0.00')
        for refund in self.refunds.all():
            if refund.counts_against_capture:
                total += refund.amount
        return total

    def request_id(self, action, *parts):
        """The PayPal-Request-Id for one logical action on this payment."""
        return '-'.join([self.request_key, action] + [str(p) for p in parts])


class PayPalRefund(models.Model):
    """One refund of a captured PayPal payment."""

    SUBMITTING = 'SUBMITTING'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    PENDING = 'PENDING'
    COMPLETED = 'COMPLETED'
    # Statuses whose amount no longer counts against the capture.
    RELEASED_STATUSES = (FAILED, CANCELLED)

    payment = models.ForeignKey(PayPalPayment, on_delete=models.CASCADE, related_name='refunds')
    idempotency_key = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # SUBMITTING until PayPal answers; then PayPal's refund status.
    status = models.CharField(max_length=32, default=SUBMITTING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_fee_returned = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['payment', 'idempotency_key'], name='paypal_refund_unique_idempotency_key'),
        ]

    def __str__(self):
        return 'Refund %s of %s (%s)' % (self.paypal_refund_id or self.pk, self.amount, self.status)

    @property
    def counts_against_capture(self):
        return self.status not in self.RELEASED_STATUSES


class PayPalCustomer(models.Model):
    """The PayPal vault customer that groups a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='paypal_customer')
    customer_id = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.customer_id
