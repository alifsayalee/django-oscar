from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    """
    Recurring subscriptions, billed through Maxio Advanced Billing.

    Runs alongside Oscar's one-off basket/checkout flow; it does not replace it.
    """
    name = 'apps.subscriptions'
    label = 'subscriptions'
    verbose_name = 'Subscriptions'
    default_auto_field = 'django.db.models.BigAutoField'
