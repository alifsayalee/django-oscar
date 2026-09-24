"""
Line PayPal's transaction records up against this site's payment records.

* PayPal's side is fetched for the whole range: split into windows no longer
  than PayPal's 31-day limit, every page of every window.
* Both sides are filtered on PayPal's clock: a transaction's initiation date,
  and the provider time stored with each of our writes when PayPal answered.
* One order owns several PayPal transactions (authorization, capture,
  refunds), so matching is per transaction id, never "first hit per order".
* The report says four different things: matched, PayPal-only, local-only
  and unsettled (our writes whose outcome PayPal has not confirmed yet).
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.utils import timezone
from paypal.core import UnsetType
from paypal.models import Money, SearchResponse, TransactionInformation

from .errors import BadRequest, ProviderUnavailable
from .models import ProviderWrite
from .paypal_client import get_client
from .safe_write import parse_time
from .services import _call

# search_transactions: "The maximum supported range is 31 days".
MAX_WINDOW = timedelta(days=31) - timedelta(seconds=1)
PAGE_SIZE = 100
MAX_PAGES_PER_WINDOW = 1000
# search_transactions: "It takes a maximum of three hours for executed
# transactions to appear in the list transactions call."
REPORTING_LAG = timedelta(hours=3)
MAX_RANGE = timedelta(days=3 * 366)  # PayPal lists the previous three years

RECONCILED_KINDS = [
    ProviderWrite.AUTHORIZE,
    ProviderWrite.REAUTHORIZE,
    ProviderWrite.CAPTURE,
    ProviderWrite.REFUND,
]


def parse_range(raw_from: str | None, raw_to: str | None) -> tuple[datetime, datetime]:
    if not raw_from or not raw_to:
        raise BadRequest('"from" and "to" are required ISO-8601 date-times.')
    start, end = _parse_iso(raw_from, "from"), _parse_iso(raw_to, "to")
    if end <= start:
        raise BadRequest('"to" must be later than "from".')
    if end - start > MAX_RANGE:
        raise BadRequest("The range may cover at most three years.")
    return start, end


def _parse_iso(raw: str, name: str) -> datetime:
    parsed = parse_time(raw.strip().replace(" ", "+"))  # a '+' may arrive URL-decoded as ' '
    if parsed is None:
        raise BadRequest(f'"{name}" is not an ISO-8601 date-time.')
    return parsed.astimezone(dt_timezone.utc)


def _set(value: object) -> Any:
    return None if isinstance(value, UnsetType) else value


def _money(m: object) -> dict[str, str] | None:
    if isinstance(m, Money):
        return {"value": m.value, "currency": m.currency_code}
    return None


def _wire(dt: datetime) -> str:
    return dt.astimezone(dt_timezone.utc).isoformat(timespec="seconds")


def fetch_provider_transactions(start: datetime, end: datetime) -> tuple[list[TransactionInformation], int]:
    """Every transaction PayPal reports for [start, end], across windows and pages."""
    client = get_client()
    found: list[TransactionInformation] = []
    pages_fetched = 0
    window_start = start
    while window_start < end:
        window_end = min(window_start + MAX_WINDOW, end)
        page = 1
        while True:
            response: SearchResponse = _call(
                lambda: client.transaction_search.search_transactions(
                    _wire(window_start),
                    _wire(window_end),
                    fields="transaction_info",
                    balance_affecting_records_only="N",
                    page_size=PAGE_SIZE,
                    page=page,
                )
            )
            pages_fetched += 1
            for detail in _set(response.transaction_details) or []:
                info = _set(detail.transaction_info)
                if info is not None:
                    found.append(info)
            total_pages = _set(response.total_pages)
            if not isinstance(total_pages, int) or page >= total_pages:
                break
            if page >= MAX_PAGES_PER_WINDOW:
                raise ProviderUnavailable(
                    "PayPal reports more transactions than one report can hold; use a shorter range.",
                    status_code=422,
                    code="range_too_large",
                )
            page += 1
        window_start = window_end
    return found, pages_fetched


def _abs_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return abs(Decimal(value))
    except InvalidOperation:
        return None


def _describe_transaction(info: TransactionInformation) -> dict[str, Any]:
    return {
        "transactionId": _set(info.transaction_id),
        "eventCode": _set(info.transaction_event_code),
        "status": _set(info.transaction_status),
        "initiatedAt": _set(info.transaction_initiation_date),
        "amount": _money(_set(info.transaction_amount)),
        "fee": _money(_set(info.fee_amount)),
        "invoiceId": _set(info.invoice_id),
        "customField": _set(info.custom_field),
        "referenceId": _set(info.paypal_reference_id),
    }


def _describe_write(write: ProviderWrite) -> dict[str, Any]:
    return {
        "orderId": write.order.number if write.order is not None else None,
        "kind": write.kind,
        "paypalId": write.provider_id or None,
        "status": write.provider_status or None,
        "outcome": write.outcome,
        "amount": {"value": str(write.amount), "currency": write.currency} if write.amount is not None else None,
        "providerTime": write.provider_time.isoformat() if write.provider_time else None,
        "requestedAt": write.claimed_at.isoformat(),
        "reference": write.reference,
    }


def build_report(start: datetime, end: datetime) -> dict[str, Any]:
    fetched, pages = fetch_provider_transactions(start, end)
    # The exact range, on PayPal's clock.
    provider: list[TransactionInformation] = []
    for info in fetched:
        initiated = parse_time(_set(info.transaction_initiation_date))
        if initiated is None or start <= initiated <= end:
            provider.append(info)

    local = list(
        ProviderWrite.objects.select_related("order")
        .filter(kind__in=RECONCILED_KINDS, provider_time__gte=start, provider_time__lte=end)
        .exclude(provider_id="")
    )
    unsettled = list(
        ProviderWrite.objects.select_related("order").filter(
            kind__in=RECONCILED_KINDS,
            outcome__in=[ProviderWrite.SENDING, ProviderWrite.UNKNOWN],
            provider_time__isnull=True,
            claimed_at__gte=start,
            claimed_at__lte=end,
        )
    )
    by_id: dict[str, ProviderWrite] = {w.provider_id: w for w in local}
    matched_ids: set[str] = set()
    matched: list[dict[str, Any]] = []
    provider_only: list[dict[str, Any]] = []
    prefix = f"{settings.PAYPAL_REFERENCE_PREFIX}-"

    for info in provider:
        txn_id = _set(info.transaction_id)
        write = by_id.get(txn_id) if isinstance(txn_id, str) else None
        if write is None:
            entry = _describe_transaction(info)
            tagged = [v for v in (_set(info.custom_field), _set(info.invoice_id)) if isinstance(v, str)]
            entry["carriesThisSitesReference"] = any(v.startswith(prefix) for v in tagged)
            provider_only.append(entry)
            continue
        matched_ids.add(write.provider_id)
        paypal_amount = _abs_decimal((_money(_set(info.transaction_amount)) or {}).get("value"))
        matched.append(
            {
                "paypal": _describe_transaction(info),
                "local": _describe_write(write),
                "amountMatches": paypal_amount is not None and paypal_amount == write.amount,
            }
        )

    now = timezone.now()
    local_only = []
    for write in local:
        if write.provider_id in matched_ids:
            continue
        entry = _describe_write(write)
        entry["withinReportingLag"] = bool(write.provider_time and write.provider_time > now - REPORTING_LAG)
        local_only.append(entry)

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypal": {"transactionsInRange": len(provider), "pagesFetched": pages},
        "summary": {
            "matched": len(matched),
            "paypalOnly": len(provider_only),
            "localOnly": len(local_only),
            "unsettled": len(unsettled),
            "amountMismatches": sum(1 for m in matched if not m["amountMatches"]),
        },
        "note": (
            "PayPal's reporting can lag live activity by up to three hours; local-only entries "
            "marked withinReportingLag may simply not be reported yet."
        ),
        "matched": matched,
        "paypalOnly": provider_only,
        "localOnly": local_only,
        "unsettled": [_describe_write(w) for w in unsettled],
    }


def reconcile(raw_from: str | None, raw_to: str | None) -> dict[str, Any]:
    start, end = parse_range(raw_from, raw_to)
    return build_report(start, end)
