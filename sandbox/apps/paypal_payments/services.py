"""
Order payment flows: place, pay (authorize), fulfil (capture), cancel (void),
refund, and saved cards.

Every provider write follows the same shape:

1. CLAIM  — a conditional UPDATE (or a unique INSERT) the database applies for
   exactly one caller, committed before PayPal is called. A caller that loses
   the claim answers from the stored record and never makes a new write.
2. CALL / CHECK / VERIFY — ``gateway.perform``, under a reference derived from
   the claimed row, so a repeat is the same write, never a second one.
3. COMPLETE — record what PayPal said (its ids, status and clock) and answer
   through ``answer_status``.

Views are non-atomic so each of these steps commits on its own.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price

from . import gateway
from .client import get_client, paypal_config
from .gateway import DONE, FAILED, NEEDS_REVIEW, PENDING, UNKNOWN, CardDetails, ProviderError, issue_of
from .models import OrderPayment, PaymentRefund, PayPalCustomer, SavedCard

logger = logging.getLogger('apps.paypal_payments')

Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
Order = get_model('order', 'Order')
PaymentEventType = get_model('order', 'PaymentEventType')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Applicator = get_class('offer.applicator', 'Applicator')
Free = get_class('shipping.methods', 'Free')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
EventHandler = get_class('order.processing', 'EventHandler')
InvalidOrderStatus = get_class('order.exceptions', 'InvalidOrderStatus')

# Authorization lifetime, from the SDK's reauthorize_payment documentation:
# a 3-day honor period, one reauthorization allowed from day 4 to day 29.
HONOR_PERIOD = timedelta(days=3)
AUTHORIZATION_LIFETIME = timedelta(days=29)
# PayPal keeps vault PayPal-Request-Id keys for 3 hours; a resend after that
# could create a second token, so it is only a check inside the window.
VAULT_KEY_WINDOW = timedelta(hours=3)

ORDER_STATUS_PAID = 'Being processed'
ORDER_STATUS_COMPLETE = 'Complete'
ORDER_STATUS_CANCELLED = 'Cancelled'


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


@dataclass
class Outcome:
    """What an endpoint answers: the HTTP status comes from the outcome alone."""
    outcome: str
    body: dict[str, Any]
    error: ProviderError | None = None
    created: bool = False


def answer_status(outcome: str, *, created: bool = False, error: ProviderError | None = None) -> int:
    """The ONE place an outcome becomes an HTTP status."""
    match outcome:
        case 'done':
            return 201 if created else 200
        case 'pending' | 'sending':
            return 202
        case 'failed':
            return error.status_code if error is not None and error.status_code != 504 else 402
        case 'needs_review':
            return 409
        case _:
            return 504


def send_window() -> timedelta:
    """Longer than any single step can take: a claim older than this was abandoned."""
    return timedelta(seconds=paypal_config().timeout * 4 + 30)


def _now() -> datetime:
    return timezone.now()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def place_order(request, items: list[tuple[int, int]]) -> OrderPayment:
    """Place an Oscar order for the caller from (product id, quantity) pairs."""
    config = paypal_config()
    product_ids = [pid for pid, _ in items]
    products = {p.pk: p for p in Product.objects.filter(pk__in=product_ids)}
    missing = [pid for pid in product_ids if pid not in products]
    if missing:
        raise ApiProblem(400, 'unknown_product', 'No catalogue item with id %s.' % missing[0])

    with transaction.atomic():
        basket = Basket.objects.create(owner=request.user)
        basket.strategy = request.strategy
        for product_id, quantity in items:
            product = products[product_id]
            if product.is_parent or not product.is_public:
                raise ApiProblem(400, 'not_purchasable',
                                 'Catalogue item %s cannot be bought directly; choose one of its variants.'
                                 % product_id)
            info = request.strategy.fetch_for_product(product)
            if not info.availability.is_available_to_buy:
                raise ApiProblem(409, 'unavailable', 'Catalogue item %s is not available.' % product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ApiProblem(409, 'quantity_not_available', str(reason))
            basket.add_product(product, quantity)
        Applicator().apply(basket, request.user, request)

        shipping_method = Free()
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        if not total.is_tax_known:
            raise ApiProblem(409, 'tax_unknown', 'The order total cannot be determined.')
        amount = total.incl_tax.quantize(gateway.TWO_PLACES)
        if amount <= 0:
            raise ApiProblem(400, 'zero_total', 'The order total must be greater than zero.')
        # Amounts come from the catalogue; the currency from configuration.
        order_total = Price(currency=config.currency, excl_tax=total.excl_tax, incl_tax=total.incl_tax)
        number = OrderNumberGenerator().order_number(basket)
        order = OrderCreator().place_order(
            basket=basket, total=order_total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=request.user, order_number=number, request=request)
        basket.submit()
        return OrderPayment.objects.create(
            order=order,
            reference='%s-%s-%s' % (config.reference_prefix, number, secrets.token_hex(4)),
            currency=config.currency,
            amount=amount,
        )


def payment_for_owner(user, order_number: str) -> OrderPayment:
    try:
        return OrderPayment.objects.select_related('order').get(order__number=order_number, order__user=user)
    except OrderPayment.DoesNotExist:
        raise ApiProblem(404, 'order_not_found', 'No such order.')


def payment_for_operator(order_number: str) -> OrderPayment:
    try:
        return OrderPayment.objects.select_related('order').get(order__number=order_number)
    except OrderPayment.DoesNotExist:
        raise ApiProblem(404, 'order_not_found', 'No such order.')


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

def _claim(model, pk: int, from_states, to_state: str, **updates: Any) -> bool:
    """Atomically move a row from one of ``from_states`` to ``to_state``; True for exactly one caller."""
    with transaction.atomic():
        return model.objects.filter(pk=pk, state__in=list(from_states)).update(
            state=to_state, claimed_at=_now(), **updates) == 1


def _claim_check(model, row, to_state: str) -> bool:
    """Take over an unresolved or abandoned claim to CHECK it — exactly one caller wins."""
    with transaction.atomic():
        return model.objects.filter(pk=row.pk, state=row.state, claimed_at=row.claimed_at).update(
            state=to_state, claimed_at=_now()) == 1


def _stale(row) -> bool:
    return row.claimed_at is None or row.claimed_at < _now() - send_window()


def _save(payment: OrderPayment, **fields: Any) -> None:
    for name, value in fields.items():
        setattr(payment, name, value)
    payment.save(update_fields=list(fields) + ['updated'])


def _error_fields(error: ProviderError | None) -> dict[str, str]:
    if error is None:
        return {'last_error_code': '', 'last_error_message': ''}
    return {'last_error_code': error.code[:64], 'last_error_message': error.message[:500]}


# ---------------------------------------------------------------------------
# Oscar bookkeeping
# ---------------------------------------------------------------------------

def _source(payment: OrderPayment):
    if payment.source_id:
        return payment.source
    source_type, _ = SourceType.objects.get_or_create(name='PayPal')
    source = Source.objects.create(
        order=payment.order, source_type=source_type, currency=payment.currency,
        reference=payment.reference, label=payment.card_last_digits and 'Card ending %s' % payment.card_last_digits)
    OrderPayment.objects.filter(pk=payment.pk).update(source=source)
    payment.source = source
    return source


def _payment_event(payment: OrderPayment, name: str, amount: Decimal, reference: str) -> None:
    event_type, _ = PaymentEventType.objects.get_or_create(name=name)
    EventHandler().create_payment_event(payment.order, event_type, amount, reference=reference)


def _order_status(payment: OrderPayment, status: str, note: str) -> None:
    order = payment.order
    try:
        EventHandler().handle_order_status_change(order, status, note_msg=note)
    except InvalidOrderStatus:
        logger.warning('Order %s cannot move from %s to %s', order.number, order.status, status)


# ---------------------------------------------------------------------------
# Pay: authorize the order total
# ---------------------------------------------------------------------------

def pay(user, order_number: str, *, card: CardDetails | None = None,
        payment_method_id: int | None = None) -> Outcome:
    payment = payment_for_owner(user, order_number)
    saved_card = None
    if payment_method_id is not None:
        saved_card = SavedCard.objects.filter(pk=payment_method_id, user=user, state=SavedCard.ACTIVE).first()
        if saved_card is None or not saved_card.paypal_token_id:
            raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')

    checking = False
    if not _claim(OrderPayment, payment.pk, OrderPayment.PAYABLE_STATES, OrderPayment.AUTHORIZING,
                  attempt=F('attempt') + 1, step='authorize', saved_card=saved_card,
                  last_error_code='', last_error_message=''):
        payment.refresh_from_db()
        if payment.state == OrderPayment.AUTHORIZATION_PENDING:
            return _recheck_authorization(payment)
        if payment.state == OrderPayment.AUTHORIZATION_UNKNOWN or (
                payment.state == OrderPayment.AUTHORIZING and _stale(payment)):
            if not _claim_check(OrderPayment, payment, OrderPayment.AUTHORIZING):
                payment.refresh_from_db()
                return Outcome('sending', payment_view(payment))
            OrderPayment.objects.filter(pk=payment.pk).update(saved_card=saved_card)
            checking = True
        elif payment.state == OrderPayment.AUTHORIZING:
            return Outcome('sending', payment_view(payment))
        elif payment.state in (OrderPayment.CANCELLED, OrderPayment.VOIDED, OrderPayment.VOIDING,
                               OrderPayment.VOID_UNKNOWN, OrderPayment.NEEDS_REVIEW):
            raise ApiProblem(409, 'order_not_payable',
                             'Order %s cannot be paid in state %s.' % (order_number, payment.state))
        else:
            # Already authorized (or further along): a double-click answers from the record.
            return Outcome(DONE, payment_view(payment))
    payment.refresh_from_db()
    return _authorize(payment, card=card, saved_card=saved_card, checking=checking)


def _authorize(payment: OrderPayment, *, card: CardDetails | None, saved_card: SavedCard | None,
               checking: bool) -> Outcome:
    client = get_client()
    reference = payment.attempt_reference
    if payment.step == 'authorize_order' and payment.paypal_order_id:
        return _authorize_created_order(payment, checking=True)
    if saved_card is not None:
        source = gateway.vaulted_card_source(saved_card.paypal_token_id)
    elif card is not None:
        source = gateway.card_source(card)
    else:
        raise ApiProblem(400, 'invalid_payment_source', 'Send card details or a saved card to check this payment.')
    claimed_at = payment.claimed_at or _now()

    def landed(exc, resending):
        issue = issue_of(exc.error)
        # PayPal refuses a second order carrying the same invoice id, and a
        # resend under the same PayPal-Request-Id: both mean an earlier one landed.
        return issue == 'DUPLICATE_INVOICE_ID' or (resending and issue == 'TRANSACTION_REFUSED')

    result = gateway.perform(
        send=lambda: gateway.create_authorization(
            client, reference, payment.amount, payment.currency, source,
            custom_id=payment.order.number, description='Order %s' % payment.order.number),
        find=lambda: gateway.find_authorization_by_invoice(
            client, reference, claimed_at - timedelta(minutes=10), _now()),
        read=gateway.read_authorization,
        outcome_of=gateway.authorization_outcome,
        checking=checking,
        repeat_is_safe=True,
        sent=(payment.amount, payment.currency),
        landed=landed,
    )
    answer = result.answer
    if answer is not None and answer.provider_id is None:
        order_status = answer.details.get('order_status')
        paypal_order_id = answer.details.get('paypal_order_id') or ''
        if order_status == gateway.OrderStatus.PAYER_ACTION_REQUIRED:
            error = ProviderError(402, 'payer_action_required',
                                  'The card issuer requires the shopper to approve this payment in a browser, '
                                  'which this API does not support. Use a different card.')
            return _complete_authorization(payment, FAILED, answer, error)
        if order_status in (gateway.OrderStatus.APPROVED, gateway.OrderStatus.CREATED) and paypal_order_id:
            _save(payment, paypal_order_id=paypal_order_id, step='authorize_order')
            return _authorize_created_order(payment, checking=False)
        return _complete_authorization(payment, UNKNOWN, answer, None)
    return _complete_authorization(payment, result.outcome, answer, result.error, released=result.released)


def _authorize_created_order(payment: OrderPayment, *, checking: bool) -> Outcome:
    """Second step, only when PayPal created the order without authorizing it."""
    client = get_client()
    result = gateway.perform(
        send=lambda: gateway.authorize_order(client, payment.paypal_order_id, payment.reference_for('authorize')),
        find=lambda: gateway.get_order(client, payment.paypal_order_id),
        read=gateway.read_authorization,
        outcome_of=gateway.authorization_outcome,
        checking=checking,
        repeat_is_safe=False,
        sent=(payment.amount, payment.currency),
    )
    return _complete_authorization(payment, result.outcome, result.answer, result.error, released=result.released)


def _complete_authorization(payment: OrderPayment, outcome: str, answer, error: ProviderError | None,
                            *, released: bool = False) -> Outcome:
    fields: dict[str, Any] = _error_fields(error)
    if answer is not None:
        details = answer.details
        if details.get('paypal_order_id'):
            fields['paypal_order_id'] = details['paypal_order_id']
        if details.get('card_brand'):
            fields['card_brand'] = str(details['card_brand'])
        if details.get('card_last_digits'):
            fields['card_last_digits'] = details['card_last_digits']
        elif payment.saved_card_id and not payment.card_last_digits:
            fields['card_brand'] = payment.saved_card.brand
            fields['card_last_digits'] = payment.saved_card.last_digits
        if answer.provider_id:
            fields.update(
                authorization_id=answer.provider_id,
                authorization_status=str(answer.status or ''),
                authorized_at=answer.provider_time,
                authorization_expires_at=details.get('expires_at'),
            )
            if not payment.original_authorization_id:
                fields.update(original_authorization_id=answer.provider_id,
                              original_authorized_at=answer.provider_time)

    if outcome == DONE:
        fields.update(state=OrderPayment.AUTHORIZED, step='')
        with transaction.atomic():
            _save(payment, **fields)
            _source(payment).allocate(payment.amount, reference=payment.authorization_id,
                                      status=payment.authorization_status)
            _payment_event(payment, 'Authorised', payment.amount, payment.authorization_id)
            _order_status(payment, ORDER_STATUS_PAID, 'Payment authorized by PayPal (%s)' % payment.authorization_id)
    elif outcome == PENDING:
        fields.update(state=OrderPayment.AUTHORIZATION_PENDING, step='authorize')
        _save(payment, **fields)
    elif outcome == FAILED:
        if error is None:
            error = ProviderError(402, 'payment_declined', 'PayPal declined the payment (%s).'
                                  % (fields.get('authorization_status') or 'no authorization'))
            fields.update(_error_fields(error))
        fields.update(state=OrderPayment.AUTHORIZATION_FAILED, step='')
        _save(payment, **fields)
    elif outcome == NEEDS_REVIEW:
        fields.update(state=OrderPayment.NEEDS_REVIEW)
        _save(payment, **fields)
    else:
        fields.update(state=OrderPayment.AUTHORIZATION_UNKNOWN)
        _save(payment, **fields)
    return Outcome(outcome, payment_view(payment), error)


def _recheck_authorization(payment: OrderPayment) -> Outcome:
    """A pending authorization: only PayPal's current word settles it."""
    if not _claim_check(OrderPayment, payment, OrderPayment.AUTHORIZING):
        payment.refresh_from_db()
        return Outcome('sending', payment_view(payment))
    payment.refresh_from_db()
    client = get_client()
    try:
        authorization = gateway.get_authorization(client, payment.authorization_id)
    except Exception as exc:  # noqa: BLE001 - translated, state restored
        _save(payment, state=OrderPayment.AUTHORIZATION_PENDING)
        return Outcome(PENDING, payment_view(payment), gateway.translate(exc))
    answer = gateway.read_authorization(authorization)
    outcome = gateway.authorization_outcome(answer.status)
    if outcome == UNKNOWN:
        outcome = PENDING  # the provider holds it; still not settled
    return _complete_authorization(payment, outcome, answer, None)


