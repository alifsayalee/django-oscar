"""
Settle PayPal writes whose outcome is not known yet.

Each one is checked through the same safe write that made it: a same-reference
resend where PayPal replays the original, a lookup otherwise. Nothing is ever
re-sent under a new reference, and nothing is marked failed for being old.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.payments_api import cards, payments
from apps.payments_api.models import ProviderWrite
from apps.payments_api.safe_write import SEND_WINDOW


class Command(BaseCommand):
    help = 'Re-check PayPal writes with an unknown outcome and retry unfinished vault deletions.'

    def handle(self, *args, **options):
        stale = timezone.now() - SEND_WINDOW
        writes = ProviderWrite.objects.filter(
            Q(outcome=ProviderWrite.UNKNOWN)
            | Q(outcome=ProviderWrite.SENDING, claimed_at__lt=stale)
            | Q(kind=ProviderWrite.VAULT_DELETE, outcome=ProviderWrite.FAILED)
        ).select_related('order').order_by('pk')
        for write in writes:
            before = write.outcome
            if write.kind == ProviderWrite.VAULT_DELETE:
                outcome = cards.delete_vault_token(write.ref, write.target_id, claimed=False)
            elif write.kind == ProviderWrite.VOID and write.order_id:
                try:
                    outcome, __ = payments.cancel(write.order)
                except Exception as e:  # reported, not fatal for the rest of the batch
                    outcome = 'error: %s' % type(e).__name__
            elif write.kind == ProviderWrite.VAULT_CREATE:
                # The card details are never stored, so only the shopper repeating the
                # request with the same Idempotency-Key can re-check this one.
                outcome = 'needs shopper repeat'
            else:
                outcome = payments.run_pending_check(write).outcome
            self.stdout.write('%s %s: %s -> %s' % (write.kind, write.ref, before, outcome))
        self.stdout.write('Checked %d write(s).' % len(writes))
