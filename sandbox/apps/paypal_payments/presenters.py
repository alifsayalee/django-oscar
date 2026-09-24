"""JSON shapes returned by the API. Nothing here ever includes card numbers."""
from decimal import Decimal
from typing import Any

from .models import PaymentOperation, PayPalPayment, SavedCard


def _amount(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _time(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def saved_card(card: SavedCard) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.public_id),
        "type": "card",
        "brand": card.brand,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "createdAt": _time(card.created_at),
    }


def refund(op: PaymentOperation) -> dict[str, Any]:
    return {
        "refundId": str(op.public_id),
        "status": op.outcome,
        "amount": _amount(op.amount),
        "currency": op.currency,
        "paypalRefundId": op.provider_id or None,
        "paypalStatus": op.provider_status or None,
        "detail": op.detail or None,
        "createdAt": _time(op.claimed_at),
    }


def payment(p: PayPalPayment) -> dict[str, Any]:
    refunds = p.operations.filter(kind=PaymentOperation.REFUND).order_by("claimed_at", "pk")
    return {
        "state": p.state,
        "detail": p.state_detail or None,
        "amount": _amount(p.amount),
        "currency": p.currency,
        "card": (
            {"brand": p.card_brand, "lastDigits": p.card_last_digits,
             "paymentMethodId": str(p.saved_card.public_id) if p.saved_card else None}
            if p.card_last_digits else None
        ),
        "paypalOrderId": p.paypal_order_id or None,
        "authorization": (
            {
                "id": p.authorization_id,
                "status": p.authorization_status,
                "createdAt": _time(p.authorization_created_at),
                "expiresAt": _time(p.authorization_expires_at),
                "reauthorizedFrom": p.reauthorized_from or None,
            }
            if p.authorization_id else None
        ),
        "capture": (
            {
                "id": p.capture_id,
                "status": p.capture_status,
                "amount": _amount(p.captured_amount),
                "paypalFee": _amount(p.paypal_fee),
                "netAmount": _amount(p.net_amount),
                "capturedAt": _time(p.captured_at),
            }
            if p.capture_id else None
        ),
        "refundedAmount": _amount(p.refunded_amount),
        "refunds": [refund(op) for op in refunds],
    }


def order(p: PayPalPayment) -> dict[str, Any]:
    o = p.order
    return {
        "orderId": str(o.number),
        "status": o.status,
        "placedAt": _time(o.date_placed),
        "total": _amount(p.amount),
        "currency": p.currency,
        "lines": [
            {"productId": line.product_id, "title": line.title, "quantity": line.quantity,
             "unitPrice": _amount(line.unit_price_incl_tax), "linePrice": _amount(line.line_price_incl_tax)}
            for line in o.lines.all()
        ],
        "payment": payment(p),
    }
