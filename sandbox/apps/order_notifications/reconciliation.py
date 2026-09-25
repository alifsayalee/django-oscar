"""
Line the provider's record of messages up against what this app believes it sent.

Both sides are filtered on the same clock — the provider's own time for the
message (when it was sent, else when it was created) — and the provider is
asked only for messages from this app's sending number.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q

from . import provider
from .models import Notification

# The provider's date filter is widened by this much each way, then narrowed here.
WIDEN = timedelta(days=1)


def mask(number: str | None) -> str | None:
    if not number:
        return number
    return f'***{number[-4:]}'


def _provider_entry(state: provider.MessageState) -> dict:
    return {
        'providerSid': state.sid,
        'status': state.status,
        'outcome': state.outcome,
        'to': mask(state.to),
        'providerTime': state.provider_time.isoformat() if state.provider_time else None,
        'errorCode': state.error_code,
    }


def _local_entry(notification: Notification) -> dict:
    return {
        'notificationId': notification.pk,
        'orderId': notification.order_id,
        'kind': notification.kind,
        'providerSid': notification.provider_sid,
        'status': notification.provider_status,
        'outcome': notification.outcome,
        'providerTime': notification.provider_time.isoformat() if notification.provider_time else None,
        'claimedAt': notification.claimed_at.isoformat(),
    }


def _in_window(moment: datetime | None, start: datetime, end: datetime) -> bool:
    return moment is not None and start <= moment < end


def reconcile(start: datetime, end: datetime) -> dict:
    fetched = provider.list_messages(sent_after=start - WIDEN, sent_before=end + WIDEN)
    fetched_by_sid = {state.sid: state for state in fetched}
    provider_in_window = [s for s in fetched if _in_window(s.provider_time, start, end)]

    # Our side, on the provider's clock. Rows the provider just told us about are
    # brought up to date first, so both sides judge the window by the same timestamps.
    candidates = Notification.objects.filter(
        Q(provider_sid__in=list(fetched_by_sid))
        | Q(provider_time__gte=start - WIDEN, provider_time__lt=end + WIDEN)
    ).select_related('order')
    local_by_sid: dict[str, Notification] = {}
    for notification in candidates:
        state = fetched_by_sid.get(notification.provider_sid or '')
        if state is not None and (notification.provider_status != state.status
                                  or notification.provider_time != state.provider_time):
            notification.provider_status = state.status
            notification.outcome = state.outcome
            notification.provider_time = state.provider_time
            notification.save(update_fields=['provider_status', 'outcome', 'provider_time',
                                             'updated_at'])
        if notification.provider_sid:
            local_by_sid[notification.provider_sid] = notification

    matched: list[dict] = []
    provider_only: list[dict] = []
    for state in provider_in_window:
        local = local_by_sid.get(state.sid)
        if local is None:
            provider_only.append(_provider_entry(state))
        else:
            entry = _local_entry(local)
            entry['provider'] = _provider_entry(state)
            entry['agrees'] = local.provider_status == state.status
            matched.append(entry)

    provider_sids = {state.sid for state in provider_in_window}
    local_only = [
        _local_entry(n) for n in local_by_sid.values()
        if _in_window(n.provider_time, start, end) and n.provider_sid not in provider_sids
    ]
    # Claimed in the window but never tied to a provider message: not a discrepancy
    # either way until the provider settles it, so reported on their own.
    unsettled = [
        _local_entry(n) for n in Notification.objects.filter(
            provider_sid__isnull=True, claimed_at__gte=start, claimed_at__lt=end,
        ).exclude(outcome=Notification.OUTCOME_FAILED)
    ]
    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'sendingNumber': mask(settings.TWILIO_FROM_NUMBER),
        'counts': {
            'provider': len(provider_in_window),
            'matched': len(matched),
            'providerOnly': len(provider_only),
            'localOnly': len(local_only),
            'unsettled': len(unsettled),
        },
        'matched': matched,
        'providerOnly': provider_only,
        'localOnly': local_only,
        'unsettled': unsettled,
    }