# ---------------------------------------------------------------------------
# Fulfil: capture (renewing a stale authorization first)
# ---------------------------------------------------------------------------

def fulfil(order_number: str) -> Outcome:
    payment = payment_for_operator(order_number)
    checking = False
    if payment.state == OrderPayment.AUTHORIZATION_PENDING:
        refreshed = _recheck_authorization(payment)
        if refreshed.outcome != DONE:
            raise ApiProblem(409, 'authorization_pending',
                             'PayPal has not finished authorizing order %s yet; retry fulfilment later.'
                             % order_number, payment=refreshed.body)
        payment.refresh_from_db()
    if not _claim(OrderPayment, payment.pk, (OrderPayment.AUTHORIZED,), OrderPayment.CAPTURING, step='capture'):
        payment.refresh_from_db()
        if payment.state == OrderPayment.CAPTURED:
            return Outcome(DONE, payment_view(payment))
        if payment.state == OrderPayment.CAPTURE_PENDING:
            return _recheck_capture(payment)
        if payment.state == OrderPayment.CAPTURE_UNKNOWN or (
                payment.state == OrderPayment.CAPTURING and _stale(payment)):
            if not _claim_check(OrderPayment, payment, OrderPayment.CAPTURING):
                payment.refresh_from_db()
                return Outcome('sending', payment_view(payment))
            checking = True
        elif payment.state == OrderPayment.CAPTURING:
            return Outcome('sending', payment_view(payment))
        elif payment.state == OrderPayment.AUTHORIZATION_EXPIRED:
            raise ApiProblem(409, 'authorization_expired', _expired_message(payment))
        else:
            raise ApiProblem(409, 'order_not_fulfillable',
                             'Order %s cannot be fulfilled in payment state %s.' % (order_number, payment.state),
                             payment=payment_view(payment))
    payment.refresh_from_db()

    if checking and payment.step == 'reauthorize':
        outcome = _reauthorize(payment, checking=True)
        if outcome is not None:
            return outcome
    elif not checking:
        now = _now()
        original = payment.original_authorized_at or payment.authorized_at
        if (payment.authorization_expires_at and now >= payment.authorization_expires_at) or (
                original and now - original >= AUTHORIZATION_LIFETIME):
            _save(payment, state=OrderPayment.AUTHORIZATION_EXPIRED, step='',
                  last_error_code='authorization_expired', last_error_message=_expired_message(payment)[:500])
            raise ApiProblem(409, 'authorization_expired', _expired_message(payment), payment=payment_view(payment))
        honor_start = payment.reauthorized_at or original
        if honor_start and now - honor_start > HONOR_PERIOD and payment.reauthorized_at is None:
            outcome = _reauthorize(payment, checking=False)
            if outcome is not None:
                return outcome
    return _capture(payment, checking=checking)


