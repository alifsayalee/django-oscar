"""
Reconciliation: PayPal's own transaction records against this app's payments.

* PayPal's search takes at most 31 days per call, so the range is split into
  windows, and every page of every window is read (bounded; a cut-short walk
  is reported as ``truncated`` in the result, never silently).
* The request is widened to whole seconds, then narrowed back to the caller's
  exact instants in code.
* Both sides are filtered on PayPal's clock: the app records the capture and
  refund times PayPal reported, not when its own rows were written.
* One order owns several PayPal records (a capture and each refund); matching
  is by PayPal transaction id against the full set, so nothing is dropped after
  a first hit.
"""

import math
from datetime import timedelta, timezone
from decimal import Decimal

from paypal.core import UnsetType

from . import gateway
from .models import PayPalPayment, PayPalRefund

WINDOW = timedelta(days=31)
MAX_RANGE = timedelta(days=3 * 365)  # PayPal lists the previous three years
MAX_PAGES = 200  # across the whole report
PAGE_SIZE = 100


class InvalidRange(ValueError):
    pass


def _fmt(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(moment):
    return moment.astimezone(timezone.utc)


def fetch_paypal_transactions(start, end):
    """All PayPal transactions initiated in [start, end). Returns (records, truncated, last_refreshed)."""
    client = gateway.get_client()
    records = []
    pages = 0
    truncated = False
    last_refreshed = None
    window_start = start
    while window_start < end and not truncated:
        window_end = min(window_start + WINDOW, end)
        # Widen to whole seconds for the filter; narrowed back below.
        query_start = window_start.replace(microsecond=0)
        query_end = window_end.replace(microsecond=0) + (timedelta(seconds=1) if window_end.microsecond else timedelta())
        page = 1
        while True:
            if pages >= MAX_PAGES:
                truncated = True
                break
            result = gateway.call(
                "transaction_search.search_transactions",
                client.transaction_search.search_transactions,
                _fmt(_utc(query_start)), _fmt(_utc(query_end)),
                page_size=PAGE_SIZE, page=page,
            )
            pages += 1
            refreshed = gateway.parse_time(result.last_refreshed_datetime)
            if refreshed is not None:
                last_refreshed = refreshed if last_refreshed is None else min(last_refreshed, refreshed)
            details = result.transaction_details
            batch = [] if isinstance(details, UnsetType) else details
            for detail in batch:
                record = _record(detail)
                if record is not None:
                    records.append(record)
            total_pages = result.total_pages
            if isinstance(total_pages, UnsetType):
                # No end signal: stop on a short page.
                if len(batch) < PAGE_SIZE:
                    break
            elif page >= total_pages:
                break
            if not batch:
                break  # no progress: never spin on an empty page
            page += 1
        window_start = window_end
    # Narrow the widened query back to the caller's instants.
    records = [r for r in records if r["time"] is None or start <= r["time"] < end]
    return records, truncated, last_refreshed


def _record(detail):
    info = detail.transaction_info
    if isinstance(info, UnsetType):
        return None
    amount = gateway.amount_of(info.transaction_amount)
    fee = gateway.amount_of(info.fee_amount)
    return {
        "transactionId": gateway.text_or_empty(info.transaction_id),
        "eventCode": gateway.text_or_empty(info.transaction_event_code),
        "status": gateway.text_or_empty(info.transaction_status),
        "time": gateway.parse_time(info.transaction_initiation_date),
        "amount": amount[0] if amount else None,
        "fee": fee[0] if fee else None,
        "currency": amount[1] if amount else "",
        "invoiceId": gateway.text_or_empty(info.invoice_id),
        "customField": gateway.text_or_empty(info.custom_field),
        "referenceId": gateway.text_or_empty(info.paypal_reference_id),
    }


def _local_records(start, end):
    """The app's PayPal money movements whose PayPal time falls in [start, end)."""
    local = []
    captures = PayPalPayment.objects.filter(capture_time__gte=start, capture_time__lt=end).exclude(capture_id="")
    for payment in captures.select_related("order"):
        local.append({
            "transactionId": payment.capture_id, "kind": "capture", "orderId": payment.order.number,
            "amount": payment.captured_amount, "currency": payment.currency, "time": payment.capture_time,
            "state": payment.state,
        })
    refunds = PayPalRefund.objects.filter(refund_time__gte=start, refund_time__lt=end).exclude(paypal_refund_id="")
    for refund in refunds.select_related("payment__order"):
        local.append({
            "transactionId": refund.paypal_refund_id, "kind": "refund", "orderId": refund.payment.order.number,
            "amount": -refund.amount, "currency": refund.currency, "time": refund.refund_time,
            "state": refund.state,
        })
    # Money movements with no PayPal time yet: unresolved, reported on their own.
    unsettled = []
    for payment in PayPalPayment.objects.filter(
        state__in=[PayPalPayment.CAPTURING, PayPalPayment.CAPTURE_UNKNOWN, PayPalPayment.NEEDS_REVIEW],
        date_updated__gte=start, date_updated__lt=end,
    ).select_related("order"):
        unsettled.append({"kind": "capture", "orderId": payment.order.number, "state": payment.state,
                          "amount": payment.amount, "currency": payment.currency})
    for refund in PayPalRefund.objects.filter(
        state__in=[PayPalRefund.SENDING, PayPalRefund.UNKNOWN], date_created__gte=start, date_created__lt=end,
    ).select_related("payment__order"):
        unsettled.append({"kind": "refund", "orderId": refund.payment.order.number, "state": refund.state,
                          "amount": refund.amount, "currency": refund.currency, "refundId": str(refund.public_id)})
    for row in local + unsettled:
        if row["amount"] is not None:
            row["amount"] = gateway.quantize(Decimal(row["amount"]), row["currency"])
    return local, unsettled


def build_report(start, end):
    if end <= start:
        raise InvalidRange("'to' must be after 'from'.")
    if end - start > MAX_RANGE:
        raise InvalidRange("The range may span at most three years.")
    paypal_records, truncated, last_refreshed = fetch_paypal_transactions(start, end)
    local, unsettled = _local_records(start, end)

    by_id: dict[str, list[dict]] = {}
    for record in paypal_records:
        by_id.setdefault(record["transactionId"], []).append(record)

    matched, app_only, not_yet_reported = [], [], []
    for item in local:
        records = by_id.pop(item["transactionId"], [])
        if records:
            paypal_amount = records[0]["amount"]
            matched.append({
                **item,
                "paypalAmount": paypal_amount,
                "paypalFee": records[0]["fee"],
                "paypalStatus": records[0]["status"],
                "amountMatches": paypal_amount is not None and item["amount"] is not None
                and Decimal(paypal_amount) == Decimal(item["amount"]),
            })
        elif last_refreshed is not None and item["time"] and item["time"] > last_refreshed:
            not_yet_reported.append(item)  # PayPal's reporting has not caught up to it yet
        else:
            app_only.append(item)
    known_orders = set(PayPalPayment.objects.values_list("order__number", flat=True))
    paypal_only = []
    for records in by_id.values():  # left over only after every local record took its own
        for record in records:
            record["referencesAppOrder"] = record["customField"] in known_orders
            paypal_only.append(record)

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "windows": math.ceil((end - start) / WINDOW),
        "truncated": truncated,
        "paypalLastRefreshed": last_refreshed.isoformat() if last_refreshed else None,
        "paypalTransactionCount": len(paypal_records),
        "summary": {
            "matched": len(matched),
            "amountMismatches": sum(1 for m in matched if not m["amountMatches"]),
            "paypalOnly": len(paypal_only),
            "appOnly": len(app_only),
            "notYetReportedByPayPal": len(not_yet_reported),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
        "notYetReportedByPayPal": not_yet_reported,
        "unsettled": unsettled,
    }
