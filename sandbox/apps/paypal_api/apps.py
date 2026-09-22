from django.apps import AppConfig


class PayPalApiConfig(AppConfig):
    """Additive REST API for PayPal card payments and saved cards.

    Lives alongside Oscar's storefront; reuses Oscar's own order, line and
    payment models rather than building a parallel set.
    """

    name = "apps.paypal_api"
    label = "paypal_api"
    verbose_name = "PayPal payments API"
    default_auto_field = "django.db.models.BigAutoField"
