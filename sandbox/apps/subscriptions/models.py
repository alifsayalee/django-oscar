from django.conf import settings
from django.db import models


class MaxioCustomer(models.Model):
    """Links a local Django user to their Maxio (Chargify) customer record.

    The link is the anchor for idempotency: a user maps to exactly one Maxio
    customer, identified by a stable ``reference`` we derive from the user's
    primary key. Storing it locally means we neither re-look-up the customer on
    every request nor risk creating a second customer for the same user.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='maxio_customer',
    )
    #: The value we send to Maxio as the customer ``reference`` (stable per user).
    reference = models.CharField(max_length=255, unique=True)
    #: The numeric customer id assigned by Maxio. Null while the local row has
    #: been claimed (to serialise concurrent first requests) but the Maxio
    #: customer has not yet been resolved.
    maxio_customer_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'subscriptions'
        verbose_name = 'Maxio customer'
        verbose_name_plural = 'Maxio customers'

    def __str__(self):
        return f'{self.reference} -> Maxio #{self.maxio_customer_id}'
