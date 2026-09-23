"""
Reconciliation: PayPal's own record of transactions for a window, lined up
against the PayPal records this app holds.

Both sides are filtered on the same clock - PayPal's timestamps (the
transaction's initiation date on PayPal's side, the provider time stored on
each local operation) - and an order keeps *all* of its PayPal records
(authorization, capture, refunds) when matching.
"""

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.db.models import Q
from django.utils import timezone
from paypal.core import UnsetType
from paypal.models import SearchResponse, TransactionInformation

from . import gateway, money
from .errors import ApiProblem
from .gateway import PayPalError, ProviderUnavailable
from .models import OrderPayment, PaymentOperation

# PayPal limits one search to 31 days and looks back three years.
MAX_WINDOW = timedelta(days=31)
MAX_RANGE = timedelta(days=3 * 366)
logger = logging.getLogger(__name__)

# Page loop bound per window; hitting it marks the report truncated.
MAX_PAGES_PER_WINDOW = 200
# Accepted by the sandbox (the maximum is undocumented); fewer, larger pages.
PAGE_SIZE = 500
# Searches are reads, so transient failures are retried with backoff.
RETRY_DELAYS = (1.0, 3.0)

# Local operations that correspond to a PayPal transaction record. A void
# changes the authorization's status rather than creating a new record.
RECORD_KINDS = (
    PaymentOperation.AUTHORIZE,
    PaymentOperation.REAUTHORIZE,
    PaymentOperation.CAPTURE,
    PaymentOperation.REFUND,
)
SETTLED = (PaymentOperation.DONE, PaymentOperation.PENDING, PaymentOperation.NEEDS_REVIEW)
UNSETTLED = (PaymentOperation.SENDING, PaymentOperation.UNKNOWN)


def parse_instant(raw: str | None, name: str) -> datetime:
    if not raw:
        raise ApiProblem(400, "invalid_request", f"'{name}' is required (ISO-8601 date-time).")
    try:
        value = datetime.fromisoformat(raw.strip().replace(" ", "+"))
    except ValueError:
        raise ApiProblem(400, "invalid_request", f"'{name}' must be an ISO-8601 date-time.") from None
    if timezone.is_naive(value):
        value = value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)


def _fmt(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: object) -> str:
    return "" if value is None or isinstance(value, UnsetType) else str(value)


@dataclass
class ProviderSide:
    transactions: list[TransactionInformation] = field(default_factory=list)
    truncated: bool = False
    last_refreshed: datetime | None = None


def fetch_provider_side(start: datetime, end: datetime) -> ProviderSide:
    """Every PayPal transaction initiated in [start, end), across 31-day windows and all pages."""
    client = gateway.get_client()
    side = ProviderSide()
    seen: set[tuple[str, str]] = set()
    window_start = start
    while window_start < end:
        window_end = min(window_start + MAX_WINDOW, end)
        page = 1
        for _ in range(MAX_PAGES_PER_WINDOW):
            current_page = page
            response: SearchResponse = _read_with_retry(
                lambda: client.transaction_search.search_transactions(
                    _fmt(window_start),
                    _fmt(window_end),
                    balance_affecting_records_only="N",
                    page_size=PAGE_SIZE,
                    page=current_page,
                )
            )
            refreshed = _parse_provider_time(response.last_refreshed_datetime)
            if refreshed and (side.last_refreshed is None or refreshed < side.last_refreshed):
                side.last_refreshed = refreshed
            details = [] if isinstance(response.transaction_details, UnsetType) else response.transaction_details
            for detail in details:
                if isinstance(detail.transaction_info, UnsetType):
                    continue
                info = detail.transaction_info
                key = (_text(info.transaction_id), _text(info.transaction_event_code))
                if key in seen:
                    continue
                seen.add(key)
                initiated = _parse_provider_time(info.transaction_initiation_date)
                # The request is window-aligned; the report is the caller's exact range.
                if initiated is None or start <= initiated < end:
                    side.transactions.append(info)
            total_pages = response.total_pages if isinstance(response.total_pages, int) else 1
            if page >= total_pages or not details:
                break
            page += 1
        else:
            side.truncated = True
        window_start = window_end
    return side


def _read_with_retry(fn: Callable[[], SearchResponse]) -> SearchResponse:
    """A read-only call: retry transient failures (5xx, 429, network) a bounded number of times."""
    for delay in RETRY_DELAYS:
        try:
            return gateway.call(fn)
        except ProviderUnavailable as e:
            logger.warning("PayPal transaction search failed (%s); retrying in %.0fs", e.message, delay)
            time.sleep(delay)
    return gateway.call(fn)


