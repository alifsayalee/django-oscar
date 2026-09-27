"""
State this app keeps beside Oscar's own order and payment models.

Oscar's ``Order``/``Line`` hold the order, Oscar's ``payment.Source`` /
``payment.Transaction`` hold the money ledger and Oscar's ``payment.Bankcard``
holds saved cards. What Oscar has no place for is the state PayPal owns (ids
and statuses of the hold, the capture and the refunds) and a durable record of
every write this app asked PayPal to make -- that is what lives here.
"""
import secrets
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, models, transaction


class Installation(models.Model):
    """
    A random id created once per database.

    It is part of every reference sent to PayPal, so a rebuilt sandbox database
    (whose order and user ids start again from 1) never re-sends a
    ``PayPal-Request-Id`` an earlier database already used.
    """

    install_id = models.CharField(max_length=16, unique=True)

    @classmethod
    def current_id(cls) -> str:
        row = cls.objects.filter(pk=1).first()
        if row is None:
            try:
                with transaction.atomic():
                    row = cls.objects.create(pk=1, install_id=secrets.token_hex(6))
            except IntegrityError:
                row = cls.objects.get(pk=1)
        return row.install_id


class OrderPayment(models.Model):
    """
    PayPal payment state for one Oscar order placed through the API.
    """

    AWAITING_PAYMENT = 'awaiting_payment'
    AUTHORIZED = 'authorized'
    CAPTURED = 'captured'
    PARTIALLY_REFUNDED = 'partially_refunded'
    REFUNDED = 'refunded'
    VOIDED = 'voided'
    CANCELLED = 'cancelled'
    STATES = [
        (AWAITING_PAYMENT, 'Awaiting payment'),
        (AUTHORIZED, 'Authorized (funds held)'),
        (CAPTURED, 'Captured'),
        (PARTIALLY_REFUNDED, 'Partially refunded'),
        (REFUNDED, 'Refunded'),
        (VOIDED, 'Authorization voided'),
        (CANCELLED, 'Cancelled before payment'),
    ]

    order = models.OneToOneField(
        'order.Order', on_delete=models.CASCADE, related_name='paypal_payment')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='paypal_payments')
    # Oscar's payment ledger for this order (allocate / debit / refund).
    source = models.OneToOneField(
        'payment.Source', on_delete=models.PROTECT, related_name='paypal_payment')
    # Optional caller-supplied key making POST /api/orders safe to repeat.
    client_key = models.CharField(max_length=64, null=True, blank=True)

    state = models.CharField(max_length=32, choices=STATES, default=AWAITING_PAYMENT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    # Incremented when a pay attempt ends failed, so the next attempt goes to
    # PayPal under a new reference.
    pay_attempt = models.PositiveIntegerField(default=1)

    paypal_order_id = models.CharField(max_length=64, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    # PayPal's clock: when the (current) authorization was created and when it
    # can no longer be captured.
    authorization_created_at = models.DateTimeField(null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)

    capture_id = models.CharField(max_length=64, blank=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))
    paypal_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Refunds: ``refund_reserved`` counts every refund not known to have
    # failed (so concurrent partial refunds can never exceed the capture);
    # ``refunded_amount`` counts the ones PayPal reported completed.
    refund_reserved = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))
    refunded_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'))

    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'client_key'], name='paypal_unique_order_client_key'),
            models.CheckConstraint(
                condition=models.Q(refund_reserved__lte=models.F('captured_amount')),
                name='paypal_refunds_within_capture'),
        ]

    def __str__(self) -> str:
        return 'PayPal payment for order %s (%s)' % (self.order.number, self.state)


class ProviderWrite(models.Model):
    """
    The claim and outcome record of one write step sent to PayPal.

    ``ref`` is unique: inserting it is the claim that lets exactly one request
    make the write, and it is sent as the ``PayPal-Request-Id`` so a repeat of
    the same step is de-duplicated by PayPal too. Refunds are rows of kind
    ``refund``.
    """

    CREATE_ORDER = 'create_order'
    AUTHORIZE = 'authorize'
    REAUTHORIZE = 'reauthorize'
    CAPTURE = 'capture'
    VOID = 'void'
    REFUND = 'refund'
    VAULT_CREATE = 'vault_create'
    KINDS = [
        (CREATE_ORDER, 'Create and authorize PayPal order'),
        (AUTHORIZE, 'Authorize PayPal order'),
        (REAUTHORIZE, 'Reauthorize payment'),
        (CAPTURE, 'Capture authorization'),
        (VOID, 'Void authorization'),
        (REFUND, 'Refund capture'),
        (VAULT_CREATE, 'Save card in PayPal vault'),
    ]

    SENDING = 'sending'
    DONE = 'done'
    PENDING = 'pending'
    FAILED = 'failed'
    NEEDS_REVIEW = 'needs_review'
    UNKNOWN = 'unknown'
    OUTCOMES = [
        (SENDING, 'Sending'),
        (DONE, 'Done'),
        (PENDING, 'Pending at PayPal'),
        (FAILED, 'Failed'),
        (NEEDS_REVIEW, 'Needs review'),
        (UNKNOWN, 'Unknown'),
    ]

    ref = models.CharField(max_length=160, unique=True)
    # The operation step the claim is for. ``ref`` is ``base_ref`` plus a
    # generation suffix once an earlier claim for the same step was released.
    base_ref = models.CharField(max_length=160, db_index=True)
    kind = models.CharField(max_length=32, choices=KINDS)
    order_payment = models.ForeignKey(
        OrderPayment, null=True, blank=True, on_delete=models.CASCADE,
        related_name='writes')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
        related_name='paypal_writes')

    # What was asked for (None for writes that move no money).
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)

    outcome = models.CharField(max_length=16, choices=OUTCOMES, default=SENDING)
    provider_id = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=32, blank=True)
    # PayPal's own event time -- the clock reconciliation filters on.
    provider_time = models.DateTimeField(null=True, blank=True, db_index=True)
    echoed_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)

    # Set once the outcome's effects are applied to Oscar's ledger / our state.
    applied = models.BooleanField(default=False)
    # A released claim no longer holds its reference (see ``release``).
    released = models.BooleanField(default=False)

    claimed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['claimed_at', 'pk']

    def __str__(self) -> str:
        return '%s %s (%s)' % (self.kind, self.ref, self.outcome)
