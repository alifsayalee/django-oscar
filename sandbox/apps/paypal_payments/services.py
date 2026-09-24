"""
The payment flows. Views call these; these call PayPal only through
``safe_write`` (writes) or directly for reads, and keep Oscar's own order and
payment models (Order, Source, Transaction) in step.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone
from oscar.core import prices
from oscar.core.loading import get_class, get_model
from paypal.core import ApiError, RawError, UnsetType

from . import gateway
from .errors import ApiProblem, ProviderError, ProviderRejected, translate
from .models import InstallReference, OrderPayment, Outcome, PayPalOperation, PayPalRefund, SavedCard
from .safe_write import (
    KEY_WINDOW_CREATE_ORDER, KEY_WINDOW_PAYMENTS, KEY_WINDOW_VAULT, safe_write,
)

logger = logging.getLogger(__name__)

Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
Order = get_model('order', 'Order')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Transaction = get_model('payment', 'Transaction')
Selector = get_class('partner.strategy', 'Selector')
Applicator = get_class('offer.applicator', 'Applicator')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
FreeShipping = get_class('shipping.methods', 'Free')
InvalidOrderStatus = get_class('order.exceptions', 'InvalidOrderStatus')

MAX_LINE_QUANTITY = 100
SOURCE_TYPE_NAME = 'PayPal'
# The Transaction Search API accepts at most 31 days per request.
SEARCH_CHUNK = timedelta(days=31)
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_PAGES = 1000


# --------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------

def reference_prefix() -> str:
    configured = getattr(settings, 'PAYPAL_REFERENCE_PREFIX', None)
    if configured:
        return configured
    install = InstallReference.objects.order_by('pk').first()
    if install is None:
        raise ApiProblem(503, 'not_migrated', 'Run migrations: the PayPal install reference is missing.')
    return install.prefix


def order_reference(order_number: str, step: str) -> str:
    return '%s-%s-%s' % (reference_prefix(), order_number, step)


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]


def currency() -> str:
    return str(settings.PAYPAL_CURRENCY).upper()


# --------------------------------------------------------------------------
# Placing an order (Oscar's own basket -> order pipeline)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RequestedLine:
    product_id: int
    quantity: int


def place_order(user: Any, lines: list[RequestedLine], request: Any) -> Any:
    wanted: dict[int, int] = defaultdict(int)
    for line in lines:
        wanted[line.product_id] += line.quantity
    strategy = Selector().strategy(request=request, user=user)
    products = []
    for product_id, quantity in wanted.items():
        if quantity > MAX_LINE_QUANTITY:
            raise ApiProblem(422, 'invalid_quantity', 'At most %d of one item per order.' % MAX_LINE_QUANTITY,
                             productId=product_id)
        product = Product.objects.filter(pk=product_id, is_public=True).first()
        if product is None or product.is_parent:
            raise ApiProblem(422, 'unknown_product', 'No purchasable catalogue item with that id.',
                             productId=product_id)
        info = strategy.fetch_for_product(product)
        if not info.price.exists or info.stockrecord is None:
            raise ApiProblem(422, 'not_for_sale', 'That catalogue item has no price.', productId=product_id)
        permitted, message = info.availability.is_purchase_permitted(quantity)
        if not permitted:
            raise ApiProblem(409, 'unavailable', str(message), productId=product_id)
        products.append((product, quantity))

    cur = currency()
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product, quantity in products:
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, request)
        shipping_method = FreeShipping()
        shipping_charge = shipping_method.calculate(basket)
        basket_total = OrderTotalCalculator().calculate(basket, shipping_charge)
        if not basket_total.is_tax_known:
            raise ApiProblem(409, 'tax_unknown', 'The order total could not be determined.')
        amount = basket_total.incl_tax
        if amount <= 0 or not gateway.is_representable(amount, cur):
            raise ApiProblem(409, 'unpayable_total', 'The order total cannot be charged in %s.' % cur,
                             total=str(amount))
        # Amounts come from catalogue prices; the charge currency comes from configuration.
        total = prices.Price(currency=cur, excl_tax=basket_total.excl_tax, incl_tax=amount)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user, request=request)
        basket.submit()
        OrderPayment.objects.create(order=order, amount=amount, currency=cur)
    return order


# --------------------------------------------------------------------------
# Oscar bookkeeping helpers
# --------------------------------------------------------------------------

def _oscar_source(payment: OrderPayment) -> Any:
    source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
    source, _ = Source.objects.get_or_create(
        order=payment.order, source_type=source_type,
        defaults={'currency': payment.currency, 'reference': payment.paypal_order_id,
                  'label': ('%s ending %s' % (payment.card_brand, payment.card_last_digits)).strip()})
    return source


def _advance_order_status(order: Any, target: str) -> None:
    """Walk Oscar's status pipeline to ``target`` (e.g. Pending -> Being processed -> Complete)."""
    for _ in range(3):
        if order.status == target:
            return
        available = order.available_statuses()
        step = target if target in available else next(
            (s for s in available if s != 'Cancelled' and target != 'Cancelled'), None)
        if step is None:
            logger.warning('Order %s cannot move from %s to %s', order.number, order.status, target)
            return
        try:
            order.set_status(step)
        except InvalidOrderStatus:
            logger.warning('Order %s refused status %s', order.number, step)
            return


