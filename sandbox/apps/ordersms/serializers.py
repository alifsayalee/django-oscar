"""Plain-dict serializers for the JSON API responses."""

from __future__ import annotations

from .models import ContactNumber, OrderNotification


def contact_number(obj: ContactNumber) -> dict:
    return {
        "contactNumberId": obj.pk,
        "number": obj.e164,
        "created": obj.created.isoformat(),
    }


def notification(obj: OrderNotification) -> dict:
    return {
        "notificationId": obj.pk,
        "orderId": obj.order_id,
        "kind": obj.kind,
        "toNumber": obj.to_number or None,
        "providerSid": obj.provider_sid or None,
        # Where the message got to, as the provider reports it.
        "providerStatus": obj.provider_status or None,
        "outcome": obj.local_outcome,
        "errorCode": obj.provider_error_code,
        "errorMessage": obj.provider_error_message or None,
        "isFollowup": obj.is_followup,
        "scheduledSendAt": obj.scheduled_send_at.isoformat() if obj.scheduled_send_at else None,
        "followupCancelled": obj.followup_cancelled,
        "contentDisposed": obj.content_disposed,
        "detail": obj.detail or None,
        "created": obj.created.isoformat(),
        "updated": obj.updated.isoformat(),
    }


def order_summary(order, notifications) -> dict:
    return {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "currency": order.currency,
        "total": str(order.total_incl_tax),
        "lines": [
            {"product": line.title, "quantity": line.quantity}
            for line in order.lines.all()
        ],
        "notifications": [notification(n) for n in notifications],
    }
