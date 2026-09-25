"""
Authorize at checkout, capture at fulfilment, void on cancel, refund after
fulfilment.

Two layers keep a double click from moving money twice:

* the order's ``OrderPayment.state`` only changes by compare-and-set
  (``UPDATE ... WHERE state IN (...)``), so one request at a time drives an
  order's payment;
* every PayPal write goes through ``safe_write`` with a reference derived from
  the order and the step, claimed in the database before PayPal is called.
"""
import logging
import uuid
from datetime import datetime
from decimal import Decimal

import httpx
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from oscar.core.loading import get_class, get_model
from paypal.core import ApiError, UnsetType
from paypal.models import (
    AmountWithBreakdown, CardRequest, OrderRequest, PaymentSource,
    PurchaseUnitRequest)
from paypal.models.enums import CheckoutPaymentIntent, OrderStatus

from . import outcomes
from .client import get_client
from .errors import NEVER_SENT, ApiProblem, OutcomeUnknown, provider_message, translate
from .models import (
    CardState, Outcome, OrderPayment, PaymentState, ProviderWrite, SavedCard)
from .money import from_wire, to_wire
from .ordering import PAYMENT_AUTHORISED
from .safe_write import (
    ORDERS_KEY_RETENTION, Answer, deterministic_ref, order_reference, safe_write, send_window)

logger = logging.getLogger('apps.payments')

EventHandler = get_class('order.processing', 'EventHandler')
PaymentEventType = get_model('order', 'PaymentEventType')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Transaction = get_model('payment', 'Transaction')

PREFER = 'return=representation'


# -- small helpers -----------------------------------------------------------

def _v(value):
    """An SDK optional member as a plain value: ``UNSET`` -> ``None``."""
    return None if isinstance(value, UnsetType) else value


def _time(value) -> datetime | None:
    value = _v(value)
    return parse_datetime(value) if isinstance(value, str) else None


def _money(money) -> tuple[Decimal | None, str | None]:
    money = _v(money)
    if money is None:
        return None, None
    return from_wire(money.value), money.currency_code


def _first(items):
    items = _v(items)
    return items[0] if items else None


def _cas(payment: OrderPayment, from_states, to_state, **extra) -> bool:
    """Move the payment to ``to_state`` only if it is still in ``from_states``."""
    updated = OrderPayment.objects.filter(pk=payment.pk, state__in=list(from_states)).update(
        state=to_state, updated_at=timezone.now(), **extra)
    payment.refresh_from_db()
    return updated == 1


def _take_over_stale(payment: OrderPayment, state) -> bool:
    """Resume an in-flight state whose request died (older than the send window)."""
    cutoff = timezone.now() - send_window()
    updated = OrderPayment.objects.filter(
        pk=payment.pk, state=state, updated_at__lt=cutoff).update(updated_at=timezone.now())
    payment.refresh_from_db()
    return updated == 1


def _set_state(payment: OrderPayment, state, **fields):
    OrderPayment.objects.filter(pk=payment.pk).update(state=state, updated_at=timezone.now(), **fields)
    payment.refresh_from_db()


def _set_order_status(order, status):
    order.refresh_from_db()
    if order.status != status and status in order.available_statuses():
        order.set_status(status)


def _payment_event(order, name, amount, reference):
    event_type, __ = PaymentEventType.objects.get_or_create(name=name)
    EventHandler().create_payment_event(order, event_type, amount, reference=reference)


def _source(payment: OrderPayment):
    if payment.source_id:
        return payment.source
    source_type, __ = SourceType.objects.get_or_create(name='PayPal')
    source = Source.objects.create(
        order=payment.order, source_type=source_type, currency=payment.currency,
        label=_card_label(payment))
    OrderPayment.objects.filter(pk=payment.pk).update(source=source)
    payment.source = source
    return source


