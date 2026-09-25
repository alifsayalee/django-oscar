"""
Line PayPal's transaction records for a period up against this app's own.

* PayPal side: ``transaction_search.search_transactions`` over the whole
  range - split into windows of at most 31 days (the documented maximum)
  and every page of every window - then narrowed back to exactly
  ``[from, to)`` on the transaction's initiation time.
* Local side: the provider writes this app made, filtered on the *provider's*
  event time stored with each outcome, so both sides use the same clock.
* Matching is per order against the set of PayPal ids it owns (every
  authorization, the capture, every refund), plus the order number PayPal
  echoes as ``custom_field`` / ``invoice_id``.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

import httpx
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal.core import ApiError

from .client import get_client
from .errors import ApiProblem, translate
from .models import Outcome, OrderPayment, PaymentRefund, ProviderWrite
from .payflow import _v
from .safe_write import install_prefix

MAX_WINDOW = timedelta(days=31)
MAX_RANGE = timedelta(days=3 * 366)
PAGE_SIZE = 100
MAX_PAGES_PER_WINDOW = 1000

# Local writes that leave a record at PayPal, and the outcomes that mean it exists.
MONEY_OPERATIONS = ('create_order', 'authorize_order', 'reauthorize', 'capture', 'refund')
EXISTS = (Outcome.DONE, Outcome.PENDING, Outcome.NEEDS_REVIEW)
UNSETTLED = (Outcome.SENDING, Outcome.UNKNOWN)


def parse_range(raw_from, raw_to) -> tuple[datetime, datetime]:
    start, end = _parse_instant(raw_from, 'from'), _parse_instant(raw_to, 'to')
    if end <= start:
        raise ApiProblem(400, 'invalid_range', '"to" must be after "from".')
    if end - start > MAX_RANGE:
        raise ApiProblem(400, 'invalid_range', 'The range may cover at most three years.')
    return start, end


def _parse_instant(raw, name) -> datetime:
    value = parse_datetime(raw or '')
    if value is None:
        raise ApiProblem(400, 'invalid_range',
                         '"%s" must be an ISO-8601 date-time, e.g. 2026-09-01T00:00:00Z.' % name)
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)


def _wire_time(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def fetch_provider_records(start: datetime, end: datetime) -> tuple[list[dict], dict]:
    client = get_client()
    records: dict[tuple, dict] = {}
    stats: dict[str, Any] = {'windows': 0, 'pages': 0, 'lastRefreshed': None}
    window_start = start
    while window_start < end:
        window_end = min(window_start + MAX_WINDOW, end)
        stats['windows'] += 1
        page = 1
        while True:
            try:
                response = client.transaction_search.search_transactions(
                    _wire_time(window_start), _wire_time(window_end),
                    balance_affecting_records_only='N', page_size=PAGE_SIZE, page=page)
            except (ApiError, httpx.RequestError, ValueError) as e:
                raise translate(e, action='transaction search') from e
            stats['pages'] += 1
            refreshed = _v(response.last_refreshed_datetime)
            if refreshed and (stats['lastRefreshed'] is None or refreshed < stats['lastRefreshed']):
                stats['lastRefreshed'] = refreshed
            for detail in _v(response.transaction_details) or []:
                info = _v(detail.transaction_info)
                if info is None:
                    continue
                record = _describe(info)
                key = (record['transactionId'], record['eventCode'], record['initiatedAt'],
                       record['status'])
                records[key] = record  # adjacent windows share their boundary instant
            total_pages = _v(response.total_pages) or 1
            if page >= total_pages or page >= MAX_PAGES_PER_WINDOW:
                break
            page += 1
        window_start = window_end
    # The query is inclusive at its ends; the report covers exactly [start, end).
    narrowed = [r for r in records.values()
                if r['_initiated'] is not None and start <= r['_initiated'] < end]
    return narrowed, stats


def _describe(info) -> dict:
    amount = _v(info.transaction_amount)
    fee = _v(info.fee_amount)
    initiated = _v(info.transaction_initiation_date)
    return {
        'transactionId': _v(info.transaction_id),
        'referenceId': _v(info.paypal_reference_id),
        'eventCode': _v(info.transaction_event_code),
        'status': _v(info.transaction_status),
        'initiatedAt': initiated,
        'amount': amount.value if amount is not None else None,
        'currency': amount.currency_code if amount is not None else None,
        'fee': fee.value if fee is not None else None,
        'invoiceId': _v(info.invoice_id),
        'customField': _v(info.custom_field),
        '_initiated': parse_datetime(initiated) if initiated else None,
    }


def _order_ids() -> tuple[dict[str, str], dict[str, str]]:
    """Every PayPal id this app knows, mapped to its order number."""
    by_id: dict[str, str] = {}
    for p in OrderPayment.objects.select_related('order'):
        for pid in (p.paypal_order_id, p.authorization_id, p.capture_id):
            if pid:
                by_id[pid] = p.order.number
    for r in PaymentRefund.objects.select_related('payment__order').exclude(paypal_refund_id=''):
        by_id[r.paypal_refund_id] = r.payment.order.number
    for w in ProviderWrite.objects.select_related('order').exclude(provider_id='').exclude(order=None):
        if w.order is None:
            continue
        by_id[w.provider_id] = w.order.number
        if '-auth-' in w.ref:  # the authorization a renewal / capture / void acted on
            by_id[w.ref.split('-auth-', 1)[1].rsplit('-', 1)[0]] = w.order.number
    # The order reference this install sends as custom_id (and invoice_id stem).
    prefix = install_prefix()
    by_reference = {'%s-%s' % (prefix, number): number
                    for number in OrderPayment.objects.values_list('order__number', flat=True)}
    return by_id, by_reference


def _order_for(record: dict, by_id: dict[str, str], by_reference: dict[str, str]) -> str | None:
    for pid in (record['transactionId'], record['referenceId']):
        if pid and pid in by_id:
            return by_id[pid]
    if record['customField'] in by_reference:
        return by_reference[record['customField']]
    invoice = record['invoiceId'] or ''
    if '-' in invoice and invoice.rsplit('-', 1)[0] in by_reference:
        return by_reference[invoice.rsplit('-', 1)[0]]
    return None


def _event(write: ProviderWrite) -> dict:
    return {
        'orderId': write.order.number if write.order is not None else None,
        'operation': write.operation,
        'paypalId': write.provider_id or None,
        'paypalStatus': write.provider_status or None,
        'outcome': write.outcome,
        'providerTime': write.provider_time.isoformat() if write.provider_time else None,
        'reference': write.ref,
    }


def reconcile(start: datetime, end: datetime) -> dict:
    provider, stats = fetch_provider_records(start, end)
    by_id, by_reference = _order_ids()

    by_order: dict[str, list[dict]] = defaultdict(list)
    provider_only = []
    seen_ids: set[str] = set()
    for record in sorted(provider, key=lambda r: r['_initiated']):
        seen_ids.update(i for i in (record['transactionId'], record['referenceId']) if i)
        public = {k: v for k, v in record.items() if not k.startswith('_')}
        number = _order_for(record, by_id, by_reference)
        if number is None:
            provider_only.append(public)
        else:
            by_order[number].append(public)

    # Local side on the provider's clock.
    writes = ProviderWrite.objects.select_related('order').filter(
        operation__in=MONEY_OPERATIONS, provider_time__gte=start, provider_time__lt=end,
        outcome__in=EXISTS).order_by('provider_time')
    refreshed = parse_datetime(stats['lastRefreshed']) if stats['lastRefreshed'] else None
    local_only, awaiting = [], []
    local_by_order: dict[str, list[dict]] = defaultdict(list)
    for write in writes:
        event = _event(write)
        if write.provider_id in seen_ids:
            local_by_order[event['orderId']].append(event)
        elif (refreshed is not None and write.provider_time is not None
              and write.provider_time > refreshed):
            awaiting.append(event)  # newer than PayPal's reporting data
        else:
            local_only.append(event)

    unsettled = [_event(w) for w in ProviderWrite.objects.select_related('order').filter(
        outcome__in=UNSETTLED, claimed_at__gte=start, claimed_at__lt=end).order_by('claimed_at')]

    matched = [{'orderId': number, 'providerTransactions': txns,
                'localEvents': local_by_order.get(number, [])}
               for number, txns in sorted(by_order.items())]
    return {
        'from': start.isoformat(), 'to': end.isoformat(),
        'providerLastRefreshed': stats['lastRefreshed'],
        'windowsQueried': stats['windows'], 'pagesFetched': stats['pages'],
        'summary': {
            'providerTransactions': len(provider), 'matchedOrders': len(matched),
            'providerOnly': len(provider_only), 'localOnly': len(local_only),
            'awaitingProviderReporting': len(awaiting), 'unsettled': len(unsettled),
        },
        'matched': matched,
        'providerOnly': provider_only,
        'localOnly': local_only,
        'awaitingProviderReporting': awaiting,
        'unsettled': unsettled,
    }
