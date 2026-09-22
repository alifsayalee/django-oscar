from django.apps import AppConfig


class ApiConfig(AppConfig):
    """PayPal payments + saved-cards REST API for the sandbox.

    Additive app: it reuses Oscar's ``order.Order``/``order.Line`` models and adds
    only the PayPal-specific state (holds, captures, refunds, vaulted cards).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.api"
    label = "paypal_api"
    verbose_name = "PayPal API"
