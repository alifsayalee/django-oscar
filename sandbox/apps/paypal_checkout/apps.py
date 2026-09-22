from django.apps import AppConfig


class PayPalCheckoutConfig(AppConfig):
    """Sandbox app that adds a PayPal-backed payment REST API on top of Oscar.

    It reuses Oscar's own ``order``/``payment`` models for orders and money
    movement, and keeps the PayPal-owned state (order/authorization/capture/
    refund ids and statuses, vaulted cards) in this app's own models.
    """

    name = "apps.paypal_checkout"
    label = "paypal_checkout"
    verbose_name = "PayPal checkout"
    default_auto_field = "django.db.models.BigAutoField"
