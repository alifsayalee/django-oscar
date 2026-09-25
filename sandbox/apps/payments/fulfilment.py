"""
What follows a hold: capture it at fulfilment (renewing it first if it has
gone stale), release it on cancel, return captured money with refunds.
"""
import logging
from datetime import timedelta
from decimal import Decimal

import httpx
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from paypal.core import ApiError
from paypal.models import CaptureRequest, Money, ReauthorizeRequest, RefundRequest
from paypal.models.enums import AuthorizationStatus

from . import outcomes
from .client import get_client
from .errors import ApiProblem, OutcomeUnknown, provider_message, translate
from .models import Outcome, OrderPayment, PaymentRefund, PaymentState, ProviderWrite, RefundState
from .money import to_wire
from .ordering import AWAITING_PAYMENT, CANCELLED, COMPLETE
from .payflow import (
    PREFER, EventHandler, Transaction, _cas, _money, _payment_event, _refresh_authorization,
    _release_allocation, _set_order_status, _set_state, _source, _status_for, _take_over_stale,
    _time, _v)
from .safe_write import (
    PAYMENTS_KEY_RETENTION, Answer, deterministic_ref, order_reference, safe_write)

logger = logging.getLogger('apps.payments')

# From the reauthorize_payment documentation: a 3-day honor period, after
# which the hold is renewed by reauthorizing; reauthorization is possible up
# to 29 days after the original authorization, after which a new
# authorization is needed.
HONOR_PERIOD = timedelta(days=3)
RENEWAL_LIMIT = timedelta(days=29)


# -- fulfil: capture the hold ------------------------------------------------

def fulfil(order) -> tuple[int, OrderPayment]:
    payment = order.paypal_payment
    state = payment.state
    if state == PaymentState.CAPTURED:
        return 200, payment
    if state == PaymentState.CAPTURE_PENDING:
        return _refresh_capture(payment)
    if state == PaymentState.AUTH_PENDING:
        __, payment = _refresh_authorization(payment)
        if payment.state != PaymentState.AUTHORIZED:
            raise ApiProblem(409, 'authorization_pending',
                             'PayPal has not finished authorizing this payment yet; try again later.')
        state = payment.state
    if state == PaymentState.CAPTURING:
        if not _take_over_stale(payment, PaymentState.CAPTURING):
            return 202, payment
    elif state in (PaymentState.AUTHORIZED, PaymentState.CAPTURE_UNKNOWN):
        if not _cas(payment, [state], PaymentState.CAPTURING, last_error=''):
            return fulfil(order)
    else:
        raise ApiProblem(409, 'not_capturable',
                         'Only an order whose payment is authorized can be fulfilled '
                         '(payment state: %s).' % payment.state)
    try:
        _renew_if_stale(payment)
        return _capture(payment)
    except ApiProblem:
        payment.refresh_from_db()
        if payment.state == PaymentState.CAPTURING:
            _set_state(payment, PaymentState.CAPTURE_UNKNOWN)
        raise
    except (ApiError, httpx.RequestError, ValueError) as e:
        problem = translate(e, action='capture')
        payment.refresh_from_db()
        if payment.state == PaymentState.CAPTURING:
            # A refused capture, or nothing sent: the hold is still there.
            back = PaymentState.CAPTURE_UNKNOWN if problem.outcome_unknown else PaymentState.AUTHORIZED
            _set_state(payment, back, last_error=problem.message[:500])
        raise problem from e
    except BaseException:
        payment.refresh_from_db()
        if payment.state == PaymentState.CAPTURING:
            _set_state(payment, PaymentState.CAPTURE_UNKNOWN)
        raise


def _expire(payment: OrderPayment, reason: str):
    """The hold cannot be taken any more: say so, and let the shopper pay again."""
    message = ('%s The held funds can no longer be captured. Ask the shopper to pay for the '
               'order again (POST /api/orders/%s/pay), or cancel the order.'
               % (reason, payment.order.number))
    with transaction.atomic():
        _set_state(payment, PaymentState.AUTHORIZATION_EXPIRED, last_error=message[:500])
        _release_allocation(payment, 'Expired', payment.authorization_id,
                            payment.authorization_status)
        _set_order_status(payment.order, AWAITING_PAYMENT)
    raise ApiProblem(409, 'authorization_expired', message)


