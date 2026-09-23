from django.apps import AppConfig


class PayPalPaymentsConfig(AppConfig):
    name = "apps.paypal_payments"
    label = "paypal_payments"
    verbose_name = "PayPal payments API"
    default_auto_field = "django.db.models.AutoField"
