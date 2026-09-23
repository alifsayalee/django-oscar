"""Persistence for the PayPal integration.

These models hold only the PayPal-owned state the app must remember to act on a
payment later (ids and statuses for the hold, the capture and the refunds), plus
the shopper-scoped saved-card records. They sit alongside Oscar's own
``order.Order``/``order.Line`` and ``payment.Source``/``payment.Transaction``
models, which remain the record of the order and its money movement — this app
reuses those rather than duplicating them.

Full card numbers and CVVs are NEVER stored here (only PayPal's vault token id
and a safe display: brand + last four).
"""

from decimal import Decimal

from django.conf import settings
from django.db import models


class PayPalPayment(models.Model):
    """PayPal-owned state for a single Oscar order's payment lifecycle."""

    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZED = 'authorized'
    CAPTURED = 'captured'
    VOIDED = 'voided'
    REFUNDED = 'refunded'
    PARTIALLY_REFUNDED = 'partially_refunded'
    STATE_CHOICES = [
        (AWAITING_PAYMENT, 'Awaiting payment'),
        (AUTHORIZED, 'Authorized (funds held)'),
        (CAPTURED, 'Captured (funds taken)'),
        (VOIDED, 'Voided (hold released)'),
        (REFUNDED, 'Refunded in full'),
        (PARTIALLY_REFUNDED, 'Partially refunded'),
    ]

    order = models.OneToOneField(
        'order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=12)

    # PayPal Orders v2 order id (the hold's container).
    paypal_order_id = models.CharField(max_length=64, blank=True)

    # Authorization (the hold).
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_expiry = models.DateTimeField(null=True, blank=True)

    # Capture (the money actually taken).
    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    amount_refunded = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    # Persisted PayPal-Request-Id values so retries of authorize/capture are
    # collapsed both locally and at PayPal (idempotency).
    authorize_request_id = models.CharField(max_length=64, blank=True)
    capture_request_id = models.CharField(max_length=64, blank=True)

    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'api'

    def __str__(self):
        return 'PayPalPayment(order=%s, state=%s)' % (self.order_id, self.state)

    @property
    def captured_amount(self):
        return self.gross_amount or Decimal('0.00')

    @property
    def refundable_amount(self):
        """Amount still refundable: captured gross minus what has been refunded."""
        return self.captured_amount - (self.amount_refunded or Decimal('0.00'))


class PayPalCustomer(models.Model):
    """Maps a sandbox shopper to their PayPal-generated customer id.

    Captured from the first vaulted card so subsequent cards vault under the same
    customer and can be listed together.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='paypal_customer')
    paypal_customer_id = models.CharField(max_length=64)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'api'

    def __str__(self):
        return 'PayPalCustomer(user=%s, id=%s)' % (self.user_id, self.paypal_customer_id)


class SavedPaymentMethod(models.Model):
    """A shopper's saved card, described safely (no PAN/CVV ever stored)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='saved_payment_methods')
    paypal_token_id = models.CharField(max_length=64, unique=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # "YYYY-MM"
    label = models.CharField(max_length=128, blank=True)
    is_active = models.BooleanField(default=True)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'api'
        ordering = ['-date_created']

    def __str__(self):
        return 'SavedPaymentMethod(user=%s, %s ****%s)' % (
            self.user_id, self.brand, self.last_digits)

    def describe(self):
        return {
            'paymentMethodId': self.id,
            'brand': self.brand,
            'lastDigits': self.last_digits,
            'expiry': self.expiry,
            'label': self.label,
        }


class RefundRecord(models.Model):
    """One refund attempt against a captured payment, keyed for idempotency.

    A repeat request under the same ``idempotency_key`` returns the stored
    result rather than refunding twice; two distinct partial refunds use
    distinct keys and both proceed.
    """

    payment = models.ForeignKey(
        PayPalPayment, on_delete=models.CASCADE, related_name='refunds')
    idempotency_key = models.CharField(max_length=128)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=32, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'api'
        unique_together = [('payment', 'idempotency_key')]
        ordering = ['date_created']

    def __str__(self):
        return 'RefundRecord(payment=%s, id=%s, amount=%s)' % (
            self.payment_id, self.paypal_refund_id, self.amount)

    def describe(self):
        return {
            'refundId': self.paypal_refund_id,
            'amount': str(self.amount),
            'status': self.status,
        }
