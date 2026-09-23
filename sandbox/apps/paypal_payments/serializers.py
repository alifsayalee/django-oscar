"""JSON shapes for the API. Amounts are decimal strings; never any card number."""
from .models import PayPalPayment


def _money(amount):
    return None if amount is None else str(amount)


def _time(moment):
    return None if moment is None else moment.isoformat()


def refund_json(refund):
    return {
        'refundId': refund.pk,
        'paypalRefundId': refund.paypal_refund_id or None,
        'amount': _money(refund.amount),
        'status': refund.status,
        'paypalFeeReturned': _money(refund.paypal_fee_returned),
        'netAmount': _money(refund.net_amount),
        'idempotencyKey': refund.idempotency_key,
        'createdAt': _time(refund.created_at),
    }


def payment_json(payment):
    from .services import refundable_amount

    captured = payment.state in (
        PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED)
    return {
        'state': payment.state,
        'amount': _money(payment.amount),
        'currency': payment.currency,
        'card': payment.card_label or None,
        'paymentMethodId': payment.saved_card_id,
        'paypalOrderId': payment.paypal_order_id or None,
        'authorization': {
            'id': payment.authorization_id,
            'status': payment.authorization_status,
            'amount': _money(payment.authorized_amount),
            'authorizedAt': _time(payment.authorized_at),
            'expiresAt': _time(payment.authorization_expires_at),
            'reauthorizations': payment.reauthorization_count,
        } if payment.authorization_id else None,
        'capture': {
            'id': payment.capture_id,
            'status': payment.capture_status,
            'amount': _money(payment.captured_amount),
            'paypalFee': _money(payment.paypal_fee),
            'netAmount': _money(payment.net_amount),
            'capturedAt': _time(payment.captured_at),
        } if payment.capture_id else None,
        'refunds': [refund_json(r) for r in payment.refunds.all()],
        'refundedAmount': _money(payment.refunded_amount),
        'refundableAmount': _money(refundable_amount(payment)) if captured else '0.00',
        'lastError': payment.last_error or None,
    }


def order_json(order):
    try:
        payment = order.paypal_payment
    except PayPalPayment.DoesNotExist:
        payment = None
    return {
        'orderId': order.pk,
        'orderNumber': order.number,
        'status': order.status,
        'total': _money(order.total_incl_tax),
        'currency': order.currency,
        'placedAt': _time(order.date_placed),
        'lines': [
            {
                'productId': line.product_id,
                'title': line.title,
                'quantity': line.quantity,
                'unitPrice': _money(line.unit_price_incl_tax),
                'lineTotal': _money(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        'payment': payment_json(payment) if payment is not None else None,
    }


def payment_method_json(bankcard):
    return {
        'paymentMethodId': bankcard.pk,
        'brand': bankcard.card_type,
        'last4': bankcard.number[-4:],
        'expiry': bankcard.expiry_date.strftime('%Y-%m'),
        'name': bankcard.name or None,
    }