def _renew_if_stale(payment: OrderPayment):
    """Reauthorize a hold past its honor period; refuse one past the renewal limit."""
    client = get_client()
    auth = client.payments.get_authorized_payment(payment.authorization_id)
    status = _v(auth.status)
    if status in (AuthorizationStatus.CAPTURED, AuthorizationStatus.PARTIALLY_CAPTURED):
        return  # an earlier capture of ours landed; the capture step finds it by reference
    if outcomes.authorization_outcome(status) != outcomes.DONE:
        _expire(payment, 'PayPal reports the authorization as %s.'
                % (outcomes.wire(status) or 'unreadable'))
    now = timezone.now()
    held_since = _time(auth.create_time) or payment.authorized_at or now
    original = payment.original_authorized_at or held_since
    if now - held_since <= HONOR_PERIOD:
        return
    if now - original >= RENEWAL_LIMIT:
        _expire(payment, 'The authorization is %d days old; PayPal only renews authorizations '
                         'within %d days.' % ((now - original).days, RENEWAL_LIMIT.days))
    ref = deterministic_ref('auth', payment.authorization_id, 'reauth')
    body = ReauthorizeRequest(amount=Money(
        currency_code=payment.currency, value=to_wire(payment.amount, payment.currency)))
    try:
        written = safe_write(
            ref, operation='reauthorize', order=payment.order, retention=PAYMENTS_KEY_RETENTION,
            sent=(payment.amount, payment.currency),
            send=lambda key: client.payments.reauthorize_payment(
                payment.authorization_id, pay_pal_request_id=key, prefer=PREFER, body=body),
            read=_read_authorization, apply=_apply_reauthorization(payment))
    except ApiError as e:
        if 400 <= e.status_code < 500 and e.status_code not in (401, 403, 429):
            message, issues = provider_message(e)
            _expire(payment, 'PayPal refused to renew the authorization: %s%s.' % (
                message, (' (%s)' % ', '.join(issues)) if issues else ''))
        raise
    if written.in_flight:
        raise ApiProblem(409, 'in_progress', 'This order is already being fulfilled.')
    payment.refresh_from_db()
    record = written.record
    if record.outcome == Outcome.PENDING:
        raise ApiProblem(409, 'authorization_pending',
                         'PayPal is still processing the renewed authorization; try again later.')
    if record.outcome == Outcome.FAILED:
        _expire(payment, 'PayPal did not renew the authorization (status %s).'
                % (record.provider_status or 'unknown'))
    if record.outcome != Outcome.DONE:
        raise OutcomeUnknown(ref)
    if written.result is None and record.provider_id and payment.authorization_id != record.provider_id:
        # Renewed by an earlier request whose bookkeeping we now finish.
        _set_state(payment, PaymentState.CAPTURING, authorization_id=record.provider_id,
                   authorization_status=record.provider_status,
                   authorized_at=record.provider_time or timezone.now())


def _read_authorization(result) -> Answer:
    auth_id, status = _v(result.id), _v(result.status)
    if not auth_id or status is None:
        raise ValueError('authorization id/status')
    amount, currency = _money(result.amount)
    return Answer(auth_id, outcomes.wire(status), outcomes.authorization_outcome(status),
                  _time(result.create_time) or _time(result.update_time), amount, currency)


def _apply_reauthorization(payment: OrderPayment):
    def apply(record: ProviderWrite, result, answer: Answer):
        if record.outcome not in (Outcome.DONE, Outcome.PENDING):
            return
        old = payment.authorization_id
        new_state = (PaymentState.CAPTURING if record.outcome == Outcome.DONE
                     else PaymentState.AUTH_PENDING)
        _set_state(payment, new_state,
                   authorization_id=answer.provider_id, authorization_status=answer.status,
                   authorized_at=answer.provider_time or timezone.now(),
                   authorization_expires_at=_time(result.expiration_time))
        source = payment.source
        if source is not None:
            source.reference = answer.provider_id
            source.save()
            Transaction.objects.create(source=source, txn_type='Reauthorise',
                                       amount=payment.amount, reference=answer.provider_id,
                                       status=answer.status)
        logger.info('Order %s: authorization %s renewed as %s', payment.order.number, old,
                    answer.provider_id)
    return apply


