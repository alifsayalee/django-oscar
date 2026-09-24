from django.apps import AppConfig


class PaymentsApiConfig(AppConfig):
    """
    HTTP API that takes real payments through PayPal: authorize at checkout,
    capture at fulfilment, void on cancel, refund on return, and saved cards
    through the PayPal vault.
    """

    name = 'apps.payments_api'
    label = 'payments_api'
    verbose_name = 'PayPal payments API'
    default_auto_field = 'django.db.models.BigAutoField'
