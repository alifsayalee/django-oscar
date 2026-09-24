"""
Line up the provider's record of messages sent from our number against what this app sent.

Both sides are compared on the provider's clock (the time the provider says it sent the message),
the provider is asked only for messages from this application's own sending number, and every page
of the provider's answer is read.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db.models import Q

from . import gateway
from .gateway import MessageState
from .models import Notification


def _mask(number: str | None) -> str | None:
    if not number:
        return number
    return f'{number[:3]}{"*" * max(len(number) - 5, 0)}{number[-2:]}'


def _provider_row(m: MessageState) -> dict[str, Any]:
    return {
        'messageSid': m.sid, 'status': m.status, 'direction': m.direction, 'to': _mask(m.to),
        'dateSent': m.date_sent.isoformat() if m.date_sent else None,
        'errorCode': m.error_code,
    }


def _local_row(n: Notification) -> dict[str, Any]:
    return {
        'notificationId': n.pk, 'orderId': n.order_id, 'kind': n.kind, 'outcome': n.outcome,
        'providerStatus': n.provider_status or None, 'messageSid': n.message_sid,
        'claimedAt': n.claimed_at.isoformat(),
        'providerDateSent': n.provider_date_sent.isoformat() if n.provider_date_sent else None,
    }


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    sender = gateway.from_number()
    provider: list[MessageState] = []
    excluded_inbound = 0
    for m in gateway.list_sent_from(sender, start, end):
        # The query was widened for the date-granular filter; narrow back to the exact window.
        if m.date_sent is None or not start <= m.date_sent < end:
            continue
        if m.direction == 'inbound':
            # The receiving leg of our own text to a number on this same account - not a send.
            excluded_inbound += 1
            continue
        provider.append(m)

    by_sid = {m.sid: m for m in provider if m.sid}
    known = {n.message_sid: n for n in Notification.objects.filter(message_sid__in=list(by_sid))}

    matched = []
    for sid, m in by_sid.items():
        n = known.get(sid)
        if n is None:
            continue
        status_before = n.provider_status
        # The provider is authoritative for what became of the message: keep our record current.
        n.outcome = gateway.status_from_provider(m.status)
        n.provider_status = m.status
        n.provider_date_sent = m.date_sent
        n.save(update_fields=['outcome', 'provider_status', 'provider_date_sent', 'updated_at'])
        row = _local_row(n)
        row['provider'] = _provider_row(m)
        row['statusChanged'] = status_before != m.status
        matched.append(row)

    provider_only = [_provider_row(m) for m in provider if not m.sid or m.sid not in known]

    in_window = Notification.objects.filter(
        provider_date_sent__gte=start, provider_date_sent__lt=end)
    app_only = [_local_row(n) for n in in_window.exclude(message_sid__in=list(known))]

    unsettled_qs = Notification.objects.filter(
        provider_date_sent__isnull=True, claimed_at__gte=start, claimed_at__lt=end,
    ).exclude(Q(message_sid__in=list(known)))
    unsettled = [_local_row(n) for n in unsettled_qs]

    return {
        'from': start.isoformat(), 'to': end.isoformat(), 'sender': _mask(sender),
        'counts': {
            'provider': len(provider), 'matched': len(matched),
            'providerOnly': len(provider_only), 'appOnly': len(app_only),
            'unsettled': len(unsettled), 'excludedInbound': excluded_inbound,
        },
        'matched': matched,
        # The provider sent it from our number; this app has no record of it.
        'providerOnly': provider_only,
        # This app recorded it as sent in the window; the provider has no such message.
        'appOnly': app_only,
        # Claimed in the window with no provider send time (scheduled, called off, never sent,
        # or outcome unknown) - reported separately, not as a discrepancy.
        'unsettled': unsettled,
    }