def _locked(payment: OrderPayment) -> OrderPayment:
    return OrderPayment.objects.select_for_update().select_related('order').get(pk=payment.pk)


def _move(payment: OrderPayment, from_states: tuple[str, ...], to_state: str) -> bool:
    """Compare-and-set in the database: only one request wins a state transition."""
    moved = OrderPayment.objects.filter(pk=payment.pk, state__in=from_states).update(
        state=to_state, updated_at=timezone.now())
    if moved:
        payment.state = to_state
    return bool(moved)


def _fail_attempt(payment: OrderPayment, message: str) -> None:
    """A payment attempt definitively failed: allow another under a new reference."""
    OrderPayment.objects.filter(pk=payment.pk, state=OrderPayment.AUTHORIZING, attempt=payment.attempt).update(
        state=OrderPayment.AWAITING_PAYMENT, attempt=F('attempt') + 1, last_error=message[:512],
        updated_at=timezone.now())
    payment.refresh_from_db()


# --------------------------------------------------------------------------
# Pay (authorize)
# --------------------------------------------------------------------------

PAID_STATES = (OrderPayment.AUTHORIZED, OrderPayment.CAPTURING, OrderPayment.CAPTURED,
               OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED)


def pay(payment: OrderPayment, *, card: gateway.CardDetails | None, saved_card: SavedCard | None) -> OrderPayment:
    if payment.state in PAID_STATES:
        return payment
    if payment.state in (OrderPayment.VOIDED, OrderPayment.CANCELLED):
        raise ApiProblem(409, 'order_cancelled', 'This order was cancelled.')
    if payment.state == OrderPayment.NEEDS_REVIEW:
        raise ApiProblem(409, 'needs_review', 'This payment is being reviewed by an operator.')
    if payment.state == OrderPayment.AWAITING_PAYMENT:
        if not _move(payment, (OrderPayment.AWAITING_PAYMENT,), OrderPayment.AUTHORIZING):
            payment.refresh_from_db()
            return pay(payment, card=card, saved_card=saved_card)
        if saved_card is not None:
            OrderPayment.objects.filter(pk=payment.pk).update(saved_card=saved_card)

    order = payment.order
    reference = order_reference(order.number, 'auth-%d' % payment.attempt)
    body = gateway.authorize_order_request(
        order_number=order.number, reference=reference,
        custom_id='%s:%s' % (reference_prefix(), order.number),
        amount=payment.amount, currency=payment.currency,
        card=card, vault_id=saved_card.paypal_token_id if saved_card else None)
    client = gateway.get_client()
    payer_action = []

    def on_complete(operation: PayPalOperation, paypal_order: Any) -> None:
        read = gateway.read_authorized_order(paypal_order)
        p = _locked(payment)
        p.paypal_order_id = read.paypal_order_id or p.paypal_order_id
        if read.authorization_id:
            p.authorization_id = read.authorization_id
            p.authorization_status = read.answer.status
            p.authorization_expires_at = read.authorization_expires_at
            p.authorized_at = read.answer.provider_time
        p.card_brand = read.card_brand or p.card_brand
        p.card_last_digits = read.card_last_digits or p.card_last_digits
        if saved_card is not None:
            p.saved_card = saved_card
        outcome = operation.outcome
        if read.order_status == 'PAYER_ACTION_REQUIRED':
            payer_action.append(True)
            p.state = OrderPayment.AWAITING_PAYMENT
            p.attempt = F('attempt') + 1
            p.last_error = 'PayPal asked for a payer action (e.g. 3-D Secure) that this API cannot complete.'
        elif outcome == Outcome.DONE:
            if p.state != OrderPayment.AUTHORIZED:
                source = _oscar_source(p)
                source.reference = p.paypal_order_id
                source.allocate(p.amount, reference=p.authorization_id, status=read.answer.status)
            p.state = OrderPayment.AUTHORIZED
            p.last_error = ''
        elif outcome == Outcome.FAILED:
            p.state = OrderPayment.AWAITING_PAYMENT
            p.attempt = F('attempt') + 1
            p.last_error = 'PayPal did not authorize the card (%s).' % (read.answer.status or read.order_status)
        elif outcome == Outcome.NEEDS_REVIEW:
            p.state = OrderPayment.NEEDS_REVIEW
        else:
            p.state = OrderPayment.AUTHORIZING
        p.save()

    try:
        safe_write(
            reference, kind=PayPalOperation.KIND_AUTHORIZE,
            send=lambda key: client.orders.create_order(body, pay_pal_request_id=key,
                                                        prefer=gateway.REPRESENTATION),
            read=lambda o: gateway.read_authorized_order(o).answer,
            refresh=lambda op: client.orders.get_order(
                OrderPayment.objects.values_list('paypal_order_id', flat=True).get(pk=payment.pk)),
            key_window=KEY_WINDOW_CREATE_ORDER,
            sent=(payment.amount, payment.currency),
            on_complete=on_complete,
            order_payment=payment, amount=payment.amount, currency=payment.currency,
        )
    except ProviderError as exc:
        if not exc.outcome_unknown:
            _fail_attempt(payment, exc.message)
        raise
    payment.refresh_from_db()
    if payer_action:
        raise ApiProblem(402, 'payer_action_required',
                         'PayPal requires the shopper to approve this card payment in a browser '
                         '(e.g. 3-D Secure). This integration does not support that challenge; '
                         'no money was taken. Pay with a different card.')
    if payment.state == OrderPayment.AWAITING_PAYMENT:
        raise ApiProblem(402, 'payment_declined', payment.last_error or 'The card was not authorized.')
    return payment


