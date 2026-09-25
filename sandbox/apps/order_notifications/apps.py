from django.apps import AppConfig


class OrderNotificationsConfig(AppConfig):
    name = 'apps.order_notifications'
    label = 'order_notifications'
    verbose_name = 'Order notifications (SMS)'
    default_auto_field = 'django.db.models.BigAutoField'
