from django.apps import AppConfig


class SmsNotificationsConfig(AppConfig):
    """
    Order notifications by SMS, sent through Twilio, exposed as a JSON API
    under ``/api/``.
    """

    name = 'apps.sms_notifications'
    label = 'sms_notifications'
    verbose_name = 'SMS order notifications'
    default_auto_field = 'django.db.models.AutoField'
