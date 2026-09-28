"""
Money movement for an order: hold at checkout, take at fulfilment, release on
cancel, give back on refund. Every PayPal write goes through ``safe_write``.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_model
from paypal.core import UNSET, ApiError, UnsetType
from paypal.models import (
    Address, AmountWithBreakdown, CapturedPayment, CaptureRequest, CardRequest, Money, Order, OrderAuthorizeResponse,
    OrderRequest, PaymentAuthorization, PaymentSource, PurchaseUnitRequest, ReauthorizeRequest, Refund,
    RefundRequest)
from paypal.models.enums import AuthorizationStatus, CheckoutPaymentIntent, OrderStatus

from . import money
from .cards import card_label, parse_card
from .errors import NEVER_SENT, ApiProblem, OutcomeUnknown, ProviderError, issues_of, paypal_read
from .models import PayPalPayment, ProviderWrite
from .orders import refundable_amount
from .outcomes import (
    DONE, FAILED, PENDING, authorization_outcome, capture_outcome, order_outcome, refund_outcome, void_outcome)
from .paypal_client import get_client
from .reconcile import find_authorization_by_custom_id
from .references import key_digest, reference
from .safe_write import Answer, complete, provider_time, safe_write, try_claim

logger = logging.getLogger(__name__)

Bankcard = get_model('payment', 'Bankcard')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Transaction = get_model('payment', 'Transaction')

REPRESENTATION = 'return=representation'
# Documented on reauthorize_payment: a hold is honored for three days; it can be
# reauthorized once, from day 4 to day 29 after the original authorization.
HONOR_PERIOD = timedelta(days=3)
REAUTHORIZE_LIMIT = timedelta(days=29)

STATUS_AUTHORIZED = 'Being processed'
STATUS_FULFILLED = 'Complete'
STATUS_CANCELLED = 'Cancelled'


def _money(value: Decimal, currency: str) -> Money:
    return Money(currency_code=currency, value=money.to_wire(value, currency))


def _set_order_status(order: Any, status: str) -> None:
    if order.status != status and status in order.available_statuses():
        order.set_status(status)


def _source(payment: PayPalPayment) -> Any:
    if payment.source is None:
        source_type, __ = SourceType.objects.get_or_create(name='PayPal')
        payment.source = Source.objects.create(
            order=payment.order, source_type=source_type, currency=payment.currency,
            label=payment.card_label)
        payment.save(update_fields=['source'])
    return payment.source


# ---------------------------------------------------------------- authorize

@dataclass
class _Seen:
    """What the last PayPal response in this request said beyond the Answer."""
    paypal_order_id: str = ''
    authorization: PaymentAuthorization | Any = None
    card: Any = None


def _authorization_answer(auth: Any, seen: _Seen) -> Answer:
    seen.authorization = auth
    amount = auth.amount if not isinstance(auth.amount, UnsetType) else None
    return Answer(
        provider_id=auth.id if isinstance(auth.id, str) else '',
        status=auth.status if not isinstance(auth.status, UnsetType) else None,
        provider_time=provider_time(auth.create_time),
        amount=amount.value if amount else None,
        currency=amount.currency_code if amount else None,
    )


def _read_paypal_order(result: Order | OrderAuthorizeResponse | PaymentAuthorization, seen: _Seen) -> Answer:
    if isinstance(result, PaymentAuthorization):  # from the lookup
        return _authorization_answer(result, seen)
    if isinstance(result.id, str):
        seen.paypal_order_id = result.id
    if not isinstance(result.payment_source, UnsetType) and not isinstance(result.payment_source.card, UnsetType):
        seen.card = result.payment_source.card
    units = result.purchase_units if isinstance(result.purchase_units, list) else []
    unit = units[0] if units else None
    payments = unit.payments if unit is not None and not isinstance(unit.payments, UnsetType) else None
    auths = payments.authorizations if payments is not None and isinstance(payments.authorizations, list) else []
    if auths:
        return _authorization_answer(auths[0], seen)
    # No hold yet: the PayPal order's own status says why.
    amount = unit.amount if unit is not None and not isinstance(unit.amount, UnsetType) else None
    return Answer(
        provider_id='',
        status=result.status if not isinstance(result.status, UnsetType) else None,
        provider_time=provider_time(result.create_time),
        amount=amount.value if amount else None,
        currency=amount.currency_code if amount else None,
    )


def _authorize_step_outcome(status: object) -> str:
    # Checked first: OrderStatus and AuthorizationStatus share wire values.
    if isinstance(status, OrderStatus):
        return order_outcome(status)
    return authorization_outcome(status)


@sensitive_variables('card', 'payload', 'source')
def authorize(user: Any, order: Any, payload: dict[str, Any]) -> tuple[str, PayPalPayment]:
    """Put a hold on the order total. Returns (outcome, payment)."""
    payment: PayPalPayment = order.paypal_payment
    latest = ProviderWrite.objects.filter(order=order, kind=ProviderWrite.AUTHORIZE).order_by('-pk').first()

    if payment.state not in PayPalPayment.PAYABLE_STATES:
        if latest is not None and latest.outcome in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN):
            # A request is in flight or unsettled: answer or check it, never a second hold.
            return _run_authorize(payment, latest.ref, send=None)
        outcome = DONE if payment.state not in (PayPalPayment.AUTHORIZATION_PENDING, PayPalPayment.NEEDS_REVIEW) \
            else (PENDING if payment.state == PayPalPayment.AUTHORIZATION_PENDING else 'needs_review')
        if payment.state in (PayPalPayment.CANCELLED, PayPalPayment.VOIDED):
            raise ApiProblem(409, 'order_cancelled', 'This order was cancelled and cannot be paid.')
        return outcome, payment

    currency, amount = payment.currency, payment.amount
    if 'paymentMethodId' in payload and 'card' in payload:
        raise ApiProblem(422, 'invalid_payment_source', 'Send either "card" or "paymentMethodId", not both.')
    saved = None
    if 'paymentMethodId' in payload:
        try:
            saved = Bankcard.objects.get(pk=int(payload['paymentMethodId']), user=user)
        except (Bankcard.DoesNotExist, TypeError, ValueError):
            raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.') from None
        source = PaymentSource(card=CardRequest(vault_id=saved.partner_reference))
        label = card_label(saved.card_type, saved.number[-4:])
    elif 'card' in payload:
        card = parse_card(payload['card'])
        source = PaymentSource(card=CardRequest(
            number=card.number, expiry=card.expiry, security_code=card.security_code,
            name=card.name or UNSET,
            billing_address=Address(**card.billing_address) if card.billing_address else UNSET))
        label = card_label('', card.number[-4:])
    else:
        raise ApiProblem(422, 'invalid_payment_source', 'Send "card" details or a saved "paymentMethodId".')

    attempt = ProviderWrite.objects.filter(order=order, kind=ProviderWrite.AUTHORIZE).count()
    ref = reference('order', order.number, 'authorize', attempt)
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            reference_id=order.number,
            custom_id=ref,  # the lookup key when the outcome is unknown
            description='Order %s' % order.number,
            amount=AmountWithBreakdown(currency_code=currency, value=money.to_wire(amount, currency)),
        )],
        payment_source=source,
    )

    def send(key: str) -> Order:
        return get_client().orders.create_order(body, pay_pal_request_id=key, prefer=REPRESENTATION)

    if not try_claim(ref, kind=ProviderWrite.AUTHORIZE, order=order, user=user, amount=amount, currency=currency):
        # Lost the race to a concurrent request for the same attempt.
        return _run_authorize(payment, ref, send=None)
    PayPalPayment.objects.filter(pk=payment.pk).update(
        state=PayPalPayment.AUTHORIZING, payment_method=saved, card_label=label, last_error='')
    payment.refresh_from_db()
    return _run_authorize(payment, ref, send=send, claimed=True)


def _run_authorize(payment: PayPalPayment, ref: str, *, send: Callable[[str], Order] | None,
                   claimed: bool = False) -> tuple[str, PayPalPayment]:
    seen = _Seen()
    client = get_client()
    claim_time = ProviderWrite.objects.get(ref=ref).claimed_at

    def find(key: str) -> Order | PaymentAuthorization | None:
        # create_order does not replay under the same request id, so an unknown
        # outcome is looked up by the reference it carried as custom_id.
        return find_authorization_by_custom_id(client, key, since=claim_time - timedelta(hours=1))

    def create(key: str) -> Order | PaymentAuthorization:
        if send is None:  # only a first attempt sends; a check never re-creates (repeat_is_safe=False)
            raise AssertionError('create_order is never re-sent')
        return send(key)

    try:
        write = safe_write(
            ref,
            send=create,
            find=find,
            read=lambda r: _read_paypal_order(r, seen),
            outcome_of=_authorize_step_outcome,
            repeat_is_safe=False,
            sent=(payment.amount, payment.currency),
            claimed=claimed,
        )
    except OutcomeUnknown:
        _mark(payment, PayPalPayment.AUTHORIZATION_UNKNOWN, 'PayPal did not confirm the authorization.')
        raise
    except ProviderError as e:
        if e.code == 'needs_review':
            _mark(payment, PayPalPayment.NEEDS_REVIEW, e.message)
        else:
            _mark(payment, PayPalPayment.AWAITING_PAYMENT, e.message)
        raise
    except NEVER_SENT:
        # Never sent (connection refused etc.): nothing happened, the order stays payable.
        _mark(payment, PayPalPayment.AWAITING_PAYMENT, 'PayPal could not be reached.')
        raise

    if write.outcome == PENDING and seen.paypal_order_id and not seen.authorization:
        # PayPal created the order but placed no hold yet (status APPROVED):
        # authorize it as a separate, separately-claimed step.
        write = _authorize_paypal_order(payment, write, seen)
    return _apply_authorization(payment, write, seen), payment


def _authorize_paypal_order(payment: PayPalPayment, parent: ProviderWrite, seen: _Seen) -> ProviderWrite:
    client = get_client()
    paypal_order_id = seen.paypal_order_id
    ref = parent.ref + ':order-authorize'
    def send(key: str) -> Order | OrderAuthorizeResponse:
        return client.orders.authorize_order(paypal_order_id, pay_pal_request_id=key, prefer=REPRESENTATION)

    def find(key: str) -> Order | OrderAuthorizeResponse:
        return client.orders.get_order(paypal_order_id)  # the order record shows its authorization

    return safe_write(
        ref,
        send=send,
        find=find,
        read=lambda r: _read_paypal_order(r, seen),
        outcome_of=_authorize_step_outcome,
        repeat_is_safe=False,
        sent=(payment.amount, payment.currency),
        claim={'kind': ProviderWrite.ORDER_AUTHORIZE, 'order': payment.order, 'user': parent.user,
               'amount': payment.amount, 'currency': payment.currency, 'target_id': paypal_order_id},
    )


def _mark(payment: PayPalPayment, state: str, error: str = '') -> None:
    PayPalPayment.objects.filter(pk=payment.pk).update(state=state, last_error=error[:512])
    payment.refresh_from_db()


def _expiry(auth: Any) -> datetime | None:
    return provider_time(getattr(auth, 'expiration_time', None)) if auth is not None else None


@transaction.atomic
def _apply_authorization(payment: PayPalPayment, write: ProviderWrite, seen: _Seen) -> str:
    payment = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
    if seen.paypal_order_id:
        payment.paypal_order_id = seen.paypal_order_id
    if seen.card is not None and isinstance(seen.card.last_digits, str):
        brand = seen.card.brand if isinstance(seen.card.brand, str) else ''
        payment.card_label = card_label(str(brand), seen.card.last_digits)
    outcome = write.outcome
    if outcome == DONE:
        if payment.authorization_id != write.provider_id:
            payment.authorization_id = write.provider_id
            payment.authorization_created_at = write.provider_time
            payment.original_authorization_at = payment.original_authorization_at or write.provider_time
            _source(payment).allocate(payment.amount, reference=write.provider_id, status=write.provider_status)
        payment.authorization_status = write.provider_status
        payment.authorization_expires_at = _expiry(seen.authorization) or payment.authorization_expires_at
        payment.state = PayPalPayment.AUTHORIZED
        payment.last_error = ''
        _set_order_status(payment.order, STATUS_AUTHORIZED)
    elif outcome == PENDING:
        payment.state = PayPalPayment.AUTHORIZATION_PENDING
        payment.authorization_id = write.provider_id or payment.authorization_id
    elif outcome == FAILED:
        payment.state = PayPalPayment.AWAITING_PAYMENT
        if write.provider_status == OrderStatus.PAYER_ACTION_REQUIRED:
            payment.last_error = ('The card issuer requires the shopper to complete a browser challenge '
                                  '(3-D Secure), which this API does not support.')
        else:
            payment.last_error = 'PayPal declined the authorization (%s).' % (write.provider_status or 'refused')
    elif outcome == ProviderWrite.NEEDS_REVIEW:
        payment.state = PayPalPayment.NEEDS_REVIEW
    elif outcome == ProviderWrite.UNKNOWN:
        payment.state = PayPalPayment.AUTHORIZATION_UNKNOWN
    payment.save()
    return str(outcome)


# ------------------------------------------------------------------ fulfil

def fulfil(order: Any) -> tuple[str, PayPalPayment]:
    """Take the held money. Renews a stale hold first, where PayPal allows it."""
    payment: PayPalPayment = order.paypal_payment
    client = get_client()

    if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        return DONE, payment
    if payment.state == PayPalPayment.CAPTURE_PENDING:
        with paypal_read():
            capture = client.payments.get_captured_payment(payment.capture_id)
        write = ProviderWrite.objects.filter(order=order, kind=ProviderWrite.CAPTURE).order_by('-pk').first()
        answer = _capture_answer(capture)
        if write is not None:
            write = complete(write.ref, capture_outcome(answer.status), answer)
            return _apply_capture(payment, write, capture), payment
        return PENDING, payment
    if payment.state == PayPalPayment.CAPTURING:
        latest = ProviderWrite.objects.filter(order=order, kind=ProviderWrite.CAPTURE).order_by('-pk').first()
        if latest is not None:
            return _capture(payment, latest.target_id)
    if payment.state != PayPalPayment.AUTHORIZED:
        raise ApiProblem(409, 'not_authorized',
                         'Order %s has no authorized payment to capture (payment state: %s).'
                         % (order.number, payment.state))

    with paypal_read():
        auth = client.payments.get_authorized_payment(payment.authorization_id)
    status = auth.status if not isinstance(auth.status, UnsetType) else None
    expires = _expiry(auth)
    PayPalPayment.objects.filter(pk=payment.pk).update(
        authorization_status=str(status or ''), authorization_expires_at=expires or payment.authorization_expires_at)
    payment.refresh_from_db()
    now = timezone.now()

    if status in (AuthorizationStatus.CAPTURED, AuthorizationStatus.PARTIALLY_CAPTURED):
        return _capture(payment, payment.authorization_id)  # an earlier capture landed: read it back
    if status in (AuthorizationStatus.VOIDED, AuthorizationStatus.DENIED):
        _mark(payment, PayPalPayment.AUTHORIZATION_EXPIRED, 'Authorization is %s at PayPal.' % status)
        raise ApiProblem(409, 'authorization_not_capturable',
                         'PayPal reports the authorization as %s, so there is nothing to capture. Ask the '
                         'shopper to pay again (POST /api/orders/%s/pay) or cancel the order.'
                         % (status, order.number))
    if expires is not None and expires <= now:
        _expired(payment, 'The authorization expired at %s.' % expires.isoformat())

    held_since = payment.authorization_created_at or provider_time(auth.create_time)
    if held_since is not None and now - held_since > HONOR_PERIOD:
        _renew(payment, now)
    return _capture(payment, payment.authorization_id)


def _expired(payment: PayPalPayment, why: str) -> None:
    message = ('%s It can no longer be renewed, so the funds are no longer held. Ask the shopper to pay '
               'again (POST /api/orders/%s/pay), or cancel the order (POST /api/orders/%s/cancel).'
               % (why, payment.order.number, payment.order.number))
    _mark(payment, PayPalPayment.AUTHORIZATION_EXPIRED, message)
    raise ApiProblem(409, 'authorization_expired', message)


def _renew(payment: PayPalPayment, now: datetime) -> None:
    """Reauthorize a hold whose honor period has passed."""
    original = payment.original_authorization_at or payment.authorization_created_at
    if payment.reauthorized:
        _expired(payment, 'The authorization is past its honor period and has already been reauthorized '
                          'once; PayPal allows a single reauthorization.')
    if original is not None and now - original > REAUTHORIZE_LIMIT:
        _expired(payment, 'The authorization is more than 29 days old; PayPal only allows '
                          'reauthorization from day 4 to day 29.')
    client = get_client()
    old_id = payment.authorization_id
    seen = _Seen()
    try:
        write = safe_write(
            reference('order', payment.order.number, 'reauthorize', old_id),
            send=lambda key: client.payments.reauthorize_payment(
                old_id, pay_pal_request_id=key, prefer=REPRESENTATION,
                body=ReauthorizeRequest(amount=_money(payment.amount, payment.currency))),
            find=lambda key: client.payments.reauthorize_payment(
                old_id, pay_pal_request_id=key, prefer=REPRESENTATION,
                body=ReauthorizeRequest(amount=_money(payment.amount, payment.currency))),
            read=lambda r: _authorization_answer(r, seen),
            outcome_of=authorization_outcome,
            repeat_is_safe=True,
            sent=(payment.amount, payment.currency),
            claim={'kind': ProviderWrite.REAUTHORIZE, 'order': payment.order, 'amount': payment.amount,
                   'currency': payment.currency, 'target_id': old_id},
        )
    except ProviderError as e:
        if e.outcome_unknown:
            raise
        issues = ', '.join(i['issue'] for i in e.paypal.get('issues', []))
        _expired(payment, 'The authorization is past its honor period and PayPal refused to renew it%s.'
                 % (' (%s)' % issues if issues else ''))
    if write.outcome != DONE:
        if write.outcome == PENDING:
            raise ApiProblem(409, 'reauthorization_pending',
                             'PayPal accepted the reauthorization but has not completed it; retry fulfilment later.')
        _expired(payment, 'PayPal did not renew the authorization (status %s).' % (write.provider_status or 'unknown'))
    with transaction.atomic():
        p = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        p.authorization_id = write.provider_id
        p.authorization_status = write.provider_status
        p.authorization_created_at = write.provider_time or now
        p.authorization_expires_at = _expiry(seen.authorization) or p.authorization_expires_at
        p.reauthorized = True
        p.save()
    payment.refresh_from_db()


def _capture_answer(capture: CapturedPayment) -> Answer:
    amount = capture.amount if not isinstance(capture.amount, UnsetType) else None
    return Answer(
        provider_id=capture.id if isinstance(capture.id, str) else '',
        status=capture.status if not isinstance(capture.status, UnsetType) else None,
        provider_time=provider_time(capture.create_time),
        amount=amount.value if amount else None,
        currency=amount.currency_code if amount else None,
    )


def _capture(payment: PayPalPayment, authorization_id: str) -> tuple[str, PayPalPayment]:
    client = get_client()
    ref = reference('order', payment.order.number, 'capture', authorization_id)
    captured: dict[str, CapturedPayment] = {}

    def send(key: str) -> CapturedPayment:
        return client.payments.capture_authorized_payment(
            authorization_id, pay_pal_request_id=key, prefer=REPRESENTATION,
            body=CaptureRequest(amount=_money(payment.amount, payment.currency), final_capture=True))

    def read(result: CapturedPayment) -> Answer:
        captured['capture'] = result
        return _capture_answer(result)

    PayPalPayment.objects.filter(pk=payment.pk, state=PayPalPayment.AUTHORIZED).update(state=PayPalPayment.CAPTURING)
    try:
        write = safe_write(
            ref, send=send, find=send, read=read, outcome_of=capture_outcome, repeat_is_safe=True,
            sent=(payment.amount, payment.currency),
            claim={'kind': ProviderWrite.CAPTURE, 'order': payment.order, 'amount': payment.amount,
                   'currency': payment.currency, 'target_id': authorization_id},
        )
    except OutcomeUnknown:
        _mark(payment, PayPalPayment.CAPTURING, 'PayPal did not confirm the capture; retry fulfilment to check.')
        raise
    except ProviderError as e:
        _mark(payment, PayPalPayment.NEEDS_REVIEW if e.code == 'needs_review' else PayPalPayment.AUTHORIZED,
              e.message)
        raise
    except NEVER_SENT:
        _mark(payment, PayPalPayment.AUTHORIZED, 'PayPal could not be reached.')
        raise
    return _apply_capture(payment, write, captured.get('capture')), payment


@transaction.atomic
def _apply_capture(payment: PayPalPayment, write: ProviderWrite, capture: CapturedPayment | None) -> str:
    payment = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
    outcome = write.outcome
    if outcome in (DONE, PENDING):
        payment.capture_id = write.provider_id
        payment.capture_status = write.provider_status
        payment.captured_amount = write.provider_amount if write.provider_amount is not None else payment.amount
        payment.captured_at = write.provider_time
        breakdown = capture.seller_receivable_breakdown if capture is not None else None
        if breakdown is not None and not isinstance(breakdown, UnsetType):
            if not isinstance(breakdown.paypal_fee, UnsetType):
                payment.paypal_fee = Decimal(breakdown.paypal_fee.value)
            if not isinstance(breakdown.net_amount, UnsetType):
                payment.net_amount = Decimal(breakdown.net_amount.value)
        if outcome == DONE:
            payment.state = PayPalPayment.CAPTURED if payment.refunded_amount == 0 else payment.state
            payment.last_error = ''
            source = _source(payment)
            if not source.transactions.filter(txn_type=Transaction.DEBIT, reference=write.provider_id).exists():
                source.debit(payment.captured_amount, reference=write.provider_id, status=write.provider_status)
            _set_order_status(payment.order, STATUS_AUTHORIZED)
            _set_order_status(payment.order, STATUS_FULFILLED)
        else:
            payment.state = PayPalPayment.CAPTURE_PENDING
    elif outcome == FAILED:
        payment.state = PayPalPayment.AUTHORIZED
        payment.last_error = ('PayPal did not capture the payment (status %s). Cancel the order to release '
                              'the hold, or ask the shopper to pay again.' % (write.provider_status or 'refused'))
    elif outcome == ProviderWrite.NEEDS_REVIEW:
        payment.state = PayPalPayment.NEEDS_REVIEW
    else:
        payment.state = PayPalPayment.CAPTURING
    payment.save()
    return str(outcome)


# ------------------------------------------------------------------ cancel

def cancel(order: Any) -> tuple[str, PayPalPayment]:
    """Cancel before fulfilment: release the hold, if there is one."""
    payment: PayPalPayment = order.paypal_payment
    if payment.state in (PayPalPayment.VOIDED, PayPalPayment.CANCELLED):
        return DONE, payment
    if payment.state in PayPalPayment.PAYABLE_STATES:
        # Nothing is held (never authorized, or the hold lapsed): no money moves.
        with transaction.atomic():
            _set_order_status(order, STATUS_CANCELLED)
            _mark(payment, PayPalPayment.CANCELLED)
        return DONE, payment
    if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED,
                         PayPalPayment.CAPTURE_PENDING):
        raise ApiProblem(409, 'already_fulfilled',
                         'Order %s has been fulfilled and the money taken; use refunds instead.' % order.number)
    if payment.state != PayPalPayment.AUTHORIZED:
        raise ApiProblem(409, 'payment_in_progress',
                         'The payment for order %s is %s; its outcome must settle before it can be cancelled.'
                         % (order.number, payment.state))

    client = get_client()
    auth_id = payment.authorization_id
    seen = _Seen()

    def send(key: str) -> PaymentAuthorization:
        return client.payments.void_payment(auth_id, pay_pal_request_id=key, prefer=REPRESENTATION)

    write = safe_write(
        reference('order', order.number, 'void', auth_id),
        send=send,
        find=lambda key: client.payments.get_authorized_payment(auth_id),
        read=lambda r: _authorization_answer(r, seen),
        outcome_of=void_outcome,
        repeat_is_safe=True,
        # "Previously voided" means an earlier attempt already released it.
        landed=lambda e: 'PREVIOUSLY_VOIDED' in issues_of(e.error),
        claim={'kind': ProviderWrite.VOID, 'order': order, 'target_id': auth_id},
    )
    with transaction.atomic():
        p = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        p.authorization_status = write.provider_status or p.authorization_status
        if write.outcome == DONE:
            p.state = PayPalPayment.VOIDED
            p.last_error = ''
            Transaction.objects.create(source=_source(p), txn_type='Void', amount=p.amount,
                                       reference=auth_id, status=write.provider_status)
            _set_order_status(order, STATUS_CANCELLED)
        elif write.outcome == FAILED:
            p.last_error = 'PayPal reports the authorization as %s; it can no longer be voided.' % write.provider_status
        p.save()
    payment.refresh_from_db()
    if write.outcome == FAILED:
        raise ApiProblem(409, 'already_captured', payment.last_error)
    return str(write.outcome), payment


# ------------------------------------------------------------------ refund

def refund(user: Any, order: Any, payload: dict[str, Any], idempotency_key: str) -> ProviderWrite:
    """Return captured money, in full or in part, at most once per key."""
    payment: PayPalPayment = order.paypal_payment
    currency = payment.currency
    requested = payload.get('amount')
    amount = money.parse(requested, currency) if requested is not None else None
    fingerprint = key_digest(amount if amount is not None else 'remaining')
    ref = reference('order', order.number, 'refund', key_digest(user.pk, idempotency_key))

    existing = ProviderWrite.objects.filter(ref=ref).first()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ApiProblem(422, 'idempotency_key_reused',
                             'This Idempotency-Key was already used for a different refund request.')
        return _run_refund(payment, existing.ref, claimed=False)

    with transaction.atomic():
        # Lock the payment so concurrent refunds reserve against the same total.
        locked = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        if locked.state not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
            raise ApiProblem(409, 'not_refundable',
                             'Order %s has no captured payment to refund (payment state: %s).'
                             % (order.number, locked.state))
        available = refundable_amount(locked)
        amount = available if amount is None else amount
        if available <= 0 or amount > available:
            raise ApiProblem(409, 'refund_exceeds_captured',
                             'At most %s %s can still be refunded on this order.'
                             % (money.to_wire(available, currency), currency),
                             refundableAmount=money.to_wire(available, currency))
        claimed = try_claim(ref, kind=ProviderWrite.REFUND, order=order, user=user, amount=amount,
                            currency=currency, request_fingerprint=fingerprint, target_id=locked.capture_id)
    return _run_refund(payment, ref, claimed=claimed)


def _refund_answer(r: Refund) -> Answer:
    amount = r.amount if not isinstance(r.amount, UnsetType) else None
    return Answer(
        provider_id=r.id if isinstance(r.id, str) else '',
        status=r.status if not isinstance(r.status, UnsetType) else None,
        provider_time=provider_time(r.create_time),
        amount=amount.value if amount else None,
        currency=amount.currency_code if amount else None,
    )


def _run_refund(payment: PayPalPayment, ref: str, *, claimed: bool) -> ProviderWrite:
    client = get_client()
    claim = ProviderWrite.objects.get(ref=ref)
    amount, currency, capture_id = claim.amount, claim.currency, claim.target_id

    def send(key: str) -> Refund:
        return client.payments.refund_captured_payment(
            capture_id, pay_pal_request_id=key, prefer=REPRESENTATION,
            body=RefundRequest(amount=_money(amount, currency),
                               custom_id=reference('order', payment.order.number)))

    write = safe_write(ref, send=send, find=send, read=_refund_answer, outcome_of=refund_outcome,
                       repeat_is_safe=True, sent=(amount, currency), claimed=claimed)
    _apply_refunds(payment)
    return write


@transaction.atomic
def _apply_refunds(payment: PayPalPayment) -> None:
    p = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
    done = ProviderWrite.objects.filter(order_id=p.order_id, kind=ProviderWrite.REFUND, outcome=DONE)
    total = sum((w.provider_amount or w.amount or Decimal('0')) for w in done)
    source = _source(p)
    recorded = {t.reference for t in source.transactions.filter(txn_type=Transaction.REFUND)}
    for w in done:
        if w.provider_id not in recorded:
            source.refund(w.provider_amount or w.amount, reference=w.provider_id, status=w.provider_status)
    p.refunded_amount = total
    if p.captured_amount is not None and total > 0:
        p.state = PayPalPayment.REFUNDED if total >= p.captured_amount else PayPalPayment.PARTIALLY_REFUNDED
    p.save()
    payment.refresh_from_db()


def run_pending_check(write: ProviderWrite) -> ProviderWrite:
    """Re-check one unsettled write (operator command). Returns the updated row."""
    payment = write.order.paypal_payment if write.order_id else None
    try:
        if write.kind == ProviderWrite.AUTHORIZE and payment is not None:
            _run_authorize(payment, write.ref, send=None)
        elif write.kind == ProviderWrite.CAPTURE and payment is not None:
            _capture(payment, write.target_id)
        elif write.kind == ProviderWrite.REFUND and payment is not None:
            _run_refund(payment, write.ref, claimed=False)
    except (ApiProblem, ApiError) as e:
        logger.info('Check of %s still unsettled: %s', write.ref, e)
    return ProviderWrite.objects.get(pk=write.pk)