# --------------------------------------------------------------------------
# Fulfil (capture, renewing a stale authorization first)
# --------------------------------------------------------------------------

def _not_renewable(payment: OrderPayment, reason: str, *, issue: str = '', expired: bool = False) -> ApiProblem:
    what = 'has expired' if expired else 'can no longer be captured'
    return ApiProblem(
        409, 'authorization_not_renewable',
        'The card authorization for order %s %s and PayPal would not renew it (%s). No money has been '
        'taken and the hold on the shopper\'s card is gone. Cancel this order and ask the shopper to '
        'place a new order and pay again.' % (payment.order.number, what, reason),
        action='cancel_order_and_request_new_payment', paypalIssue=issue or None)


def fulfil(payment: OrderPayment) -> OrderPayment:
    if payment.state in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED):
        return payment
    if payment.state not in (OrderPayment.AUTHORIZED, OrderPayment.CAPTURING):
        raise ApiProblem(409, 'not_authorized',
                         'Only an order with an authorized payment can be fulfilled (payment state: %s).'
                         % payment.state, paymentState=payment.state)
    if payment.state == OrderPayment.AUTHORIZED and not _move(
            payment, (OrderPayment.AUTHORIZED,), OrderPayment.CAPTURING):
        payment.refresh_from_db()
        return fulfil(payment)
    try:
        _capture_flow(payment)
    except ProviderError as exc:
        if not exc.outcome_unknown:
            _move(payment, (OrderPayment.CAPTURING,), OrderPayment.AUTHORIZED)
        raise
    except ApiProblem as exc:
        if exc.code != 'in_progress':  # another request is capturing: leave its state alone
            _move(payment, (OrderPayment.CAPTURING,), OrderPayment.AUTHORIZED)
        raise
    payment.refresh_from_db()
    return payment


def _capture_flow(payment: OrderPayment) -> None:
    client = gateway.get_client()
    # A read: refresh what PayPal says about the hold before acting on it.
    try:
        auth = client.payments.get_authorized_payment(payment.authorization_id)
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc
    read = gateway.read_authorization(auth)
    now = timezone.now()
    expires = gateway.parse_time(auth.expiration_time) or payment.authorization_expires_at
    authorized_at = read.provider_time or payment.authorized_at
    OrderPayment.objects.filter(pk=payment.pk).update(
        authorization_status=read.status, authorization_expires_at=expires, authorized_at=authorized_at)
    payment.refresh_from_db()

    if read.status in ('VOIDED', 'DENIED'):
        raise _not_renewable(payment, 'PayPal reports the authorization as %s' % read.status)
    if expires is not None and now >= expires and read.status not in ('CAPTURED', 'PARTIALLY_CAPTURED'):
        raise _not_renewable(payment, 'it expired at %s' % expires.isoformat(), expired=True)

    honor = timedelta(days=int(settings.PAYPAL_AUTH_HONOR_PERIOD_DAYS))
    reauthorized = False
    if read.status == 'CREATED' and authorized_at is not None and now - authorized_at > honor:
        _reauthorize(payment)
        reauthorized = True
    try:
        _capture(payment)
    except ProviderRejected as refused:
        if reauthorized or refused.provider_status != 422:
            raise
        logger.info('Capture for order %s refused (%s); renewing the authorization',
                    payment.order.number, refused.issue)
        _reauthorize(payment, capture_issue=refused.issue)
        _capture(payment)


