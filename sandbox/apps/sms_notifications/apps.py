from django.apps import AppConfig


class SmsNotificationsConfig(AppConfig):
    name = "apps.sms_notifications"
    label = "sms_notifications"
    verbose_name = "Order SMS notifications"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401  (connects the order signal receivers)