def _read_capture(result) -> Answer:
    capture_id, status = _v(result.id), _v(result.status)
    if not capture_id or status is None:
        raise ValueError('capture id/status')
    amount, currency = _money(result.amount)
    return Answer(capture_id, outcomes.wire(status), outcomes.capture_outcome(status),
                  _time(result.create_time) or _time(result.update_time), amount, currency)


def _capture_fields(result, answer: Answer) -> dict:
    fields: dict[str, object] = {'capture_id': answer.provider_id, 'capture_status': answer.status,
              'captured_at': answer.provider_time}
    breakdown = _v(result.seller_receivable_breakdown)
    if breakdown is not None:
        gross, __ = _money(breakdown.gross_amount)
        fee, __ = _money(breakdown.paypal_fee)
        net, __ = _money(breakdown.net_amount)
        fields.update(paypal_fee=fee, net_amount=net)
        if gross is not None:
            fields['captured_amount'] = gross
    if 'captured_amount' not in fields and answer.amount is not None:
        fields['captured_amount'] = answer.amount
    return fields


def _finish_capture(payment: OrderPayment, outcome: str, fields: dict):
    order = payment.order
    if outcome == Outcome.DONE:
        _set_state(payment, PaymentState.CAPTURED, last_error='', **fields)
        _source(payment).debit(payment.captured_amount, reference=payment.capture_id,
                               status=payment.capture_status)
        _payment_event(order, 'Captured', payment.captured_amount, payment.capture_id)
        EventHandler().consume_stock_allocations(order)
        _set_order_status(order, COMPLETE)
    elif outcome == Outcome.PENDING:
        _set_state(payment, PaymentState.CAPTURE_PENDING, **fields)
    elif outcome == Outcome.FAILED:
        _set_state(payment, PaymentState.CAPTURE_FAILED,
                   last_error='PayPal did not capture the payment (status %s).'
                              % fields.get('capture_status', 'unknown'), **fields)
    elif outcome == Outcome.NEEDS_REVIEW:
        _set_state(payment, PaymentState.NEEDS_REVIEW,
                   last_error='PayPal captured an amount different from the order total.',
                   **fields)
    else:
        _set_state(payment, PaymentState.CAPTURE_UNKNOWN, **fields)


def _capture(payment: OrderPayment) -> tuple[int, OrderPayment]:
    client = get_client()
    ref = deterministic_ref('auth', payment.authorization_id, 'capture')
    body = CaptureRequest(
        amount=Money(currency_code=payment.currency,
                     value=to_wire(payment.amount, payment.currency)),
        final_capture=True, invoice_id='%s-%d' % (order_reference(payment.order), payment.attempt))
    written = safe_write(
        ref, operation='capture', order=payment.order, retention=PAYMENTS_KEY_RETENTION,
        sent=(payment.amount, payment.currency),
        send=lambda key: client.payments.capture_authorized_payment(
            payment.authorization_id, pay_pal_request_id=key, prefer=PREFER, body=body),
        read=_read_capture,
        apply=lambda record, result, answer: _finish_capture(
            payment, record.outcome, _capture_fields(result, answer)))
    payment.refresh_from_db()
    if written.in_flight:
        return 202, payment
    if written.result is None and payment.state == PaymentState.CAPTURING:
        # The capture's outcome was stored by an earlier request.
        record = written.record
        if record.outcome == Outcome.FAILED:
            _set_state(payment, PaymentState.AUTHORIZED, last_error=record.detail[:500])
        elif record.outcome != Outcome.DONE:
            _set_state(payment, PaymentState.CAPTURE_UNKNOWN)
    return _status_for(payment), payment


def _refresh_capture(payment: OrderPayment) -> tuple[int, OrderPayment]:
    try:
        result = get_client().payments.get_captured_payment(payment.capture_id)
        answer = _read_capture(result)
    except (ApiError, ValueError, httpx.RequestError) as e:
        raise translate(e, action='refresh capture') from e
    if answer.outcome != outcomes.PENDING:
        with transaction.atomic():
            if _cas(payment, [PaymentState.CAPTURE_PENDING], PaymentState.CAPTURING):
                _finish_capture(payment, answer.outcome, _capture_fields(result, answer))
    payment.refresh_from_db()
    return _status_for(payment), payment


# -- cancel: release the hold ------------------------------------------------

