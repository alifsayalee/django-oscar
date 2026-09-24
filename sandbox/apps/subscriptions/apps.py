from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    """Recurring-subscription billing, with Maxio Advanced Billing as the system of record."""

    name = 'apps.subscriptions'
    label = 'subscriptions'
    verbose_name = 'Subscriptions'
    default_auto_field = 'django.db.models.BigAutoField'
