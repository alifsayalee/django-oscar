"""
Line PayPal's transaction report up against this app's orders.

* PayPal side: every page of every ≤31-day window, narrowed back to [from, to).
* App side: filtered on *PayPal's* timestamps we recorded (authorization,
  capture, refund ``create_time``), never on our own ``date_created``.
* Matching is against the set of every PayPal id an order owns, so an order
  with an authorization, a capture and refunds matches all of them; records
  carrying one of our payments' unique ``custom_id`` in ``custom_field`` but an
  id we do not know are still attributed to that order.
"""

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from django.db.models import Q

from . import money, paypal_calls
from .models import PaymentState, PayPalPayment, PayPalRefund, RefundState

UNSETTLED_STATES = [
    PaymentState.AUTHORIZING,
    PaymentState.AUTHORIZATION_UNKNOWN,
    PaymentState.CAPTURING,
    PaymentState.CAPTURE_UNKNOWN,
    PaymentState.VOIDING,
    PaymentState.VOID_UNKNOWN,
    PaymentState.NEEDS_REVIEW,
]


def _in(ts, start, end):
    return ts is not None and start <= ts < end


def _dec(value):
    return None if value is None else str(value)


def _local_records():
    """Every PayPal record the app knows of, keyed by PayPal id."""
    records = {}
    payments = PayPalPayment.objects.select_related("order").exclude(
        authorization_id="", capture_id=""
    )
    for p in payments.prefetch_related("refunds"):
        number = p.order.number
        for auth_id in list(p.previous_authorization_ids or []):
            records[auth_id] = {
                "orderId": number,
                "currency": p.currency,
                "kind": "authorization",
                "paypalId": auth_id,
                "time": None,
                "amount": p.amount,
            }
        if p.authorization_id:
            records[p.authorization_id] = {
                "orderId": number,
                "currency": p.currency,
                "kind": "authorization",
                "paypalId": p.authorization_id,
                "time": p.authorized_at,
                "amount": p.authorized_amount,
            }
        if p.original_authorized_at and p.previous_authorization_ids:
            records[p.previous_authorization_ids[0]]["time"] = p.original_authorized_at
        if p.capture_id:
            records[p.capture_id] = {
                "orderId": number,
                "currency": p.currency,
                "kind": "capture",
                "paypalId": p.capture_id,
                "time": p.captured_at,
                "amount": p.captured_amount,
            }
        for r in p.refunds.all():
            if r.paypal_refund_id:
                records[r.paypal_refund_id] = {
                    "orderId": number,
                    "currency": p.currency,
                    "kind": "refund",
                    "paypalId": r.paypal_refund_id,
                    "time": r.refunded_at,
                    "amount": r.amount,
                }
    return records


def _provider_dict(txn):
    return {
        "transactionId": txn.transaction_id,
        "eventCode": txn.event_code,
        "status": txn.status,
        "initiatedAt": txn.initiated_at.isoformat() if txn.initiated_at else None,
        "amount": _dec(txn.amount[0]) if txn.amount else None,
        "currency": txn.amount[1] if txn.amount else None,
        "fee": _dec(txn.fee[0]) if txn.fee else None,
        "customField": txn.custom_field,
        "invoiceId": txn.invoice_id,
    }


