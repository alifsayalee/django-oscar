from django.apps import AppConfig


class PayPalApiConfig(AppConfig):
    """Sandbox app exposing PayPal card payments and saved cards over /api/."""

    name = "apps.paypal_api"
    label = "paypal_api"
    verbose_name = "PayPal API"
    default_auto_field = "django.db.models.BigAutoField"
