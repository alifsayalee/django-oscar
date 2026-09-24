from django.apps import AppConfig


class PaymentsApiConfig(AppConfig):
    """PayPal payments and saved cards for the sandbox, exposed under /api/."""

    name = 'apps.payments_api'
    label = 'payments_api'
    verbose_name = 'PayPal payments API'
    default_auto_field = 'django.db.models.AutoField'
