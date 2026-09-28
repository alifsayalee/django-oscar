"""
PayPal's own transaction record, lined up against this app's writes.

Both sides are filtered on PayPal's clock: PayPal rows by their initiation
date, local writes by the PayPal event time stored when the write completed.
Writes PayPal has not settled yet (no PayPal time) are reported as unsettled,
and local writes newer than PayPal's last reporting refresh as not yet
reported -- PayPal's reporting lags live activity by up to three hours.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Any, Iterator

from paypal import PaypalClient
from paypal.core import ApiError, UnsetType
from paypal.models import PaymentAuthorization, TransactionInformation

from .errors import ApiProblem, paypal_read
from .models import InstallIdentity, ProviderWrite
from .safe_write import provider_time

# Documented maximum range of one transaction search.
MAX_WINDOW = timedelta(days=31)
MAX_RANGE = timedelta(days=366 * 3)
PAGE_SIZE = 100
MAX_PAGES_PER_WINDOW = 1000

RECONCILED_KINDS = (ProviderWrite.AUTHORIZE, ProviderWrite.ORDER_AUTHORIZE, ProviderWrite.REAUTHORIZE,
                    ProviderWrite.CAPTURE, ProviderWrite.VOID, ProviderWrite.REFUND)


def _wire_time(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _windows(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    cursor = start
    while cursor < end:
        upper = min(cursor + MAX_WINDOW - timedelta(seconds=1), end)
        yield cursor, upper
        cursor = upper


def search(client: PaypalClient, start: datetime, end: datetime) -> tuple[list[TransactionInformation], str | None]:
    """Every transaction PayPal reports in [start, end], across windows and pages."""
    rows: list[TransactionInformation] = []
    seen: set[tuple[object, ...]] = set()  # adjacent windows share their boundary second
    refreshed: str | None = None
    for lower, upper in _windows(start, end):
        page = 1
        while True:
            response = client.transaction_search.search_transactions(
                _wire_time(lower), _wire_time(upper), fields='transaction_info',
                balance_affecting_records_only='N', page_size=PAGE_SIZE, page=page)
            if isinstance(response.last_refreshed_datetime, str):
                refreshed = min(refreshed, response.last_refreshed_datetime) if refreshed else \
                    response.last_refreshed_datetime
            details = response.transaction_details if isinstance(response.transaction_details, list) else []
            for d in details:
                info = d.transaction_info
                if isinstance(info, UnsetType):
                    continue
                identity = (info.transaction_id, info.transaction_event_code, info.transaction_initiation_date,
                            info.transaction_status)
                if identity not in seen:
                    seen.add(identity)
                    rows.append(info)
            total_pages = response.total_pages if isinstance(response.total_pages, int) else None
            if total_pages is None:
                if len(details) < PAGE_SIZE:
                    break
            elif page >= total_pages:
                break
            page += 1
            if page > MAX_PAGES_PER_WINDOW:
                raise ApiProblem(422, 'range_too_large',
                                 'PayPal reports more than %d transactions in one 31-day window; '
                                 'use a shorter range.' % (PAGE_SIZE * MAX_PAGES_PER_WINDOW))
    return rows, refreshed


def find_authorization_by_custom_id(client: PaypalClient, custom_id: str,
                                    since: datetime) -> PaymentAuthorization | None:
    """The authorization PayPal made for a create-order carrying ``custom_id``.

    Raises the SDK's errors (the safe write treats them as "still unknown")."""
    now = datetime.now(dt_timezone.utc)
    rows, __ = search(client, since, now + timedelta(minutes=1))
    for row in rows:
        if row.custom_field != custom_id:
            continue
        for candidate in (row.transaction_id, row.paypal_reference_id):
            if not isinstance(candidate, str):
                continue
            try:
                auth = client.payments.get_authorized_payment(candidate)
            except ApiError as e:
                if e.status_code == 404:
                    continue
                raise
            if auth.custom_id == custom_id:
                return auth
    return None


def _money(m: Any) -> tuple[str | None, str | None]:
    if m is None or isinstance(m, UnsetType):
        return None, None
    return m.value, m.currency_code


def _provider_row(row: TransactionInformation, prefix: str) -> dict[str, Any]:
    amount, currency = _money(row.transaction_amount)
    fee, __ = _money(row.fee_amount)
    custom = row.custom_field if isinstance(row.custom_field, str) else None
    return {
        'transactionId': row.transaction_id if isinstance(row.transaction_id, str) else None,
        'referenceId': row.paypal_reference_id if isinstance(row.paypal_reference_id, str) else None,
        'eventCode': row.transaction_event_code if isinstance(row.transaction_event_code, str) else None,
        'status': row.transaction_status if isinstance(row.transaction_status, str) else None,
        'initiatedAt': row.transaction_initiation_date if isinstance(row.transaction_initiation_date, str) else None,
        'amount': amount,
        'fee': fee,
        'currency': currency,
        'customField': custom,
        'sentByThisApp': bool(custom and custom.startswith(prefix + ':')),
    }


