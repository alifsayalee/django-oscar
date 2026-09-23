from django.apps import AppConfig


class PayPalPaymentsConfig(AppConfig):
    """
    PayPal payments and saved cards for the sandbox, exposed as a JSON API
    under ``/api/``.

    The package is deliberately not called ``paypal``: that is the import root
    of the PayPal Server SDK this app talks to.
    """

    name = 'apps.paypal_payments'
    label = 'paypal_payments'
    verbose_name = 'PayPal payments'
    default_auto_field = 'django.db.models.BigAutoField'
