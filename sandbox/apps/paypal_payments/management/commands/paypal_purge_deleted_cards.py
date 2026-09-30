from django.core.management.base import BaseCommand

from apps.paypal_payments.models import SavedCard
from apps.paypal_payments.services import purge_vault_token


class Command(BaseCommand):
    help = (
        "Remove from PayPal's vault the tokens of saved cards that shoppers deleted "
        "while PayPal could not be reached. Safe to run repeatedly (e.g. from cron)."
    )

    def handle(self, *args, **options):
        pending = SavedCard.objects.filter(deleted_at__isnull=False, vault_token_deleted=False)
        removed = failed = 0
        for card in pending:
            if purge_vault_token(card):
                removed += 1
            else:
                failed += 1
        self.stdout.write(f"Removed {removed} vault token(s); {failed} still pending.")
