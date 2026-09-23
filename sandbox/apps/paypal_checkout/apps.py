from django.apps import AppConfig


class PayPalCheckoutConfig(AppConfig):
    """Sandbox app that adds PayPal card payments and saved cards on top of the
    existing Oscar catalogue/basket/order flow.

    It is purely additive: orders, order lines and payment sources reuse Oscar's
    own models (``oscar.apps.order`` and ``oscar.apps.payment``); this app only
    adds the PayPal-specific state that Oscar has no field for, and the HTTP API
    that drives the money movement.
    """

    name = "apps.paypal_checkout"
    label = "paypal_checkout"
    verbose_name = "PayPal checkout"
    default_auto_field = "django.db.models.BigAutoField"
