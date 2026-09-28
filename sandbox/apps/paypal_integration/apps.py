from django.apps import AppConfig


class PayPalIntegrationConfig(AppConfig):
    name = 'apps.paypal_integration'
    label = 'paypal_integration'
    verbose_name = 'PayPal integration'
    default_auto_field = 'django.db.models.AutoField'
