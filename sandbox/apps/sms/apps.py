import logging

from django.apps import AppConfig


class SmsConfig(AppConfig):
    label = "sms"
    name = "apps.sms"
    verbose_name = "SMS order notifications"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # The underlying HTTP libraries log full request URLs at DEBUG, and the number-lookup
        # URL carries the phone number in its path. Keep a shopper's number out of the logs by
        # never letting these emit below WARNING.
        for name in ("httpx", "httpcore", "hpack", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)
