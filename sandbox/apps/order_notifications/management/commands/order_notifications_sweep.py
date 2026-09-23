from django.core.management.base import BaseCommand

from apps.order_notifications import services
from apps.order_notifications import status as st
from apps.order_notifications.models import Notification


class Command(BaseCommand):
    help = (
        "Retry follow-up cancellations that did not complete, and bring notifications "
        "whose outcome is not final up to date with the provider."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, limit, **options):
        pending_cancels = list(
            Notification.objects.filter(
                kind=Notification.FOLLOW_UP, cancel_requested_at__isnull=False
            ).exclude(outcome__in=st.TERMINAL)[:limit]
        )
        for notification in pending_cancels:
            services.refresh(notification)
        others = list(
            Notification.objects.filter(cancel_requested_at__isnull=True)
            .exclude(outcome__in=st.TERMINAL)
            .order_by("created_at")[:limit]
        )
        for notification in others:
            services.refresh(notification)
        still = Notification.objects.filter(
            kind=Notification.FOLLOW_UP, cancel_requested_at__isnull=False
        ).exclude(outcome__in=st.TERMINAL)
        self.stdout.write(
            "refreshed %s; follow-ups still awaiting cancellation: %s"
            % (len(pending_cancels) + len(others), still.count())
        )
