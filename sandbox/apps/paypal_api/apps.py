from django.apps import AppConfig


class PayPalApiConfig(AppConfig):
    name = "apps.paypal_api"
    label = "paypal_api"
    verbose_name = "PayPal payments API"
    default_auto_field = "django.db.models.BigAutoField"
