"""JSON shapes returned by the API. Card data is limited to brand, last 4 and expiry."""

from datetime import timezone

from . import money


def _amount(value, currency=None):
    if value is None:
        return None
    return money.to_wire(value, currency) if currency else str(value)


def _time(value):
    return value.astimezone(timezone.utc).isoformat() if value else None


def saved_card(card):
    return {
        "paymentMethodId": str(card.id),
        "brand": card.brand or None,
        "lastDigits": card.last_digits or None,
        "expiry": card.expiry or None,
        "holderName": card.holder_name or None,
        "createdAt": _time(card.date_created),
    }


def refund(record):
    return {
        "refundId": str(record.id),
        "idempotencyKey": record.idempotency_key,
        "amount": _amount(record.amount, record.currency),
        "currency": record.currency,
        "state": record.state,
        "paypalRefundId": record.paypal_refund_id or None,
        "paypalStatus": record.paypal_status or None,
        "refundedAt": _time(record.refunded_at),
        "error": record.last_error or None,
    }


def payment(p):
    captured = p.captured_amount
    remaining = None
    if captured is not None and p.state == "captured":
        remaining = captured - p.refund_reserved
    if p.refunded_amount and captured is not None and p.refunded_amount >= captured:
        refund_state = "refunded"
    elif p.refunded_amount:
        refund_state = "partially_refunded"
    else:
        refund_state = "none"
    return {
        "state": p.state,
        "amount": _amount(p.amount, p.currency),
        "currency": p.currency,
        "card": {
            "brand": p.card_brand or None,
            "lastDigits": p.card_last_digits or None,
            "paymentMethodId": str(p.saved_card_id) if p.saved_card_id else None,
        },
        "paypalOrderId": p.paypal_order_id or None,
        "authorization": {
            "id": p.authorization_id or None,
            "status": p.authorization_status or None,
            "amount": _amount(p.authorized_amount, p.currency),
            "authorizedAt": _time(p.authorized_at),
            "expiresAt": _time(p.authorization_expires_at),
            "reauthorizations": p.reauthorization_count,
            "previousIds": list(p.previous_authorization_ids or []),
        },
        "capture": {
            "id": p.capture_id or None,
            "status": p.capture_status or None,
            "amount": _amount(captured, p.currency),
            "paypalFee": _amount(p.paypal_fee, p.currency),
            "netAmount": _amount(p.net_amount, p.currency),
            "capturedAt": _time(p.captured_at),
        },
        "refundState": refund_state,
        "refundedAmount": _amount(p.refunded_amount, p.currency),
        "refundableAmount": _amount(remaining, p.currency),
        "refunds": [refund(r) for r in p.refunds.all()],
        "voidedAt": _time(p.voided_at),
        "lastError": p.last_error or None,
    }


def order(o):
    body = {
        "orderId": o.number,
        "status": o.status,
        "placedAt": _time(o.date_placed),
        "currency": o.currency,
        "total": _amount(o.total_incl_tax),
        "shippingTotal": _amount(o.shipping_incl_tax),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _amount(line.unit_price_incl_tax),
                "linePrice": _amount(line.line_price_incl_tax),
                "status": line.status,
            }
            for line in o.lines.all()
        ],
    }
    p = getattr(o, "paypal_payment", None)
    body["payment"] = payment(p) if p is not None else None
    return body