def _expired_message(payment: OrderPayment) -> str:
    expired = payment.authorization_expires_at or (
        (payment.original_authorized_at or payment.authorized_at or _now()) + AUTHORIZATION_LIFETIME)
    return ('The card authorization for order %s expired on %s and PayPal can no longer capture or renew it '
            '(a hold can be renewed only once, between day 4 and day 29). No money was taken. Ask the shopper '
            'to pay again (POST /api/orders/%s/pay), then retry fulfilment - or cancel the order.'
            % (payment.order.number, expired.strftime('%Y-%m-%d %H:%M UTC'), payment.order.number))


def _reauthorize(payment: OrderPayment, *, checking: bool) -> Outcome | None:
    """Renew an authorization past its honor period. Returns None to continue to capture."""
    client = get_client()
    _save(payment, step='reauthorize')
    result = gateway.perform(
        send=lambda: gateway.reauthorize(client, payment.authorization_id, payment.reference_for('reauthorize'),
                                         payment.amount, payment.currency),
        find=lambda: gateway.reauthorize(client, payment.authorization_id, payment.reference_for('reauthorize'),
                                         payment.amount, payment.currency),
        read=gateway.read_authorization,
        outcome_of=gateway.authorization_outcome,
        checking=checking,
        repeat_is_safe=True,
        sent=(payment.amount, payment.currency),
    )
    answer = result.answer
    if result.outcome in (DONE, PENDING) and answer is not None and answer.provider_id:
        fields = dict(
            authorization_id=answer.provider_id, authorization_status=str(answer.status or ''),
            authorized_at=answer.provider_time, authorization_expires_at=answer.details.get('expires_at'),
            reauthorized_at=answer.provider_time or _now(), step='capture')
        if result.outcome == PENDING:
            fields.update(state=OrderPayment.AUTHORIZATION_PENDING, step='')
            _save(payment, **fields)
            return Outcome(PENDING, payment_view(payment))
        _save(payment, **fields)
        return None
    if result.outcome == UNKNOWN:
        _save(payment, state=OrderPayment.CAPTURE_UNKNOWN, **_error_fields(result.error))
        return Outcome(UNKNOWN, payment_view(payment), result.error)
    if result.outcome == NEEDS_REVIEW:
        _save(payment, state=OrderPayment.NEEDS_REVIEW, **_error_fields(result.error))
        return Outcome(NEEDS_REVIEW, payment_view(payment), result.error)
    # PayPal would not renew it. The original hold is still within its
    # lifetime, so try to capture it; if that fails too the operator is told why.
    logger.warning('Reauthorization of %s refused (%s); capturing the original',
                   payment.authorization_id, result.error.code if result.error else answer and answer.status)
    _save(payment, step='capture', reauthorized_at=_now(),
          last_error_code='reauthorization_refused',
          last_error_message=(result.error.message if result.error else 'PayPal refused to renew the hold.')[:500])
    return None


