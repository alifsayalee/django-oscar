"""
Line up the provider's own record of messages sent from TWILIO_FROM_NUMBER over a
window against what this app believes it sent.

Both sides are compared on the provider's clock (its ``date_sent``), which the app
stores on every notification it hears back about. The provider is asked only for
this app's sending number; its whole-day filter is widened to cover the window and
the answer is narrowed back to the exact window here.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings

from . import gateway
from .models import Notification, mask_number
from .safe_write import apply_provider_state, verify_and_complete

Outcome = Notification.Outcome


@dataclass
class Report:
    start: datetime
    end: datetime
    provider_count: int = 0
    matched: list[dict[str, Any]] = field(default_factory=list)
    provider_only: list[dict[str, Any]] = field(default_factory=list)
    local_only: list[dict[str, Any]] = field(default_factory=list)
    unsettled: list[dict[str, Any]] = field(default_factory=list)
    recovered: list[dict[str, Any]] = field(default_factory=list)
    excluded_inbound: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "sendingNumber": mask_number(str(settings.TWILIO_FROM_NUMBER)),
            "summary": {
                "providerMessages": self.provider_count,
                "matched": len(self.matched),
                "providerOnly": len(self.provider_only),
                "localOnly": len(self.local_only),
                "unsettled": len(self.unsettled),
                "recoveredFromUnknown": len(self.recovered),
                "excludedInboundCopies": self.excluded_inbound,
            },
            "matched": self.matched,
            "providerOnly": self.provider_only,
            "localOnly": self.local_only,
            "unsettled": self.unsettled,
            "recovered": self.recovered,
        }


def _provider_entry(msg: gateway.ProviderMessage) -> dict[str, Any]:
    return {
        "providerSid": msg.sid,
        "providerStatus": msg.status,
        "to": mask_number(msg.to or ""),
        "dateSent": msg.date_sent.isoformat() if msg.date_sent else None,
        "errorCode": msg.error_code,
    }


def _local_entry(n: Notification) -> dict[str, Any]:
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "outcome": n.outcome,
        "providerSid": n.provider_sid,
        "providerStatus": n.provider_status or None,
        "dateSent": n.provider_date_sent.isoformat() if n.provider_date_sent else None,
        "createdAt": n.created_at.isoformat(),
    }


def reconcile(start: datetime, end: datetime) -> Report:
    """Raises gateway.ProviderError if the provider's record cannot be read in full."""
    report = Report(start=start, end=end)

    # Provider side: this app's sending number only, whole days covering [start, end), every page.
    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    provider: list[gateway.ProviderMessage] = []
    try:
        for msg in gateway.iter_messages(
            from_=settings.TWILIO_FROM_NUMBER, sent_on_or_after=day_start, sent_on_or_before=day_end
        ):
            if msg.direction == "inbound":
                report.excluded_inbound += 1  # another account number's copy of a message from ours
                continue
            if msg.date_sent is not None and start <= msg.date_sent < end:
                provider.append(msg)
    except gateway.SDK_FAILURES as e:
        raise gateway.translate(e) from e
    report.provider_count = len(provider)

    # Match against every local record by the provider's id (not only in-window ones: our copy
    # may not have heard of the send time yet), refreshing our copy from the provider's record.
    by_sid = {n.provider_sid: n for n in Notification.objects.filter(provider_sid__in=[m.sid for m in provider if m.sid])}
    unresolved = {
        n.ref_token: n for n in Notification.objects.filter(provider_sid__isnull=True, outcome__in=[Outcome.UNKNOWN, Outcome.SENDING])
    }
    matched_ids: set[int] = set()
    for msg in provider:
        n = by_sid.get(msg.sid)
        if n is None:
            # Perhaps a send of ours whose answer was lost: its body carries our reference token.
            n = next((u for token, u in unresolved.items() if msg.body and f"(ref {token})" in msg.body), None)
            if n is not None:
                unresolved.pop(n.ref_token)
                n = verify_and_complete(n, msg)
                report.recovered.append(_local_entry(n))
        elif n.provider_date_sent != msg.date_sent or n.provider_status != (msg.status or ""):
            apply_provider_state(n, msg)
        if n is None:
            report.provider_only.append(_provider_entry(msg))
            continue
        matched_ids.add(n.pk)
        report.matched.append({**_local_entry(n), "provider": _provider_entry(msg)})

    # Local side on the same clock: what we believe was sent in the window, but the provider did not list.
    for n in Notification.objects.filter(provider_date_sent__gte=start, provider_date_sent__lt=end).exclude(
        pk__in=matched_ids
    ):
        report.local_only.append(_local_entry(n))

    # Created in the window with no provider send time: queued, cancelled, never sent or unconfirmed.
    for n in Notification.objects.filter(
        created_at__gte=start, created_at__lt=end, provider_date_sent__isnull=True
    ).exclude(pk__in=matched_ids):
        report.unsettled.append(_local_entry(n))
    return report
