from django.core.management.base import BaseCommand

from apps.paypal_payments import services


class Command(BaseCommand):
    help = "Re-read PayPal for every payment, refund, saved card or card deletion whose outcome is not yet known."

    def handle(self, *args, **options):
        count = services.settle_all()
        self.stdout.write(f"Re-checked {count} record(s) against PayPal.")