def _capture(payment: OrderPayment, *, checking: bool) -> Outcome:
    client = get_client()
    reference = payment.reference_for('capture')

    def send():
        return gateway.capture(client, payment.authorization_id, reference, payment.amount, payment.currency)

    result = gateway.perform(
        send=send,
        find=send,              # PayPal replays the original capture for the same PayPal-Request-Id
        read=gateway.read_capture,
        outcome_of=gateway.capture_outcome,
        checking=checking,
        repeat_is_safe=True,
        sent=(payment.amount, payment.currency),
        landed=lambda exc, resending: issue_of(exc.error) == 'AUTHORIZATION_ALREADY_CAPTURED',
    )
    return _complete_capture(payment, result)


def _complete_capture(payment: OrderPayment, result: gateway.StepResult) -> Outcome:
    answer = result.answer
    fields: dict[str, Any] = _error_fields(result.error)
    if answer is not None and answer.provider_id:
        fields.update(capture_id=answer.provider_id, capture_status=str(answer.status or ''),
                      captured_at=answer.provider_time)
    outcome = result.outcome
    if outcome == DONE:
        assert answer is not None and answer.amount is not None  # perform verified the echoed amount
        fields.update(state=OrderPayment.CAPTURED, step='', captured_amount=answer.amount,
                      paypal_fee=answer.details.get('fee'), net_amount=answer.details.get('net'))
        with transaction.atomic():
            _save(payment, **fields)
            _source(payment).debit(answer.amount, reference=payment.capture_id, status=payment.capture_status)
            _payment_event(payment, 'Captured', answer.amount, payment.capture_id)
            EventHandler().consume_stock_allocations(payment.order)
            _order_status(payment, ORDER_STATUS_COMPLETE, 'Fulfilled; payment captured by PayPal (%s)'
                          % payment.capture_id)
        return Outcome(DONE, payment_view(payment))
    if outcome == PENDING:
        fields.update(state=OrderPayment.CAPTURE_PENDING, step='capture')
        _save(payment, **fields)
        return Outcome(PENDING, payment_view(payment))
    if outcome == FAILED and result.released:
        # Nothing was captured. Say why in terms the operator can act on.
        error = result.error
        expired = _authorization_no_longer_capturable(payment)
        if expired:
            fields.update(state=OrderPayment.AUTHORIZATION_EXPIRED, step='')
            error = ProviderError(409, 'authorization_expired', _expired_message(payment),
                                  issue=error.issue if error else None)
        else:
            fields.update(state=OrderPayment.AUTHORIZED, step='')
            if error is not None and error.status_code < 500:
                error = ProviderError(409, 'capture_failed',
                                      'PayPal refused the capture: %s The hold is still in place; retry '
                                      'fulfilment or cancel the order.' % error.message, issue=error.issue)
        fields.update(_error_fields(error))
        _save(payment, **fields)
        return Outcome(FAILED, payment_view(payment), error)
    if outcome == FAILED:
        error = ProviderError(409, 'capture_failed', 'PayPal reported the capture as %s; review this order.'
                              % (answer.status if answer else 'failed'))
        fields.update(state=OrderPayment.NEEDS_REVIEW, **_error_fields(error))
        _save(payment, **fields)
        return Outcome(FAILED, payment_view(payment), error)
    if outcome == NEEDS_REVIEW:
        fields.update(state=OrderPayment.NEEDS_REVIEW)
        _save(payment, **fields)
        return Outcome(NEEDS_REVIEW, payment_view(payment), result.error)
    fields.update(state=OrderPayment.CAPTURE_UNKNOWN, step='capture')
    _save(payment, **fields)
    return Outcome(UNKNOWN, payment_view(payment), result.error)