def _reauthorize(payment: OrderPayment, capture_issue: str = '') -> None:
    client = gateway.get_client()
    old_id = payment.authorization_id
    reference = order_reference(payment.order.number, 'reauth-%s' % old_id)

    def on_complete(operation: PayPalOperation, auth: Any) -> None:
        answer = gateway.read_authorization(auth)
        p = _locked(payment)
        if operation.outcome == Outcome.DONE and answer.provider_id:
            p.authorization_id = answer.provider_id
            p.authorized_at = answer.provider_time
            p.authorization_expires_at = gateway.parse_time(auth.expiration_time)
            p.authorization_status = answer.status
            Transaction.objects.create(source=_oscar_source(p), txn_type='Reauthorise', amount=p.amount,
                                       reference=answer.provider_id, status=answer.status)
        elif operation.outcome == Outcome.NEEDS_REVIEW:
            p.state = OrderPayment.NEEDS_REVIEW
        p.save()

    try:
        written = safe_write(
            reference, kind=PayPalOperation.KIND_REAUTHORIZE,
            send=lambda key: client.payments.reauthorize_payment(
                old_id, pay_pal_request_id=key, prefer=gateway.REPRESENTATION,
                body=gateway.reauthorize_request(payment.amount, payment.currency)),
            read=gateway.read_authorization,
            key_window=KEY_WINDOW_PAYMENTS,
            sent=(payment.amount, payment.currency),
            on_complete=on_complete,
            order_payment=payment, amount=payment.amount, currency=payment.currency,
        )
    except ProviderRejected as refused:
        reason = refused.description or refused.issue or refused.message
        if capture_issue:
            reason = 'capture refused with %s; renewal refused: %s' % (capture_issue, reason)
        raise _not_renewable(payment, reason, issue=refused.issue) from refused
    payment.refresh_from_db()
    outcome = written.operation.outcome
    if outcome == Outcome.FAILED:
        raise _not_renewable(payment, 'PayPal answered the renewal with %s' % written.operation.provider_status)
    if outcome != Outcome.DONE:
        raise _Accepted('The authorization is being renewed by PayPal; fulfil again shortly.')


class _Accepted(ProviderError):
    """Not done yet, not failed: the caller gets 202 and the payment stays in its in-flight state."""

    def __init__(self, message: str) -> None:
        super().__init__(202, 'accepted', message, outcome_unknown=True)


def _capture(payment: OrderPayment) -> None:
    client = gateway.get_client()
    auth_id = payment.authorization_id
    reference = order_reference(payment.order.number, 'capture-%s' % auth_id)

    def on_complete(operation: PayPalOperation, capture: Any) -> None:
        answer = gateway.read_capture(capture)
        breakdown = gateway.read_capture_breakdown(capture)
        p = _locked(payment)
        p.capture_id = answer.provider_id or p.capture_id
        p.capture_status = answer.status
        p.captured_at = answer.provider_time or p.captured_at
        if answer.amount is not None:
            p.captured_amount = answer.amount
        p.paypal_fee = breakdown.fee if breakdown.fee is not None else p.paypal_fee
        p.net_amount = breakdown.net if breakdown.net is not None else p.net_amount
        if operation.outcome == Outcome.DONE:
            if p.state not in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED):
                _oscar_source(p).debit(p.captured_amount, reference=p.capture_id, status=answer.status)
                _advance_order_status(p.order, 'Complete')
                p.state = OrderPayment.CAPTURED
            p.last_error = ''
        elif operation.outcome == Outcome.FAILED:
            p.state = OrderPayment.AUTHORIZED
            p.last_error = 'PayPal did not complete the capture (%s).' % answer.status
        elif operation.outcome == Outcome.NEEDS_REVIEW:
            p.state = OrderPayment.NEEDS_REVIEW
        else:
            p.state = OrderPayment.CAPTURING
        p.save()

    safe_write(
        reference, kind=PayPalOperation.KIND_CAPTURE,
        send=lambda key: client.payments.capture_authorized_payment(
            auth_id, pay_pal_request_id=key, prefer=gateway.REPRESENTATION,
            body=gateway.capture_request(payment.amount, payment.currency)),
        read=gateway.read_capture,
        refresh=lambda op: client.payments.get_captured_payment(op.provider_id),
        key_window=KEY_WINDOW_PAYMENTS,
        sent=(payment.amount, payment.currency),
        on_complete=on_complete,
        order_payment=payment, amount=payment.amount, currency=payment.currency,
    )
    payment.refresh_from_db()
    if payment.state == OrderPayment.AUTHORIZED:
        raise ApiProblem(402, 'capture_failed', payment.last_error or 'PayPal did not complete the capture.')
    if payment.state == OrderPayment.CAPTURING:
        raise _Accepted('PayPal has not finished the capture yet; fulfil again to re-check.')


# --------------------------------------------------------------------------
# Cancel (void the hold)
# --------------------------------------------------------------------------

