"""
Reconciliation: PayPal's own transaction record for a window, lined up against
this app's captures and refunds — on PayPal's clock on both sides.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from pay_pal_server_sdk.models import SearchResponse, TransactionInformation

from .common import ClientError, reference_prefix
from .models import OrderPayment, Outcome, PaymentRefund, ProviderWrite
from .paypal import get_client, unset_to_none
from .safe_write import provider_time

# transaction_search.search_transactions: "The maximum supported range is 31 days."
MAX_WINDOW = timedelta(days=31)
PAGE_SIZE = 100
MAX_PAGES_PER_WINDOW = 1000  # a runaway guard, far above any sandbox volume

# Balance-affecting writes: the ones PayPal's transaction search reports by default.
RECONCILED_KINDS = ("capture", "refund")
SETTLED = (Outcome.DONE, Outcome.PENDING, Outcome.NEEDS_REVIEW)


def _wire(dt: datetime) -> str:
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    windows = []
    cursor = start
    while cursor < end:
        upper = min(cursor + MAX_WINDOW, end)
        windows.append((cursor, upper))
        cursor = upper
    return windows


def fetch_provider_records(start: datetime, end: datetime) -> tuple[list[TransactionInformation], str | None]:
    """Every page of every ≤31-day window, narrowed back to [start, end); plus PayPal's data freshness."""
    client = get_client()
    refreshed: list[str] = []
    records: dict[str, TransactionInformation] = {}
    for lo, hi in _windows(start, end):
        page = 1
        while True:
            response: SearchResponse = client.transaction_search.search_transactions(
                _wire(lo), _wire(hi), page_size=PAGE_SIZE, page=page
            )
            for detail in unset_to_none(response.transaction_details) or []:
                info = unset_to_none(detail.transaction_info)
                if info is None:
                    continue
                txn_id = unset_to_none(info.transaction_id)
                when = provider_time(unset_to_none(info.transaction_initiation_date))
                if txn_id and when is not None and start <= when < end:
                    records[txn_id] = info
            last_refreshed = unset_to_none(response.last_refreshed_datetime)
            if last_refreshed:
                refreshed.append(last_refreshed)
            total_pages = unset_to_none(response.total_pages) or 0
            if page >= total_pages:
                break
            page += 1
            if page > MAX_PAGES_PER_WINDOW:
                raise ClientError(422, "range_too_large", "Too many PayPal transactions in this range; narrow it")
    return list(records.values()), min(refreshed) if refreshed else None


def _money(m: Any) -> dict[str, str] | None:
    m = unset_to_none(m)
    if m is None:
        return None
    return {"value": m.value, "currency": m.currency_code}


def _order_for_invoice(invoice_id: str | None, prefix: str) -> str | None:
    # invoice_id is "{prefix}-{order number}-a{attempt}" for our own payments.
    if not invoice_id or not invoice_id.startswith(prefix + "-"):
        return None
    return invoice_id[len(prefix) + 1:].rsplit("-a", 1)[0]


def _provider_row(info: TransactionInformation, prefix: str) -> dict[str, Any]:
    invoice_id = unset_to_none(info.invoice_id)
    return {
        "transactionId": unset_to_none(info.transaction_id),
        "referenceId": unset_to_none(info.paypal_reference_id),
        "eventCode": unset_to_none(info.transaction_event_code),
        "status": unset_to_none(info.transaction_status),
        "initiatedAt": unset_to_none(info.transaction_initiation_date),
        "amount": _money(info.transaction_amount),
        "fee": _money(info.fee_amount),
        "invoiceId": invoice_id,
        "orderId": _order_for_invoice(invoice_id, prefix),
    }


def _local_row(write: ProviderWrite, orders: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": write.kind,
        "reference": write.ref,
        "paypalId": write.provider_id or None,
        "outcome": write.outcome,
        "paypalStatus": write.provider_status or None,
        "providerTime": write.provider_time.isoformat() if write.provider_time else None,
        "claimedAt": write.claimed_at.isoformat(),
        "amount": str(write.amount) if write.amount is not None else None,
        "currency": write.currency or None,
        "orderId": orders.get(write.ref),
    }


def _orders_by_ref(writes: list[ProviderWrite]) -> dict[str, str]:
    refs = [w.ref for w in writes]
    result: dict[str, str] = {}
    for row in PaymentRefund.objects.filter(ref__in=refs).select_related("payment__order"):
        result[row.ref] = row.payment.order.number
    prefix = reference_prefix()
    order_pks = {}
    for w in writes:
        if w.kind == "capture" and w.ref.startswith(prefix + ":o"):
            order_pks[w.ref] = int(w.ref[len(prefix) + 2:].split(":", 1)[0])
    numbers = dict(OrderPayment.objects.filter(order_id__in=order_pks.values()).values_list("order_id", "order__number"))
    for ref, pk in order_pks.items():
        if pk in numbers:
            result[ref] = numbers[pk]
    return result


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    if end <= start:
        raise ClientError(422, "invalid_range", "'to' must be after 'from'")
    prefix = reference_prefix()
    provider, refreshed_at = fetch_provider_records(start, end)

    # Our side on PayPal's clock: the provider time stored when each write completed.
    local = list(ProviderWrite.objects.filter(
        kind__in=RECONCILED_KINDS, outcome__in=SETTLED, provider_time__gte=start, provider_time__lt=end
    ))
    # No PayPal time yet (in flight / unknown): reported, never dropped or folded in.
    unsettled = list(ProviderWrite.objects.filter(
        kind__in=RECONCILED_KINDS, provider_time__isnull=True, claimed_at__gte=start, claimed_at__lt=end
    ).exclude(outcome=Outcome.FAILED))
    orders = _orders_by_ref(local + unsettled)

    by_id: dict[str, list[TransactionInformation]] = defaultdict(list)
    for info in provider:
        by_id[unset_to_none(info.transaction_id) or ""].append(info)

    matched = []
    local_only = []
    for write in local:
        records = by_id.pop(write.provider_id, []) if write.provider_id else []
        if records:
            matched.append({"local": _local_row(write, orders), "paypal": [_provider_row(r, prefix) for r in records]})
        else:
            local_only.append(_local_row(write, orders))
    provider_only = [_provider_row(r, prefix) for records in by_id.values() for r in records]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "summary": {
            "paypalTransactions": len(provider),
            "matched": len(matched),
            "localOnly": len(local_only),
            "paypalOnly": len(provider_only),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "localOnly": local_only,
        "paypalOnly": provider_only,
        "unsettled": [_local_row(w, orders) for w in unsettled],
        # PayPal's reporting lags live activity: nothing after this time is in its data yet.
        "paypalDataRefreshedAt": refreshed_at,
        "note": "PayPal's transaction reporting lags live activity; very recent payments may not be listed yet.",
    }
