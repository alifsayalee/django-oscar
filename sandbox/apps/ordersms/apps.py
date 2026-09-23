from django.apps import AppConfig


class OrderSmsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ordersms"
    label = "ordersms"
    verbose_name = "SMS order notifications"
