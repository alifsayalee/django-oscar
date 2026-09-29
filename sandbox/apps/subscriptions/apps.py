from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    name = 'apps.subscriptions'
    label = 'maxio_subscriptions'
    verbose_name = 'Subscriptions (Maxio Advanced Billing)'
    default_auto_field = 'django.db.models.BigAutoField'
