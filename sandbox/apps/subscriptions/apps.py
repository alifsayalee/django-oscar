from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    """Maxio-backed recurring subscription billing for the sandbox.

    This is an additive, parallel capability alongside Oscar's one-time
    commerce flow -- it exposes an HTTP API under ``/api/`` and keeps a small
    idempotency ledger; it does not replace the basket/checkout pipeline.
    """

    name = 'apps.subscriptions'
    label = 'subscriptions'
    default_auto_field = 'django.db.models.BigAutoField'
    verbose_name = 'Maxio subscriptions'
