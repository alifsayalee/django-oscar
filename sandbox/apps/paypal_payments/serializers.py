"""JSON shapes returned by the API."""


def _money(value):
    return str(value) if value is not None else None


def _time(value):
    return value.isoformat() if value else None


def refund_dict(refund):
    return {
        'refundId': str(refund.public_id),
        'amount': _money(refund.amount),
        'status': refund.state,
        'paypalRefundId': refund.paypal_refund_id or None,
        'paypalStatus': refund.paypal_status or None,
        'note': refund.note or None,
        'createdAt': _time(refund.created),
        'error': _error(refund),
    }


def _error(obj):
    if not obj.last_error_code and not obj.outcome_unknown:
        return None
    return {'code': obj.last_error_code or None, 'message': obj.last_error_message or None,
            'outcomeUnknown': obj.outcome_unknown}


def payment_dict(payment):
    return {
        'status': payment.payment_status,
        'amount': _money(payment.amount),
        'currency': payment.currency,
        'card': ({'brand': payment.card_brand or None, 'lastDigits': payment.card_last_digits,
                  'paymentMethodId': str(payment.saved_card.public_id) if payment.saved_card else None}
                 if payment.card_last_digits else None),
        'authorization': ({
            'paypalOrderId': payment.paypal_order_id,
            'authorizationId': payment.authorization_id,
            'status': payment.authorization_status,
            'authorizedAt': _time(payment.authorized_at),
            'expiresAt': _time(payment.authorization_expires_at),
            'reauthorizations': payment.reauthorization_count,
        } if payment.authorization_id else None),
        'capture': ({
            'captureId': payment.capture_id,
            'status': payment.capture_status,
            'amount': _money(payment.captured_amount),
            'paypalFee': _money(payment.paypal_fee),
            'netAmount': _money(payment.net_amount),
            'capturedAt': _time(payment.captured_at),
        } if payment.capture_id else None),
        'refundedAmount': _money(payment.refunded_amount),
        'refundableAmount': _money(payment.refundable_amount),
        'refunds': [refund_dict(r) for r in payment.refunds.all()],
        'error': _error(payment),
    }


def order_dict(order, payment=None):
    payment = payment if payment is not None else getattr(order, 'paypal_payment', None)
    return {
        'orderId': str(order.number),
        'status': order.status,
        'placedAt': _time(order.date_placed),
        'currency': order.currency,
        'total': _money(order.total_incl_tax),
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'unitPrice': _money(line.unit_price_incl_tax),
            'linePrice': _money(line.line_price_incl_tax),
        } for line in order.lines.all()],
        'payment': payment_dict(payment) if payment is not None else None,
    }


def card_dict(card):
    return {
        'paymentMethodId': str(card.public_id),
        'brand': card.brand or None,
        'lastDigits': card.last_digits,
        'expiry': card.expiry,
        'createdAt': _time(card.created),
    }