def _card_label(payment: OrderPayment) -> str:
    if payment.card_last_digits:
        return '%s ending %s' % (payment.card_brand or 'Card', payment.card_last_digits)
    return 'PayPal'


def _release_allocation(payment: OrderPayment, txn_type: str, reference: str, status: str):
    source = payment.source
    if source is None:
        return
    source.amount_allocated = max(Decimal('0.00'), source.amount_allocated - payment.amount)
    source.save()
    Transaction.objects.create(source=source, txn_type=txn_type, amount=payment.amount,
                               reference=reference, status=status)


# -- pay: authorize the order total -----------------------------------------

def parse_card(raw) -> dict:
    """Validate card details. The result is used for one request and never stored."""
    if not isinstance(raw, dict):
        raise ApiProblem(400, 'invalid_request', '"card" must be an object.')
    number = ''.join(str(raw.get('number') or '').split())
    expiry = str(raw.get('expiry') or '')
    security_code = str(raw.get('securityCode') or '')
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise ApiProblem(400, 'invalid_card', 'Card number is not valid.')
    if len(expiry) != 7 or expiry[4] != '-' or not (expiry[:4] + expiry[5:]).isdigit() \
            or not 1 <= int(expiry[5:]) <= 12:
        raise ApiProblem(400, 'invalid_card', 'Card expiry must be YYYY-MM.')
    today = timezone.now().date()
    if (int(expiry[:4]), int(expiry[5:])) < (today.year, today.month):
        raise ApiProblem(400, 'invalid_card', 'Card has expired.')
    if security_code and (not security_code.isdigit() or not 3 <= len(security_code) <= 4):
        raise ApiProblem(400, 'invalid_card', 'Security code must be 3 or 4 digits.')
    card: dict[str, object] = {'number': number, 'expiry': expiry}
    if security_code:
        card['security_code'] = security_code
    name = str(raw.get('name') or '').strip()
    if name:
        card['name'] = name[:300]
    address = raw.get('billingAddress')
    if isinstance(address, dict):
        card['billing_address'] = _address(address)
    return card


def _address(raw: dict) -> dict:
    mapping = {'line1': 'address_line_1', 'line2': 'address_line_2', 'city': 'admin_area_2',
               'state': 'admin_area_1', 'postcode': 'postal_code', 'countryCode': 'country_code'}
    address = {wire: str(raw[key])[:300] for key, wire in mapping.items() if raw.get(key)}
    if 'country_code' not in address:
        raise ApiProblem(400, 'invalid_card', 'billingAddress.countryCode is required.')
    address['country_code'] = address['country_code'].upper()
    return address


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, digit in enumerate(reversed(number)):
        d = int(digit)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def pay(user, order, payload: dict) -> tuple[int, OrderPayment]:
    """Authorize the order total. Returns (HTTP status, payment)."""
    payment = order.paypal_payment
    card_details, saved_card = _payment_instrument(user, payload)

    state = payment.state
    if state in (PaymentState.AUTHORIZED, PaymentState.CAPTURING, PaymentState.CAPTURE_PENDING,
                 PaymentState.CAPTURE_UNKNOWN, PaymentState.CAPTURE_FAILED, PaymentState.CAPTURED):
        return 200, payment  # already paid for: a repeat changes nothing
    if state == PaymentState.AUTH_PENDING:
        return _refresh_authorization(payment)
    if state == PaymentState.AUTHORIZING:
        if not _take_over_stale(payment, PaymentState.AUTHORIZING):
            return 202, payment  # another request is authorizing right now
    elif state == PaymentState.AUTH_UNKNOWN:
        # Settle the earlier attempt under its own reference; never a new one.
        if not _cas(payment, [PaymentState.AUTH_UNKNOWN], PaymentState.AUTHORIZING):
            return 202, payment
    elif state in (PaymentState.AWAITING_PAYMENT, PaymentState.AUTH_FAILED,
                   PaymentState.AUTHORIZATION_EXPIRED):
        started = _cas(
            payment, [state], PaymentState.AUTHORIZING, attempt=F('attempt') + 1,
            saved_card=saved_card, paypal_order_id='', paypal_order_status='', authorization_id='',
            authorization_status='', authorized_at=None, original_authorized_at=None,
            authorization_expires_at=None, card_brand='', card_last_digits='', last_error='')
        if not started:
            return pay(user, order, payload)  # lost the race: answer from the winner's state
    else:
        raise ApiProblem(409, 'invalid_state',
                         'This order cannot be paid for (payment state: %s).' % payment.state)
    return _authorize(payment, card_details, saved_card)