def cancel(order) -> tuple[int, OrderPayment]:
    payment = order.paypal_payment
    state = payment.state
    if state in (PaymentState.VOIDED, PaymentState.CANCELLED):
        return 200, payment
    if state in (PaymentState.AWAITING_PAYMENT, PaymentState.AUTH_FAILED,
                 PaymentState.AUTHORIZATION_EXPIRED):
        # No hold exists: nothing to release at PayPal.
        with transaction.atomic():
            if not _cas(payment, [state], PaymentState.CANCELLED):
                return cancel(order)
            _cancel_order(order)
        return 200, payment
    if state == PaymentState.VOID_PENDING:
        return _refresh_void(payment)
    if state == PaymentState.VOIDING:
        if not _take_over_stale(payment, PaymentState.VOIDING):
            return 202, payment
    elif state in (PaymentState.AUTHORIZED, PaymentState.CAPTURE_FAILED, PaymentState.VOID_UNKNOWN):
        if not _cas(payment, [state], PaymentState.VOIDING):
            return cancel(order)
    elif state in (PaymentState.CAPTURED, PaymentState.CAPTURE_PENDING):
        raise ApiProblem(409, 'already_captured',
                         'The payment has been captured; return money with a refund instead.')
    else:
        raise ApiProblem(409, 'invalid_state',
                         'The payment is not in a state that can be cancelled yet '
                         '(payment state: %s).' % state)
    client = get_client()
    ref = deterministic_ref('auth', payment.authorization_id, 'void')
    try:
        written = safe_write(
            ref, operation='void', order=order, retention=PAYMENTS_KEY_RETENTION,
            send=lambda key: client.payments.void_payment(
                payment.authorization_id, pay_pal_request_id=key, prefer=PREFER),
            read=_read_void,
            apply=lambda record, result, answer: _finish_void(payment, record.outcome,
                                                              answer.status))
    except ApiProblem:
        payment.refresh_from_db()
        if payment.state == PaymentState.VOIDING:
            _set_state(payment, PaymentState.VOID_UNKNOWN)
        raise
    except (ApiError, httpx.RequestError, ValueError) as e:
        problem = translate(e, action='void')
        payment.refresh_from_db()
        if payment.state == PaymentState.VOIDING:
            back = PaymentState.VOID_UNKNOWN if problem.outcome_unknown else PaymentState.AUTHORIZED
            _set_state(payment, back, last_error=problem.message[:500])
        raise problem from e
    payment.refresh_from_db()
    if written.in_flight:
        return 202, payment
    if written.result is None and payment.state == PaymentState.VOIDING:
        _finish_void(payment, written.record.outcome, written.record.provider_status)
    return _status_for(payment), payment


def _read_void(result) -> Answer:
    auth_id, status = _v(result.id), _v(result.status)
    if not auth_id or status is None:
        raise ValueError('authorization id/status')
    amount, currency = _money(result.amount)
    return Answer(auth_id, outcomes.wire(status), outcomes.void_outcome(status),
                  _time(result.update_time) or _time(result.create_time), amount, currency)


def _finish_void(payment: OrderPayment, outcome: str, status: str):
    if outcome == Outcome.DONE:
        _set_state(payment, PaymentState.VOIDED, authorization_status=status, last_error='')
        _release_allocation(payment, 'Void', payment.authorization_id, status)
        _payment_event(payment.order, 'Voided', payment.amount, payment.authorization_id)
        _cancel_order(payment.order)
    elif outcome == Outcome.PENDING:
        _set_state(payment, PaymentState.VOID_PENDING, authorization_status=status)
    elif outcome == Outcome.FAILED:
        captured = status in (AuthorizationStatus.CAPTURED, AuthorizationStatus.PARTIALLY_CAPTURED)
        _set_state(payment, PaymentState.NEEDS_REVIEW if captured else PaymentState.AUTHORIZED,
                   authorization_status=status,
                   last_error='PayPal did not release the hold (status %s).' % status)
    else:
        _set_state(payment, PaymentState.VOID_UNKNOWN, authorization_status=status)


def _refresh_void(payment: OrderPayment) -> tuple[int, OrderPayment]:
    try:
        result = get_client().payments.get_authorized_payment(payment.authorization_id)
        answer = _read_void(result)
    except (ApiError, ValueError, httpx.RequestError) as e:
        raise translate(e, action='refresh void') from e
    if answer.outcome != outcomes.PENDING:
        with transaction.atomic():
            if _cas(payment, [PaymentState.VOID_PENDING], PaymentState.VOIDING):
                _finish_void(payment, answer.outcome, answer.status)
    payment.refresh_from_db()
    return _status_for(payment), payment


