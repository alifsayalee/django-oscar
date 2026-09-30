from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    """Recurring subscriptions billed through Maxio Advanced Billing."""

    name = "apps.subscriptions"
    label = "subscriptions"
    verbose_name = "Subscriptions"
    default_auto_field = "django.db.models.BigAutoField"