def _payment_instrument(user, payload):
    has_card = payload.get('card') is not None
    method_id = payload.get('paymentMethodId')
    if has_card == (method_id is not None):
        raise ApiProblem(400, 'invalid_request',
                         'Send either "card" or "paymentMethodId", not both and not neither.')
    if has_card:
        return parse_card(payload['card']), None
    try:
        public_id = uuid.UUID(str(method_id))
    except ValueError:
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.') from None
    card = SavedCard.objects.filter(public_id=public_id, user=user, state=CardState.ACTIVE).first()
    if card is None:
        # Another shopper's card is indistinguishable from a missing one.
        raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')
    return None, card


def _order_request(payment: OrderPayment, card_details, saved_card) -> OrderRequest:
    order = payment.order
    if saved_card is not None:
        card = CardRequest(vault_id=saved_card.paypal_token_id)
    else:
        card = CardRequest.model_validate(card_details)
    return OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            reference_id=order.number,
            amount=AmountWithBreakdown(currency_code=payment.currency,
                                       value=to_wire(payment.amount, payment.currency)),
            custom_id=order_reference(order),
            invoice_id='%s-%d' % (order_reference(order), payment.attempt),
            description='Order %s' % order.number,
        )],
        payment_source=PaymentSource(card=card),
    )


def _read_order(result) -> Answer:
    """Answer for create_order / authorize_order (both carry the order and its authorization)."""
    order_id = _v(result.id)
    status = _v(result.status)
    if not order_id or status is None:
        raise ValueError('order id/status')
    unit = _first(result.purchase_units)
    payments = _v(unit.payments) if unit is not None else None
    auth = _first(payments.authorizations) if payments is not None else None
    order_outcome = outcomes.order_outcome(status)
    if order_outcome == outcomes.DONE:
        if auth is None or not _v(auth.id):
            raise ValueError('authorization')
        amount, currency = _money(auth.amount)
        # The authorization is what holds the money: it is the id this step answers with.
        return Answer(auth.id, '%s/%s' % (outcomes.wire(status), outcomes.wire(_v(auth.status))),
                      outcomes.authorization_outcome(_v(auth.status)),
                      _time(auth.create_time) or _time(result.update_time), amount, currency)
    amount, currency = _money(unit.amount) if unit is not None else (None, None)
    return Answer(order_id, outcomes.wire(status), order_outcome,
                  _time(result.update_time) or _time(result.create_time), amount, currency)


def _apply_order(payment: OrderPayment):
    def apply(record: ProviderWrite, result, answer: Answer):
        _apply_order_result(payment, record.outcome, result, answer)
    return apply


def _apply_order_result(payment: OrderPayment, outcome: str, result, answer: Answer):
    unit = _first(result.purchase_units)
    payments = _v(unit.payments) if unit is not None else None
    auth = _first(payments.authorizations) if payments is not None else None
    fields = {'paypal_order_id': _v(result.id),
              'paypal_order_status': outcomes.wire(_v(result.status))}
    source = _v(result.payment_source)
    card = _v(source.card) if source is not None else None
    if card is not None:
        fields.update(card_brand=outcomes.wire(_v(card.brand)),
                      card_last_digits=_v(card.last_digits) or '')
    if auth is not None and _v(auth.id):
        created = _time(auth.create_time) or timezone.now()
        fields.update(authorization_id=auth.id,
                      authorization_status=outcomes.wire(_v(auth.status)),
                      authorized_at=created,
                      original_authorized_at=payment.original_authorized_at or created,
                      authorization_expires_at=_time(auth.expiration_time))
    _finish_authorization(payment, outcome, fields, order_status=_v(result.status))


