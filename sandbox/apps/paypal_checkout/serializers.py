"""Serialize domain objects into caller-safe JSON (never full card details)."""


def _money(value):
    return "%0.2f" % value if value is not None else None


def serialize_payment(payment):
    order = payment.order
    return {
        "orderId": order.id,
        "orderNumber": order.number,
        "orderStatus": order.status,
        "currency": payment.currency,
        "amount": _money(payment.amount),
        "paymentState": payment.state,
        "paypalOrderId": payment.paypal_order_id or None,
        "authorizationId": payment.authorization_id or None,
        "authorizationStatus": payment.authorization_status or None,
        "authorizationExpiry": (
            payment.authorization_expiry.isoformat()
            if payment.authorization_expiry
            else None
        ),
        "captureId": payment.capture_id or None,
        "captureStatus": payment.capture_status or None,
        "capturedAmount": _money(payment.captured_amount),
        "paypalFee": _money(payment.paypal_fee),
        "netAmount": _money(payment.net_amount),
        "refundableAmount": _money(payment.refundable_amount),
        "refunds": [serialize_refund(r) for r in payment.refunds.all()],
    }


def serialize_refund(refund):
    return {
        "refundId": refund.refund_id,
        "amount": _money(refund.amount),
        "currency": refund.currency,
        "status": refund.status,
        "idempotencyKey": refund.idempotency_key,
        "dateCreated": refund.date_created.isoformat(),
    }


def serialize_order_line(line):
    return {
        "productId": line.product_id,
        "title": line.title,
        "quantity": line.quantity,
        "lineTotal": _money(line.line_price_incl_tax),
    }


def serialize_order_summary(payment):
    order = payment.order
    data = serialize_payment(payment)
    data["lines"] = [serialize_order_line(line) for line in order.lines.all()]
    data["datePlaced"] = order.date_placed.isoformat() if order.date_placed else None
    return data


def serialize_bankcard(bankcard):
    return {
        "paymentMethodId": bankcard.id,
        "brand": bankcard.card_type,
        "maskedNumber": bankcard.number,
        "expiry": bankcard.expiry_month("%Y-%m"),
        "name": bankcard.name,
    }
