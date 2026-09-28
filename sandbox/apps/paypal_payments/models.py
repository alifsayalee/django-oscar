import uuid

from django.conf import settings
from django.db import models


class InstallIdentity(models.Model):
    """One row: a random id minted once per database, used as the prefix of every reference this
    install sends to PayPal, so a rebuilt database never replays an older install's request ids."""
    key = models.CharField(max_length=32, unique=True)
    created = models.DateTimeField(auto_now_add=True)


class ProviderWrite(models.Model):
    """The claim and outcome of ONE provider write step (the safe write's store).

    The unique ``ref`` is the claim: inserting it is how a request wins the right to call PayPal;
    the same ref is sent to PayPal as ``PayPal-Request-Id``.
    """
    SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN = (
        'sending', 'done', 'pending', 'failed', 'needs_review', 'unknown')
    OUTCOMES = [(o, o) for o in (SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN)]

    ref = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=32, db_index=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name='+')
    order = models.ForeignKey('order.Order', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='paypal_writes')
    outcome = models.CharField(max_length=16, choices=OUTCOMES, default=SENDING)
    claimed_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=64, blank=True)
    # PayPal's own clock for the event: reconciliation filters on this, never on claimed_at.
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    sent_amount = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    # What the response said (ids, statuses, fee/net, card label) and any error detail — never
    # card numbers or security codes.
    data = models.JSONField(default=dict, blank=True)
    error = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=['outcome', 'claimed_at'])]

    def __str__(self):
        return '%s [%s]' % (self.ref, self.outcome)


class PayPalCustomer(models.Model):
    """The PayPal vault customer id holding a shopper's saved cards."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='paypal_customer')
    customer_id = models.CharField(max_length=64, blank=True)
    # Bumped whenever a saved card is deleted, so re-saving the same card is a new write.
    deletions = models.PositiveIntegerField(default=0)


class SavedCard(models.Model):
    ACTIVE, DELETING, DELETED = 'active', 'deleting', 'deleted'
    STATES = [(s, s) for s in (ACTIVE, DELETING, DELETED)]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='paypal_saved_cards')
    token_id = models.CharField(max_length=64, unique=True)   # PayPal vault payment token id
    customer_id = models.CharField(max_length=64, blank=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)       # YYYY-MM
    name = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=16, choices=STATES, default=ACTIVE)
    created = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created']

    def as_dict(self):
        return {
            'paymentMethodId': str(self.public_id),
            'brand': self.brand,
            'lastDigits': self.last_digits,
            'expiry': self.expiry,
            'name': self.name,
            'label': '%s ending %s (exp %s)' % (self.brand or 'Card', self.last_digits, self.expiry),
            'created': self.created.isoformat() if self.created else None,
        }


class PayPalPayment(models.Model):
    """PayPal state for one Oscar order: enough of what PayPal owns (ids + statuses of the hold,
    the capture and the refunds) for any later request to act on it."""
    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZING = 'authorizing'
    AUTHORIZED = 'authorized'
    CAPTURING = 'capturing'
    CAPTURED = 'captured'
    PARTIALLY_REFUNDED = 'partially_refunded'
    REFUNDED = 'refunded'
    VOIDING = 'voiding'
    VOIDED = 'voided'
    CANCELLED = 'cancelled'
    NEEDS_REVIEW = 'needs_review'
    STATES = [(s, s) for s in (
        AWAITING_PAYMENT, AUTHORIZING, AUTHORIZED, CAPTURING, CAPTURED, PARTIALLY_REFUNDED, REFUNDED,
        VOIDING, VOIDED, CANCELLED, NEEDS_REVIEW)]

    order = models.OneToOneField('order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    state = models.CharField(max_length=24, choices=STATES, default=AWAITING_PAYMENT, db_index=True)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    pay_attempt = models.PositiveIntegerField(default=1)
    last_error = models.JSONField(default=dict, blank=True)

    saved_card = models.ForeignKey(SavedCard, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='payments')
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    # When the CURRENT authorization (original or reauthorized) was created — its honor period
    # starts here — and when the ORIGINAL one was, which bounds reauthorization.
    authorized_at = models.DateTimeField(null=True, blank=True)
    original_authorized_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorization_count = models.PositiveIntegerField(default=0)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    voided_at = models.DateTimeField(null=True, blank=True)

    # Money promised to refunds that are not known to have failed (the over-refund guard), and
    # money PayPal confirmed refunded.
    refund_reserved = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'PayPal payment for order %s [%s]' % (self.order_id, self.state)


class PayPalRefund(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.CASCADE, related_name='refunds')
    idempotency_key = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255, blank=True)
    ref = models.CharField(max_length=200, unique=True)
    outcome = models.CharField(max_length=16, default=ProviderWrite.SENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    reservation_released = models.BooleanField(default=False)
    recorded = models.BooleanField(default=False)   # Oscar ledger + refunded_amount applied once
    created = models.DateTimeField(auto_now_add=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['payment', 'idempotency_key'],
                                               name='paypal_refund_unique_key_per_payment')]
        ordering = ['created']

    def as_dict(self):
        return {
            'refundId': str(self.public_id),
            'amount': str(self.amount),
            'currency': self.payment.currency,
            'status': self.outcome,
            'paypalRefundId': self.paypal_refund_id or None,
            'paypalStatus': self.paypal_status or None,
            'idempotencyKey': self.idempotency_key,
            'created': self.created.isoformat() if self.created else None,
        }


class OrderRequestKey(models.Model):
    """Caller-supplied Idempotency-Key for POST /api/orders: the same key returns the same order."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    order = models.ForeignKey('order.Order', null=True, blank=True, on_delete=models.CASCADE,
                              related_name='+')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'key'], name='paypal_order_request_key')]
