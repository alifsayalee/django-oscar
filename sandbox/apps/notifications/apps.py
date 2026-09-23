from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """SMS order-notifications app for the sandbox.

    Additive: it does not replace Oscar's catalogue/basket/order flow. It stores the shopper's
    mobile contact number, records every message sent as an order moves, and exposes the operator's
    view of what reached the customer -- all through Twilio.
    """

    name = "apps.notifications"
    # Explicit label to avoid any clash with Oscar's own app labels.
    label = "sms_notifications"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "SMS order notifications"
