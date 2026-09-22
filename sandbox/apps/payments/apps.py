from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """Sandbox app exposing PayPal payments and saved cards over an HTTP API.

    Additive: it reuses Oscar's ``order`` and ``payment`` models and does not
    replace any of the existing catalogue/basket/order flow.
    """

    name = "apps.payments"
    label = "sandbox_payments"
    verbose_name = "Sandbox PayPal payments"
    default_auto_field = "django.db.models.BigAutoField"
