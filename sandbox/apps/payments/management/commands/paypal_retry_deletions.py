from django.core.management.base import BaseCommand

from apps.payments import services
from apps.payments.models import PaymentOperation


class Command(BaseCommand):
    help = "Retry deleting saved cards from PayPal's vault that were removed locally but not confirmed deleted."

    def handle(self, *args, **options):
        pending = PaymentOperation.objects.filter(kind=PaymentOperation.VAULT_DELETE).exclude(
            outcome=PaymentOperation.DONE
        )
        for op in pending:
            op = services.delete_from_vault(op)
            self.stdout.write(f"{op.ref}: {op.outcome}")
        self.stdout.write(f"{pending.count()} deletion(s) still outstanding")