def cancel(payment: OrderPayment) -> OrderPayment:
    if payment.state in (OrderPayment.VOIDED, OrderPayment.CANCELLED):
        return payment
    if payment.state == OrderPayment.AWAITING_PAYMENT:
        if _move(payment, (OrderPayment.AWAITING_PAYMENT,), OrderPayment.CANCELLED):
            _advance_order_status(payment.order, 'Cancelled')
            return payment
        payment.refresh_from_db()
        return cancel(payment)
    if payment.state == OrderPayment.AUTHORIZING:
        raise ApiProblem(409, 'payment_in_progress',
                         'The payment outcome is not settled yet; pay again to re-check it, then cancel.')
    if payment.state in (OrderPayment.CAPTURING, OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED,
                         OrderPayment.REFUNDED):
        raise ApiProblem(409, 'already_fulfilled',
                         'The order has been fulfilled and the money taken; use a refund instead.')
    if payment.state == OrderPayment.NEEDS_REVIEW:
        raise ApiProblem(409, 'needs_review', 'This payment is being reviewed by an operator.')
    if payment.state == OrderPayment.AUTHORIZED and not _move(
            payment, (OrderPayment.AUTHORIZED,), OrderPayment.VOIDING):
        payment.refresh_from_db()
        return cancel(payment)

    client = gateway.get_client()
    auth_id = payment.authorization_id
    reference = order_reference(payment.order.number, 'void-%s' % auth_id)

    def on_complete(operation: PayPalOperation, auth: Any) -> None:
        answer = gateway.read_authorization(auth, for_void=True)
        p = _locked(payment)
        p.void_status = answer.status
        p.authorization_status = answer.status or p.authorization_status
        if operation.outcome == Outcome.DONE:
            if p.state != OrderPayment.VOIDED:
                Transaction.objects.create(source=_oscar_source(p), txn_type='Void', amount=p.amount,
                                           reference=auth_id, status=answer.status)
                _advance_order_status(p.order, 'Cancelled')
            p.state = OrderPayment.VOIDED
            p.voided_at = answer.provider_time or timezone.now()
        elif operation.outcome == Outcome.FAILED:
            p.state = OrderPayment.NEEDS_REVIEW
            p.last_error = 'PayPal reports the authorization as %s; it was not released.' % answer.status
        else:
            p.state = OrderPayment.VOIDING
        p.save()

    try:
        safe_write(
            reference, kind=PayPalOperation.KIND_VOID,
            send=lambda key: client.payments.void_payment(auth_id, pay_pal_request_id=key,
                                                          prefer=gateway.REPRESENTATION),
            read=lambda auth: gateway.read_authorization(auth, for_void=True),
            refresh=lambda op: client.payments.get_authorized_payment(op.provider_id or auth_id),
            key_window=KEY_WINDOW_PAYMENTS,
            sent=None,
            on_complete=on_complete,
            order_payment=payment,
        )
    except ProviderError as exc:
        if not exc.outcome_unknown:
            _move(payment, (OrderPayment.VOIDING,), OrderPayment.AUTHORIZED)
        raise
    payment.refresh_from_db()
    if payment.state == OrderPayment.NEEDS_REVIEW:
        raise ApiProblem(409, 'void_refused', payment.last_error)
    if payment.state == OrderPayment.VOIDING:
        raise _Accepted('PayPal has not confirmed the release yet; cancel again to re-check.')
    return payment


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------

REFUNDABLE_STATES = (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED)
RESERVING_OUTCOMES = (Outcome.SENDING, Outcome.DONE, Outcome.PENDING, Outcome.UNKNOWN, Outcome.NEEDS_REVIEW)


def refundable_amount(payment: OrderPayment) -> Decimal:
    if payment.captured_amount is None:
        return Decimal('0')
    reserved = payment.refunds.filter(outcome__in=RESERVING_OUTCOMES).aggregate(s=Sum('amount'))['s']
    return max(payment.captured_amount - (reserved or Decimal('0')), Decimal('0'))


def _reserve_refund(payment: OrderPayment, key: str, amount: Decimal | None) -> tuple[PayPalRefund, bool]:
    """
    Insert (or reuse) the refund row that reserves its amount. The first write
    takes the database write lock on the payment row, so two concurrent
    refunds reserve one after the other and never exceed the captured amount.
    Returns (refund, is_replay).
    """
    with transaction.atomic():
        OrderPayment.objects.filter(pk=payment.pk).update(lock_version=F('lock_version') + 1)
        p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        existing = PayPalRefund.objects.filter(order_payment=p, idempotency_key=key).first()
        if existing is not None:
            if amount is not None and amount != existing.amount:
                raise ApiProblem(422, 'idempotency_key_reused',
                                 'This idempotency key was already used for a refund of a different amount.',
                                 refundId=str(existing.public_id))
            if existing.outcome != Outcome.FAILED:
                return existing, True
        if p.state not in REFUNDABLE_STATES:
            raise ApiProblem(409, 'not_refundable',
                             'Only a fulfilled (captured) order can be refunded (payment state: %s).' % p.state,
                             paymentState=p.state)
        remaining = refundable_amount(p)
        wanted = amount if amount is not None else (existing.amount if existing else remaining)
        if wanted <= 0 or wanted > remaining:
            raise ApiProblem(422, 'exceeds_refundable',
                             'The refund must be more than zero and at most the %s %s still refundable.'
                             % (gateway.format_amount(remaining, p.currency), p.currency),
                             refundableAmount=gateway.format_amount(remaining, p.currency))
        if existing is not None:  # a failed attempt under the same key: try again under the same reference
            existing.amount = wanted
            existing.outcome = Outcome.SENDING
            existing.error_message = ''
            existing.save()
            return existing, False
        try:
            with transaction.atomic():
                refund = PayPalRefund.objects.create(
                    order_payment=p, idempotency_key=key, amount=wanted, currency=p.currency,
                    reference=order_reference(p.order.number, 'refund-%s' % key_digest(key)))
        except IntegrityError:
            raise ApiProblem(409, 'in_progress', 'The same refund is already being processed; retry shortly.')
        return refund, False


