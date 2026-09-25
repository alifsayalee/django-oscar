from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.sms_notifications import services
from apps.sms_notifications.models import Notification
from apps.sms_notifications.safe_write import TERMINAL


class Command(BaseCommand):
    help = (
        "Ask the provider what became of every unsettled SMS notification "
        "(delivery status, unknown outcomes, pending cancellations)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args: Any, **options: Any) -> None:
        candidates = Notification.objects.exclude(outcome__in=TERMINAL) | Notification.objects.filter(
            cancel_requested_at__isnull=False
        ).exclude(cancel_outcome__in=TERMINAL)
        batch = list(candidates.distinct().order_by("id")[: options["limit"]])
        services.refresh_many(batch, len(batch))
        self.stdout.write(f"Checked {len(batch)} notification(s).")
