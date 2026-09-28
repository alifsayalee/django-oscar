"""
Storage for the PayPal integration.

Oscar already models the order (``order.Order``), the money bookkeeping
(``payment.Source`` / ``payment.Transaction``) and a shopper's saved cards
(``payment.Bankcard``). What it has no place for is the state PayPal owns --
authorization, capture and refund ids and statuses, PayPal's fee -- and a
durable record of every write sent to PayPal. Those are the two models here.
"""
import secrets

from django.conf import settings
from django.db import models, transaction


class InstallIdentity(models.Model):
    """
    A random identifier generated once per database.

    Every reference sent to PayPal starts with it (unless
    ``PAYPAL_REFERENCE_PREFIX`` is configured), so two installs sharing one
    PayPal account -- or a rebuilt database that reuses order numbers -- never
    send the same reference twice.
    """

    SINGLETON_ID = 1

    prefix = models.CharField(max_length=32, unique=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payments_api'

    def __str__(self):
        return self.prefix

    @classmethod
    def reference_prefix(cls) -> str:
        configured = getattr(settings, 'PAYPAL_REFERENCE_PREFIX', '')
        if configured:
            return configured
        with transaction.atomic():
            identity, __ = cls.objects.get_or_create(
                pk=cls.SINGLETON_ID,
                defaults={'prefix': 'osc-%s' % secrets.token_hex(6)})
        return str(identity.prefix)


class PayPalPayment(models.Model):
    """The PayPal side of one Oscar order's payment."""

    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZING = 'authorizing'
    AUTHORIZATION_PENDING = 'authorization_pending'
    AUTHORIZATION_UNKNOWN = 'authorization_unknown'
    AUTHORIZED = 'authorized'
    AUTHORIZATION_EXPIRED = 'authorization_expired'
    CAPTURING = 'capturing'
    CAPTURE_PENDING = 'capture_pending'
    CAPTURED = 'captured'
    PARTIALLY_REFUNDED = 'partially_refunded'
    REFUNDED = 'refunded'
    VOIDED = 'voided'
    CANCELLED = 'cancelled'
    NEEDS_REVIEW = 'needs_review'

    # States in which the shopper may (again) ask for an authorization.
    PAYABLE_STATES = (AWAITING_PAYMENT, AUTHORIZATION_EXPIRED)

    order_id: int  # the FK column Django adds for ``order``
    order = models.OneToOneField(
        'order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    source = models.OneToOneField(
        'payment.Source', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='paypal_payment')
    state = models.CharField(max_length=32, default=AWAITING_PAYMENT)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(decimal_places=4, max_digits=16)

    payment_method = models.ForeignKey(
        'payment.Bankcard', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+')
    card_label = models.CharField(max_length=64, blank=True)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    # When PayPal created the current hold; its honor period runs from here.
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    # When PayPal created the first hold; reauthorization is bounded by it.
    original_authorization_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorized = models.BooleanField(default=False)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(
        decimal_places=4, max_digits=16, null=True, blank=True)
    paypal_fee = models.DecimalField(
        decimal_places=4, max_digits=16, null=True, blank=True)
    net_amount = models.DecimalField(
        decimal_places=4, max_digits=16, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    refunded_amount = models.DecimalField(
        decimal_places=4, max_digits=16, default=0)

    last_error = models.CharField(max_length=512, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'payments_api'

    def __str__(self):
        return '%s (%s)' % (self.order.number, self.state)


class ProviderWrite(models.Model):
    """
    One write sent (or about to be sent) to PayPal: the claim.

    The row is committed *before* the PayPal call, keyed by the reference that
    travels with the call. The unique constraint on ``ref`` is what stops a
    double-click, a caller retry or a second worker from sending the same write
    twice; the stored outcome answers every repeat.
    """

    SENDING = 'sending'
    DONE = 'done'
    PENDING = 'pending'
    FAILED = 'failed'
    NEEDS_REVIEW = 'needs_review'
    UNKNOWN = 'unknown'
    OUTCOMES = (SENDING, DONE, PENDING, FAILED, NEEDS_REVIEW, UNKNOWN)

    AUTHORIZE = 'authorize'
    ORDER_AUTHORIZE = 'order_authorize'
    REAUTHORIZE = 'reauthorize'
    CAPTURE = 'capture'
    VOID = 'void'
    REFUND = 'refund'
    VAULT_CREATE = 'vault_create'
    VAULT_DELETE = 'vault_delete'

    order_id: int | None  # the FK column Django adds for ``order``
    ref = models.CharField(max_length=190, unique=True)
    kind = models.CharField(max_length=32, db_index=True)
    order = models.ForeignKey(
        'order.Order', on_delete=models.PROTECT, null=True, blank=True,
        related_name='paypal_writes')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name='+')
    outcome = models.CharField(max_length=16, default=SENDING, db_index=True)

    # What we asked for.
    amount = models.DecimalField(
        decimal_places=4, max_digits=16, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    # Hash of the caller's idempotency key plus a fingerprint of the request,
    # so a key reused for a different request can be refused.
    request_fingerprint = models.CharField(max_length=64, blank=True)
    # A PayPal id known before the call (e.g. the PayPal order an authorize
    # step acts on, or the vault token being deleted).
    target_id = models.CharField(max_length=64, blank=True)

    # What PayPal said.
    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(max_length=32, blank=True)
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_amount = models.DecimalField(
        decimal_places=4, max_digits=16, null=True, blank=True)
    # PayPal's error name/issue/debug id, never request data.
    detail = models.JSONField(default=dict, blank=True)

    claimed_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'payments_api'
        ordering = ['pk']

    def __str__(self):
        return '%s [%s]' % (self.ref, self.outcome)
