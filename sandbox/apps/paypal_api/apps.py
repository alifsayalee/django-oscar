from django.apps import AppConfig


class PaypalApiConfig(AppConfig):
    name = "apps.paypal_api"
    label = "paypal_api"
    verbose_name = "PayPal payments API"
    default_auto_field = "django.db.models.BigAutoField"