def _local_row(write: ProviderWrite) -> dict[str, Any]:
    return {
        'orderId': write.order.number if write.order_id else None,
        'kind': write.kind,
        'paypalId': write.provider_id or None,
        'outcome': write.outcome,
        'paypalStatus': write.provider_status or None,
        'amount': str(write.provider_amount if write.provider_amount is not None else write.amount)
        if (write.provider_amount is not None or write.amount is not None) else None,
        'currency': write.currency or None,
        'paypalTime': write.provider_time.isoformat() if write.provider_time else None,
        'reference': write.ref,
    }


def report(client: PaypalClient, start: datetime, end: datetime) -> dict[str, Any]:
    if end <= start:
        raise ApiProblem(422, 'invalid_range', '"to" must be after "from".')
    if end - start > MAX_RANGE:
        raise ApiProblem(422, 'invalid_range', 'The range may span at most three years.')
    prefix = InstallIdentity.reference_prefix()

    with paypal_read():
        fetched, refreshed = search(client, start, end)
    provider = [r for r in fetched
                if (t := provider_time(r.transaction_initiation_date)) is not None and start <= t < end]

    writes = ProviderWrite.objects.select_related('order').filter(kind__in=RECONCILED_KINDS)
    local = list(writes.filter(provider_time__gte=start, provider_time__lt=end).exclude(provider_id=''))
    unsettled = list(writes.filter(claimed_at__gte=start, claimed_at__lt=end,
                                   outcome__in=(ProviderWrite.SENDING, ProviderWrite.UNKNOWN)))

    by_id: dict[str, list[TransactionInformation]] = defaultdict(list)
    for r in provider:
        if isinstance(r.transaction_id, str):
            by_id[r.transaction_id].append(r)
    local_ids = {w.provider_id for w in local}
    related: dict[str, list[TransactionInformation]] = defaultdict(list)
    orphans: list[TransactionInformation] = []
    for tid in list(by_id):
        if tid in local_ids:
            continue
        for r in by_id.pop(tid):
            ref_id = r.paypal_reference_id if isinstance(r.paypal_reference_id, str) else None
            if ref_id is not None and ref_id in local_ids:
                related[ref_id].append(r)  # e.g. PayPal's record of a void against our authorization
            else:
                orphans.append(r)

    refreshed_at = provider_time(refreshed)
    matched: list[dict[str, Any]] = []
    local_only: list[dict[str, Any]] = []
    not_yet_reported: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    groups: dict[str, list[ProviderWrite]] = defaultdict(list)
    for w in local:
        groups[w.provider_id].append(w)
    for paypal_id, group in groups.items():
        records = by_id.get(paypal_id, []) + related.get(paypal_id, [])
        if not records:
            newest = max(w.provider_time for w in group if w.provider_time)
            target = not_yet_reported if refreshed_at is None or newest > refreshed_at else local_only
            target.extend(_local_row(w) for w in group)
            continue
        entry = {'paypalId': paypal_id, 'local': [_local_row(w) for w in group],
                 'paypal': [_provider_row(r, prefix) for r in records]}
        matched.append(entry)
        for w in group:
            asked = w.provider_amount if w.provider_amount is not None else w.amount
            if asked is None or w.kind not in (ProviderWrite.CAPTURE, ProviderWrite.REFUND):
                continue
            for r in by_id.get(paypal_id, []):
                value, __ = _money(r.transaction_amount)
                if value is not None and abs(Decimal(value)) != asked:
                    mismatches.append({'paypalId': paypal_id, 'local': str(asked), 'paypal': value,
                                       'orderId': w.order.number if w.order_id else None})

    provider_only = [_provider_row(r, prefix) for r in orphans]
    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'paypalLastRefreshed': refreshed,
        'summary': {
            'paypalTransactions': len(provider),
            'matched': len(matched),
            'paypalOnly': len(provider_only),
            'paypalOnlySentByThisApp': sum(1 for r in provider_only if r['sentByThisApp']),
            'appOnly': len(local_only),
            'notYetReported': len(not_yet_reported),
            'unsettled': len(unsettled),
            'amountMismatches': len(mismatches),
        },
        'matched': matched,
        'paypalOnly': provider_only,
        'appOnly': local_only,
        'notYetReported': not_yet_reported,
        'unsettled': [_local_row(w) for w in unsettled],
        'amountMismatches': mismatches,
    }
