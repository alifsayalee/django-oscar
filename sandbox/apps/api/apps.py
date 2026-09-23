from django.apps import AppConfig


class ApiConfig(AppConfig):
    """The additive PayPal payments/checkout API for the sandbox.

    It reuses Oscar's own order, payment and bankcard models (see ``models.py``)
    and only adds the small amount of PayPal-owned state that has no home on an
    Oscar model.
    """

    name = "apps.api"
    label = "api"
    verbose_name = "PayPal checkout API"
    default_auto_field = "django.db.models.BigAutoField"
