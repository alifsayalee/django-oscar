from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.order_notifications import services
from apps.order_notifications.models import Notification


class Command(BaseCommand):
    help = ('Ask Twilio for the current state of unfinished SMS notifications, settle unknown '
            'outcomes by lookup, and call off follow-ups of cancelled orders.')

    def handle(self, *args, **options):
        notifications = Notification.objects.filter(
            Q(outcome__in=[Notification.OUTCOME_SENDING, Notification.OUTCOME_PENDING,
                           Notification.OUTCOME_UNKNOWN])
            | (Q(kind=Notification.KIND_DELIVERY_FOLLOWUP,
                 order__status=services.ORDER_CANCELLED_STATUS)
               & ~Q(cancel_state__in=[Notification.CANCEL_DONE, Notification.CANCEL_TOO_LATE]))
        ).select_related('contact_number', 'order')
        count = 0
        for notification in notifications:
            services.sweep([notification])
            count += 1
        self.stdout.write(f'Checked {count} notification(s).')
