from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """PayPal card payments, saved cards and reconciliation for the sandbox API."""

    name = "apps.payments"
    label = "sandbox_payments"
    verbose_name = "PayPal payments"
    default_auto_field = "django.db.models.BigAutoField"
