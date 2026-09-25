from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    name = "apps.payments"
    label = "sandbox_payments"
    verbose_name = "PayPal payments"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import paypal_gateway, services

        # The client is built lazily on first use (after any worker fork) from settings read at that time.
        paypal_gateway.configure(lambda: paypal_gateway.build_client(services.config()))
