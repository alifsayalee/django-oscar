"""JSON shapes returned by the payments API."""

from decimal import Decimal
from typing import Any

from . import money
from .models import PaypalPayment, ProviderWrite, SavedCard
from .services import refund_writes, refundable_amount, refunded_amount


def _amount(value: Decimal | None, currency: str) -> str | None:
    return money.format_amount(value, currency) if value is not None else None


def _time(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def saved_card(card: SavedCard) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.pk),
        "type": "card",
        "brand": card.brand or None,
        "lastDigits": card.last_digits or None,
        "expiry": card.expiry or None,
        "label": str(card),
        "createdAt": _time(card.created),
    }


def refund(record: ProviderWrite) -> dict[str, Any]:
    return {
        "refundId": str(record.public_id),
        "paypalRefundId": record.provider_id or None,
        "amount": _amount(record.amount, record.currency),
        "currency": record.currency,
        "status": record.provider_status or None,
        "outcome": record.outcome,
        "createdAt": _time(record.provider_time or record.claimed_at),
    }


def payment(p: PaypalPayment) -> dict[str, Any]:
    cur = p.currency
    return {
        "state": p.state,
        "amount": _amount(p.amount, cur),
        "currency": cur,
        "card": p.card_label or None,
        "paymentMethodId": str(p.saved_card_id) if p.saved_card_id else None,
        "paypalOrderId": p.paypal_order_id or None,
        "authorization": {
            "id": p.authorization_id or None,
            "status": p.authorization_status or None,
            "createdAt": _time(p.authorization_created_at),
            "expiresAt": _time(p.authorization_expires_at),
            "originalId": p.original_authorization_id or None,
            "reauthorizedAt": _time(p.reauthorized_at),
        },
        "capture": {
            "id": p.capture_id or None,
            "status": p.capture_status or None,
            "amount": _amount(p.captured_amount, cur),
            "paypalFee": _amount(p.paypal_fee, cur),
            "netAmount": _amount(p.net_amount, cur),
            "capturedAt": _time(p.captured_at),
        },
        "refunds": [refund(r) for r in refund_writes(p.order)],
        "refundedAmount": _amount(refunded_amount(p.order), cur),
        "refundableAmount": _amount(refundable_amount(p), cur),
        "detail": p.detail or None,
    }


def order(o: Any) -> dict[str, Any]:
    p: PaypalPayment = o.paypal_payment
    return {
        "orderId": o.number,
        "status": o.status,
        "total": _amount(o.total_incl_tax, p.currency),
        "currency": p.currency,
        "placedAt": _time(o.date_placed),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _amount(line.unit_price_incl_tax, p.currency),
                "lineTotal": _amount(line.line_price_incl_tax, p.currency),
            }
            for line in o.lines.all()
        ],
        "payment": payment(p),
    }
