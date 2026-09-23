from django.core.management.base import BaseCommand

from apps.sms_notifications import services
from apps.sms_notifications.models import Notification


class Command(BaseCommand):
    help = (
        "Ask Twilio for the current state of unsettled order SMS notifications: retry "
        "follow-up cancellations that were not confirmed, resolve sends whose outcome "
        "is unknown, and refresh delivery statuses."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **options):
        result = services.sync_notifications(Notification.objects.all(), limit=options["limit"])
        self.stdout.write("checked %s notification(s), %s not yet confirmed" % (result.checked, result.errors))
