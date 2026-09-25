from django.core.management.base import BaseCommand

from apps.sms_notifications import services
from apps.sms_notifications.models import SmsNotification


class Command(BaseCommand):
    help = ("Ask Twilio what became of every notification whose outcome is not settled "
            "(there is no callback URL, so the provider is polled).")

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500)

    def handle(self, *args, **options):
        rows = SmsNotification.objects.exclude(outcome__in=[SmsNotification.DONE, SmsNotification.FAILED]) \
            | SmsNotification.objects.filter(cancel_state__in=['pending', 'unknown'])
        rows = list(rows.distinct().order_by('pk'))
        services.refresh_all(rows, limit=options['limit'])
        counts: dict[str, int] = {}
        for row in SmsNotification.objects.filter(pk__in=[r.pk for r in rows]):
            counts[row.outcome] = counts.get(row.outcome, 0) + 1
        self.stdout.write("checked %d notification(s): %s" % (min(len(rows), options['limit']), counts))
