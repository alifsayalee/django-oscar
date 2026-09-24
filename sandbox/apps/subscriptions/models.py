"""
Local records for the Maxio integration.

Maxio is the system of record for *what exists* (customers, subscriptions,
plans). These tables only record *that this site asked* for a provider write,
so that a double-click, a caller retry or two racing workers can never create
a second customer or subscription, and so that a write whose answer was lost
can be looked up again by the reference it was sent with.
"""
import secrets

from django.db import IntegrityError, models, transaction

from oscar.core import compat


def _new_install_token() -> str:
    return secrets.token_hex(5)


class MaxioInstall(models.Model):
    """
    One row per database: a random token that makes every reference this
    install sends to Maxio unique, even on a Maxio site shared with other
    installs that loaded the same user fixtures.
    """

    SINGLETON_ID = 1

    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_ID)
    token = models.CharField(max_length=32, default=_new_install_token, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Maxio install identity'

    def __str__(self) -> str:
        return self.token

    @classmethod
    def current_token(cls) -> str:
        try:
            with transaction.atomic():
                install, _ = cls.objects.get_or_create(id=cls.SINGLETON_ID)
        except IntegrityError:
            # Another request created it between our read and our insert.
            install = cls.objects.get(id=cls.SINGLETON_ID)
        return install.token


class MaxioWriteClaim(models.Model):
    """
    A claim on one provider write step, taken *before* the provider is called.

    The unique ``reference`` is the claim: the database rejects a second insert
    for the same reference, and the loser answers from what is recorded here.
    The same reference travels to Maxio on the write, so a write whose outcome
    is unknown can be found again by it.
    """

    KIND_CUSTOMER = 'customer'
    KIND_SUBSCRIPTION = 'subscription'
    KIND_CHOICES = (
        (KIND_CUSTOMER, 'Customer'),
        (KIND_SUBSCRIPTION, 'Subscription'),
    )

    # Claimed, no answer yet: nobody else calls the provider for it.
    SENDING = 'sending'
    # The provider says what was asked for is in effect.
    DONE = 'done'
    # The provider accepted it and it is not (yet) in good standing.
    PENDING = 'pending'
    # Never sent, refused, or reported failed/undone by the provider.
    FAILED = 'failed'
    # It happened, but not as asked (e.g. a different plan came back).
    NEEDS_REVIEW = 'needs_review'
    # May have happened; only a lookup by reference can settle it.
    UNKNOWN = 'unknown'
    OUTCOME_CHOICES = (
        (SENDING, 'Sending'),
        (DONE, 'Done'),
        (PENDING, 'Pending'),
        (FAILED, 'Failed'),
        (NEEDS_REVIEW, 'Needs review'),
        (UNKNOWN, 'Unknown'),
    )

    reference = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    user = models.ForeignKey(
        compat.AUTH_USER_MODEL, related_name='maxio_write_claims', on_delete=models.CASCADE)
    plan_handle = models.CharField(max_length=255, blank=True)
    outcome = models.CharField(max_length=32, choices=OUTCOME_CHOICES, default=SENDING)

    provider_id = models.CharField(max_length=64, blank=True)
    provider_state = models.CharField(max_length=64, blank=True)
    # The provider's own clock (created_at), for reconciliation.
    provider_time = models.DateTimeField(null=True, blank=True)

    # Snapshot of what the provider answered, to answer a repeat request.
    plan_name = models.CharField(max_length=255, blank=True)
    price_in_cents = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    next_billing_at = models.DateTimeField(null=True, blank=True)
    detail = models.TextField(blank=True)

    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-claimed_at',)
        indexes = [models.Index(fields=('user', 'kind'))]

    def __str__(self) -> str:
        return '%s (%s)' % (self.reference, self.outcome)