def build_report(start, end):
    # PayPal has nothing after "now"; never ask it about the future.
    search = paypal_calls.search_transactions(
        start, min(end, datetime.now(timezone.utc))
    )
    # The search is window-granular; the report is not. Narrow back to [start, end).
    provider = [t for t in search.transactions if _in(t.initiated_at, start, end)]

    local = _local_records()
    by_custom_id = {
        p.custom_id: p.order.number
        for p in PayPalPayment.objects.select_related("order")
    }

    matched = defaultdict(list)
    provider_only = []
    seen_ids = set()
    unmatched = []
    # Pass 1: exact PayPal ids. Pass 2 (below) only sees what pass 1 left.
    for txn in provider:
        record = local.get(txn.transaction_id)
        if record is None:
            unmatched.append(txn)
            continue
        entry = _provider_dict(txn)
        seen_ids.add(txn.transaction_id)
        entry["matchedBy"] = "paypalId"
        entry["kind"] = record["kind"]
        if txn.amount and record["amount"] is not None:
            entry["amountMatches"] = abs(txn.amount[0]) == Decimal(record["amount"])
        matched[record["orderId"]].append(entry)
    for txn in unmatched:
        entry = _provider_dict(txn)
        number = by_custom_id.get(txn.custom_field or "")
        if number is None:
            provider_only.append(entry)
            continue
        # Ours (custom_id is unique to one of our payments) under an id we did not
        # record, e.g. a reporting id that differs from the API id. Pair it with one
        # unmatched record of that order with the same amount, when there is one.
        entry["matchedBy"] = "customId"
        for paypal_id, candidate in local.items():
            if (
                paypal_id not in seen_ids
                and candidate["orderId"] == number
                and txn.amount
                and candidate["amount"] is not None
                and abs(txn.amount[0]) == Decimal(candidate["amount"])
            ):
                seen_ids.add(paypal_id)
                entry.update(kind=candidate["kind"], appPaypalId=paypal_id)
                break
        matched[number].append(entry)

    app_only, not_yet_reported = [], []
    refreshed = search.last_refreshed_at
    for paypal_id, record in local.items():
        if paypal_id in seen_ids or not _in(record["time"], start, end):
            continue
        item = {
            "orderId": record["orderId"],
            "kind": record["kind"],
            "paypalId": paypal_id,
            "time": record["time"].isoformat(),
            "amount": (
                None
                if record["amount"] is None
                else money.to_wire(record["amount"], record["currency"])
            ),
            "currency": record["currency"],
        }
        if refreshed is None or record["time"] > refreshed:
            not_yet_reported.append(item)  # PayPal's report has not caught up yet
        else:
            app_only.append(item)

    unsettled = [
        {
            "orderId": p.order.number,
            "state": p.state,
            "lastError": p.last_error,
            "updatedAt": p.date_updated.isoformat(),
        }
        for p in PayPalPayment.objects.select_related("order").filter(
            state__in=UNSETTLED_STATES, date_updated__gte=start, date_updated__lt=end
        )
    ] + [
        {
            "orderId": r.payment.order.number,
            "refundId": str(r.id),
            "state": r.state,
            "lastError": r.last_error,
            "updatedAt": r.date_updated.isoformat(),
        }
        for r in PayPalRefund.objects.select_related("payment__order").filter(
            Q(
                state__in=[
                    RefundState.SENDING,
                    RefundState.UNKNOWN,
                    RefundState.PENDING,
                ]
            ),
            date_updated__gte=start,
            date_updated__lt=end,
        )
    ]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalLastRefreshed": refreshed.isoformat() if refreshed else None,
        "complete": not search.truncated,
        "truncated": search.truncated,
        "pagesFetched": search.pages_fetched,
        "summary": {
            "paypalTransactions": len(provider),
            "matchedOrders": len(matched),
            "paypalOnly": len(provider_only),
            "appOnly": len(app_only),
            "notYetReported": len(not_yet_reported),
            "unsettled": len(unsettled),
        },
        "matched": [
            {"orderId": number, "transactions": txns}
            for number, txns in sorted(matched.items())
        ],
        "paypalOnly": provider_only,
        "appOnly": app_only,
        "notYetReported": not_yet_reported,
        "unsettled": unsettled,
    }


def parse_range(raw_from, raw_to):
    """ISO-8601 date-times; naive values are taken as UTC. Returns (start, end) or raises ValueError."""

    def parse(value, name):
        if not value:
            raise ValueError("%s is required (ISO-8601 date-time)" % name)
        parsed = paypal_calls.parse_time(
            value.replace(" ", "+")
        )  # '+' decoded to ' ' in query strings
        if parsed is None:
            raise ValueError("%s is not an ISO-8601 date-time" % name)
        return parsed

    start, end = parse(raw_from, "from"), parse(raw_to, "to")
    if start >= end:
        raise ValueError("from must be before to")
    if (end - start).days > 3 * 366:
        raise ValueError(
            "The range may not exceed three years (PayPal reports only the last three years)"
        )
    return start, end
