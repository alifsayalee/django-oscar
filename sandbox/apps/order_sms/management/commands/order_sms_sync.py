"""
Bring every unsettled notification up to date by asking the provider.

The provider cannot call back into this application, so outcomes are learned by asking. The read
endpoints do this for the records they show; this command does it for all of them - including
calling off any follow-up that must no longer go out - and can be run by an operator at any time.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.order_sms import notifications
from apps.order_sms.models import Notification


class Command(BaseCommand):
    help = 'Re-read unsettled SMS notifications from the provider and call off stale follow-ups.'

    def handle(self, *args: object, **options: object) -> None:
        qs = Notification.objects.filter(
            Q(outcome__in=[Notification.PENDING, Notification.UNKNOWN, Notification.SENDING])
            | Q(kind=Notification.KIND_FOLLOWUP, cancel_outcome__in=[
                Notification.ACTION_REQUESTED, Notification.PENDING, Notification.UNKNOWN])
            | Q(kind=Notification.KIND_FOLLOWUP, order__status='Cancelled')
            | Q(kind=Notification.KIND_FOLLOWUP, contact__deleted_at__isnull=False)
        ).exclude(kind=Notification.KIND_FOLLOWUP, cancel_outcome=Notification.DONE).distinct()
        for n in qs.select_related('contact', 'order'):
            n = notifications.refresh(n, force=True)
            self.stdout.write(f'notification {n.pk}: {n.outcome} '
                              f'(provider {n.provider_status or "-"}, call-off {n.cancel_outcome or "-"})')
