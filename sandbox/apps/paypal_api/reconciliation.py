"""
Line PayPal's own transaction record up against this app's payments.

Both sides are filtered on PayPal's clock: transactions on
``transaction_initiation_date``, local operations on the ``provider_time``
PayPal returned when they completed. One local operation may own several
PayPal records (the same id reported pending and then settled), so matching is
done against the set.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal.core import UnsetType

from . import gateway
from .errors import ApiProblem, call_paypal
from .models import PayPalOperation as Op
from .money import to_minor, wire

WINDOW = timedelta(days=30)  # PayPal rejects ranges over 31 days
PAGE_SIZE = 100
MAX_RANGE = timedelta(days=3 * 365)  # PayPal lists the previous three years
MONEY_KINDS = (Op.AUTHORIZE, Op.REAUTHORIZE, Op.CAPTURE, Op.REFUND)


def v(value: Any) -> Any:
    return None if isinstance(value, UnsetType) else value


def parse_range(raw_from: str | None, raw_to: str | None) -> tuple[datetime, datetime]:
    def parse(value: str | None, name: str) -> datetime:
        parsed = parse_datetime(value or "")
        if parsed is None or timezone.is_naive(parsed):
            raise ApiProblem(400, "invalid_range",
                             f"'{name}' must be an ISO-8601 date-time with a UTC offset, e.g. 2026-09-01T00:00:00Z.")
        return parsed

    start, end = parse(raw_from, "from"), parse(raw_to, "to")
    if end <= start:
        raise ApiProblem(400, "invalid_range", "'to' must be after 'from'.")
    if end - start > MAX_RANGE:
        raise ApiProblem(400, "invalid_range", "The range may cover at most three years.")
    return start, end


def _stamp(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_transactions(start: datetime, end: datetime) -> tuple[list[dict[str, Any]], datetime | None]:
    """Every PayPal transaction initiated in [start, end): all windows, all pages."""
    client = gateway.get_client()
    records: list[dict[str, Any]] = []
    refreshed: datetime | None = None
    window_start = start
    now = timezone.now()
    while window_start < min(end, now):
        # PayPal's filter is inclusive and second-granular: widen by a second, narrow back below.
        window_end = min(window_start + WINDOW, end + timedelta(seconds=1), now)
        page, pages = 1, 1
        while page <= pages:
            result = call_paypal(lambda: gateway.read_with_retry(lambda: client.transaction_search.search_transactions(
                _stamp(window_start), _stamp(window_end), fields="transaction_info",
                balance_affecting_records_only="N", page_size=PAGE_SIZE, page=page)))
            pages = v(result.total_pages) or 1
            stamp = parse_datetime(v(result.last_refreshed_datetime) or "")
            if stamp is not None and (refreshed is None or stamp < refreshed):
                refreshed = stamp
            for details in v(result.transaction_details) or []:
                info = v(details.transaction_info)
                if info is None:
                    continue
                initiated = parse_datetime(v(info.transaction_initiation_date) or "")
                if initiated is None or not start <= initiated < end:
                    continue
                amount, fee = v(info.transaction_amount), v(info.fee_amount)
                records.append({
                    "transactionId": v(info.transaction_id),
                    "referenceId": v(info.paypal_reference_id),
                    "eventCode": v(info.transaction_event_code),
                    "status": v(info.transaction_status),
                    "initiatedAt": initiated.isoformat(),
                    "amount": amount.value if amount else None,
                    "currency": amount.currency_code if amount else None,
                    "fee": fee.value if fee else None,
                    "invoiceId": v(info.invoice_id),
                    "customField": v(info.custom_field),
                })
            page += 1
        window_start = window_end
    return records, refreshed


def _order_for_reference(value: str | None, prefix: str) -> str | None:
    """Our invoice/custom ids look like '<prefix>-<order number>[-<attempt>]'."""
    if not value or not value.startswith(prefix + "-"):
        return None
    return value[len(prefix) + 1:].split("-")[0] or None


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    prefix = gateway.reference_prefix()
    provider, refreshed = fetch_transactions(start, end)

    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in provider:
        by_id[record["transactionId"] or ""].append(record)

    local = (Op.objects.filter(kind__in=MONEY_KINDS, provider_time__gte=start, provider_time__lt=end)
             .exclude(provider_id="").exclude(outcome=Op.FAILED).select_related("order").order_by("provider_time"))
    matched, local_only, not_yet_reported = [], [], []
    for op in local:
        entry = {
            "orderId": op.order.number if op.order else None,
            "operation": op.kind,
            "paypalId": op.provider_id,
            "outcome": op.outcome,
            "paypalStatus": op.provider_status,
            "amount": wire(op.amount_minor, op.currency) if op.amount_minor is not None and op.currency else None,
            "currency": op.currency or None,
            "at": op.provider_time.isoformat() if op.provider_time else None,
        }
        records = by_id.pop(op.provider_id, [])
        if records:
            entry["paypalRecords"] = records
            local_amount = op.amount_minor
            mismatched = [r for r in records if r["amount"] and r["currency"] == op.currency and local_amount is not None
                          and abs(to_minor(r["amount"], op.currency)) != local_amount]
            if mismatched:
                entry["amountMismatch"] = True
            matched.append(entry)
        elif refreshed is not None and op.provider_time and op.provider_time > refreshed:
            not_yet_reported.append(entry)
        else:
            local_only.append(entry)

    provider_only = []
    for records in by_id.values():
        for record in records:
            order_number = (_order_for_reference(record["invoiceId"], prefix)
                            or _order_for_reference(record["customField"], prefix))
            provider_only.append({**record, "orderId": order_number, "fromThisSite": order_number is not None})

    unsettled = [
        {"orderId": op.order.number if op.order else None, "operation": op.kind, "outcome": op.outcome,
         "claimedAt": op.claimed_at.isoformat(), "reference": op.ref}
        for op in Op.objects.filter(outcome__in=(Op.SENDING, Op.UNKNOWN, Op.NEEDS_REVIEW),
                                    claimed_at__gte=start, claimed_at__lt=end).select_related("order")
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalDataRefreshedAt": refreshed.isoformat() if refreshed else None,
        "summary": {
            "paypalTransactions": len(provider),
            "matched": len(matched),
            "paypalOnly": len(provider_only),
            "paypalOnlyFromThisSite": sum(1 for r in provider_only if r["fromThisSite"]),
            "appOnly": len(local_only),
            "notYetReportedByPayPal": len(not_yet_reported),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "paypalOnly": provider_only,
        "appOnly": local_only,
        "notYetReportedByPayPal": not_yet_reported,
        "unsettled": unsettled,
    }