def _authorization_no_longer_capturable(payment: OrderPayment) -> bool:
    now = _now()
    if payment.authorization_expires_at and now >= payment.authorization_expires_at:
        return True
    try:
        authorization = gateway.get_authorization(get_client(), payment.authorization_id)
    except Exception:  # noqa: BLE001 - a failed lookup proves nothing either way
        return False
    answer = gateway.read_authorization(authorization)
    expires = answer.details.get('expires_at')
    return answer.status == gateway.AuthorizationStatus.VOIDED or bool(expires and now >= expires)


def _recheck_capture(payment: OrderPayment) -> Outcome:
    if not _claim_check(OrderPayment, payment, OrderPayment.CAPTURING):
        payment.refresh_from_db()
        return Outcome('sending', payment_view(payment))
    payment.refresh_from_db()
    try:
        captured = gateway.get_capture(get_client(), payment.capture_id)
    except Exception as exc:  # noqa: BLE001 - translated, state restored
        _save(payment, state=OrderPayment.CAPTURE_PENDING)
        return Outcome(PENDING, payment_view(payment), gateway.translate(exc))
    answer = gateway.read_capture(captured)
    outcome = gateway.capture_outcome(answer.status)
    if outcome == UNKNOWN:
        outcome = PENDING
    return _complete_capture(payment, gateway.StepResult(outcome, answer=answer, result=captured))


# ---------------------------------------------------------------------------
# Cancel: release the hold
# ---------------------------------------------------------------------------

def cancel(order_number: str) -> Outcome:
    payment = payment_for_operator(order_number)
    unpaid = (OrderPayment.AWAITING_PAYMENT, OrderPayment.AUTHORIZATION_FAILED, OrderPayment.AUTHORIZATION_EXPIRED)
    if _claim(OrderPayment, payment.pk, unpaid, OrderPayment.CANCELLED, step=''):
        # Nothing is held at PayPal: no provider write needed.
        payment.refresh_from_db()
        with transaction.atomic():
            EventHandler().cancel_stock_allocations(payment.order)
            _order_status(payment, ORDER_STATUS_CANCELLED, 'Cancelled before payment')
        return Outcome(DONE, payment_view(payment))

    checking = False
    if not _claim(OrderPayment, payment.pk, (OrderPayment.AUTHORIZED, OrderPayment.AUTHORIZATION_PENDING),
                  OrderPayment.VOIDING, step='void'):
        payment.refresh_from_db()
        if payment.state in (OrderPayment.VOIDED, OrderPayment.CANCELLED):
            return Outcome(DONE, payment_view(payment))
        if payment.state == OrderPayment.VOID_UNKNOWN or (
                payment.state == OrderPayment.VOIDING and _stale(payment)):
            if not _claim_check(OrderPayment, payment, OrderPayment.VOIDING):
                payment.refresh_from_db()
                return Outcome('sending', payment_view(payment))
            checking = True
        elif payment.state == OrderPayment.VOIDING:
            return Outcome('sending', payment_view(payment))
        elif payment.state in (OrderPayment.CAPTURED, OrderPayment.CAPTURING, OrderPayment.CAPTURE_PENDING,
                               OrderPayment.CAPTURE_UNKNOWN):
            raise ApiProblem(409, 'already_fulfilled',
                             'Order %s has been fulfilled and its payment captured; issue a refund instead.'
                             % order_number)
        else:
            raise ApiProblem(409, 'order_not_cancellable',
                             'Order %s cannot be cancelled in payment state %s.' % (order_number, payment.state))
    payment.refresh_from_db()
    client = get_client()
    result = gateway.perform(
        send=lambda: gateway.void(client, payment.authorization_id, payment.reference_for('void')),
        find=lambda: gateway.get_authorization(client, payment.authorization_id),
        read=gateway.read_void,
        outcome_of=gateway.cancel_outcome,
        checking=checking,
        repeat_is_safe=True,
        landed=lambda exc, resending: issue_of(exc.error) == 'PREVIOUSLY_VOIDED',
    )
    fields: dict[str, Any] = _error_fields(result.error)
    if result.answer is not None:
        fields['authorization_status'] = str(result.answer.status or '')
    if result.outcome == DONE:
        fields.update(state=OrderPayment.VOIDED, step='',
                      voided_at=(result.answer.provider_time if result.answer else None) or _now())
        with transaction.atomic():
            _save(payment, **fields)
            _source(payment)._create_transaction('Void', payment.amount, reference=payment.authorization_id,
                                                 status=payment.authorization_status)
            _payment_event(payment, 'Voided', payment.amount, payment.authorization_id)
            EventHandler().cancel_stock_allocations(payment.order)
            _order_status(payment, ORDER_STATUS_CANCELLED, 'Cancelled; PayPal hold released (%s)'
                          % payment.authorization_id)
        return Outcome(DONE, payment_view(payment))
    if result.outcome == FAILED:
        error = result.error
        if error is not None and error.issue == 'PREVIOUSLY_CAPTURED' or not result.released:
            error = ProviderError(409, 'already_captured',
                                  'PayPal reports this payment as already captured; issue a refund instead.')
            fields.update(state=OrderPayment.NEEDS_REVIEW, **_error_fields(error))
        else:
            fields.update(state=OrderPayment.AUTHORIZED, step='')
        _save(payment, **fields)
        return Outcome(FAILED, payment_view(payment), error)
    if result.outcome == NEEDS_REVIEW:
        fields.update(state=OrderPayment.NEEDS_REVIEW)
        _save(payment, **fields)
        return Outcome(NEEDS_REVIEW, payment_view(payment), result.error)
    fields.update(state=OrderPayment.VOID_UNKNOWN, step='void')
    _save(payment, **fields)
    return Outcome(UNKNOWN, payment_view(payment), result.error)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def refund(user, order_number: str, idempotency_key: str, amount: Decimal | None) -> Outcome:
    payment = payment_for_owner(user, order_number)
    if payment.state != OrderPayment.CAPTURED or payment.captured_amount is None:
        raise ApiProblem(409, 'not_refundable',
                         'Order %s has no captured payment to refund (payment state %s).'
                         % (order_number, payment.state))

    existing = PaymentRefund.objects.filter(payment=payment, idempotency_key=idempotency_key).first()
    if existing is None:
        refund_row = _claim_new_refund(payment, idempotency_key, amount)
        if refund_row is not None:
            return _send_refund(payment, refund_row, checking=False, created=True)
        # Another request with the same key won the claim: answer from its record.
        existing = PaymentRefund.objects.get(payment=payment, idempotency_key=idempotency_key)
        return Outcome(_refund_outcome_of(existing), refund_view(existing), _refund_error(existing))

    if amount is not None and amount != existing.amount:
        raise ApiProblem(409, 'idempotency_key_reused',
                         'This idempotency key was already used for a refund of %s.' % existing.amount)
    if existing.state in (PaymentRefund.DONE, PaymentRefund.NEEDS_REVIEW) or (
            existing.state == PaymentRefund.FAILED and existing.paypal_refund_id):
        return Outcome(_refund_outcome_of(existing), refund_view(existing), _refund_error(existing))
    if existing.state == PaymentRefund.FAILED:
        # Refused or never sent: nothing happened, so the same key may try again.
        if not _reclaim_failed_refund(payment, existing):
            existing.refresh_from_db()
            return Outcome(_refund_outcome_of(existing), refund_view(existing), _refund_error(existing))
        existing.refresh_from_db()
        return _send_refund(payment, existing, checking=False)
    if existing.state == PaymentRefund.PENDING:
        return _recheck_refund(payment, existing)
    if existing.state == PaymentRefund.UNKNOWN or (existing.state == PaymentRefund.SENDING and _stale(existing)):
        if not _claim_check(PaymentRefund, existing, PaymentRefund.SENDING):
            existing.refresh_from_db()
            return Outcome('sending', refund_view(existing))
        existing.refresh_from_db()
        return _send_refund(payment, existing, checking=True)
    return Outcome('sending', refund_view(existing))