def _parse_provider_time(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def _provider_row(info: TransactionInformation) -> dict[str, Any]:
    amount = money.from_money(info.transaction_amount)
    fee = money.from_money(info.fee_amount)
    return {
        "transactionId": _text(info.transaction_id),
        "eventCode": _text(info.transaction_event_code),
        "status": _text(info.transaction_status),
        "initiatedAt": _text(info.transaction_initiation_date),
        "amount": str(amount[0]) if amount else None,
        "fee": str(fee[0]) if fee else None,
        "currency": amount[1] if amount else None,
        "invoiceId": _text(info.invoice_id) or None,
        "customField": _text(info.custom_field) or None,
        "referenceId": _text(info.paypal_reference_id) or None,
    }


def _local_row(op: PaymentOperation) -> dict[str, Any]:
    payment = op.order_payment
    currency = payment.currency if payment else ""
    return {
        "kind": op.kind,
        "paypalId": op.provider_id or None,
        "status": op.status,
        "paypalStatus": op.provider_status or None,
        "at": op.provider_time.isoformat() if op.provider_time else None,
        "amount": money.to_str(op.amount, currency) if op.amount is not None and currency else None,
        "reference": str(op.request_id),
    }


def build_report(start: datetime, end: datetime) -> dict[str, Any]:
    if end <= start:
        raise ApiProblem(400, "invalid_request", "'to' must be after 'from'.")
    if end - start > MAX_RANGE:
        raise ApiProblem(400, "invalid_request", "The range may span at most three years.")

    try:
        provider = fetch_provider_side(start, end)
    except PayPalError as e:
        raise ApiProblem(e.status_code if e.status_code >= 500 else 502, "paypal_unavailable",
                         e.message, **e.details()) from e

    ops = list(
        PaymentOperation.objects.select_related("order_payment__order")
        .filter(kind__in=RECORD_KINDS, order_payment__isnull=False)
        .filter(
            Q(status__in=SETTLED, provider_time__gte=start, provider_time__lt=end)
            | Q(status__in=UNSETTLED, date_created__gte=start, date_created__lt=end)
        )
        .exclude(provider_id="", status__in=SETTLED)
    )
    local = [op for op in ops if op.status in SETTLED]
    unsettled = [op for op in ops if op.status in UNSETTLED]

    # Every PayPal id this app knows about (in any window) -> its order, so a
    # provider record just outside the local window is not called "unknown".
    known_ids = dict(
        PaymentOperation.objects.filter(kind__in=RECORD_KINDS)
        .exclude(provider_id="")
        .values_list("provider_id", "order_payment__order__number")
    )

    hinted = {
        candidate
        for info in provider.transactions
        for candidate in (_text(info.custom_field), _text(info.invoice_id).split("-", 1)[0])
        if candidate
    }
    local_numbers = set(
        OrderPayment.objects.filter(order__number__in=hinted).values_list("order__number", flat=True)
    )

    by_id: dict[str, list[TransactionInformation]] = defaultdict(list)
    for info in provider.transactions:
        by_id[_text(info.transaction_id)].append(info)

    orders: dict[str, dict[str, Any]] = {}
    local_only: list[dict[str, Any]] = []
    not_yet_reported: list[dict[str, Any]] = []
    for op in local:
        number = _order_number(op)
        entry = orders.setdefault(number, {"orderId": number, "local": [], "paypal": []})
        entry["local"].append(_local_row(op))
        matches = by_id.pop(op.provider_id, [])
        if matches:
            entry["paypal"].extend(_provider_row(info) for info in matches)
            continue
        row = {"orderId": number, **_local_row(op)}
        if provider.last_refreshed and op.provider_time and op.provider_time > provider.last_refreshed:
            not_yet_reported.append(row)  # PayPal's reporting has not caught up yet
        else:
            local_only.append(row)

    provider_only: list[dict[str, Any]] = []
    for transaction_id, infos in by_id.items():
        for info in infos:
            row = _provider_row(info)
            related = known_ids.get(transaction_id) or _order_hint(info, local_numbers)
            if transaction_id in known_ids and related in orders:
                orders[related]["paypal"].append(row)  # ours, recorded outside the window
                continue
            if related:
                row["relatedOrderId"] = related
            provider_only.append(row)

    matched = [entry for entry in orders.values() if entry["paypal"]]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "complete": not provider.truncated,
        "truncated": provider.truncated,
        "paypalDataRefreshedAt": provider.last_refreshed.isoformat() if provider.last_refreshed else None,
        "summary": {
            "paypalTransactions": len(provider.transactions),
            "matchedOrders": len(matched),
            "paypalOnly": len(provider_only),
            "localOnly": len(local_only),
            "notYetReportedByPayPal": len(not_yet_reported),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "paypalOnly": provider_only,
        "localOnly": local_only,
        "notYetReportedByPayPal": not_yet_reported,
        "unsettled": [
            {"orderId": _order_number(op), **_local_row(op)} for op in unsettled
        ],
    }


def _order_number(op: PaymentOperation) -> str:
    payment = op.order_payment
    return str(payment.order.number) if payment is not None else ""


def _order_hint(info: TransactionInformation, local_numbers: set[str]) -> str | None:
    """
    Our order number from the custom/invoice fields this app sets on every
    PayPal order - only when such an order exists here.
    """
    for candidate in (_text(info.custom_field), _text(info.invoice_id).split("-", 1)[0]):
        if candidate and candidate in local_numbers:
            return candidate
    return None
