from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    """Maxio Advanced Billing subscription capability for the sandbox.

    An additive, parallel capability to Oscar's one-time commerce flow: it lets a
    logged-in shopper browse Maxio plans, subscribe, and read their subscriptions back,
    with Maxio as the billing system of record.
    """

    name = "apps.subscriptions"
    label = "subscriptions"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "Maxio Subscriptions"
