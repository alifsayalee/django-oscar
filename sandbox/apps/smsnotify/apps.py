import logging

from django.apps import AppConfig


class SMSNotifyConfig(AppConfig):
    """Sandbox app that sends order-progress SMS notifications through Twilio.

    Additive to Oscar's checkout/order flow -- it reuses Oscar's own Order/Line
    models and only adds the shopper's contact numbers and the notification log.
    """

    name = "apps.smsnotify"
    label = "smsnotify"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "SMS order notifications"

    def ready(self):
        # httpx logs the request line ("HTTP Request: GET .../PhoneNumbers/+1...")
        # at INFO; a Lookup URL embeds the shopper's number in the path. Keep those
        # loggers at WARNING so a shopper's number is never written to logs.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
