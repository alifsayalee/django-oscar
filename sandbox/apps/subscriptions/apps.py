from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    """Maxio Advanced Billing recurring-subscription capability.

    An additive, parallel capability to Oscar's one-time cart/checkout flow. Maxio is the
    system of record for customers and subscriptions, so this app keeps no local models of
    its own and reuses Oscar's user model for caller identity.
    """

    name = 'apps.subscriptions'
    label = 'maxio_subscriptions'
    verbose_name = 'Maxio subscriptions'
    default_auto_field = 'django.db.models.BigAutoField'