def _finish_authorization(payment: OrderPayment, outcome: str, fields: dict, order_status=None):
    order = payment.order
    if outcome == Outcome.DONE:
        _set_state(payment, PaymentState.AUTHORIZED, **fields)
        source = _source(payment)
        source.label = _card_label(payment)
        source.reference = payment.authorization_id
        source.save()
        source.allocate(payment.amount, reference=payment.authorization_id,
                        status=payment.authorization_status)
        _payment_event(order, 'Authorised', payment.amount, payment.authorization_id)
        _set_order_status(order, PAYMENT_AUTHORISED)
    elif outcome == Outcome.PENDING:
        _set_state(payment, PaymentState.AUTH_PENDING, **fields)
    elif outcome == Outcome.FAILED:
        if order_status == OrderStatus.PAYER_ACTION_REQUIRED:
            error = ('The card issuer requires the shopper to approve this payment in a browser '
                     '(3-D Secure). That flow is not supported; use a different card.')
        else:
            error = 'PayPal did not authorize the payment (status %s).' % (
                fields.get('authorization_status') or fields.get('paypal_order_status') or 'unknown')
        _set_state(payment, PaymentState.AUTH_FAILED, last_error=error, **fields)
    elif outcome == Outcome.NEEDS_REVIEW:
        _set_state(payment, PaymentState.NEEDS_REVIEW,
                   last_error='PayPal held an amount different from the order total.', **fields)
    else:
        _set_state(payment, PaymentState.AUTH_UNKNOWN, **fields)


def _authorize(payment: OrderPayment, card_details, saved_card) -> tuple[int, OrderPayment]:
    client = get_client()
    order = payment.order
    sent = (payment.amount, payment.currency)
    ref = deterministic_ref('ord', order.number, 'a%d' % payment.attempt, 'create')
    body = _order_request(payment, card_details, saved_card)
    try:
        written = safe_write(
            ref, operation='create_order', order=order, retention=ORDERS_KEY_RETENTION, sent=sent,
            send=lambda key: client.orders.create_order(body, pay_pal_request_id=key, prefer=PREFER),
            read=_read_order, apply=_apply_order(payment))
        if written.in_flight:
            return 202, payment
        payment.refresh_from_db()
        if written.result is None:
            # Answered from the stored outcome of this attempt.
            _finish_from_record(payment, written.record)
        # A card order PayPal approved but did not authorize in one step.
        if payment.state == PaymentState.AUTH_PENDING and payment.paypal_order_status == OrderStatus.APPROVED:
            return _authorize_approved(payment)
    except (OutcomeUnknown, ApiProblem):
        payment.refresh_from_db()
        if payment.state == PaymentState.AUTHORIZING:
            _set_state(payment, PaymentState.AUTH_UNKNOWN)
        raise
    except ApiError as e:
        message, __ = provider_message(e)
        _set_state(payment, PaymentState.AUTH_FAILED, last_error=message[:500])
        raise translate(e, action='authorize') from e
    except NEVER_SENT as e:
        _set_state(payment, PaymentState.AUTH_FAILED, last_error='PayPal could not be reached.')
        raise translate(e, action='authorize') from e
    except BaseException:
        payment.refresh_from_db()
        if payment.state == PaymentState.AUTHORIZING:
            _set_state(payment, PaymentState.AUTH_UNKNOWN)
        raise
    return _status_for(payment), payment


