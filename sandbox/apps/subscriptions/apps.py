from django.apps import AppConfig


# Django ships no type hints and django-stubs is not installed, so AppConfig is Any
# under mypy --strict; the subclass is otherwise sound.
class SubscriptionsConfig(AppConfig):  # type: ignore[misc]
    name = "apps.subscriptions"
    label = "maxio_subscriptions"
    verbose_name = "Maxio Subscriptions"
