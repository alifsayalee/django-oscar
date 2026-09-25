"""JSON shapes for the API. Nothing here ever carries card numbers or security codes."""

from .models import PaymentOperation, PayPalPayment


def _money(value):
    return None if value is None else str(value)


def _time(value):
    return value.isoformat() if value else None


def payment_status(payment):
    if payment.lifecycle == PayPalPayment.CAPTURED and payment.refunded_amount > 0:
        if payment.captured_amount is not None and payment.refunded_amount >= payment.captured_amount:
            return "refunded"
        return "partially_refunded"
    return payment.lifecycle


def refund_json(op):
    return {
        "refundId": str(op.public_id),
        "paypalRefundId": op.provider_id or None,
        "amount": _money(op.amount),
        "currency": op.currency or None,
        "status": op.outcome,
        "paypalStatus": op.provider_status or None,
        "createdAt": _time(op.claimed_at),
    }


def attempt_json(op):
    if op is None:
        return None
    detail = op.detail or {}
    return {
        "outcome": op.outcome,
        "paypalStatus": op.provider_status or None,
        "payerActionRequired": bool(detail.get("payer_action_required")),
        "issues": detail.get("issues") or [],
        "attemptedAt": _time(op.claimed_at),
    }


def payment_json(payment):
    ops = list(payment.operations.all())
    refunds = [op for op in ops if op.kind == PaymentOperation.REFUND]
    attempts = [op for op in ops if op.kind in (PaymentOperation.CREATE_ORDER, PaymentOperation.AUTHORIZE)]
    return {
        "status": payment_status(payment),
        "amount": _money(payment.amount),
        "currency": payment.currency,
        "card": (
            {
                "brand": payment.card_brand or None,
                "lastDigits": payment.card_last_digits or None,
                "paymentMethodId": payment.bankcard_id,
            }
            if payment.card_last_digits
            else None
        ),
        "authorization": (
            {
                "id": payment.authorization_id,
                "status": payment.authorization_status or None,
                "authorizedAt": _time(payment.authorized_at),
                "expiresAt": _time(payment.authorization_expires_at),
                "reauthorized": payment.reauthorized,
            }
            if payment.authorization_id
            else None
        ),
        "capture": (
            {
                "id": payment.capture_id,
                "status": payment.capture_status or None,
                "amount": _money(payment.captured_amount),
                "paypalFee": _money(payment.paypal_fee),
                "netAmount": _money(payment.net_amount),
                "capturedAt": _time(payment.captured_at),
            }
            if payment.capture_id
            else None
        ),
        "refundedAmount": _money(payment.refunded_amount),
        "refundableAmount": _money(payment.refundable_amount),
        "refunds": [refund_json(op) for op in refunds],
        "lastAttempt": attempt_json(attempts[-1] if attempts else None),
        "paypalOrderId": payment.paypal_order_id or None,
    }


def order_json(order):
    payment = order.paypal_payment
    return {
        "orderId": order.number,
        "status": order.status,
        "placedAt": _time(order.date_placed),
        "total": _money(order.total_incl_tax),
        "currency": order.currency,
        "lines": [
            {
                "itemId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _money(line.unit_price_incl_tax),
                "lineTotal": _money(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": payment_json(payment),
    }


def card_json(card):
    return {
        "paymentMethodId": card.pk,
        "brand": card.card_type,
        "lastDigits": card.number[-4:],
        "expiry": card.expiry_date.strftime("%Y-%m"),
        "name": card.name or None,
        "label": f"{card.card_type} ending {card.number[-4:]}, expires {card.expiry_date.strftime('%m/%y')}",
    }
