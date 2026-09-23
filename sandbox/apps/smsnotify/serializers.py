"""Plain JSON serialization helpers (no DRF in the sandbox)."""


def _mask(number):
    """Mask a phone number for responses that are not the owner's own list."""
    if not number:
        return number
    return "***" + number[-4:]


def serialize_contact_number(cn):
    return {
        "contactNumberId": cn.pk,
        "phoneNumber": cn.phone_number,  # owner's own number -- shown in full to them
        "created": cn.created.isoformat(),
    }


def serialize_notification(n):
    return {
        "notificationId": n.pk,
        "category": n.category,
        "status": n.status,
        "twilioStatus": n.twilio_status,
        "providerSid": n.provider_sid,
        "errorCode": n.error_code,
        "errorMessage": n.error_message,
        "recipient": _mask(n.recipient),
        "isScheduled": n.is_scheduled,
        "sendAt": n.send_at.isoformat() if n.send_at else None,
        "canceled": n.canceled,
        "contentRedacted": n.content_redacted,
        "created": n.created.isoformat(),
    }


def serialize_order(order, notifications=None):
    data = {
        "orderId": order.pk,
        "number": order.number,
        "status": order.status,
        "currency": order.currency,
        "total": str(order.total_incl_tax),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
            }
            for line in order.lines.all()
        ],
    }
    if notifications is not None:
        data["notifications"] = [serialize_notification(n) for n in notifications]
    return data
