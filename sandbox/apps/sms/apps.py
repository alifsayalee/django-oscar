from django.apps import AppConfig


class SmsConfig(AppConfig):
    name = "apps.sms"
    label = "sms"
    verbose_name = "Order SMS notifications"
    default_auto_field = "django.db.models.BigAutoField"