def _reserve(payment: OrderPayment, amount: Decimal) -> bool:
    """Reserve ``amount`` of the capture; the database refuses anything beyond it."""
    return OrderPayment.objects.filter(
        pk=payment.pk, state=OrderPayment.CAPTURED,
        captured_amount__gte=F('refund_reserved') + amount,
    ).update(refund_reserved=F('refund_reserved') + amount) == 1


def _release(payment: OrderPayment, amount: Decimal) -> None:
    OrderPayment.objects.filter(pk=payment.pk).update(refund_reserved=F('refund_reserved') - amount)


def _claim_new_refund(payment: OrderPayment, key: str, amount: Decimal | None) -> PaymentRefund | None:
    try:
        with transaction.atomic():
            if amount is None:
                payment.refresh_from_db()
                amount = payment.refundable_amount
                if amount <= 0:
                    raise ApiProblem(409, 'nothing_to_refund', 'Order %s has been fully refunded.'
                                     % payment.order.number)
            row = PaymentRefund.objects.create(payment=payment, idempotency_key=key, amount=amount,
                                               state=PaymentRefund.SENDING, claimed_at=_now())
            if not _reserve(payment, amount):
                payment.refresh_from_db()
                raise ApiProblem(409, 'refund_exceeds_captured',
                                 'A refund of %s would exceed what remains refundable (%s).'
                                 % (gateway.money_str(amount), gateway.money_str(max(payment.refundable_amount,
                                                                                     Decimal('0')))))
            return row
    except IntegrityError:
        return None


def _reclaim_failed_refund(payment: OrderPayment, row: PaymentRefund) -> bool:
    with transaction.atomic():
        claimed = PaymentRefund.objects.filter(pk=row.pk, state=PaymentRefund.FAILED, paypal_refund_id='').update(
            state=PaymentRefund.SENDING, claimed_at=_now()) == 1
        if not claimed:
            return False
        if not _reserve(payment, row.amount):
            raise ApiProblem(409, 'refund_exceeds_captured',
                             'A refund of %s would exceed what remains refundable.' % gateway.money_str(row.amount))
        return True


def _send_refund(payment: OrderPayment, row: PaymentRefund, *, checking: bool, created: bool = False) -> Outcome:
    payment.refresh_from_db()
    client = get_client()

    def send():
        return gateway.refund(client, payment.capture_id, row.reference, row.amount, payment.currency)

    result = gateway.perform(
        send=send,
        find=send,              # PayPal replays the original refund for the same PayPal-Request-Id
        read=gateway.read_refund,
        outcome_of=gateway.refund_outcome,
        checking=checking,
        repeat_is_safe=True,
        sent=(row.amount, payment.currency),
    )
    return _complete_refund(payment, row, result, created=created)


def _complete_refund(payment: OrderPayment, row: PaymentRefund, result: gateway.StepResult, *,
                     created: bool = False) -> Outcome:
    answer = result.answer
    fields: dict[str, Any] = _error_fields(result.error)
    if answer is not None and answer.provider_id:
        fields.update(paypal_refund_id=answer.provider_id, paypal_status=str(answer.status or ''),
                      provider_time=answer.provider_time)
    state = {DONE: PaymentRefund.DONE, PENDING: PaymentRefund.PENDING, FAILED: PaymentRefund.FAILED,
             NEEDS_REVIEW: PaymentRefund.NEEDS_REVIEW}.get(result.outcome, PaymentRefund.UNKNOWN)
    fields['state'] = state
    with transaction.atomic():
        for name, value in fields.items():
            setattr(row, name, value)
        row.save()
        if state == PaymentRefund.DONE:
            OrderPayment.objects.filter(pk=payment.pk).update(refunded_amount=F('refunded_amount') + row.amount)
            payment.refresh_from_db()
            _source(payment).refund(row.amount, reference=row.paypal_refund_id, status=row.paypal_status)
            _payment_event(payment, 'Refunded', row.amount, row.paypal_refund_id)
        elif state == PaymentRefund.FAILED:
            _release(payment, row.amount)
    return Outcome(result.outcome, refund_view(row), result.error or _refund_error(row), created=created)