def _finish_from_record(payment: OrderPayment, record: ProviderWrite):
    """The step's outcome was already stored; make sure the payment reflects it."""
    if payment.state != PaymentState.AUTHORIZING:
        return
    if record.outcome == Outcome.FAILED:
        _set_state(payment, PaymentState.AUTH_FAILED, last_error=record.detail[:500])
    elif record.outcome == Outcome.NEEDS_REVIEW:
        _set_state(payment, PaymentState.NEEDS_REVIEW)
    elif record.outcome in (Outcome.UNKNOWN, Outcome.SENDING):
        _set_state(payment, PaymentState.AUTH_UNKNOWN)


def _authorize_approved(payment: OrderPayment) -> tuple[int, OrderPayment]:
    client = get_client()
    ref = deterministic_ref('ord', payment.order.number, 'a%d' % payment.attempt, 'authorize')
    written = safe_write(
        ref, operation='authorize_order', order=payment.order, retention=ORDERS_KEY_RETENTION,
        sent=(payment.amount, payment.currency),
        send=lambda key: client.orders.authorize_order(
            payment.paypal_order_id, pay_pal_request_id=key, prefer=PREFER),
        read=_read_order, apply=_apply_order(payment))
    payment.refresh_from_db()
    if written.in_flight:
        return 202, payment
    return _status_for(payment), payment


def _refresh_authorization(payment: OrderPayment) -> tuple[int, OrderPayment]:
    """Re-read a pending authorization (a read: no claim needed)."""
    client = get_client()
    try:
        if payment.authorization_id:
            auth = client.payments.get_authorized_payment(payment.authorization_id)
            outcome = outcomes.authorization_outcome(_v(auth.status))
            if outcome != outcomes.PENDING:
                with transaction.atomic():
                    OrderPayment.objects.filter(
                        pk=payment.pk, state=PaymentState.AUTH_PENDING).update(
                        state=PaymentState.AUTHORIZING)
                    payment.refresh_from_db()
                    if payment.state == PaymentState.AUTHORIZING:
                        _finish_authorization(payment, outcome, {
                            'authorization_status': outcomes.wire(_v(auth.status))})
        elif payment.paypal_order_id:
            paypal_order = client.orders.get_order(payment.paypal_order_id)
            if _v(paypal_order.status) == OrderStatus.APPROVED:
                OrderPayment.objects.filter(pk=payment.pk).update(
                    paypal_order_status=OrderStatus.APPROVED.value)
                payment.refresh_from_db()
                return _authorize_approved(payment)
            answer = _read_order(paypal_order)
            if answer.outcome != outcomes.PENDING:
                with transaction.atomic():
                    OrderPayment.objects.filter(
                        pk=payment.pk, state=PaymentState.AUTH_PENDING).update(
                        state=PaymentState.AUTHORIZING)
                    payment.refresh_from_db()
                    if payment.state == PaymentState.AUTHORIZING:
                        _apply_order_result(payment, answer.outcome, paypal_order, answer)
    except (ApiError, ValueError, httpx.RequestError) as e:
        raise translate(e, action='refresh authorization') from e
    payment.refresh_from_db()
    return _status_for(payment), payment


def _status_for(payment: OrderPayment) -> int:
    statuses: dict[str, int] = {
        PaymentState.AUTHORIZED: 200,
        PaymentState.CAPTURED: 200,
        PaymentState.VOIDED: 200,
        PaymentState.CANCELLED: 200,
        PaymentState.AUTH_PENDING: 202,
        PaymentState.CAPTURE_PENDING: 202,
        PaymentState.VOID_PENDING: 202,
        PaymentState.AUTHORIZING: 202,
        PaymentState.CAPTURING: 202,
        PaymentState.VOIDING: 202,
        PaymentState.AUTH_FAILED: 402,
        PaymentState.CAPTURE_FAILED: 402,
        PaymentState.AUTHORIZATION_EXPIRED: 409,
        PaymentState.NEEDS_REVIEW: 409,
    }
    return statuses.get(payment.state, 504)
