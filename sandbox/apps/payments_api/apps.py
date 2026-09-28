from django.apps import AppConfig


class PaymentsApiConfig(AppConfig):
    """JSON API that takes payment for Oscar orders through PayPal."""

    name = 'apps.payments_api'
    label = 'payments_api'
    verbose_name = 'Payments API (PayPal)'
    default_auto_field = 'django.db.models.BigAutoField'
