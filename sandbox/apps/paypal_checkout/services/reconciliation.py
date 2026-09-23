"""Reconciliation report: PayPal's own transaction record for a date range,
lined up against this app's orders so a payment one side knows about and the
other does not is visible.

Covers the whole range (all pages of PayPal's paged report), not just the first.
"""

from ..models import PayPalPayment
from . import gateway


def reconcile(from_dt, to_dt):
    """Return a report dict comparing PayPal transactions and app orders.

    ``from_dt``/``to_dt`` are timezone-aware datetimes.
    """
    txns = gateway.search_transactions(from_dt, to_dt)

    # Index app orders whose PayPal payment falls in the range, by the keys PayPal
    # reports back to us (order number as invoice_id, and the capture id).
    payments = (
        PayPalPayment.objects.filter(
            order__date_placed__gte=from_dt, order__date_placed__lte=to_dt
        )
        .exclude(authorization_id="")
        .select_related("order")
    )
    by_number = {}
    by_capture = {}
    app_index = {}
    for p in payments:
        entry = {
            "orderId": p.order_id,
            "orderNumber": p.order.number,
            "state": p.state,
            "amount": _money_str(p.amount),
            "currency": p.currency,
            "authorizationId": p.authorization_id or None,
            "captureId": p.capture_id or None,
        }
        app_index[p.order.number] = entry
        by_number[p.order.number] = p
        if p.capture_id:
            by_capture[p.capture_id] = p

    matched = []
    only_in_paypal = []
    matched_numbers = set()

    for txn in txns:
        invoice = txn.get("invoice_id")
        custom = txn.get("custom_field") or ""
        # invoice_id is "<orderNumber>-<paymentPk>"; custom_field is "ORDER-<n>".
        number = None
        if invoice:
            number = invoice.split("-", 1)[0]
        if number not in by_number and custom.startswith("ORDER-"):
            number = custom[len("ORDER-"):]
        payment = None
        if number and number in by_number:
            payment = by_number[number]
        elif txn.get("transaction_id") in by_capture:
            payment = by_capture[txn["transaction_id"]]
            number = payment.order.number

        txn_view = {
            "transactionId": txn.get("transaction_id"),
            "status": txn.get("status"),
            "amount": _money_str(txn.get("amount")),
            "fee": _money_str(txn.get("fee")),
            "currency": txn.get("currency"),
            "invoiceId": invoice,
            "date": txn.get("initiation_date"),
        }
        if payment is not None:
            matched_numbers.add(payment.order.number)
            matched.append({"order": app_index[payment.order.number], "paypal": txn_view})
        else:
            only_in_paypal.append(txn_view)

    only_in_app = [
        entry for number, entry in app_index.items() if number not in matched_numbers
    ]

    return {
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "paypalTransactionCount": len(txns),
        "appOrderCount": len(app_index),
        "matched": matched,
        "onlyInPayPal": only_in_paypal,
        "onlyInApp": only_in_app,
    }


def _money_str(value):
    if value is None:
        return None
    return "%0.2f" % value
