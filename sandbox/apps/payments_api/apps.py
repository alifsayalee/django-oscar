from django.apps import AppConfig


class PaymentsApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payments_api"
    label = "payments_api"
    verbose_name = "PayPal payments API"
