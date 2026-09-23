"""Plain-dict representations of orders, payments, refunds and saved cards."""


def _money(value):
    return None if value is None else str(value)


def _dt(value):
    return value.isoformat() if value else None


def payment_method_to_dict(bankcard):
    return {
        'paymentMethodId': bankcard.pk,
        'brand': bankcard.card_type,
        'lastDigits': bankcard.number[-4:],
        'expiry': bankcard.expiry_date.strftime('%Y-%m'),
        'name': bankcard.name,
        'label': '%s ending %s' % (bankcard.card_type, bankcard.number[-4:]),
    }


def refund_to_dict(refund):
    return {
        'refundId': refund.pk,
        'paypalRefundId': refund.paypal_refund_id or None,
        'status': refund.status,
        'amount': _money(refund.amount),
        'currency': refund.currency,
        'idempotencyKey': refund.idempotency_key,
        'note': refund.note,
        'errorCode': refund.error_code or None,
        'errorMessage': refund.error_message or None,
        'createdAt': _dt(refund.date_created),
    }


def payment_to_dict(payment):
    if payment is None:
        return None
    return {
        'state': payment.state,
        'amount': _money(payment.amount),
        'currency': payment.currency,
        'card': {
            'brand': payment.card_brand,
            'lastDigits': payment.card_last_digits,
            'paymentMethodId': payment.payment_method_id,
        } if payment.card_last_digits else None,
        'paypalOrderId': payment.paypal_order_id or None,
        'authorization': {
            'id': payment.authorization_id,
            'originalId': payment.original_authorization_id,
            'status': payment.authorization_status,
            'authorizedAt': _dt(payment.authorized_at),
            'expiresAt': _dt(payment.authorization_expires_at),
            'reauthorizedAt': _dt(payment.reauthorized_at),
        } if payment.authorization_id else None,
        'capture': {
            'id': payment.capture_id,
            'status': payment.capture_status,
            'capturedAt': _dt(payment.captured_at),
            'amount': _money(payment.captured_amount),
            'paypalFee': _money(payment.paypal_fee),
            'netAmount': _money(payment.net_amount),
        } if payment.capture_id else None,
        'refundedAmount': _money(payment.refunded_total()),
        'refundableAmount': _money(payment.refundable_amount()),
        'refunds': [refund_to_dict(r) for r in payment.refunds.all()],
        'voidedAt': _dt(payment.voided_at),
        'lastError': {
            'code': payment.last_error_code,
            'message': payment.last_error_message,
            'paypalDebugId': payment.last_error_debug_id or None,
        } if payment.last_error_code else None,
    }


def order_to_dict(order):
    payment = getattr(order, 'paypal_payment', None)
    return {
        'orderId': order.pk,
        'number': order.number,
        'status': order.status,
        'placedAt': _dt(order.date_placed),
        'total': _money(payment.amount if payment else order.total_incl_tax),
        'currency': payment.currency if payment else order.currency,
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'unitPrice': _money(line.unit_price_incl_tax),
            'total': _money(line.line_price_incl_tax),
        } for line in order.lines.all()],
        'payment': payment_to_dict(payment),
    }