def _recheck_refund(payment: OrderPayment, row: PaymentRefund) -> Outcome:
    if not _claim_check(PaymentRefund, row, PaymentRefund.SENDING):
        row.refresh_from_db()
        return Outcome('sending', refund_view(row))
    row.refresh_from_db()
    try:
        record = gateway.get_refund(get_client(), row.paypal_refund_id)
    except Exception as exc:  # noqa: BLE001 - translated, state restored
        PaymentRefund.objects.filter(pk=row.pk).update(state=PaymentRefund.PENDING)
        row.refresh_from_db()
        return Outcome(PENDING, refund_view(row), gateway.translate(exc))
    answer = gateway.read_refund(record)
    outcome = gateway.refund_outcome(answer.status)
    if outcome == UNKNOWN:
        outcome = PENDING
    return _complete_refund(payment, row, gateway.StepResult(outcome, answer=answer, result=record))


def _refund_outcome_of(row: PaymentRefund) -> str:
    return {PaymentRefund.DONE: DONE, PaymentRefund.PENDING: PENDING, PaymentRefund.FAILED: FAILED,
            PaymentRefund.NEEDS_REVIEW: NEEDS_REVIEW, PaymentRefund.SENDING: 'sending'}.get(row.state, UNKNOWN)


def _refund_error(row: PaymentRefund) -> ProviderError | None:
    if row.state == PaymentRefund.FAILED:
        return ProviderError(409, row.last_error_code or 'refund_failed',
                             row.last_error_message or 'PayPal did not complete the refund.')
    return None


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

def _card_reference(user, card: CardDetails, idempotency_key: str | None) -> str:
    prefix = paypal_config().reference_prefix
    if idempotency_key:
        material = 'card|%s|key|%s' % (user.pk, idempotency_key)
    else:
        # No key: the same card saved again by the same shopper is the same
        # write. Keyed with SECRET_KEY, so the reference reveals nothing.
        deletions = SavedCard.objects.filter(user=user, state=SavedCard.DELETED).count()
        material = 'card|%s|%s|%s|%d' % (user.pk, card.number, card.expiry, deletions)
    digest = hmac.new(settings.SECRET_KEY.encode(), material.encode(), hashlib.sha256).hexdigest()[:32]
    return '%s-card-%s' % (prefix, digest)


def save_card(user, card: CardDetails, idempotency_key: str | None) -> Outcome:
    reference = _card_reference(user, card, idempotency_key)
    checking = False
    try:
        with transaction.atomic():
            row = SavedCard.objects.create(user=user, reference=reference, state=SavedCard.SENDING,
                                           claimed_at=_now())
        created = True
    except IntegrityError:
        created = False
        row = SavedCard.objects.get(reference=reference)
        if row.state == SavedCard.ACTIVE:
            return Outcome(DONE, card_view(row))
        if row.state == SavedCard.FAILED and not row.paypal_token_id:
            if not _claim(SavedCard, row.pk, (SavedCard.FAILED,), SavedCard.SENDING):
                row.refresh_from_db()
                return Outcome('sending', card_view(row))
        elif row.state == SavedCard.UNKNOWN or (row.state == SavedCard.SENDING and _stale(row)):
            if not _claim_check(SavedCard, row, SavedCard.SENDING):
                row.refresh_from_db()
                return Outcome('sending', card_view(row))
            checking = True
        elif row.state == SavedCard.SENDING:
            return Outcome('sending', card_view(row))
        else:
            raise ApiProblem(409, 'idempotency_key_reused',
                             'This request was already used for a card that is now %s.' % row.state)
        row.refresh_from_db()

    client = get_client()
    customer = PayPalCustomer.objects.filter(user=user).first()

    def send():
        return gateway.vault_card(client, reference, card, customer.customer_id if customer else None)

    # PayPal only de-duplicates within its key window; past it a resend could
    # vault a second token, so the check becomes lookup-only (and stays unknown).
    within_window = _now() - row.created < VAULT_KEY_WINDOW
    result = gateway.perform(
        send=send,
        find=send if within_window else (lambda: None),
        read=gateway.read_payment_token,
        outcome_of=gateway.vault_outcome,
        checking=checking,
        repeat_is_safe=within_window,
    )
    answer = result.answer
    fields: dict[str, Any] = {}
    if result.error is not None:
        fields.update(last_error_code=result.error.code[:64], last_error_message=result.error.message[:255])
    if answer is not None and answer.provider_id:
        fields.update(paypal_token_id=answer.provider_id, brand=str(answer.details.get('brand') or '')[:32],
                      last_digits=answer.details.get('last_digits') or '', expiry=answer.details.get('expiry') or '')
    error = result.error
    if result.outcome == DONE:
        fields.update(state=SavedCard.ACTIVE, last_error_code='', last_error_message='')
        customer_id = answer.details.get('customer_id') if answer else None
        if customer_id and customer is None:
            PayPalCustomer.objects.get_or_create(user=user, defaults={'customer_id': customer_id})
    elif result.outcome == FAILED:
        fields['state'] = SavedCard.FAILED
        if answer is not None and answer.provider_id:
            error = ProviderError(402, 'card_verification_failed', 'The card could not be verified.')
            fields.update(last_error_code=error.code, last_error_message=error.message)
            _discard_token(answer.provider_id)
        elif error is None:
            error = ProviderError(402, 'card_rejected', 'PayPal did not save the card.')
    else:
        fields['state'] = SavedCard.UNKNOWN
    SavedCard.objects.filter(pk=row.pk).update(**fields)
    row.refresh_from_db()
    return Outcome(result.outcome, card_view(row), error, created=created)


def _discard_token(token_id: str) -> None:
    """Best effort: a token that failed verification should not stay vaulted."""
    try:
        gateway.delete_payment_token(get_client(), token_id)
    except Exception as exc:  # noqa: BLE001 - logged; the local card is already failed
        logger.warning('Could not discard unverified PayPal token: %s', type(exc).__name__)


