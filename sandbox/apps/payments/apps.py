from django.apps import AppConfig


class PaymentsApiConfig(AppConfig):
    name = "apps.payments"
    label = "sandbox_payments"
    verbose_name = "PayPal payments API"
    default_auto_field = "django.db.models.BigAutoField"
