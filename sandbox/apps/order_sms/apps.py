from django.apps import AppConfig


class OrderSmsConfig(AppConfig):
    name = "apps.order_sms"
    label = "order_sms"
    verbose_name = "Order SMS notifications"
    default_auto_field = "django.db.models.BigAutoField"