def refund(payment: OrderPayment, *, key: str, amount: Decimal | None) -> tuple[PayPalRefund, bool]:
    """Returns (refund, created_now)."""
    if amount is not None and (amount <= 0 or not gateway.is_representable(amount, payment.currency)):
        raise ApiProblem(422, 'invalid_amount', 'The amount must be positive with at most %d decimals.'
                         % gateway.currency_places(payment.currency))
    refund_row, replay = _reserve_refund(payment, key, amount)
    if replay and refund_row.outcome in (Outcome.DONE, Outcome.FAILED, Outcome.NEEDS_REVIEW):
        return refund_row, False
    client = gateway.get_client()
    capture_id = payment.capture_id

    def on_complete(operation: PayPalOperation, paypal_refund: Any) -> None:
        answer = gateway.read_refund(paypal_refund)
        p = _locked(payment)
        r = PayPalRefund.objects.select_for_update().get(pk=refund_row.pk)
        was_done = r.outcome == Outcome.DONE
        r.outcome = operation.outcome
        r.paypal_refund_id = answer.provider_id or r.paypal_refund_id
        r.provider_status = answer.status
        r.provider_time = answer.provider_time or r.provider_time
        r.save()
        if r.outcome == Outcome.DONE and not was_done:
            _oscar_source(p).refund(r.amount, reference=r.paypal_refund_id, status=answer.status)
        _update_refund_totals(p)

    try:
        safe_write(
            refund_row.reference, kind=PayPalOperation.KIND_REFUND,
            send=lambda k: client.payments.refund_captured_payment(
                capture_id, pay_pal_request_id=k, prefer=gateway.REPRESENTATION,
                body=gateway.refund_request(refund_row.amount, refund_row.currency)),
            read=gateway.read_refund,
            refresh=lambda op: client.payments.get_refund(op.provider_id),
            key_window=KEY_WINDOW_PAYMENTS,
            sent=(refund_row.amount, refund_row.currency),
            on_complete=on_complete,
            order_payment=payment, amount=refund_row.amount, currency=refund_row.currency,
        )
    except ProviderError as exc:
        outcome = Outcome.UNKNOWN if exc.outcome_unknown else Outcome.FAILED
        if exc.code == 'amount_mismatch':
            outcome = Outcome.NEEDS_REVIEW
        PayPalRefund.objects.filter(pk=refund_row.pk).exclude(outcome__in=(Outcome.DONE, Outcome.PENDING)).update(
            outcome=outcome, error_message=exc.message[:512])
        raise
    except ApiProblem as exc:  # in progress elsewhere: leave the reservation as it is
        if exc.code != 'in_progress':
            raise
    refund_row.refresh_from_db()
    return refund_row, not replay


def _update_refund_totals(p: OrderPayment) -> None:
    done = p.refunds.filter(outcome=Outcome.DONE).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    p.refunded_amount = done
    if p.captured_amount is not None and done >= p.captured_amount:
        p.state = OrderPayment.REFUNDED
    elif done > 0:
        p.state = OrderPayment.PARTIALLY_REFUNDED
    p.save()


# --------------------------------------------------------------------------
# Saved cards (PayPal vault)
# --------------------------------------------------------------------------

