"""
Line PayPal's own transaction records up against this shop's orders.

PayPal's side comes from the transaction search (every page of every window
the range needs). The app's side is every capture and refund it recorded in
the range. A record on only one side is the discrepancy an operator must look
at: money PayPal moved that the app doesn't know about, or money the app
believes moved that PayPal has no record of.
"""

from datetime import datetime

from django.conf import settings

from . import gateway
from .models import PayPalPayment, PayPalRefund


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _app_record(kind, order_number, paypal_id, amount, currency, at):
    return {
        "type": kind,
        "orderId": order_number,
        "paypalTransactionId": paypal_id,
        "amount": str(amount) if amount is not None else None,
        "currency": currency,
        "recordedAt": _iso(at),
    }


def _paypal_record(txn):
    return {
        "transactionId": txn.transaction_id,
        "eventCode": txn.event_code,
        "status": txn.status,
        "initiatedAt": txn.initiated_at,
        "amount": str(txn.amount) if txn.amount is not None else None,
        "currency": txn.currency,
        "fee": str(txn.fee) if txn.fee is not None else None,
        "invoiceId": txn.invoice_id,
        "customField": txn.custom_field,
    }


def reconcile(start, end):
    page = gateway.search_transactions(start, end)
    matched, paypal_only = _match_paypal_records(page.transactions)
    app_only = _app_records_paypal_lacks(start, end, {t.transaction_id for t in page.transactions},
                                         page.last_refreshed)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalDataRefreshedAt": page.last_refreshed,
        "summary": {
            "paypalTransactions": len(page.transactions),
            "matched": len(matched),
            "paypalOnly": len(paypal_only),
            "paypalOnlyForThisShop": sum(1 for r in paypal_only if r["reason"] != "not_from_this_shop"),
            "appOnly": len(app_only),
        },
        "matched": matched,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
    }


def _known_records(paypal_ids):
    """Every app record PayPal mentions, whenever the app recorded it, by PayPal id."""
    by_id = {}
    for p in PayPalPayment.objects.select_related("order").filter(capture_id__in=paypal_ids):
        by_id[p.capture_id] = _app_record("capture", p.order.number, p.capture_id, p.captured_amount,
                                          p.currency, p.captured_at)
    for p in PayPalPayment.objects.select_related("order").filter(authorization_id__in=paypal_ids):
        by_id.setdefault(p.authorization_id, _app_record("authorization", p.order.number, p.authorization_id,
                                                         p.amount, p.currency, p.authorized_at))
    for r in PayPalRefund.objects.select_related("payment__order").filter(paypal_refund_id__in=paypal_ids):
        by_id[r.paypal_refund_id] = _app_record("refund", r.payment.order.number, r.paypal_refund_id,
                                                -r.amount, r.payment.currency, r.date_created)
    return by_id


def _match_paypal_records(transactions):
    prefix = settings.PAYPAL_INVOICE_PREFIX
    by_id = _known_records({t.transaction_id for t in transactions})
    by_invoice = {
        p.invoice_id: p.order.number
        for p in PayPalPayment.objects.select_related("order").filter(
            invoice_id__in={t.invoice_id for t in transactions if t.invoice_id})
    }
    matched, paypal_only = [], []
    for txn in sorted(transactions, key=lambda t: t.initiated_at):
        app = by_id.get(txn.transaction_id)
        if app is not None:
            entry = {"paypal": _paypal_record(txn), "app": app, "matchedBy": "transactionId"}
            if app["amount"] is not None and txn.amount is not None and str(txn.amount) != app["amount"]:
                entry["amountMismatch"] = True
            matched.append(entry)
        elif txn.invoice_id in by_invoice:
            # PayPal knows a movement for one of our orders that the app did not record.
            paypal_only.append({**_paypal_record(txn), "orderId": by_invoice[txn.invoice_id],
                                "reason": "unrecorded_transaction_for_known_order"})
        else:
            ours = bool(txn.invoice_id and txn.invoice_id.startswith(prefix))
            paypal_only.append({**_paypal_record(txn), "orderId": None,
                                "reason": "unknown_order_with_shop_prefix" if ours else "not_from_this_shop"})
    return matched, paypal_only


def _app_records_paypal_lacks(start, end, paypal_ids, last_refreshed):
    """Every capture and refund the app recorded in the range that PayPal has no record of."""
    app_only = []
    captures = PayPalPayment.objects.select_related("order").exclude(capture_id="").filter(
        captured_at__gte=start, captured_at__lt=end)
    for p in captures:
        if p.capture_id not in paypal_ids:
            app_only.append(_app_record("capture", p.order.number, p.capture_id, p.captured_amount,
                                        p.currency, p.captured_at))
    refunds = PayPalRefund.objects.select_related("payment__order").exclude(paypal_refund_id="").filter(
        date_created__gte=start, date_created__lt=end)
    for r in refunds:
        if r.paypal_refund_id not in paypal_ids:
            app_only.append(_app_record("refund", r.payment.order.number, r.paypal_refund_id, -r.amount,
                                        r.payment.currency, r.date_created))
    refreshed = _parse(last_refreshed)
    for record in app_only:
        at = _parse(record["recordedAt"])
        # PayPal's reporting lags live activity by up to a few hours.
        record["withinReportingLag"] = bool(refreshed is None or (at is not None and at > refreshed))
    return app_only


def _parse(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
