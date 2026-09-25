"""
Money movement for one Oscar order: authorize (hold) at payment, capture at
fulfilment (renewing a stale hold first), void at cancellation, refund after
fulfilment. Every PayPal write goes through ``writes.safe_write``.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_class
from paypal.core import UnsetType
from paypal.models import (
    AmountWithBreakdown, AuthorizationWithAdditionalData, CapturedPayment, CaptureRequest, CardRequest, Money,
    Order, OrderAuthorizeResponse, OrderRequest, PaymentAuthorization, PaymentSource, PurchaseUnitRequest,
    ReauthorizeRequest, Refund, RefundRequest)
from paypal.models.enums import AuthorizationStatus, CheckoutPaymentIntent, OrderStatus

from . import money, outcomes
from .cards import owned_card, parse_card
from .client import get_client
from .errors import PaymentAPIError, translate
from .models import Outcome, PaymentState, PayPalOperation, PayPalPayment
from .writes import Answer, invoice_prefix, make_ref, provider_time, safe_write, try_claim

logger = logging.getLogger('apps.paypal_payments')
EventHandler = get_class('order.processing', 'EventHandler')

PREFER = 'return=representation'
ORDER_AUTHORIZED = 'Being processed'
ORDER_FULFILLED = 'Complete'
ORDER_CANCELLED = 'Cancelled'

Kind = PayPalOperation.Kind


# ---------------------------------------------------------------- lookups

def payment_for(order_number: str, user=None) -> PayPalPayment:
    """The order's payment; with ``user``, only that shopper's (404 otherwise)."""
    qs = PayPalPayment.objects.select_related('order', 'source')
    if user is not None:
        qs = qs.filter(order__user=user)
    payment = qs.filter(order__number=order_number).first()
    if payment is None:
        raise PaymentAPIError(404, 'order_not_found', 'No such order.')
    return payment


def next_seq(payment: PayPalPayment, kind: str) -> int:
    """
    The attempt a new request for this step claims: a new one only after the
    last attempt failed; otherwise the same one, so a repeat is answered from
    (or checked against) it. The UNIQUE constraint decides any race.
    """
    last = payment.operations.filter(kind=kind).order_by('-seq').first()
    if last is None:
        return 1
    return last.seq + 1 if last.outcome == Outcome.FAILED else last.seq


def _money(value: Decimal, code: str) -> Money:
    return Money(currency_code=code, value=money.fmt(value, code))


def _set_order_status(order, status: str) -> None:
    if order.status != status:
        order.set_status(status)


# ---------------------------------------------------------------- readers

def _first_authorization(r: Order | OrderAuthorizeResponse) -> AuthorizationWithAdditionalData | None:
    units = r.purchase_units
    if isinstance(units, UnsetType) or not units:
        return None
    payments = units[0].payments
    if isinstance(payments, UnsetType) or isinstance(payments.authorizations, UnsetType):
        return None
    return payments.authorizations[0] if payments.authorizations else None


def _unit_amount(r: Order | OrderAuthorizeResponse) -> tuple[str | None, str | None]:
    units = r.purchase_units
    if isinstance(units, UnsetType) or not units or isinstance(units[0].amount, UnsetType):
        return None, None
    return units[0].amount.value, units[0].amount.currency_code


def _read_order(r: Order | OrderAuthorizeResponse) -> Answer:
    """
    The authorization decides once there is one (its id and status are what
    later requests act on); before that, the PayPal order's own status.
    """
    auth = _first_authorization(r)
    if auth is not None:
        value, code = money.money_value(auth.amount)
        return Answer(provider_id=auth.id if isinstance(auth.id, str) else None, status=auth.status,
                      provider_time=provider_time(auth.create_time), amount=value, currency=code)
    value, code = _unit_amount(r)
    return Answer(provider_id=r.id if isinstance(r.id, str) else None, status=r.status,
                  provider_time=provider_time(r.create_time), amount=value, currency=code)


def _order_outcome(status: object) -> str:
    return outcomes.authorization(status) if _is_auth_status(status) else outcomes.order_create_step(status)


def _is_auth_status(status: object) -> bool:
    return isinstance(status, AuthorizationStatus)


def _read_authorization(r: PaymentAuthorization) -> Answer:
    value, code = money.money_value(r.amount)
    return Answer(provider_id=r.id if isinstance(r.id, str) else None, status=r.status,
                  provider_time=provider_time(r.create_time), amount=value, currency=code)


def _read_capture(r: CapturedPayment) -> Answer:
    value, code = money.money_value(r.amount)
    return Answer(provider_id=r.id if isinstance(r.id, str) else None, status=r.status,
                  provider_time=provider_time(r.create_time), amount=value, currency=code)


def _read_refund(r: Refund) -> Answer:
    value, code = money.money_value(r.amount)
    return Answer(provider_id=r.id if isinstance(r.id, str) else None, status=r.status,
                  provider_time=provider_time(r.create_time), amount=value, currency=code)


# ---------------------------------------------------------------- pay

def _apply_authorization(payment: PayPalPayment, op: PayPalOperation, r: Order | OrderAuthorizeResponse) -> None:
    payment.refresh_from_db()
    if isinstance(r.id, str):
        payment.paypal_order_id = r.id
    payment.paypal_order_status = '' if isinstance(r.status, UnsetType) else str(r.status)
    auth = _first_authorization(r)
    if auth is not None and isinstance(auth.id, str):
        payment.authorization_id = auth.id
        payment.authorization_status = '' if isinstance(auth.status, UnsetType) else str(auth.status)
        payment.authorized_at = provider_time(auth.create_time)
        payment.authorization_expires_at = provider_time(auth.expiration_time)
    _apply_hold_outcome(payment, op)


def _apply_hold_outcome(payment: PayPalPayment, op: PayPalOperation) -> None:
    if op.outcome == Outcome.DONE:
        payment.state = PaymentState.AUTHORIZED
        payment.last_error = ''
        payment.source.allocate(payment.amount, reference=payment.authorization_id,
                                status=payment.authorization_status)
        _set_order_status(payment.order, ORDER_AUTHORIZED)
    elif op.outcome == Outcome.FAILED:
        payment.state = PaymentState.AUTHORIZATION_FAILED
        payment.last_error = 'PayPal status %s' % op.provider_status
    elif op.outcome == Outcome.NEEDS_REVIEW:
        payment.state = PaymentState.NEEDS_REVIEW
        payment.last_error = op.detail
    else:
        payment.state = PaymentState.AUTHORIZING
    payment.save()


def _record_failure(payment: PayPalPayment, op: PayPalOperation, failed_state: str, pending_state: str,
                    error: PaymentAPIError) -> None:
    """After a write raised: the payment says what the claim now records."""
    op.refresh_from_db()
    payment.refresh_from_db()
    if op.outcome == Outcome.FAILED:
        payment.state = failed_state
    elif op.outcome == Outcome.NEEDS_REVIEW:
        payment.state = PaymentState.NEEDS_REVIEW
    else:
        payment.state = pending_state
    payment.last_error = error.message
    payment.save(update_fields=['state', 'last_error', 'updated'])


@sensitive_variables('payload', 'card', 'body', 'source')
def pay(user, order_number: str, payload: object) -> tuple[PayPalPayment, str]:
    """Authorize the order total. Returns (payment, outcome of the hold)."""
    if not isinstance(payload, dict):
        raise PaymentAPIError(400, 'invalid_request', 'Body must be a JSON object.')
    payment = payment_for(order_number, user=user)
    has_card, has_saved = 'card' in payload, 'paymentMethodId' in payload
    if has_card == has_saved:
        raise PaymentAPIError(400, 'invalid_request', 'Send either card or paymentMethodId.')

    if payment.state in (PaymentState.AUTHORIZED, PaymentState.CAPTURING, PaymentState.CAPTURED,
                         PaymentState.PARTIALLY_REFUNDED, PaymentState.REFUNDED):
        return payment, Outcome.DONE  # already paid: a repeat, not a second hold
    if payment.state not in (PaymentState.AWAITING_PAYMENT, PaymentState.AUTHORIZING,
                             PaymentState.AUTHORIZATION_FAILED):
        raise PaymentAPIError(409, 'not_payable', 'The order cannot be paid in state %s.' % payment.state)

    seq = next_seq(payment, Kind.CREATE_ORDER)
    saved = owned_card(user, payload['paymentMethodId']) if has_saved else None
    op, won = try_claim(
        make_ref('pay', payment.order.number, seq), Kind.CREATE_ORDER, payment=payment, user=user, seq=seq,
        amount=payment.amount, currency=payment.currency,
        inputs={'paymentMethodId': saved.bankcard_id} if saved else {'card': 'one-off'})
    if won:
        PayPalPayment.objects.filter(pk=payment.pk).update(state=PaymentState.AUTHORIZING)

    if op.outcome in (Outcome.DONE, Outcome.PENDING) and not won:
        return _continue_hold(payment, op, recheck=True)

    # Build the identical request this attempt sends (and resends).
    if 'paymentMethodId' in op.inputs:
        if saved is None or saved.bankcard_id != op.inputs['paymentMethodId']:
            saved = owned_card(user, op.inputs['paymentMethodId'])
        card = CardRequest(vault_id=saved.payment_token_id)
    else:
        if not has_card:
            raise PaymentAPIError(409, 'card_required',
                                  'This payment attempt used card details; resend them to check its outcome.')
        card = CardRequest(**parse_card(payload['card']))
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            reference_id=payment.order.number,
            custom_id=payment.order.number,
            invoice_id='%s%s-%s' % (invoice_prefix(), payment.order.number, seq),
            description='Order %s' % payment.order.number,
            amount=AmountWithBreakdown(currency_code=payment.currency,
                                       value=money.fmt(payment.amount, payment.currency)))],
        payment_source=PaymentSource(card=card))
    client = get_client()
    try:
        op, _ = safe_write(
            op, won,
            send=lambda key: client.orders.create_order(body, pay_pal_request_id=key, prefer=PREFER),
            read=_read_order, outcome_of=_order_outcome,
            apply=lambda o, r, a: _apply_authorization(payment, o, r), action='payment')
    except PaymentAPIError as e:
        _record_failure(payment, op, PaymentState.AUTHORIZATION_FAILED, PaymentState.AUTHORIZING, e)
        raise
    return _continue_hold(payment, op)


def _continue_hold(payment: PayPalPayment, create_op: PayPalOperation,
                   recheck: bool = False) -> tuple[PayPalPayment, str]:
    """
    After the create step: a PayPal order that is APPROVED without an
    authorization still needs the authorize step; a hold reported pending
    earlier is re-read when the shopper repeats the request.
    """
    payment.refresh_from_db()
    if create_op.outcome == Outcome.FAILED and create_op.provider_status == OrderStatus.PAYER_ACTION_REQUIRED:
        raise PaymentAPIError(
            422, 'payer_action_required',
            'PayPal asked for the cardholder to approve this payment in a browser (3-D Secure). '
            'That approval flow is not supported by this API; the payment was not taken.')
    if create_op.outcome == Outcome.FAILED:
        raise PaymentAPIError(422, 'payment_declined', 'PayPal declined the payment (status %s).'
                              % create_op.provider_status)
    if payment.paypal_order_status == OrderStatus.APPROVED and not payment.authorization_id:
        return _authorize_step(payment, create_op.seq)
    if recheck and create_op.outcome == Outcome.PENDING and payment.authorization_id:
        return _refresh_authorization(payment)
    return payment, create_op.outcome


def _authorize_step(payment: PayPalPayment, seq: int) -> tuple[PayPalPayment, str]:
    op, won = try_claim(
        make_ref('pay', payment.order.number, seq, 'authorize'), Kind.AUTHORIZE_ORDER, payment=payment,
        seq=seq, amount=payment.amount, currency=payment.currency, inputs={'paypalOrderId': payment.paypal_order_id})
    client = get_client()
    try:
        op, _ = safe_write(
            op, won,
            send=lambda key: client.orders.authorize_order(op.inputs['paypalOrderId'], pay_pal_request_id=key,
                                                           prefer=PREFER),
            read=_read_order, outcome_of=_order_outcome,
            apply=lambda o, r, a: _apply_authorization(payment, o, r), action='payment authorization')
    except PaymentAPIError as e:
        _record_failure(payment, op, PaymentState.AUTHORIZATION_FAILED, PaymentState.AUTHORIZING, e)
        raise
    payment.refresh_from_db()
    if op.outcome == Outcome.FAILED:
        raise PaymentAPIError(422, 'payment_declined', 'PayPal declined the authorization (status %s).'
                              % op.provider_status)
    return payment, op.outcome


def _refresh_authorization(payment: PayPalPayment) -> tuple[PayPalPayment, str]:
    """Re-read a pending hold (a read: safe to repeat)."""
    try:
        auth = get_client().payments.get_authorized_payment(payment.authorization_id)
    except Exception as e:  # ApiError, httpx transport errors, decode failures
        raise translate(e, action='authorization lookup') from e
    outcome = outcomes.authorization(auth.status)
    with transaction.atomic():
        payment.refresh_from_db()
        payment.authorization_status = '' if isinstance(auth.status, UnsetType) else str(auth.status)
        op = payment.operations.filter(kind__in=[Kind.CREATE_ORDER, Kind.AUTHORIZE_ORDER]).order_by('-seq', '-pk').first()
        if op is not None and op.outcome == Outcome.PENDING and outcome != Outcome.PENDING:
            op.outcome = outcome
            op.provider_status = payment.authorization_status
            op.save()
            _apply_hold_outcome(payment, op)
        else:
            payment.save()
    return payment, outcome


# ---------------------------------------------------------------- fulfil

def fulfil(order_number: str) -> tuple[PayPalPayment, str]:
    payment = payment_for(order_number)
    if payment.state in (PaymentState.CAPTURED, PaymentState.PARTIALLY_REFUNDED, PaymentState.REFUNDED):
        return payment, Outcome.DONE
    if payment.state not in (PaymentState.AUTHORIZED, PaymentState.CAPTURING, PaymentState.CAPTURE_FAILED):
        raise PaymentAPIError(409, 'not_authorized',
                              'Only an order with an authorized payment can be fulfilled (state %s).' % payment.state)

    last_capture = payment.operations.filter(kind=Kind.CAPTURE).order_by('-seq').first()
    unresolved = last_capture is not None and last_capture.outcome in (
        Outcome.SENDING, Outcome.UNKNOWN, Outcome.PENDING, Outcome.DONE)
    if last_capture is not None and last_capture.outcome == Outcome.PENDING:
        return _refresh_capture(payment, last_capture)
    if not unresolved:
        _renew_stale_authorization(payment)
        payment.refresh_from_db()

    seq = next_seq(payment, Kind.CAPTURE)
    op, won = try_claim(
        make_ref('capture', payment.order.number, seq), Kind.CAPTURE, payment=payment, seq=seq,
        amount=payment.amount, currency=payment.currency, inputs={'authorizationId': payment.authorization_id})
    if won:
        PayPalPayment.objects.filter(pk=payment.pk).update(state=PaymentState.CAPTURING)
    body = CaptureRequest(amount=_money(payment.amount, payment.currency), final_capture=True)
    client = get_client()
    try:
        op, _ = safe_write(
            op, won,
            send=lambda key: client.payments.capture_authorized_payment(
                op.inputs['authorizationId'], pay_pal_request_id=key, prefer=PREFER, body=body),
            read=_read_capture, outcome_of=outcomes.capture,
            apply=lambda o, r, a: _apply_capture(payment, o, r), action='capture')
    except PaymentAPIError as e:
        _record_failure(payment, op, PaymentState.CAPTURE_FAILED, PaymentState.CAPTURING, e)
        raise
    payment.refresh_from_db()
    if op.outcome == Outcome.FAILED:
        raise PaymentAPIError(422, 'capture_failed', 'PayPal did not complete the capture (status %s).'
                              % op.provider_status)
    return payment, op.outcome


def _apply_capture(payment: PayPalPayment, op: PayPalOperation, r: CapturedPayment) -> None:
    payment.refresh_from_db()
    if isinstance(r.id, str):
        payment.capture_id = r.id
    payment.capture_status = '' if isinstance(r.status, UnsetType) else str(r.status)
    payment.captured_at = provider_time(r.create_time)
    breakdown = r.seller_receivable_breakdown
    if not isinstance(breakdown, UnsetType):
        payment.paypal_fee = money.decimal_of(breakdown.paypal_fee)
        payment.net_amount = money.decimal_of(breakdown.net_amount)
    if op.outcome == Outcome.DONE:
        gross = money.decimal_of(r.amount) or payment.amount
        if not isinstance(breakdown, UnsetType):
            gross = money.decimal_of(breakdown.gross_amount) or gross
        payment.captured_amount = gross
        payment.state = PaymentState.CAPTURED
        payment.last_error = ''
        payment.source.debit(gross, reference=payment.capture_id, status=payment.capture_status)
        EventHandler().consume_stock_allocations(payment.order)
        _set_order_status(payment.order, ORDER_FULFILLED)
    elif op.outcome == Outcome.FAILED:
        payment.state = PaymentState.CAPTURE_FAILED
        payment.last_error = 'PayPal capture status %s' % payment.capture_status
    elif op.outcome == Outcome.NEEDS_REVIEW:
        payment.state = PaymentState.NEEDS_REVIEW
        payment.last_error = op.detail
    else:
        payment.state = PaymentState.CAPTURING
    payment.save()


def _refresh_capture(payment: PayPalPayment, op: PayPalOperation) -> tuple[PayPalPayment, str]:
    try:
        captured = get_client().payments.get_captured_payment(op.provider_id)
    except Exception as e:  # ApiError, httpx transport errors, decode failures
        raise translate(e, action='capture lookup') from e
    outcome = outcomes.capture(captured.status)
    if outcome != Outcome.PENDING:
        with transaction.atomic():
            op.outcome = outcome
            op.provider_status = '' if isinstance(captured.status, UnsetType) else str(captured.status)
            op.save()
            _apply_capture(payment, op, captured)
    payment.refresh_from_db()
    return payment, outcome


def _renew_stale_authorization(payment: PayPalPayment) -> None:
    """
    PayPal honours an authorization for a limited period; an older one is
    reauthorized before capture. One that cannot be renewed is reported in
    terms an operator can act on.
    """
    now = timezone.now()
    if payment.authorization_expires_at and now >= payment.authorization_expires_at:
        raise PaymentAPIError(
            409, 'authorization_expired',
            'The payment authorization %s expired on %s, so the held funds can no longer be captured. '
            'Cancel this order and ask the shopper to place and pay for a new one.'
            % (payment.authorization_id, payment.authorization_expires_at.isoformat()))
    honor = timedelta(days=settings.PAYPAL_AUTH_HONOR_PERIOD_DAYS)
    if payment.authorized_at is None or now - payment.authorized_at < honor:
        return

    seq = next_seq(payment, Kind.REAUTHORIZE)
    op, won = try_claim(
        make_ref('reauthorize', payment.order.number, seq), Kind.REAUTHORIZE, payment=payment, seq=seq,
        amount=payment.amount, currency=payment.currency, inputs={'authorizationId': payment.authorization_id})
    body = ReauthorizeRequest(amount=_money(payment.amount, payment.currency))
    client = get_client()
    try:
        op, _ = safe_write(
            op, won,
            send=lambda key: client.payments.reauthorize_payment(
                op.inputs['authorizationId'], pay_pal_request_id=key, prefer=PREFER, body=body),
            read=_read_authorization, outcome_of=outcomes.authorization,
            apply=lambda o, r, a: _apply_reauthorization(payment, o, r), action='reauthorization')
    except PaymentAPIError as e:
        issues = {i['issue'] for i in e.paypal.get('issues', [])}
        if 'REAUTHORIZATION_TOO_SOON' in issues:
            # PayPal still honours the original authorization: capture it.
            logger.info('Order %s: PayPal says reauthorization is too soon; capturing the original',
                        payment.order.number)
            return
        if e.code == 'paypal_rejected':
            age = (now - payment.authorized_at).days
            raise PaymentAPIError(
                409, 'authorization_not_renewable',
                'The payment authorization %s is %d days old, past the %d-day honor period, and PayPal '
                'refused to renew it (%s). The held funds cannot be captured: cancel this order and ask the '
                'shopper to place and pay for a new one.'
                % (payment.authorization_id, age, honor.days, e.message.split(': ', 1)[-1]),
                paypal=e.paypal) from e
        raise
    if op.outcome != Outcome.DONE:
        raise PaymentAPIError(
            409, 'authorization_not_renewed',
            'PayPal did not renew the stale authorization %s (status %s); retry fulfilment shortly, or cancel '
            'the order if it stays that way.' % (op.inputs['authorizationId'], op.provider_status))


def _apply_reauthorization(payment: PayPalPayment, op: PayPalOperation, r: PaymentAuthorization) -> None:
    if op.outcome != Outcome.DONE or not isinstance(r.id, str):
        return
    payment.refresh_from_db()
    payment.authorization_id = r.id
    payment.authorization_status = '' if isinstance(r.status, UnsetType) else str(r.status)
    payment.authorized_at = provider_time(r.create_time) or timezone.now()
    payment.authorization_expires_at = provider_time(r.expiration_time) or payment.authorization_expires_at
    payment.reauthorization_count = F('reauthorization_count') + 1
    payment.save()
    payment.source.transactions.create(
        txn_type='Reauthorise', amount=payment.amount, reference=r.id, status=payment.authorization_status)


# ---------------------------------------------------------------- cancel

def cancel(order_number: str) -> tuple[PayPalPayment, str]:
    payment = payment_for(order_number)
    order = payment.order
    if payment.state == PaymentState.VOIDED or (
            order.status == ORDER_CANCELLED and payment.state in (
                PaymentState.AWAITING_PAYMENT, PaymentState.AUTHORIZATION_FAILED)):
        return payment, Outcome.DONE
    if payment.state in (PaymentState.CAPTURED, PaymentState.PARTIALLY_REFUNDED, PaymentState.REFUNDED,
                         PaymentState.CAPTURING):
        raise PaymentAPIError(409, 'already_fulfilled',
                              'The payment has been captured; refund it instead of cancelling.')
    if payment.state in (PaymentState.AWAITING_PAYMENT, PaymentState.AUTHORIZATION_FAILED):
        # No hold exists at PayPal: nothing to release.
        with transaction.atomic():
            _set_order_status(order, ORDER_CANCELLED)
            EventHandler().cancel_stock_allocations(order)
        payment.refresh_from_db()
        return payment, Outcome.DONE
    if payment.state not in (PaymentState.AUTHORIZED, PaymentState.VOIDING, PaymentState.CAPTURE_FAILED):
        raise PaymentAPIError(409, 'not_cancellable', 'The order cannot be cancelled in state %s.' % payment.state)

    seq = next_seq(payment, Kind.VOID)
    op, won = try_claim(
        make_ref('void', order.number, seq), Kind.VOID, payment=payment, seq=seq,
        inputs={'authorizationId': payment.authorization_id})
    if won:
        PayPalPayment.objects.filter(pk=payment.pk).update(state=PaymentState.VOIDING)
    client = get_client()
    try:
        op, _ = safe_write(
            op, won,
            send=lambda key: client.payments.void_payment(op.inputs['authorizationId'], pay_pal_request_id=key,
                                                          prefer=PREFER),
            read=_read_authorization, outcome_of=outcomes.void,
            apply=lambda o, r, a: _apply_void(payment, o, r), action='cancellation')
    except PaymentAPIError as e:
        _record_failure(payment, op, PaymentState.AUTHORIZED, PaymentState.VOIDING, e)
        raise
    payment.refresh_from_db()
    if op.outcome == Outcome.FAILED:
        raise PaymentAPIError(409, 'void_refused',
                              'PayPal could not release the hold (authorization status %s).' % op.provider_status)
    return payment, op.outcome


def _apply_void(payment: PayPalPayment, op: PayPalOperation, r: PaymentAuthorization) -> None:
    payment.refresh_from_db()
    payment.authorization_status = '' if isinstance(r.status, UnsetType) else str(r.status)
    if op.outcome == Outcome.DONE:
        payment.state = PaymentState.VOIDED
        payment.last_error = ''
        source = payment.source
        source.amount_allocated -= payment.amount
        source.save()
        source.transactions.create(txn_type='Void', amount=payment.amount, reference=payment.authorization_id,
                                   status=payment.authorization_status)
        _set_order_status(payment.order, ORDER_CANCELLED)
        EventHandler().cancel_stock_allocations(payment.order)
    elif op.outcome == Outcome.FAILED:
        payment.state = PaymentState.AUTHORIZED
        payment.last_error = 'PayPal void answered status %s' % payment.authorization_status
    else:
        payment.state = PaymentState.VOIDING
    payment.save()


# ---------------------------------------------------------------- refund

def refund(order_number: str, idempotency_key: str, payload: object) -> tuple[PayPalPayment, PayPalOperation]:
    if not isinstance(payload, dict):
        raise PaymentAPIError(400, 'invalid_request', 'Body must be a JSON object.')
    payment = payment_for(order_number)
    ref = make_ref('refund', payment.order.number, idempotency_key)
    earlier = PayPalOperation.objects.filter(ref=ref).first()

    if 'amount' in payload and payload['amount'] is not None:
        try:
            amount = money.parse_amount(payload['amount'], payment.currency)
        except ValueError as e:
            raise PaymentAPIError(400, 'invalid_amount', str(e))
    elif earlier is not None and earlier.amount is not None:
        amount = earlier.amount
    else:
        amount = payment.captured_amount - payment.refund_reserved
    if earlier is None and payment.state not in (PaymentState.CAPTURED, PaymentState.PARTIALLY_REFUNDED):
        raise PaymentAPIError(409, 'not_refundable',
                              'Only a fulfilled (captured) payment can be refunded (state %s).' % payment.state)
    if amount <= 0:
        raise PaymentAPIError(422, 'nothing_to_refund', 'Nothing is left to refund on this order.')

    op, won = try_claim(ref, Kind.REFUND, payment=payment, seq=_refund_seq(payment),
                        idempotency_key=idempotency_key, amount=amount, currency=payment.currency,
                        inputs={'captureId': payment.capture_id})
    if not won and op.amount != amount:
        raise PaymentAPIError(409, 'idempotency_key_reused',
                              'This Idempotency-Key was used for a refund of %s %s.' % (op.amount, op.currency))
    if won:
        # The refund cap, enforced by one conditional UPDATE: two concurrent
        # refunds cannot both fit into what is left.
        reserved = PayPalPayment.objects.filter(
            pk=payment.pk, refund_reserved__lte=F('captured_amount') - amount,
            state__in=[PaymentState.CAPTURED, PaymentState.PARTIALLY_REFUNDED],
        ).update(refund_reserved=F('refund_reserved') + amount)
        if not reserved:
            op.outcome = Outcome.FAILED
            op.detail = 'exceeds the refundable amount'
            op.save()
            payment.refresh_from_db()
            raise PaymentAPIError(
                422, 'refund_exceeds_captured',
                'A refund of %s %s would exceed what is left to refund (%s %s).'
                % (amount, payment.currency, payment.captured_amount - payment.refund_reserved, payment.currency))
        op.reserved = True
        op.save(update_fields=['reserved', 'updated'])
    elif op.outcome == Outcome.PENDING and op.provider_id:
        return _refresh_refund(payment, op)

    body = RefundRequest(amount=_money(amount, payment.currency), note_to_payer='Refund for order %s' %
                         payment.order.number)
    client = get_client()
    try:
        op, _ = safe_write(
            op, won,
            send=lambda key: client.payments.refund_captured_payment(
                op.inputs['captureId'], pay_pal_request_id=key, prefer=PREFER, body=body),
            read=_read_refund, outcome_of=outcomes.refund,
            apply=lambda o, r, a: _apply_refund(payment, o), action='refund')
    except PaymentAPIError:
        op.refresh_from_db()
        _release_if_failed(payment, op)
        raise
    payment.refresh_from_db()
    return payment, op


def _refund_seq(payment: PayPalPayment) -> int:
    # Refunds are keyed by the caller's key; seq only keeps (payment, kind, seq) unique.
    last = payment.operations.filter(kind=Kind.REFUND).order_by('-seq').first()
    return 1 if last is None else last.seq + 1


def _apply_refund(payment: PayPalPayment, op: PayPalOperation) -> None:
    if op.outcome == Outcome.DONE:
        PayPalPayment.objects.filter(pk=payment.pk).update(refunded_amount=F('refunded_amount') + op.amount)
        payment.refresh_from_db()
        payment.source.refund(op.amount, reference=op.provider_id, status=op.provider_status)
        payment.state = (PaymentState.REFUNDED if payment.refunded_amount >= payment.captured_amount
                         else PaymentState.PARTIALLY_REFUNDED)
        payment.save(update_fields=['state', 'updated'])
    else:
        _release_if_failed(payment, op)


def _release_if_failed(payment: PayPalPayment, op: PayPalOperation) -> None:
    if op.outcome == Outcome.FAILED and op.reserved:
        with transaction.atomic():
            released = PayPalOperation.objects.filter(pk=op.pk, reserved=True).update(reserved=False)
            if released:
                PayPalPayment.objects.filter(pk=payment.pk).update(refund_reserved=F('refund_reserved') - op.amount)
        op.reserved = False


def _refresh_refund(payment: PayPalPayment, op: PayPalOperation) -> tuple[PayPalPayment, PayPalOperation]:
    try:
        r = get_client().payments.get_refund(op.provider_id)
    except Exception as e:  # ApiError, httpx transport errors, decode failures
        raise translate(e, action='refund lookup') from e
    outcome = outcomes.refund(r.status)
    if outcome != Outcome.PENDING:
        with transaction.atomic():
            op.outcome = outcome
            op.provider_status = '' if isinstance(r.status, UnsetType) else str(r.status)
            op.save()
            _apply_refund(payment, op)
    payment.refresh_from_db()
    return payment, op

