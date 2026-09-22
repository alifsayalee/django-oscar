from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    """Maxio Advanced Billing subscription-billing app.

    An additive, parallel capability alongside the sandbox's one-time commerce
    flow. It owns no database models: Maxio is the system of record and the
    app's users are Oscar's own auth users, so there is nothing to migrate.
    """

    name = 'apps.subscriptions'
    label = 'maxio_subscriptions'
    verbose_name = 'Maxio Subscriptions'
