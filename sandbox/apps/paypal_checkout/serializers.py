"""Plain-dict shaping for JSON responses.

Everything the API returns is built here so that (a) no SDK ``UNSET`` sentinel or
raw model ever crosses the boundary, and (b) card data is only ever described
safely (brand + last digits + expiry), never in full.
"""
from decimal import Decimal


def _dec(value):
    return None if value is None else str(value)


def saved_card_dict(card):
    return {
        "paymentMethodId": card.id,
        "brand": card.brand,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "description": _card_description(card),
        "created": card.created.isoformat(),
    }


def _card_description(card):
    brand = card.brand or "Card"
    tail = card.last_digits or "????"
    return f"{brand} ending {tail}" + (f" (exp {card.expiry})" if card.expiry else "")


def refund_dict(refund):
    return {
        "refundId": refund.refund_id or None,
        "localRefundId": refund.id,
        "amount": _dec(refund.amount),
        "currency": refund.currency,
        "status": refund.status,
        "idempotencyKey": refund.idempotency_key,
        "created": refund.created.isoformat(),
    }


def payment_dict(payment):
    return {
        "state": payment.status,
        "currency": payment.currency,
        "amount": _dec(payment.amount),
        "paypalOrderId": payment.paypal_order_id or None,
        "authorizationId": payment.authorization_id or None,
        "authorizationStatus": payment.authorization_status or None,
        "authorizationExpiry": (
            payment.authorization_expiry.isoformat() if payment.authorization_expiry else None
        ),
        "captureId": payment.capture_id or None,
        "captureStatus": payment.capture_status or None,
        "capturedAmount": _dec(payment.captured_value),
        "paypalFee": _dec(payment.paypal_fee),
        "netAmount": _dec(payment.net_amount),
        "refundedTotal": _dec(payment.refunded_total) if payment.captured_value is not None else None,
        "refundableRemaining": (
            _dec(payment.refundable_remaining) if payment.captured_value is not None else None
        ),
        "refunds": [refund_dict(r) for r in payment.refunds.all()],
    }


def order_line_dict(line):
    return {
        "productId": line.product_id,
        "title": line.title,
        "quantity": line.quantity,
        "unitPrice": _dec(line.unit_price_incl_tax),
        "lineTotal": _dec(line.line_price_incl_tax),
    }


def order_dict(order, payment=None):
    if payment is None:
        payment = getattr(order, "paypal_payment", None)
    data = {
        "orderId": str(order.number),
        "orderStatus": order.status,
        "currency": order.currency,
        "total": _dec(order.total_incl_tax),
        "datePlaced": order.date_placed.isoformat() if order.date_placed else None,
        "lines": [order_line_dict(line) for line in order.lines.all()],
        "payment": payment_dict(payment) if payment is not None else None,
    }
    return data
