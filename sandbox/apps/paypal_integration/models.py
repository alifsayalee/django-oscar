"""
PayPal state that Oscar's own models have no place for.

Money movement itself is recorded on Oscar's ``payment.Source`` (amounts
allocated / debited / refunded) and ``payment.Transaction`` rows, and saved
cards are Oscar ``payment.Bankcard`` rows whose ``partner_reference`` is the
PayPal vault token. The models here only carry the provider identifiers and
statuses a later request needs, plus the claim table that makes every PayPal
write idempotent.
"""
import uuid

from django.conf import settings
from django.db import models


class InstallSetting(models.Model):
    """Per-database values generated once, e.g. this install's reference prefix."""

    key = models.CharField(max_length=64, unique=True)
    value = models.CharField(max_length=255)

    def __str__(self):
        return self.key


class PayPalCustomer(models.Model):
    """The PayPal vault customer that owns a shopper's saved cards."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='paypal_customer')
    customer_id = models.CharField(max_length=64)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.customer_id


class PayPalPayment(models.Model):
    """PayPal's side of the payment for one Oscar order."""

    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZED = 'authorized'
    CAPTURE_PENDING = 'capture_pending'
    CAPTURED = 'captured'
    PARTIALLY_REFUNDED = 'partially_refunded'
    REFUNDED = 'refunded'
    VOIDED = 'voided'
    STATES = [
        (AWAITING_PAYMENT, 'Awaiting payment'),
        (AUTHORIZED, 'Authorized'),
        (CAPTURE_PENDING, 'Capture pending'),
        (CAPTURED, 'Captured'),
        (PARTIALLY_REFUNDED, 'Partially refunded'),
        (REFUNDED, 'Refunded'),
        (VOIDED, 'Voided'),
    ]

    source = models.OneToOneField(
        'payment.Source', on_delete=models.CASCADE, related_name='paypal')
    state = models.CharField(max_length=32, choices=STATES, default=AWAITING_PAYMENT)
    # Bumped only after an authorization attempt definitively failed, so a
    # shopper can retry with another card while a double-click still maps to
    # the same attempt (and so the same PayPal-Request-Id).
    authorize_attempt = models.PositiveIntegerField(default=1)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)
    date_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return '%s (%s)' % (self.source.order.number, self.state)


class ProviderWrite(models.Model):
    """
    One claimed PayPal write step (authorize, capture, void, refund, ...).

    The unique ``ref`` is taken *before* PayPal is called and is sent as the
    PayPal-Request-Id, so a second request for the same step is rejected by
    the database and a write whose answer was lost is re-checked under the
    same reference. No card data is ever stored here.
    """

    SENDING = 'sending'
    DONE = 'done'
    PENDING = 'pending'
    FAILED = 'failed'
    NEEDS_REVIEW = 'needs_review'
    UNKNOWN = 'unknown'
    OUTCOMES = [(o, o) for o in (SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN)]

    AUTHORIZE = 'authorize'
    REAUTHORIZE = 'reauthorize'
    CAPTURE = 'capture'
    VOID = 'void'
    REFUND = 'refund'
    VAULT_CREATE = 'vault_create'
    VAULT_DELETE = 'vault_delete'
    KINDS = [(k, k) for k in (AUTHORIZE, REAUTHORIZE, CAPTURE, VOID, REFUND, VAULT_CREATE, VAULT_DELETE)]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    ref = models.CharField(max_length=128, unique=True)
    kind = models.CharField(max_length=16, choices=KINDS)
    outcome = models.CharField(max_length=16, choices=OUTCOMES, default=SENDING)

    order = models.ForeignKey(
        'order.Order', null=True, blank=True, on_delete=models.PROTECT,
        related_name='paypal_writes')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='paypal_writes')
    # The saved card a vault delete (or a saved-card payment) acts on.
    bankcard_id = models.IntegerField(null=True, blank=True, db_index=True)

    amount = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    # What identifies the request behind a caller-supplied idempotency key, so
    # the key cannot be replayed with a different body.
    fingerprint = models.CharField(max_length=64, blank=True)

    provider_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=32, blank=True)
    # PayPal's own event time, for reconciliation on PayPal's clock.
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    message = models.CharField(max_length=512, blank=True)

    claimed_at = models.DateTimeField()
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['kind', 'outcome'])]

    def __str__(self):
        return '%s %s' % (self.ref, self.outcome)
