from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """
    JSON API that takes payment for Oscar orders through PayPal and lets a
    shopper keep cards in PayPal's vault.
    """

    name = 'apps.payments'
    label = 'sandbox_payments'
    verbose_name = 'Payments (PayPal)'
    default_auto_field = 'django.db.models.BigAutoField'
