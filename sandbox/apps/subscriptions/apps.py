from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    """
    Recurring-subscription billing with Maxio Advanced Billing as the system
    of record. Additive to (and independent of) Oscar's basket/checkout.
    """

    name = "apps.subscriptions"
    label = "subscriptions"
    verbose_name = "Subscriptions (Maxio)"
    default_auto_field = "django.db.models.BigAutoField"
