"""
Reconciliation: PayPal's own transaction records for a date range, lined up
against the provider ids this app recorded.

* The whole range is covered: it is split into windows PayPal accepts (at most
  31 days each) and every page of every window is read.
* Both sides are filtered on PayPal's clock: the transaction's initiation
  time on PayPal's side, and on ours the provider time recorded when each
  write settled - never when our row was created.
* One order owns every PayPal record it produced (authorization, capture,
  refunds), so matching is by the full set of ids, never first-hit.
"""
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any

from paypal.core import ApiError
from paypal.models import TransactionInformation

from . import statuses
from .gateway import get_client, read_with_retry
from .models import PaymentOperation
from .orders import reference_prefix

MAX_WINDOW = timedelta(days=31)  # search_transactions: "The maximum supported range is 31 days"
MAX_RANGE = timedelta(days=3 * 365)  # the reporting API lists the previous three years
PAGE_SIZE = 500

# Steps whose provider id is a PayPal transaction of its own. A void's id is
# the authorization it voided; a vault token is not a transaction.
MONEY_KINDS = (PaymentOperation.PAY, PaymentOperation.AUTHORIZE, PaymentOperation.REAUTHORIZE,
               PaymentOperation.CAPTURE, PaymentOperation.REFUND)


def _wire_time(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class FeedStatus:
    """What PayPal said about the data behind a report."""

    refreshed_at: str = ""  # PayPal's last_refreshed_datetime
    # Windows PayPal has not processed yet (reporting lags live activity):
    # [start, end) pairs. Not an error, and not "no transactions" either.
    not_yet_available: list[tuple[str, str]] = field(default_factory=list)


def iterate_transactions(
    start: datetime, end: datetime, status: FeedStatus | None = None
) -> Iterator[TransactionInformation]:
    """Every PayPal transaction initiated in [start, end), all windows, all pages."""
    client = get_client()
    seen: set[tuple[str, str, str]] = set()
    cursor = start.replace(microsecond=0)  # PayPal takes whole seconds: widen, then narrow below
    while cursor < end:
        window_end = min(cursor + MAX_WINDOW, end)
        query_end = window_end if window_end.microsecond == 0 else window_end.replace(microsecond=0) + timedelta(seconds=1)
        page = 1
        while True:
            try:
                response = read_with_retry(
                    lambda: client.transaction_search.search_transactions(
                        _wire_time(cursor), _wire_time(query_end),
                        page_size=PAGE_SIZE, page=page, balance_affecting_records_only="N",
                    )
                )
            except ApiError as exc:
                # Seen in the sandbox: a window starting after PayPal's last
                # refresh answers 404 "Data for the given start date is not
                # available." That is the reporting lag, not a failure.
                if exc.status_code != 404:
                    raise
                if status is not None:
                    status.not_yet_available.append((_wire_time(cursor), _wire_time(window_end)))
                break
            if status is not None and isinstance(response.last_refreshed_datetime, str):
                status.refreshed_at = response.last_refreshed_datetime
            details = response.transaction_details if isinstance(response.transaction_details, list) else []
            for detail in details:
                info = detail.transaction_info
                if not isinstance(info, TransactionInformation):
                    continue
                initiated = statuses.parse_time(info.transaction_initiation_date)
                if initiated is None or not start <= initiated < end:
                    continue  # the query was widened to whole seconds; the report is not
                key = (statuses.text(info.transaction_id), statuses.text(info.transaction_event_code),
                       statuses.text(info.transaction_initiation_date))
                if key in seen:
                    continue  # a record on the boundary of two windows
                seen.add(key)
                yield info
            total_pages = response.total_pages if isinstance(response.total_pages, int) else 1
            if page >= total_pages:
                break
            page += 1
        cursor = window_end


def _paypal_row(info: TransactionInformation) -> dict[str, Any]:
    amount, currency = statuses.money(info.transaction_amount)
    fee, _ = statuses.money(info.fee_amount)
    return {
        "transactionId": statuses.text(info.transaction_id),
        "eventCode": statuses.text(info.transaction_event_code),
        "status": statuses.text(info.transaction_status),
        "initiatedAt": statuses.text(info.transaction_initiation_date),
        "amount": str(amount) if amount is not None else None,
        "currency": currency,
        "fee": str(fee) if fee is not None else None,
        "invoiceId": statuses.text(info.invoice_id),
        "customId": statuses.text(info.custom_field),
        "referenceId": statuses.text(info.paypal_reference_id),
    }


def _order_number(op: PaymentOperation) -> str:
    return str(op.payment.order.number) if op.payment is not None else ""


def _app_row(op: PaymentOperation) -> dict[str, Any]:
    return {
        "orderId": _order_number(op) or None,
        "step": op.kind,
        "paypalId": op.provider_id,
        "paypalStatus": op.provider_status,
        "outcome": op.outcome,
        "amount": str(op.amount) if op.amount is not None else None,
        "currency": op.currency,
        "paypalTime": op.provider_time.isoformat() if op.provider_time else None,
        "reference": op.reference,
    }


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    prefix = reference_prefix()

    # App side, on PayPal's clock.
    local_ops = list(
        PaymentOperation.objects.filter(kind__in=MONEY_KINDS, provider_time__gte=start, provider_time__lt=end)
        .exclude(provider_id="").select_related("payment__order")
    )
    by_id: dict[str, PaymentOperation] = {}
    for op in local_ops:
        by_id.setdefault(op.provider_id, op)  # a pay and an authorize step can carry the same id
    unsettled = list(
        PaymentOperation.objects.filter(
            kind__in=MONEY_KINDS, provider_time__isnull=True, claimed_at__gte=start, claimed_at__lt=end,
            outcome__in=(PaymentOperation.SENDING, PaymentOperation.UNKNOWN, PaymentOperation.PENDING),
        ).select_related("payment__order")
    )

    # PayPal side, and matching against the set.
    matched: dict[str, dict[str, Any]] = {}
    matched_ids: set[str] = set()
    ours_unrecorded: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    total = 0
    feed = FeedStatus()
    for info in iterate_transactions(start, end, feed):
        total += 1
        row = _paypal_row(info)
        # By its own id (authorization, capture, refund rows), or through the
        # id it references (e.g. the void event of one of our authorizations).
        owner = by_id.get(row["transactionId"]) or by_id.get(row["referenceId"])
        if owner is not None:
            order_id = _order_number(owner)
            group = matched.setdefault(order_id, {"orderId": order_id, "paypal": [], "app": []})
            group["paypal"].append(row)
            matched_ids.add(owner.provider_id)
        elif row["invoiceId"].startswith(prefix + "-") or row["customId"].startswith(prefix + "-"):
            ours_unrecorded.append(row)  # carries our reference, but we hold no record of this id
        else:
            foreign.append(row)

    app_only = []
    for op in local_ops:
        if op.provider_id in matched_ids:
            matched[_order_number(op)]["app"].append(_app_row(op))
        else:
            app_only.append(_app_row(op))

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "summary": {
            "paypalTransactions": total,
            "matchedOrders": len(matched),
            "paypalOnlyWithOurReference": len(ours_unrecorded),
            "paypalOnlyOther": len(foreign),
            "appOnly": len(app_only),
            "unsettled": len(unsettled),
        },
        "matched": list(matched.values()),
        "paypalOnly": {"withOurReference": ours_unrecorded, "other": foreign},
        "appOnly": app_only,
        "unsettled": [_app_row(op) for op in unsettled],
        "paypalDataRefreshedAt": feed.refreshed_at or None,
        "paypalDataNotYetAvailable": [{"from": a, "to": b} for a, b in feed.not_yet_available],
        "note": (
            "PayPal's reporting lags live activity by up to three hours, so recent payments may appear "
            "under appOnly (or in a window PayPal has not processed yet) until PayPal reports them."
        ),
    }
