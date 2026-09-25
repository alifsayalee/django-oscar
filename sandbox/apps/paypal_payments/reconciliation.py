"""
Lines PayPal's own transaction record for a date range up against this app's
captures and refunds, on PayPal's clock.

- PayPal side: the range is split into windows of at most 31 days (the
  search's maximum) and every page of every window is read.
- App side: capture/refund operations whose stored PayPal event time falls in
  the range; operations PayPal never answered are listed as unsettled.
- Matching is by transaction id, against the whole set, so an order with a
  capture and several refunds matches each of them.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from paypal.core import ApiError, RawError, UnsetType
from paypal.models import TransactionInformation

from . import money
from .client import get_client
from .errors import PaymentAPIError, translate
from .models import Outcome, PayPalOperation
from .writes import invoice_prefix, provider_time

MAX_WINDOW = timedelta(days=31)
PAGE_SIZE = 100
MAX_PAGES_PER_WINDOW = 1000
# Transactions take up to three hours to appear in PayPal's reporting.
REPORTING_LAG = timedelta(hours=3)
MAX_RANGE = timedelta(days=3 * 365)


def parse_range(raw_from: str | None, raw_to: str | None) -> tuple[datetime, datetime]:
    def parse(name: str, raw: str | None) -> datetime:
        value = parse_datetime(raw) if raw else None
        if value is None:
            raise PaymentAPIError(400, 'invalid_range', '%s must be an ISO-8601 date-time.' % name)
        return timezone.make_aware(value, dt_timezone.utc) if timezone.is_naive(value) else value

    start, end = parse('from', raw_from), parse('to', raw_to)
    if start >= end:
        raise PaymentAPIError(400, 'invalid_range', 'from must be before to.')
    if end - start > MAX_RANGE:
        raise PaymentAPIError(400, 'invalid_range', 'The range may cover at most three years.')
    return start, end


def _stamp(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _not_available_yet(e: Exception) -> bool:
    """
    PayPal answers 404 INVALID_REQUEST ("Data for the given start date is not
    available") for a window that starts after its reporting data ends -
    reporting lags live activity by up to three hours. No data yet, not a failure.
    """
    if not isinstance(e, ApiError) or e.status_code != 404 or not isinstance(e.error, RawError):
        return False
    try:
        body = e.error.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get('name') == 'INVALID_REQUEST'


def fetch_paypal(start: datetime, end: datetime) -> tuple[list[TransactionInformation], str | None, list[dict]]:
    client = get_client()
    found: dict[tuple[str, str], TransactionInformation] = {}
    refreshed: str | None = None
    unavailable: list[dict] = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + MAX_WINDOW, end)
        page, total_pages = 1, 1
        while page <= total_pages:
            if page > MAX_PAGES_PER_WINDOW:
                raise PaymentAPIError(502, 'report_too_large', 'PayPal returned more pages than can be read.')
            try:
                response = client.transaction_search.search_transactions(
                    _stamp(window_start), _stamp(window_end), fields='transaction_info',
                    page_size=PAGE_SIZE, page=page)
            except Exception as e:  # ApiError (always RawError here), httpx transport errors, decode failures
                if page == 1 and _not_available_yet(e):
                    unavailable.append({'from': _stamp(window_start), 'to': _stamp(window_end)})
                    break
                raise translate(e, action='transaction search') from e
            if isinstance(response.total_pages, int):
                total_pages = response.total_pages
            if isinstance(response.last_refreshed_datetime, str):
                refreshed = response.last_refreshed_datetime
            details = [] if isinstance(response.transaction_details, UnsetType) else response.transaction_details
            if not details:
                break
            for detail in details:
                info = detail.transaction_info
                if isinstance(info, UnsetType) or not isinstance(info.transaction_id, str):
                    continue
                when = provider_time(info.transaction_initiation_date)
                # Windows share their boundary instant; narrow back to the caller's range.
                if when is None or not start <= when < end:
                    continue
                code = info.transaction_event_code if isinstance(info.transaction_event_code, str) else ''
                found[(info.transaction_id, code)] = info
            page += 1
        window_start = window_end
    return list(found.values()), refreshed, unavailable


def _paypal_row(info: TransactionInformation) -> dict:
    value, code = money.money_value(info.transaction_amount)
    fee, _ = money.money_value(info.fee_amount)
    return {
        'transactionId': info.transaction_id,
        'eventCode': info.transaction_event_code if isinstance(info.transaction_event_code, str) else None,
        'status': info.transaction_status if isinstance(info.transaction_status, str) else None,
        'initiatedAt': info.transaction_initiation_date if isinstance(info.transaction_initiation_date, str) else None,
        'amount': value,
        'fee': fee,
        'currency': code,
        'invoiceId': info.invoice_id if isinstance(info.invoice_id, str) else None,
        'customField': info.custom_field if isinstance(info.custom_field, str) else None,
        'referenceId': info.paypal_reference_id if isinstance(info.paypal_reference_id, str) else None,
    }


def _app_row(op: PayPalOperation) -> dict:
    order = op.payment.order if op.payment else None
    return {
        'kind': op.kind,
        'orderId': order.number if order else None,
        'transactionId': op.provider_id or None,
        'amount': str(op.amount) if op.amount is not None else None,
        'currency': op.currency or None,
        'outcome': op.outcome,
        'paypalStatus': op.provider_status or None,
        'paypalTime': op.provider_time.isoformat() if op.provider_time else None,
        'reference': op.request_id,
    }


def report(start: datetime, end: datetime) -> dict:
    paypal_rows, refreshed, unavailable = fetch_paypal(start, end)
    kinds = [PayPalOperation.Kind.CAPTURE, PayPalOperation.Kind.REFUND]
    ops = PayPalOperation.objects.select_related('payment__order').filter(kind__in=kinds)
    local = list(ops.filter(provider_time__gte=start, provider_time__lt=end).exclude(provider_id='')
                 .exclude(outcome=Outcome.FAILED))
    unsettled = list(ops.filter(provider_time__isnull=True, claimed_at__gte=start, claimed_at__lt=end,
                                outcome__in=[Outcome.SENDING, Outcome.UNKNOWN]))

    by_id: dict[str, list[TransactionInformation]] = defaultdict(list)
    for info in paypal_rows:
        by_id[str(info.transaction_id)].append(info)

    reported_until = provider_time(refreshed) if refreshed else None
    matched, app_only, mismatched = [], [], []
    for op in local:
        records = by_id.pop(op.provider_id, [])
        if not records:
            row = _app_row(op)
            when = op.provider_time
            lag = when is not None and (when > timezone.now() - REPORTING_LAG
                                        or (reported_until is not None and when > reported_until)
                                        or any(w['from'] <= _stamp(when) < w['to'] for w in unavailable))
            row['reason'] = 'not_yet_reported' if lag else 'missing_at_paypal'
            app_only.append(row)
            continue
        for info in records:
            row = {'app': _app_row(op), 'paypal': _paypal_row(info)}
            paypal_amount = money.decimal_of(info.transaction_amount)
            if op.amount is not None and paypal_amount is not None and abs(paypal_amount) != Decimal(op.amount):
                mismatched.append(row)
            else:
                matched.append(row)

    prefix = invoice_prefix()
    paypal_only = []
    for records in by_id.values():
        for info in records:
            row = _paypal_row(info)
            row['ours'] = bool(row['invoiceId'] and row['invoiceId'].startswith(prefix))
            paypal_only.append(row)

    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'paypalLastRefreshed': refreshed,
        # Windows PayPal has no reporting data for yet; app records in them show as not_yet_reported.
        'paypalNotYetAvailable': unavailable,
        'summary': {
            'paypalTransactions': len(paypal_rows),
            'appTransactions': len(local),
            'matched': len(matched),
            'amountMismatch': len(mismatched),
            'appOnly': len(app_only),
            'paypalOnly': len(paypal_only),
            'unsettled': len(unsettled),
        },
        'matched': matched,
        'amountMismatch': mismatched,
        'appOnly': app_only,
        'paypalOnly': paypal_only,
        'unsettled': [_app_row(op) for op in unsettled],
    }
