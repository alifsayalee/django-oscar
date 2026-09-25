"""
Line PayPal's own transaction records up against this app's payments.

* PayPal's search accepts at most 31 days per call, so the range is split into
  windows, and every page of every window is read.
* Both sides are filtered on PayPal's clock: the provider's transaction date, and
  the provider time stored with each of our operations.
* One order owns several PayPal transactions (authorization, capture, refunds),
  so matching is against the set, not the first hit.
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

from django.db.models import Q
from paypal.core import UnsetType
from paypal.models import SearchResponse, TransactionInformation

from .gateway import get_client
from .models import Outcome, PayPalOperation

MAX_WINDOW = timedelta(days=31)
MAX_PAGES_PER_WINDOW = 1000
# PayPal rejects a page_size above 500.
PAGE_SIZE = 500
PAGE_CONCURRENCY = 4
MONEY_KINDS = [
    PayPalOperation.AUTHORIZE,
    PayPalOperation.REAUTHORIZE,
    PayPalOperation.CAPTURE,
    PayPalOperation.VOID,
    PayPalOperation.REFUND,
]


@dataclass(frozen=True)
class ProviderTxn:
    transaction_id: str
    reference_id: str
    event_code: str
    status: str
    initiated_at: datetime | None
    amount: str | None
    fee: str | None
    currency: str | None
    invoice_id: str
    custom_field: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "transactionId": self.transaction_id,
            "paypalReferenceId": self.reference_id or None,
            "eventCode": self.event_code,
            "status": self.status,
            "initiatedAt": self.initiated_at.isoformat() if self.initiated_at else None,
            "amount": self.amount,
            "fee": self.fee,
            "currency": self.currency,
            "invoiceId": self.invoice_id or None,
            "customField": self.custom_field or None,
        }


def _s(value: object) -> str:
    return "" if isinstance(value, UnsetType) or value is None else str(value)


def _parse_time(value: object) -> datetime | None:
    text = _s(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _txn(info: TransactionInformation) -> ProviderTxn:
    amount = info.transaction_amount
    fee = info.fee_amount
    return ProviderTxn(
        transaction_id=_s(info.transaction_id),
        reference_id=_s(info.paypal_reference_id),
        event_code=_s(info.transaction_event_code),
        status=_s(info.transaction_status),
        initiated_at=_parse_time(info.transaction_initiation_date),
        amount=None if isinstance(amount, UnsetType) else amount.value,
        fee=None if isinstance(fee, UnsetType) else fee.value,
        currency=None if isinstance(amount, UnsetType) else amount.currency_code,
        invoice_id=_s(info.invoice_id),
        custom_field=_s(info.custom_field),
    )


def _rfc3339(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _search_page(window_start: datetime, query_end: datetime, page: int) -> SearchResponse:
    return get_client().transaction_search.search_transactions(
        _rfc3339(window_start),
        _rfc3339(query_end),
        fields="transaction_info",
        balance_affecting_records_only="N",  # include authorizations and voids
        page_size=PAGE_SIZE,
        page=page,
    )


def _collect(response: SearchResponse, found: dict[tuple[str, str], ProviderTxn]) -> None:
    details = [] if isinstance(response.transaction_details, UnsetType) else response.transaction_details
    for detail in details:
        if isinstance(detail.transaction_info, UnsetType):
            continue
        txn = _txn(detail.transaction_info)
        if txn.transaction_id:
            found[(txn.transaction_id, txn.event_code)] = txn


def fetch_provider_transactions(start: datetime, end: datetime) -> list[ProviderTxn]:
    """Every PayPal transaction in [start, end), across all windows and pages."""
    found: dict[tuple[str, str], ProviderTxn] = {}
    window_start = start.replace(microsecond=0)
    with ThreadPoolExecutor(max_workers=PAGE_CONCURRENCY) as pool:
        while window_start < end:
            window_end = min(window_start + MAX_WINDOW, end)
            # Seconds are required by the search, so round the end up to the next second.
            query_end = window_end.replace(microsecond=0) + (
                timedelta(seconds=1) if window_end.microsecond else timedelta()
            )
            first = _search_page(window_start, query_end, 1)
            _collect(first, found)
            total_pages = 0 if isinstance(first.total_pages, UnsetType) else first.total_pages
            if total_pages > MAX_PAGES_PER_WINDOW:
                raise ValueError(f"PayPal reports {total_pages} pages for one window; narrow the range.")
            pages = range(2, total_pages + 1)
            for response in pool.map(partial(_search_page, window_start, query_end), pages):
                _collect(response, found)
            window_start = window_end
    # The query was widened to whole seconds: narrow back to the caller's instants.
    return [t for t in found.values() if t.initiated_at is not None and start <= t.initiated_at < end]


def build_report(start: datetime, end: datetime, prefix: str) -> dict[str, Any]:
    provider = fetch_provider_transactions(start, end)

    # Our side, on the same clock: provider time stored when each write completed.
    local_ops = list(
        PayPalOperation.objects.filter(
            kind__in=MONEY_KINDS, outcome=Outcome.DONE, provider_time__gte=start, provider_time__lt=end
        ).select_related("order")
    )
    unsettled = list(
        PayPalOperation.objects.filter(kind__in=MONEY_KINDS + [PayPalOperation.CREATE_ORDER])
        .filter(~Q(outcome__in=[Outcome.DONE, Outcome.FAILED]) | Q(outcome=Outcome.DONE, provider_time__isnull=True))
        .filter(claimed_at__gte=start, claimed_at__lt=end)
        .select_related("order")
    )
    # Authorizations and voids are non-balance-affecting records, keyed by the
    # authorization id; captures and refunds by their own ids.
    reportable = local_ops

    by_id: dict[str, list[ProviderTxn]] = defaultdict(list)
    by_invoice: dict[str, list[ProviderTxn]] = defaultdict(list)
    for t in provider:
        by_id[t.transaction_id].append(t)
        if t.invoice_id.startswith(prefix) or t.custom_field.startswith(prefix):
            by_invoice[t.custom_field or t.invoice_id].append(t)

    claimed: set[tuple[str, str]] = set()
    orders: dict[str, dict[str, Any]] = {}
    local_only: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for op in reportable:
        number = op.order.number if op.order is not None else ""
        entry = orders.setdefault(number, {"orderId": number, "operations": [], "paypalTransactions": []})
        entry["operations"].append(_op_dict(op))
        hits = by_id.get(op.provider_id, [])
        if not hits:
            local_only.append(_op_dict(op))
            continue
        for t in hits:
            claimed.add((t.transaction_id, t.event_code))
            entry["paypalTransactions"].append(t.as_dict())
            if op.amount is not None and t.amount is not None and abs(Decimal(t.amount)) != op.amount:
                mismatches.append({"orderId": number, "operation": _op_dict(op), "paypal": t.as_dict()})

    # PayPal records carrying this install's reference but no id we recorded
    # (for example a follow-on record PayPal created) are attributed to their order.
    for key, txns in by_invoice.items():
        for t in txns:
            if (t.transaction_id, t.event_code) in claimed:
                continue
            number = key.split(":", 1)[1] if ":" in key else ""
            if number in orders:
                orders[number]["paypalTransactions"].append(t.as_dict())
                claimed.add((t.transaction_id, t.event_code))

    provider_only = [t.as_dict() for t in provider if (t.transaction_id, t.event_code) not in claimed]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalTransactionCount": len(provider),
        "matched": [o for o in orders.values() if o["paypalTransactions"]],
        "localOnly": local_only,
        "paypalOnly": provider_only,
        "unsettled": [_op_dict(op) for op in unsettled],
        "amountMismatches": mismatches,
        "note": "PayPal's transaction reporting can lag live activity by up to three hours.",
    }


def _op_dict(op: PayPalOperation) -> dict[str, Any]:
    return {
        "orderId": op.order.number if op.order is not None else None,
        "kind": op.kind,
        "outcome": op.outcome,
        "paypalId": op.provider_id or None,
        "paypalStatus": op.provider_status or None,
        "paypalTime": op.provider_time.isoformat() if op.provider_time else None,
        "amount": str(op.amount) if op.amount is not None else None,
        "currency": op.currency or None,
    }
