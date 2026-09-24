"""
PayPal-owned payment state, kept beside Oscar's own models.

The money bookkeeping lives in Oscar's ``payment.Source`` / ``payment.Transaction``
and ``order.PaymentEvent``; saved cards are Oscar ``payment.Bankcard`` rows whose
``partner_reference`` is the PayPal vault token. These models only hold what
Oscar has no place for: the ids and current status of the PayPal authorization,
capture and refunds, and the idempotency keys that make repeated requests safe.
No card number or security code is ever stored.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models


class PayPalPayment(models.Model):
    # Our state machine. Transitions are claimed with compare-and-set updates
    # so a double-click cannot start the same PayPal write twice.
    UNPAID = 'unpaid'
    AUTHORIZING = 'authorizing'
    AUTHORIZED = 'authorized'
    DECLINED = 'declined'
    CAPTURING = 'capturing'
    CAPTURED = 'captured'
    CAPTURE_FAILED = 'capture_failed'
    VOIDING = 'voiding'
    VOIDED = 'voided'
    STATE_CHOICES = [
        (UNPAID, 'Awaiting payment'),
        (AUTHORIZING, 'Authorization in flight'),
        (AUTHORIZED, 'Authorized (funds held)'),
        (DECLINED, 'Declined'),
        (CAPTURING, 'Capture in flight'),
        (CAPTURED, 'Captured'),
        (CAPTURE_FAILED, 'Capture failed'),
        (VOIDING, 'Void in flight'),
        (VOIDED, 'Voided (funds released)'),
    ]

    order = models.OneToOneField(
        'order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    source = models.OneToOneField(
        'payment.Source', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='paypal_payment')
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=UNPAID, db_index=True)

    # Authorization attempt bookkeeping: a new attempt (and a new PayPal
    # request id) is only started after a definitive decline.
    attempt = models.PositiveIntegerField(default=0)
    auth_request_id = models.CharField(max_length=128, blank=True)
    invoice_id = models.CharField(max_length=127, blank=True, db_index=True)
    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    saved_card = models.ForeignKey(
        'payment.Bankcard', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    # The hold. After a reauthorization the new id replaces the old one, which
    # is kept in ``original_authorization_id``.
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    original_authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized_at = models.DateTimeField(null=True, blank=True)

    # The capture, as PayPal reported it.
    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'PayPal payment'

    def __str__(self):
        return 'PayPal payment for order %s (%s)' % (self.order.number, self.state)

    def reserved_refund_total(self):
        """
        Everything refunded or being refunded. In-flight refunds count, so two
        concurrent partial refunds can never exceed the captured amount.
        """
        # Summed in Python: SQL SUM() over decimals goes through floats on SQLite.
        amounts = self.refunds.exclude(state__in=PayPalRefund.RELEASED_STATES).values_list('amount', flat=True)
        return sum(amounts, Decimal('0.00'))

    def refundable_amount(self):
        if self.captured_amount is None:
            return Decimal('0.00')
        return max(self.captured_amount - self.reserved_refund_total(), Decimal('0.00'))


class PayPalRefund(models.Model):
    REQUESTED = 'requested'  # sent (or about to be); outcome not yet known
    PENDING = 'pending'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    STATE_CHOICES = [
        (REQUESTED, 'Requested'),
        (PENDING, 'Pending at PayPal'),
        (COMPLETED, 'Completed'),
        (FAILED, 'Failed'),
        (CANCELLED, 'Cancelled'),
    ]
    # States whose amount no longer counts against the captured amount.
    RELEASED_STATES = (FAILED, CANCELLED)

    payment = models.ForeignKey(PayPalPayment, on_delete=models.CASCADE, related_name='refunds')
    idempotency_key = models.CharField(max_length=255)
    request_id = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=REQUESTED)
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'PayPal refund'
        constraints = [
            models.UniqueConstraint(
                fields=['payment', 'idempotency_key'], name='payments_api_refund_unique_key'),
        ]

    def __str__(self):
        return 'Refund %s of %s (%s)' % (self.pk, self.amount, self.state)


class PayPalCustomer(models.Model):
    """The PayPal vault customer that groups a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='paypal_customer')
    customer_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'PayPal customer'

    def __str__(self):
        return 'PayPal customer for user %s' % self.user_id
