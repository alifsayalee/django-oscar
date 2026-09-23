from django.apps import AppConfig


class PaypalCheckoutConfig(AppConfig):
    name = "apps.paypal_checkout"
    label = "paypal_checkout"
    verbose_name = "PayPal checkout API"
    default_auto_field = "django.db.models.BigAutoField"