def list_cards(user) -> list[dict[str, Any]]:
    return [card_view(c) for c in SavedCard.objects.filter(user=user, state=SavedCard.ACTIVE)]


def delete_card(user, card_id: int) -> Outcome:
    row = SavedCard.objects.filter(pk=card_id, user=user).first()
    if row is None:
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')
    checking = False
    if row.state in (SavedCard.FAILED,) and not row.paypal_token_id:
        SavedCard.objects.filter(pk=row.pk).update(state=SavedCard.DELETED, deleted_at=_now())
        row.refresh_from_db()
        return Outcome(DONE, card_view(row))
    if not _claim(SavedCard, row.pk, (SavedCard.ACTIVE,), SavedCard.DELETING):
        row.refresh_from_db()
        if row.state == SavedCard.DELETED:
            return Outcome(DONE, card_view(row))
        if row.state == SavedCard.DELETE_UNKNOWN or (row.state == SavedCard.DELETING and _stale(row)):
            if not _claim_check(SavedCard, row, SavedCard.DELETING):
                row.refresh_from_db()
                return Outcome('sending', card_view(row))
            checking = True
        elif row.state == SavedCard.DELETING:
            return Outcome('sending', card_view(row))
        else:
            raise ApiProblem(409, 'card_not_saved_yet',
                             'This card is still being saved; repeat the save request to settle it first.')
    row.refresh_from_db()
    client = get_client()

    def send():
        return gateway.delete_payment_token(client, row.paypal_token_id)

    result = gateway.perform(
        send=send,
        find=send,              # deleting by id is idempotent (a repeat, or 404, means gone)
        read=gateway.read_delete,
        outcome_of=gateway.delete_outcome,
        checking=checking,
        repeat_is_safe=True,
    )
    if result.outcome == DONE:
        SavedCard.objects.filter(pk=row.pk).update(state=SavedCard.DELETED, deleted_at=_now(),
                                                   last_error_code='', last_error_message='')
        row.refresh_from_db()
        return Outcome(DONE, card_view(row))
    if result.outcome == FAILED:
        error = result.error or ProviderError(409, 'paypal_refused_delete', 'PayPal refused to delete the card.')
        SavedCard.objects.filter(pk=row.pk).update(state=SavedCard.ACTIVE, last_error_code=error.code[:64],
                                                   last_error_message=error.message[:255])
        row.refresh_from_db()
        return Outcome(FAILED, card_view(row), error)
    SavedCard.objects.filter(pk=row.pk).update(state=SavedCard.DELETE_UNKNOWN)
    row.refresh_from_db()
    return Outcome(UNKNOWN, card_view(row), result.error)


# ---------------------------------------------------------------------------
# Views of local records
# ---------------------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    return value.astimezone(dt_timezone.utc).isoformat() if value else None


def _money(value: Decimal | None) -> str | None:
    return gateway.money_str(value) if value is not None else None


def payment_state(payment: OrderPayment) -> str:
    if payment.state == OrderPayment.CAPTURED and payment.refunded_amount > 0:
        if payment.captured_amount is not None and payment.refunded_amount >= payment.captured_amount:
            return 'refunded'
        return 'partially_refunded'
    return payment.state


def card_view(card: SavedCard) -> dict[str, Any]:
    return {
        'paymentMethodId': card.pk,
        'state': card.state,
        'brand': card.brand,
        'lastDigits': card.last_digits,
        'expiry': card.expiry,
        'createdAt': _iso(card.created),
        'error': {'code': card.last_error_code, 'message': card.last_error_message}
        if card.last_error_code else None,
    }


def refund_view(row: PaymentRefund) -> dict[str, Any]:
    payment = row.payment
    payment.refresh_from_db()
    return {
        'refundId': row.pk,
        'orderId': payment.order.number,
        'state': row.state,
        'amount': _money(row.amount),
        'currency': payment.currency,
        'idempotencyKey': row.idempotency_key,
        'paypalRefundId': row.paypal_refund_id or None,
        'paypalStatus': row.paypal_status or None,
        'refundedAt': _iso(row.provider_time),
        'orderRefundedAmount': _money(payment.refunded_amount),
        'orderRefundableAmount': _money(max(payment.refundable_amount, Decimal('0.00'))),
        'error': {'code': row.last_error_code, 'message': row.last_error_message} if row.last_error_code else None,
    }


def payment_view(payment: OrderPayment) -> dict[str, Any]:
    order = payment.order
    order.refresh_from_db(fields=['status'])
    return {
        'orderId': str(order.number),
        'orderStatus': order.status,
        'placedAt': _iso(order.date_placed),
        'total': _money(payment.amount),
        'currency': payment.currency,
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
            'linePrice': _money(line.line_price_incl_tax),
        } for line in order.lines.all()],
        'payment': {
            'state': payment_state(payment),
            'paymentMethodId': payment.saved_card_id,
            'card': {'brand': payment.card_brand, 'lastDigits': payment.card_last_digits}
            if payment.card_last_digits else None,
            'paypalOrderId': payment.paypal_order_id or None,
            'authorization': {
                'id': payment.authorization_id,
                'status': payment.authorization_status,
                'amount': _money(payment.amount),
                'authorizedAt': _iso(payment.authorized_at),
                'expiresAt': _iso(payment.authorization_expires_at),
                'reauthorizedAt': _iso(payment.reauthorized_at),
                'voidedAt': _iso(payment.voided_at),
            } if payment.authorization_id else None,
            'capture': {
                'id': payment.capture_id,
                'status': payment.capture_status,
                'amount': _money(payment.captured_amount),
                'paypalFee': _money(payment.paypal_fee),
                'netAmount': _money(payment.net_amount),
                'capturedAt': _iso(payment.captured_at),
            } if payment.capture_id else None,
            'refundedAmount': _money(payment.refunded_amount),
            'refundableAmount': _money(max(payment.refundable_amount, Decimal('0.00')))
            if payment.captured_amount is not None else None,
            'refunds': [{
                'refundId': r.pk, 'state': r.state, 'amount': _money(r.amount),
                'paypalRefundId': r.paypal_refund_id or None, 'paypalStatus': r.paypal_status or None,
            } for r in payment.refunds.all()],
            'error': {'code': payment.last_error_code, 'message': payment.last_error_message}
            if payment.last_error_code else None,
        },
    }