def _cancel_order(order):
    order.refresh_from_db()
    if order.status == CANCELLED:
        return
    EventHandler().cancel_stock_allocations(order)
    _set_order_status(order, CANCELLED)


# -- refunds -----------------------------------------------------------------

KEY_CHARS = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')


def validate_key(key) -> str:
    if not isinstance(key, str) or not 1 <= len(key) <= 64 or not set(key) <= KEY_CHARS:
        raise ApiProblem(400, 'invalid_idempotency_key',
                         'An idempotency key of 1-64 characters [A-Za-z0-9_-] is required '
                         '(Idempotency-Key header or "idempotencyKey").')
    return key


def refund(user, order, amount: Decimal | None, key: str) -> tuple[int, PaymentRefund]:
    payment = order.paypal_payment
    key = validate_key(key)
    existing = PaymentRefund.objects.filter(payment=payment, idempotency_key=key).first()
    if existing is not None:
        return _repeat_refund(payment, existing, amount)
    if payment.state != PaymentState.CAPTURED or not payment.capture_id:
        raise ApiProblem(409, 'not_refundable',
                         'Only a fulfilled (captured) order can be refunded '
                         '(payment state: %s).' % payment.state)
    if amount is None:
        amount = payment.captured_amount - payment.refund_reserved
        if amount <= 0:
            raise ApiProblem(409, 'nothing_to_refund',
                             'The captured amount has been fully refunded.')
    try:
        with transaction.atomic():
            record = PaymentRefund.objects.create(
                payment=payment, idempotency_key=key, amount=amount, requested_by=user)
            _reserve(payment, amount)
    except IntegrityError:
        # A concurrent request with the same key won the claim.
        existing = PaymentRefund.objects.get(payment=payment, idempotency_key=key)
        return _repeat_refund(payment, existing, amount)
    return _send_refund(payment, record)


def _reserve(payment: OrderPayment, amount: Decimal):
    """Reserve ``amount`` against the capture in one conditional UPDATE."""
    reserved = OrderPayment.objects.filter(
        pk=payment.pk, state=PaymentState.CAPTURED,
        refund_reserved__lte=F('captured_amount') - amount,
    ).update(refund_reserved=F('refund_reserved') + amount, updated_at=timezone.now())
    if reserved != 1:
        payment.refresh_from_db()
        remaining = max(payment.captured_amount - payment.refund_reserved, Decimal('0'))
        raise ApiProblem(409, 'exceeds_refundable',
                         'A refund of %s would exceed what is left to refund (%s %s).'
                         % (to_wire(amount, payment.currency),
                            to_wire(remaining, payment.currency), payment.currency))


def _release(payment: OrderPayment, amount: Decimal):
    OrderPayment.objects.filter(pk=payment.pk).update(
        refund_reserved=F('refund_reserved') - amount, updated_at=timezone.now())


def _repeat_refund(payment, existing: PaymentRefund, amount) -> tuple[int, PaymentRefund]:
    if amount is not None and amount != existing.amount:
        raise ApiProblem(409, 'idempotency_key_reused',
                         'This idempotency key was already used for a refund of %s.'
                         % to_wire(existing.amount, payment.currency))
    if existing.state == RefundState.FAILED:
        # Nothing was refunded under this key: try again, same reference.
        with transaction.atomic():
            if PaymentRefund.objects.filter(pk=existing.pk, state=RefundState.FAILED).update(
                    state=RefundState.SENDING) != 1:
                existing.refresh_from_db()
                return _refund_status(existing), existing
            _reserve(payment, existing.amount)
        existing.refresh_from_db()
        return _send_refund(payment, existing)
    if existing.state == RefundState.PENDING and existing.paypal_refund_id:
        return _refresh_refund(payment, existing)
    if existing.state in (RefundState.SENDING, RefundState.UNKNOWN):
        return _send_refund(payment, existing)  # safe_write decides: in flight, or check
    return _refund_status(existing), existing


def _read_refund(result) -> Answer:
    refund_id, status = _v(result.id), _v(result.status)
    if not refund_id or status is None:
        raise ValueError('refund id/status')
    amount, currency = _money(result.amount)
    return Answer(refund_id, outcomes.wire(status), outcomes.refund_outcome(status),
                  _time(result.create_time) or _time(result.update_time), amount, currency)


