"""
Line PayPal's transaction records for a period up against this site's orders.

Both sides are selected on PayPal's clock: PayPal's transaction dates, and the
provider times stored when each authorization, capture and refund completed.
Records with no provider time yet are reported as unsettled, never dropped.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.db.models import Q

from . import gateway
from .client import get_client, paypal_config
from .models import OrderPayment, PaymentRefund


@dataclass
class LocalEvent:
    payment: OrderPayment
    kind: str               # authorization | capture | refund
    paypal_id: str
    amount: Decimal | None
    at: datetime


def _transaction_view(info) -> dict[str, Any]:
    amount, currency = gateway.info_amount(info, 'transaction_amount')
    fee, _ = gateway.info_amount(info, 'fee_amount')
    initiated = gateway.info_value(info, 'transaction_initiation_date')
    return {
        'transactionId': gateway.info_value(info, 'transaction_id'),
        'referenceId': gateway.info_value(info, 'paypal_reference_id'),
        'eventCode': gateway.info_value(info, 'transaction_event_code'),
        'status': gateway.info_value(info, 'transaction_status'),
        'invoiceId': gateway.info_value(info, 'invoice_id'),
        'customField': gateway.info_value(info, 'custom_field'),
        'amount': gateway.money_str(amount) if amount is not None else None,
        'fee': gateway.money_str(fee) if fee is not None else None,
        'currency': currency,
        'initiatedAt': initiated,
    }


def _local_events(start: datetime, end: datetime) -> list[LocalEvent]:
    events: list[LocalEvent] = []
    payments = OrderPayment.objects.select_related('order').filter(
        Q(original_authorized_at__gte=start, original_authorized_at__lt=end)
        | Q(authorized_at__gte=start, authorized_at__lt=end)
        | Q(captured_at__gte=start, captured_at__lt=end))
    for p in payments:
        if p.original_authorization_id and p.original_authorized_at and start <= p.original_authorized_at < end:
            events.append(LocalEvent(p, 'authorization', p.original_authorization_id, p.amount,
                                     p.original_authorized_at))
        if (p.authorization_id and p.authorization_id != p.original_authorization_id and p.authorized_at
                and start <= p.authorized_at < end):
            events.append(LocalEvent(p, 'reauthorization', p.authorization_id, p.amount, p.authorized_at))
        if p.capture_id and p.captured_at and start <= p.captured_at < end:
            events.append(LocalEvent(p, 'capture', p.capture_id, p.captured_amount, p.captured_at))
    refunds = PaymentRefund.objects.select_related('payment__order').filter(
        provider_time__gte=start, provider_time__lt=end).exclude(paypal_refund_id='')
    for r in refunds:
        events.append(LocalEvent(r.payment, 'refund', r.paypal_refund_id, -r.amount, r.provider_time))
    return events


def _unsettled(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Writes claimed in the period whose outcome PayPal has not confirmed."""
    rows = []
    open_states = (OrderPayment.AUTHORIZING, OrderPayment.AUTHORIZATION_UNKNOWN, OrderPayment.AUTHORIZATION_PENDING,
                   OrderPayment.CAPTURING, OrderPayment.CAPTURE_UNKNOWN, OrderPayment.CAPTURE_PENDING,
                   OrderPayment.VOIDING, OrderPayment.VOID_UNKNOWN, OrderPayment.NEEDS_REVIEW)
    for p in OrderPayment.objects.select_related('order').filter(
            state__in=open_states, claimed_at__gte=start, claimed_at__lt=end):
        rows.append({'orderId': p.order.number, 'kind': p.step or 'payment', 'state': p.state,
                     'reference': p.attempt_reference, 'claimedAt': p.claimed_at.isoformat()})
    for r in PaymentRefund.objects.select_related('payment__order').filter(
            state__in=(PaymentRefund.SENDING, PaymentRefund.UNKNOWN, PaymentRefund.PENDING,
                       PaymentRefund.NEEDS_REVIEW),
            claimed_at__gte=start, claimed_at__lt=end):
        rows.append({'orderId': r.payment.order.number, 'kind': 'refund', 'state': r.state,
                     'refundId': r.pk, 'amount': gateway.money_str(r.amount),
                     'claimedAt': r.claimed_at.isoformat()})
    return rows


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    client = get_client()
    prefix = paypal_config().reference_prefix

    # PayPal side: every page of every <=31-day window.
    transactions = []
    pages = 0
    last_refreshed: datetime | None = None
    for page in gateway.iter_transaction_pages(client, start, end):
        pages += 1
        transactions.extend(page.transactions)
        if page.last_refreshed is not None:
            last_refreshed = page.last_refreshed if last_refreshed is None else min(last_refreshed, page.last_refreshed)
    total_reported = len(transactions)

    # Attribute each PayPal record to one of this site's payments: by our
    # invoice id (the authorization and everything inheriting it) or by id.
    references = dict(OrderPayment.objects.values_list('reference', 'pk'))
    known_ids: dict[str, int] = {}
    for pk, auth, original, capture in OrderPayment.objects.values_list(
            'pk', 'authorization_id', 'original_authorization_id', 'capture_id'):
        for paypal_id in (auth, original, capture):
            if paypal_id:
                known_ids[paypal_id] = pk
    for payment_id, refund_id in PaymentRefund.objects.exclude(paypal_refund_id='').values_list(
            'payment_id', 'paypal_refund_id'):
        known_ids[refund_id] = payment_id

    by_payment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen_ids: dict[str, dict[str, Any]] = {}
    paypal_only = []
    for info in transactions:
        view = _transaction_view(info)
        if view['transactionId']:
            seen_ids[view['transactionId']] = view
        invoice = view['invoiceId'] or ''
        payment_pk = references.get(invoice.rsplit('-', 1)[0]) if invoice else None
        if payment_pk is None:
            payment_pk = known_ids.get(view['transactionId'] or '') or known_ids.get(view['referenceId'] or '')
        if payment_pk is None:
            view['looksLikeThisSite'] = invoice.startswith(prefix + '-')
            paypal_only.append(view)
        else:
            by_payment[payment_pk].append(view)

    # App side, on PayPal's clock.
    events = _local_events(start, end)
    app_only = []
    not_yet_reported = []
    event_payments = {e.payment.pk: e.payment for e in events}
    for event in events:
        record = seen_ids.get(event.paypal_id)
        if record is not None:
            continue
        row = {'orderId': event.payment.order.number, 'kind': event.kind, 'paypalId': event.paypal_id,
               'amount': gateway.money_str(event.amount) if event.amount is not None else None,
               'at': event.at.isoformat()}
        if last_refreshed is not None and event.at > last_refreshed:
            not_yet_reported.append(row)
        else:
            app_only.append(row)

    matched = []
    payments = {p.pk: p for p in OrderPayment.objects.select_related('order').filter(pk__in=list(by_payment))}
    payments.update(event_payments)
    for pk, records in sorted(by_payment.items()):
        payment = payments.get(pk)
        if payment is None:
            continue
        expected = {e.paypal_id: e for e in events if e.payment.pk == pk}
        mismatches = []
        for record in records:
            expected_event = expected.get(record['transactionId'])
            if expected_event is None or expected_event.amount is None or record['amount'] is None:
                continue
            if Decimal(record['amount']) != expected_event.amount.quantize(gateway.TWO_PLACES):
                mismatches.append({'paypalId': expected_event.paypal_id,
                                   'app': gateway.money_str(expected_event.amount),
                                   'paypal': record['amount']})
        matched.append({
            'orderId': payment.order.number,
            'paymentState': payment.state,
            'reference': payment.reference,
            'total': gateway.money_str(payment.amount),
            'paypalTransactions': records,
            'amountMismatches': mismatches,
        })

    unsettled = _unsettled(start, end)
    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'paypal': {
            'transactions': total_reported,
            'pagesFetched': pages,
            'lastRefreshed': last_refreshed.isoformat() if last_refreshed else None,
            'note': 'PayPal reporting lags live activity (up to about three hours); records after lastRefreshed '
                    'may not be listed yet.',
        },
        'summary': {
            'matchedOrders': len(matched),
            'paypalOnly': len(paypal_only),
            'appOnly': len(app_only),
            'notYetReported': len(not_yet_reported),
            'unsettled': len(unsettled),
            'amountMismatches': sum(len(m['amountMismatches']) for m in matched),
        },
        'matched': matched,
        'paypalOnly': paypal_only,
        'appOnly': app_only,
        'notYetReported': not_yet_reported,
        'unsettled': unsettled,
    }
