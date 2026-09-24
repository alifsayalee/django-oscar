from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SubscriptionsConfig(AppConfig):
    label = 'subscriptions'
    name = 'apps.subscriptions'
    verbose_name = _('Subscriptions')
    default_auto_field = 'django.db.models.BigAutoField'
