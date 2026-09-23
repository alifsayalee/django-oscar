from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """
    PayPal payments and saved cards for the sandbox, exposed as a JSON API.
    """

    name = "apps.payments"
    label = "payments"
    verbose_name = "PayPal payments"
    default_auto_field = "django.db.models.BigAutoField"