def save_card(user: Any, card: gateway.CardDetails, key: str) -> tuple[SavedCard | None, str, bool]:
    """Returns (saved card or None, outcome, created_now)."""
    reference = '%s-u%s-vault-%s' % (reference_prefix(), user.pk, key_digest(key))
    known_customer = (SavedCard.objects.filter(user=user).exclude(paypal_customer_id='')
                      .values_list('paypal_customer_id', flat=True).first())
    body = gateway.payment_token_request(
        card, customer_id=known_customer,
        merchant_customer_id='%s-u%s' % (reference_prefix(), user.pk))
    client = gateway.get_client()
    created = []

    def on_complete(operation: PayPalOperation, token: Any) -> None:
        vaulted = gateway.read_payment_token(token)
        if operation.outcome != Outcome.DONE:
            return
        _, was_created = SavedCard.objects.get_or_create(
            operation_reference=reference,
            defaults={'user': user, 'paypal_token_id': vaulted.answer.provider_id,
                      'paypal_customer_id': vaulted.customer_id, 'brand': vaulted.brand,
                      'last_digits': vaulted.last_digits, 'expiry': vaulted.expiry, 'name': vaulted.name[:128]})
        if was_created:
            created.append(True)

    written = safe_write(
        reference, kind=PayPalOperation.KIND_VAULT,
        send=lambda k: client.vault.create_payment_token(body, pay_pal_request_id=k),
        read=lambda t: gateway.read_payment_token(t).answer,
        key_window=KEY_WINDOW_VAULT,
        sent=None,
        on_complete=on_complete,
        user=user,
    )
    operation = written.operation
    if operation.outcome == Outcome.FAILED and operation.provider_id:
        _delete_token_at_paypal(operation.provider_id)
        raise ApiProblem(402, 'card_verification_failed', 'PayPal could not verify this card; it was not saved.')
    saved = SavedCard.objects.filter(operation_reference=reference, user=user).first()
    return saved, operation.outcome, bool(created)


def _delete_token_at_paypal(token_id: str) -> bool:
    """True when PayPal no longer holds the token."""
    try:
        result = gateway.get_client().vault.with_raw_response.delete_payment_token(token_id)
    except (ApiError, httpx.RequestError, ValueError) as exc:
        logger.warning('Deleting vault token failed: %s', type(exc).__name__)
        return False
    if result.response.status_code in (200, 204):
        return True
    if result.response.status_code == 404:
        return True
    logger.warning('Deleting vault token answered HTTP %s', result.response.status_code)
    return False


def delete_card(card: SavedCard) -> bool:
    """Removes the card for the shopper at once; returns whether PayPal confirmed the deletion."""
    SavedCard.objects.filter(pk=card.pk, deleted_at__isnull=True).update(deleted_at=timezone.now())
    if card.provider_deleted:
        return True
    confirmed = _delete_token_at_paypal(card.paypal_token_id)
    if confirmed:
        SavedCard.objects.filter(pk=card.pk).update(provider_deleted=True)
    return confirmed


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def _provider_transactions(start: datetime, end: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    client = gateway.get_client()
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    chunks = pages = 0
    last_refreshed: str | None = None
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + SEARCH_CHUNK, end)
        chunks += 1
        page = 1
        while True:
            try:
                response = client.transaction_search.search_transactions(
                    gateway.rfc3339(chunk_start), gateway.rfc3339(chunk_end),
                    page_size=SEARCH_PAGE_SIZE, page=page)
            except ApiError as exc:
                if isinstance(exc.error, RawError) and exc.status_code in (400, 422):
                    try:
                        body = exc.error.json()
                    except ValueError:
                        body = {}
                    message = body.get('message') if isinstance(body, dict) else None
                    raise ApiProblem(422, 'paypal_rejected', message or 'PayPal rejected the date range.',
                                     paypalIssue=body.get('name') if isinstance(body, dict) else None) from exc
                raise translate(exc) from exc
            except (httpx.RequestError, ValueError) as exc:
                raise translate(exc) from exc
            pages += 1
            if not isinstance(response.last_refreshed_datetime, UnsetType):
                last_refreshed = response.last_refreshed_datetime
            details = response.transaction_details if not isinstance(response.transaction_details, UnsetType) else []
            for detail in details:
                info = detail.transaction_info
                if isinstance(info, UnsetType) or isinstance(info.transaction_id, UnsetType):
                    continue
                record = _provider_record(info)
                records[(record['transactionId'], record['eventCode'] or '', record['initiatedAt'] or '')] = record
            total_pages = response.total_pages if not isinstance(response.total_pages, UnsetType) else 1
            if page >= total_pages or not details or page >= SEARCH_MAX_PAGES:
                break
            page += 1
        chunk_start = chunk_end
    meta = {'chunks': chunks, 'pagesFetched': pages, 'paypalLastRefreshedAt': last_refreshed}
    return list(records.values()), meta


def _opt(value: Any) -> Any:
    return None if isinstance(value, UnsetType) else value


def _provider_record(info: Any) -> dict[str, Any]:
    amount = info.transaction_amount
    fee = info.fee_amount
    return {
        'transactionId': info.transaction_id,
        'eventCode': _opt(info.transaction_event_code),
        'initiatedAt': _opt(info.transaction_initiation_date),
        'status': _opt(info.transaction_status),
        'amount': None if isinstance(amount, UnsetType) else amount.value,
        'currency': None if isinstance(amount, UnsetType) else amount.currency_code,
        'fee': None if isinstance(fee, UnsetType) else fee.value,
        'invoiceId': _opt(info.invoice_id),
        'customField': _opt(info.custom_field),
        'paypalReferenceId': _opt(info.paypal_reference_id),
    }


