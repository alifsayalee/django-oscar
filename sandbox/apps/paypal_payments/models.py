"""
PayPal-owned payment state, kept beside Oscar's own order and payment models.

Oscar's ``order.Order`` is the order, ``payment.Source``/``Transaction`` is the
payment ledger and ``payment.Bankcard`` is a saved card. The models here carry
only what PayPal owns (ids and current statuses) so a later request can act on
a payment started by an earlier one. No card number is ever stored.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum


class PayPalCustomer(models.Model):
    """The PayPal vault customer that a shopper's saved cards belong to."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='paypal_customer')
    paypal_customer_id = models.CharField(max_length=64, unique=True)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.paypal_customer_id


class PayPalPayment(models.Model):
    # Lifecycle of the money for one order
    NEW = 'NEW'                          # nothing held yet
    AUTHORIZING = 'AUTHORIZING'          # hold requested, outcome not yet recorded
    AUTHORIZED = 'AUTHORIZED'            # money held
    FAILED = 'FAILED'                    # last authorization attempt was refused
    EXPIRED = 'EXPIRED'                  # hold lapsed before fulfilment
    VOIDED = 'VOIDED'                    # hold released (cancelled)
    CAPTURED = 'CAPTURED'                # money taken
    PARTIALLY_REFUNDED = 'PARTIALLY_REFUNDED'
    REFUNDED = 'REFUNDED'
    STATE_CHOICES = [(s, s) for s in (
        NEW, AUTHORIZING, AUTHORIZED, FAILED, EXPIRED, VOIDED, CAPTURED,
        PARTIALLY_REFUNDED, REFUNDED)]

    PAYABLE_STATES = (NEW, FAILED, EXPIRED)
    CAPTURED_STATES = (CAPTURED, PARTIALLY_REFUNDED, REFUNDED)

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order = models.OneToOneField(
        'order.Order', on_delete=models.PROTECT, related_name='paypal_payment')
    source = models.OneToOneField(
        'payment.Source', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='paypal_payment')
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=NEW)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # Guards a PayPal call in flight so a double-click cannot start a second one.
    lock_until = models.DateTimeField(null=True, blank=True)
    attempt = models.PositiveIntegerField(default=0)

    # The funding card used for the current authorization (never the number).
    payment_method = models.ForeignKey(
        'payment.Bankcard', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+')
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    invoice_id = models.CharField(max_length=127, blank=True, db_index=True)

    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    # The authorization that was first created; reauthorization replaces
    # ``authorization_id`` but PayPal allows it only once per original.
    original_authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    captured_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)

    voided_at = models.DateTimeField(null=True, blank=True)

    # Last refusal/problem reported for this payment, for operators.
    last_error_code = models.CharField(max_length=128, blank=True)
    last_error_message = models.TextField(blank=True)
    last_error_debug_id = models.CharField(max_length=64, blank=True)

    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date_created']

    def __str__(self):
        return 'PayPal payment for order %s (%s)' % (self.order.number, self.state)

    def refunded_total(self):
        """Refunded or in-flight refund amount — what is no longer refundable."""
        total = self.refunds.exclude(
            status__in=PayPalRefund.RELEASED_STATUSES
        ).aggregate(total=Sum('amount'))['total']
        return (total or Decimal('0')).quantize(Decimal('0.01'))

    def refundable_amount(self):
        if self.state not in self.CAPTURED_STATES or self.captured_amount is None:
            return Decimal('0.00')
        return max(self.captured_amount - self.refunded_total(), Decimal('0.00'))

    def clear_error(self):
        self.last_error_code = self.last_error_message = self.last_error_debug_id = ''


class PayPalRefund(models.Model):
    SUBMITTING = 'SUBMITTING'   # recorded locally, PayPal outcome not yet known
    PENDING = 'PENDING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    REJECTED = 'REJECTED'       # PayPal refused the request outright
    STATUS_CHOICES = [(s, s) for s in (
        SUBMITTING, PENDING, COMPLETED, FAILED, CANCELLED, REJECTED)]
    # Refunds in these states did not (and will not) move money.
    RELEASED_STATUSES = (FAILED, CANCELLED, REJECTED)

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.PROTECT, related_name='refunds')
    idempotency_key = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SUBMITTING)
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    error_code = models.CharField(max_length=128, blank=True)
    error_message = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+')
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['date_created']
        constraints = [
            models.UniqueConstraint(
                fields=['payment', 'idempotency_key'], name='uniq_refund_idempotency_key'),
            models.CheckConstraint(condition=Q(amount__gt=0), name='refund_amount_positive'),
        ]

    def __str__(self):
        return 'Refund %s of %s %s (%s)' % (self.pk, self.amount, self.currency, self.status)
