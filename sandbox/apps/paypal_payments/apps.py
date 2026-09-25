from django.apps import AppConfig


class PaypalPaymentsConfig(AppConfig):
    """
    JSON API that takes payment for Oscar orders through PayPal (card
    authorization, capture at fulfilment, void, refund) and keeps shoppers'
    saved cards in PayPal's vault.
    """

    name = 'apps.paypal_payments'
    label = 'paypal_payments'
    verbose_name = 'PayPal payments'
    default_auto_field = 'django.db.models.AutoField'
