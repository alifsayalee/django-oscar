from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    """Recurring-subscription billing backed by Maxio Advanced Billing."""

    name = 'apps.subscriptions'
    label = 'maxio_subscriptions'
    verbose_name = 'Maxio subscriptions'
    default_auto_field = 'django.db.models.BigAutoField'
