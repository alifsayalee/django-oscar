"""
PayPal-owned state for Oscar orders and saved cards.

Oscar's own models stay the record of the order (``order.Order``) and of the
money ledger (``payment.Source`` / ``payment.Transaction``). These models carry
what PayPal owns — ids and current statuses of the order, authorization,
capture and refunds — so a later request can act on a payment it did not start.

No card number or security code is ever stored here.
"""
import uuid

from django.conf import settings
from django.db import models


class PaypalCustomer(models.Model):
    """The PayPal vault customer that a shopper's saved cards belong to."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='paypal_customer')
    paypal_customer_id = models.CharField(max_length=64, unique=True)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.paypal_customer_id


class SavedCard(models.Model):
    """A card vaulted at PayPal. Only the vault token and display details are kept."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='saved_cards')
    paypal_token_id = models.CharField(max_length=64, unique=True)
    paypal_customer_id = models.CharField(max_length=64)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    # Set when the shopper removes the card. A removed card is never listed or
    # usable again; ``paypal_delete_pending`` records that PayPal has not yet
    # confirmed the deletion, so repeating the DELETE retries it.
    date_removed = models.DateTimeField(null=True, blank=True)
    paypal_delete_pending = models.BooleanField(default=False)

    class Meta:
        ordering = ['-date_created', '-pk']

    def __str__(self):
        return f'{self.brand} ****{self.last_digits}'


class PaypalPayment(models.Model):
    """The PayPal side of one Oscar order's payment."""

    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZING = 'authorizing'
    AUTHORIZATION_PENDING = 'authorization_pending'
    AUTHORIZED = 'authorized'
    CAPTURING = 'capturing'
    CAPTURE_PENDING = 'capture_pending'
    CAPTURED = 'captured'
    PARTIALLY_REFUNDED = 'partially_refunded'
    REFUNDED = 'refunded'
    VOIDING = 'voiding'
    VOIDED = 'voided'
    CANCELLED = 'cancelled'
    FAILED = 'failed'
    UNKNOWN = 'unknown'
    STATUS_CHOICES = [(s, s) for s in (
        AWAITING_PAYMENT, AUTHORIZING, AUTHORIZATION_PENDING, AUTHORIZED, CAPTURING,
        CAPTURE_PENDING, CAPTURED, PARTIALLY_REFUNDED, REFUNDED, VOIDING, VOIDED,
        CANCELLED, FAILED, UNKNOWN)]

    # The operation whose outcome is unknown when status == UNKNOWN.
    OP_AUTHORIZE = 'authorize'
    OP_CAPTURE = 'capture'
    OP_VOID = 'void'

    order = models.OneToOneField(
        'order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    # Our reference for this payment at PayPal: sent as the purchase unit's
    # custom_id and used to derive every PayPal-Request-Id, so it must be unique
    # across every install sharing the PayPal account — hence a UUID.
    reference = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=AWAITING_PAYMENT)
    unknown_operation = models.CharField(max_length=16, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    # Incremented for each new authorization attempt (a declined card can be
    # retried with another). Repeats of an attempt whose outcome is unknown
    # reuse the same attempt number, and so the same PayPal-Request-Id.
    attempt = models.PositiveIntegerField(default=0)

    saved_card = models.ForeignKey(
        SavedCard, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)

    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    # The id of the authorization this one replaced after a reauthorization.
    original_authorization_id = models.CharField(max_length=64, blank=True)
    reauthorized_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    capture_status_reason = models.CharField(max_length=64, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    voided_at = models.DateTimeField(null=True, blank=True)

    # The last failure, worded for whoever has to act on it.
    last_error = models.TextField(blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)

    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'PayPal payment for order {self.order_id} ({self.status})'


class PaypalRefund(models.Model):
    """One refund of a captured payment, keyed by the caller's idempotency key."""

    SENDING = 'sending'
    COMPLETED = 'completed'
    PENDING = 'pending'
    FAILED = 'failed'
    UNKNOWN = 'unknown'
    STATUS_CHOICES = [(s, s) for s in (SENDING, COMPLETED, PENDING, FAILED, UNKNOWN)]
    # Statuses whose amount may have left, or will leave, the merchant's
    # balance — they count against what remains refundable.
    RESERVING_STATUSES = (SENDING, COMPLETED, PENDING, UNKNOWN)

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(PaypalPayment, on_delete=models.CASCADE, related_name='refunds')
    idempotency_key = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    last_error = models.TextField(blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['date_created', 'pk']
        constraints = [
            models.UniqueConstraint(
                fields=['payment', 'idempotency_key'], name='payments_api_refund_idempotency'),
        ]

    def __str__(self):
        return f'Refund {self.public_id} of {self.amount} {self.currency} ({self.status})'
