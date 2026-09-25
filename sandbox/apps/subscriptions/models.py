"""
Claims on writes made to Maxio Advanced Billing.

Maxio is the system of record for customers and subscriptions. These rows do
not copy it: each one records that *this* install asked Maxio for something,
under a reference that is sent with the write, so that a repeated request can
be answered from it and a write whose outcome is unknown can be looked up.
Every row is inserted (and committed) before the provider call is made.
"""
import secrets

from django.db import models
from django.db.models import Q

from oscar.core.compat import AUTH_USER_MODEL


class Outcome(models.TextChoices):
    SENDING = 'sending', 'Sending'            # claimed, no answer yet
    DONE = 'done', 'Done'
    PENDING = 'pending', 'Pending'            # provider accepted it, not finished / not in good standing
    FAILED = 'failed', 'Failed'               # never sent, refused, or ended/undone at the provider
    UNKNOWN = 'unknown', 'Unknown'            # may have happened; settled only by a provider lookup
    NEEDS_REVIEW = 'needs_review', 'Needs review'  # happened, but not as asked


class BillingInstall(models.Model):
    """
    Singleton holding a random token unique to this database, used to prefix
    every reference sent to Maxio so two installs sharing a Maxio site never
    collide on the same user id.
    """
    token = models.CharField(max_length=32, unique=True)

    @classmethod
    def current_token(cls) -> str:
        install, _ = cls.objects.get_or_create(
            pk=1, defaults={'token': secrets.token_hex(6)})
        return str(install.token)


class ProviderWriteClaim(models.Model):
    reference = models.CharField(max_length=191, unique=True)
    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.SENDING)
    provider_time = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BillingCustomer(ProviderWriteClaim):
    """The claim on creating the Maxio customer for one Oscar user."""
    user = models.OneToOneField(
        AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='maxio_billing_customer')
    maxio_customer_id = models.BigIntegerField(null=True, blank=True, unique=True)

    def __str__(self) -> str:
        return f'{self.reference} ({self.outcome})'


class SubscriptionClaim(ProviderWriteClaim):
    """
    The claim on creating one Maxio subscription for (user, plan).

    ``slot_open`` rows are unique per (user, plan_handle): while a subscription
    to a plan is live, in progress or unresolved, a second subscribe to the same
    plan is answered from this row instead of creating another subscription.
    """
    user = models.ForeignKey(
        AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='maxio_subscription_claims')
    plan_handle = models.CharField(max_length=128)
    slot_open = models.BooleanField(default=True)
    maxio_subscription_id = models.BigIntegerField(null=True, blank=True, unique=True)
    state = models.CharField(max_length=32, blank=True)
    plan_name = models.CharField(max_length=255, blank=True)
    price_in_cents = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    next_billing_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-claimed_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'plan_handle'], condition=Q(slot_open=True),
                name='maxio_one_open_subscription_per_user_plan'),
        ]

    def __str__(self) -> str:
        return f'{self.reference} ({self.outcome})'
