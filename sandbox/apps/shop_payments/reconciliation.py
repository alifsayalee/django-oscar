"""
Line PayPal's transaction records for a period up against this shop's captures
and refunds.

Both sides are filtered on PayPal's clock: the provider rows by their
``transaction_initiation_date``, the local writes by the event time PayPal
returned when the write completed.  Writes PayPal has not settled yet have no
provider time; they are reported separately as ``unsettled``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal.core import UnsetType
from paypal.models import Money, TransactionInformation

from . import errors
from .errors import ApiProblem
from .models import PaymentWrite
from .paypal_client import get_client
from .safe_write import provider_time, reference_prefix

# transaction search accepts at most 31 days per request.
MAX_WINDOW = timedelta(days=31)
MAX_RANGE = timedelta(days=3 * 366)
PAGE_SIZE = 100
MAX_PAGES = 1000

SETTLED_KINDS = (PaymentWrite.CAPTURE, PaymentWrite.REFUND)


def parse_instant(raw: str | None, name: str) -> datetime:
    if not raw:
        raise ApiProblem(400, "invalid_range", f"'{name}' is required (ISO-8601 date-time).")
    value = parse_datetime(raw.replace(" ", "+"))  # a '+' offset arrives as a space when not URL-encoded
    if value is None:
        raise ApiProblem(400, "invalid_range", f"'{name}' is not an ISO-8601 date-time.")
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value


def windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out = []
    cursor = start
    while cursor < end:
        upper = min(cursor + MAX_WINDOW - timedelta(seconds=1), end)
        out.append((cursor, upper))
        cursor = upper
    return out


def rfc3339(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_transactions(start: datetime, end: datetime) -> tuple[list[TransactionInformation], str | None]:
    """Every transaction PayPal reports in [start, end], across every window and page."""
    client = get_client()
    rows: dict[tuple[str, str], TransactionInformation] = {}
    refreshed: str | None = None
    for lower, upper in windows(start, end):
        page = 1
        while True:
            response = errors.read(
                lambda: client.transaction_search.search_transactions(
                    rfc3339(lower), rfc3339(upper), fields="transaction_info", page_size=PAGE_SIZE, page=page
                ),
                what="transaction search",
            )
            if isinstance(response.last_refreshed_datetime, str):
                refreshed = response.last_refreshed_datetime
            details = response.transaction_details
            for detail in details if isinstance(details, list) else []:
                info = detail.transaction_info
                if isinstance(info, UnsetType):
                    continue
                key = (_text(info.transaction_id), _text(info.transaction_event_code))
                rows[key] = info  # window edges can repeat a row; keep one
            total_pages = response.total_pages if isinstance(response.total_pages, int) else 1
            if page >= total_pages or page >= MAX_PAGES:
                break
            page += 1
    return list(rows.values()), refreshed


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _amount(value: object) -> Decimal | None:
    if isinstance(value, Money):
        try:
            return Decimal(value.value)
        except InvalidOperation:
            return None
    return None


def _describe(info: TransactionInformation) -> dict[str, Any]:
    amount = _amount(info.transaction_amount)
    fee = _amount(info.fee_amount)
    return {
        "transactionId": _text(info.transaction_id),
        "eventCode": _text(info.transaction_event_code),
        "status": _text(info.transaction_status),
        "initiatedAt": _text(info.transaction_initiation_date),
        "amount": str(amount) if amount is not None else None,
        "fee": str(fee) if fee is not None else None,
        "currency": info.transaction_amount.currency_code if isinstance(info.transaction_amount, Money) else None,
        "reference": _text(info.custom_field) or _text(info.invoice_id) or None,
        "relatedTransactionId": _text(info.paypal_reference_id) or None,
    }


def _describe_write(write: PaymentWrite) -> dict[str, Any]:
    return {
        "kind": write.kind,
        "orderId": write.order.number if write.order is not None else None,
        "paypalId": write.provider_id or None,
        "outcome": write.outcome,
        "amount": str(write.amount) if write.amount is not None else None,
        "currency": write.currency or None,
        "paypalTime": write.provider_time.isoformat() if write.provider_time else None,
        "reference": write.reference,
    }


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    if end <= start:
        raise ApiProblem(400, "invalid_range", "'to' must be after 'from'.")
    if end - start > MAX_RANGE:
        raise ApiProblem(400, "invalid_range", "The range may span at most three years (PayPal's reporting limit).")

    fetched, refreshed = fetch_transactions(start, end)
    provider = []
    for info in fetched:
        when = provider_time(info.transaction_initiation_date)
        if when is None or start <= when < end:
            provider.append(info)

    settled = list(
        PaymentWrite.objects.filter(
            kind__in=SETTLED_KINDS,
            provider_time__gte=start,
            provider_time__lt=end,
        )
        .exclude(provider_id="")
        .select_related("order")
    )
    unsettled = list(
        PaymentWrite.objects.filter(kind__in=SETTLED_KINDS + (PaymentWrite.AUTHORIZE,), claimed_at__gte=start,
                                    claimed_at__lt=end)
        .filter(outcome__in=(PaymentWrite.SENDING, PaymentWrite.UNKNOWN, PaymentWrite.PENDING))
        .filter(provider_time__isnull=True)
        .select_related("order")
    )

    by_id: dict[str, list[TransactionInformation]] = defaultdict(list)
    for info in provider:
        by_id[_text(info.transaction_id)].append(info)

    matched, local_only = [], []
    for write in settled:
        records = by_id.pop(write.provider_id, [])
        if not records:
            local_only.append(_describe_write(write))
            continue
        entry = _describe_write(write)
        entry["paypal"] = [_describe(r) for r in records]
        paypal_amounts = {abs(a) for a in (_amount(r.transaction_amount) for r in records) if a is not None}
        entry["amountMatches"] = write.amount is not None and abs(write.amount) in paypal_amounts
        matched.append(entry)

    prefix = reference_prefix()
    provider_only = []
    for records in by_id.values():
        for info in records:
            row = _describe(info)
            row["referencesThisShop"] = bool(row["reference"] and str(row["reference"]).startswith(prefix + ":"))
            provider_only.append(row)
    provider_only.sort(key=lambda r: r["initiatedAt"] or "")

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalLastRefreshed": refreshed,
        "note": "PayPal's transaction reporting can lag live activity by up to three hours.",
        "summary": {
            "paypalTransactions": len(provider),
            "matched": len(matched),
            "amountMismatches": sum(1 for m in matched if not m["amountMatches"]),
            "localOnly": len(local_only),
            "paypalOnly": len(provider_only),
            "paypalOnlyReferencingThisShop": sum(1 for r in provider_only if r["referencesThisShop"]),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "localOnly": local_only,
        "paypalOnly": provider_only,
        "unsettled": [_describe_write(w) for w in unsettled],
    }
