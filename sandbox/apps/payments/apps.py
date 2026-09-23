from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """PayPal payments and saved cards, exposed as a JSON API under /api/."""

    name = "apps.payments"
    label = "payments"
    verbose_name = "PayPal payments"
