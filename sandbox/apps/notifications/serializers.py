"""Plain dict builders for JSON responses (no DRF in the sandbox).

Anything crossing this boundary is a JSON-safe primitive -- no SDK model, no UNSET sentinel.
"""

from __future__ import annotations


def _iso(dt):
    return dt.isoformat() if dt else None


def serialize_contact_number(cn) -> dict:
    return {
        "contactNumberId": cn.id,
        "number": cn.canonical_number,
        "createdAt": _iso(cn.created_at),
    }


def serialize_notification(row) -> dict:
    return {
        "notificationId": row.id,
        "orderId": row.order.number,
        "kind": row.kind,
        "outcome": row.outcome,
        "providerStatus": row.provider_status or None,
        "providerSid": row.provider_sid or None,
        "errorCode": row.error_code,
        "isFollowup": row.is_followup,
        "canceled": row.canceled,
        "contentRedacted": row.content_redacted,
        "scheduledSendAt": _iso(row.scheduled_send_at),
        "providerTime": _iso(row.provider_time),
        "createdAt": _iso(row.created_at),
    }


def serialize_order(order, notifications=None) -> dict:
    data = {
        "orderId": order.number,
        "status": order.status,
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "numLines": order.num_lines,
        "createdAt": _iso(order.date_placed),
    }
    if notifications is not None:
        data["notifications"] = [serialize_notification(n) for n in notifications]
    return data