def _local_record(op: PayPalOperation) -> dict[str, Any]:
    payment = op.order_payment
    return {
        'reference': op.reference,
        'kind': op.kind,
        'orderId': payment.order.number if payment else None,
        'transactionId': op.provider_id or None,
        'amount': None if op.amount is None else gateway.format_amount(op.amount, op.currency or currency()),
        'currency': op.currency or None,
        'outcome': op.outcome,
        'paypalStatus': op.provider_status or None,
        'paypalTime': op.provider_time.isoformat() if op.provider_time else None,
    }


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    fetched, meta = _provider_transactions(start, end)
    # The query boundaries are PayPal's; the report covers exactly [start, end).
    provider = []
    for record in fetched:
        when = gateway.parse_time(record['initiatedAt'] or '')
        if when is None or start <= when < end:
            provider.append(record)

    money_kinds = (PayPalOperation.KIND_CAPTURE, PayPalOperation.KIND_REFUND)
    ops = PayPalOperation.objects.select_related('order_payment__order')
    # Our side on PayPal's clock: the provider time stored when each write completed.
    local = list(ops.filter(kind__in=money_kinds, provider_time__gte=start, provider_time__lt=end)
                 .exclude(provider_id='').exclude(outcome=Outcome.FAILED))
    unsettled = list(ops.filter(kind__in=money_kinds, provider_time__isnull=True,
                                outcome__in=(Outcome.SENDING, Outcome.UNKNOWN, Outcome.PENDING),
                                claimed_at__gte=start, claimed_at__lt=end))
    known_ids = {op.provider_id: op for op in ops.exclude(provider_id='')
                 .filter(provider_id__in=[r['transactionId'] for r in provider])}

    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in provider:
        by_id[record['transactionId']].append(record)

    # PayPal's reporting lags: a write newer than its last refresh cannot be listed yet.
    refreshed = gateway.parse_time(meta['paypalLastRefreshedAt'] or '')
    matched = []
    local_only = []
    not_yet_reported = []
    for op in local:
        records = by_id.pop(op.provider_id, [])
        if not records:
            if refreshed is not None and op.provider_time is not None and op.provider_time > refreshed:
                not_yet_reported.append(_local_record(op))
            else:
                local_only.append(_local_record(op))
            continue
        for record in records:
            matched.append(_match(op, record))
    # PayPal records for writes of ours that fall outside the local window (e.g. an authorization).
    provider_only = []
    for transaction_id, records in by_id.items():
        op = known_ids.get(transaction_id)
        for record in records:
            if op is not None:
                matched.append(_match(op, record))
            else:
                custom = record.get('customField') or ''
                prefix = reference_prefix() + ':'
                record = dict(record, likelyOrderId=custom[len(prefix):] if custom.startswith(prefix) else None)
                provider_only.append(record)

    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'paypal': dict(meta, recordsInRange=len(provider)),
        'summary': {
            'matched': len(matched),
            'amountMismatches': sum(1 for m in matched if not m['amountMatches']),
            'paypalOnly': len(provider_only),
            'localOnly': len(local_only),
            'awaitingPayPalReporting': len(not_yet_reported),
            'unsettled': len(unsettled),
        },
        'matched': matched,
        'paypalOnly': provider_only,
        'localOnly': local_only,
        'awaitingPayPalReporting': not_yet_reported,
        'unsettled': [_local_record(op) for op in unsettled],
        'note': 'PayPal can take up to three hours to list new transactions; writes newer than '
                'paypal.paypalLastRefreshedAt are listed under awaitingPayPalReporting, not localOnly.',
    }


def _match(op: PayPalOperation, record: dict[str, Any]) -> dict[str, Any]:
    provider_amount = gateway.parse_decimal(record['amount']) if record['amount'] is not None else None
    amount_matches = (provider_amount is not None and op.amount is not None
                      and abs(provider_amount) == op.amount and record['currency'] == op.currency)
    return {
        'transactionId': record['transactionId'],
        'kind': op.kind,
        'orderId': op.order_payment.order.number if op.order_payment else None,
        'reference': op.reference,
        'paypalAmount': record['amount'],
        'paypalFee': record['fee'],
        'paypalCurrency': record['currency'],
        'paypalStatus': record['status'],
        'paypalEventCode': record['eventCode'],
        'paypalInitiatedAt': record['initiatedAt'],
        'localAmount': None if op.amount is None else gateway.format_amount(op.amount, op.currency),
        'localOutcome': op.outcome,
        'amountMatches': amount_matches,
    }


def parse_instant(value: str, name: str) -> datetime:
    value = (value or '').strip().replace(' ', '+')  # an unencoded '+' in a query string arrives as a space
    if not value:
        raise ApiProblem(400, 'missing_parameter', '%r is required (ISO-8601 date-time).' % name)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        value += 'T00:00:00'
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ApiProblem(400, 'invalid_parameter', '%r must be an ISO-8601 date-time.' % name) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)