def _finish_refund(payment: OrderPayment, record: PaymentRefund, outcome: str,
                   answer: Answer | None):
    fields: dict[str, object] = {}
    if answer is not None:
        fields = {'paypal_refund_id': answer.provider_id, 'paypal_status': answer.status}
    states: dict[str, RefundState] = {
        Outcome.DONE: RefundState.DONE, Outcome.PENDING: RefundState.PENDING,
        Outcome.FAILED: RefundState.FAILED, Outcome.NEEDS_REVIEW: RefundState.NEEDS_REVIEW,
    }
    new_state = states.get(outcome, RefundState.UNKNOWN)
    # Only the request that moves the refund out of an open state books it.
    moved = PaymentRefund.objects.filter(
        pk=record.pk, state__in=[RefundState.SENDING, RefundState.UNKNOWN, RefundState.PENDING],
    ).update(state=new_state, updated_at=timezone.now(), **fields)
    record.refresh_from_db()
    if not moved:
        return
    if new_state == RefundState.DONE:
        OrderPayment.objects.filter(pk=payment.pk).update(
            refunded_amount=F('refunded_amount') + record.amount, updated_at=timezone.now())
        payment.refresh_from_db()
        if payment.source is not None:
            payment.source.refund(record.amount, reference=record.paypal_refund_id,
                                  status=record.paypal_status)
        _payment_event(payment.order, 'Refunded', record.amount, record.paypal_refund_id)
    elif new_state == RefundState.FAILED:
        _release(payment, record.amount)


def _send_refund(payment: OrderPayment, record: PaymentRefund) -> tuple[int, PaymentRefund]:
    client = get_client()
    ref = deterministic_ref('ord', payment.order.number, 'refund', record.idempotency_key)
    body = RefundRequest(
        amount=Money(currency_code=payment.currency,
                     value=to_wire(record.amount, payment.currency)),
        custom_id=order_reference(payment.order))
    try:
        written = safe_write(
            ref, operation='refund', order=payment.order, retention=PAYMENTS_KEY_RETENTION,
            sent=(record.amount, payment.currency),
            send=lambda key: client.payments.refund_captured_payment(
                payment.capture_id, pay_pal_request_id=key, prefer=PREFER, body=body),
            read=_read_refund,
            apply=lambda rec, result, answer: _finish_refund(payment, record, rec.outcome, answer))
    except ApiProblem:
        PaymentRefund.objects.filter(pk=record.pk, state=RefundState.SENDING).update(
            state=RefundState.UNKNOWN)
        record.refresh_from_db()
        raise
    except (ApiError, httpx.RequestError, ValueError) as e:
        problem = translate(e, action='refund')
        with transaction.atomic():
            if not problem.outcome_unknown:
                # Refused, or never sent: nothing was refunded.
                _finish_refund(payment, record, Outcome.FAILED, None)
            else:
                PaymentRefund.objects.filter(pk=record.pk, state=RefundState.SENDING).update(
                    state=RefundState.UNKNOWN)
        raise problem from e
    record.refresh_from_db()
    if written.in_flight:
        return 202, record
    if written.result is None and record.state in (RefundState.SENDING, RefundState.UNKNOWN):
        stored = written.record
        with transaction.atomic():
            _finish_refund(payment, record, stored.outcome, Answer(
                stored.provider_id, stored.provider_status, stored.outcome, stored.provider_time))
    return _refund_status(record), record


def _refresh_refund(payment, record: PaymentRefund) -> tuple[int, PaymentRefund]:
    try:
        answer = _read_refund(get_client().payments.get_refund(record.paypal_refund_id))
    except (ApiError, ValueError, httpx.RequestError) as e:
        raise translate(e, action='refresh refund') from e
    if answer.outcome != outcomes.PENDING:
        with transaction.atomic():
            _finish_refund(payment, record, answer.outcome, answer)
    record.refresh_from_db()
    return _refund_status(record), record


def _refund_status(record: PaymentRefund) -> int:
    statuses: dict[str, int] = {
        RefundState.DONE: 201, RefundState.PENDING: 202, RefundState.SENDING: 202,
        RefundState.FAILED: 402, RefundState.NEEDS_REVIEW: 409}
    return statuses.get(record.state, 504)
