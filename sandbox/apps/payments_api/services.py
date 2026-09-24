"""
Order, payment, refund, saved-card and reconciliation logic behind the API.

Orders go through Oscar's own basket and ``OrderCreator``; payment bookkeeping
goes to Oscar's ``Source``/``Transaction``/``PaymentEvent``; PayPal-owned state
goes to :mod:`apps.payments_api.models`.

Every PayPal write follows the same discipline:

1. claim the transition with a compare-and-set update, committed *before* the
   PayPal call, so a double-click finds the work already claimed;
2. call PayPal under a deterministic ``PayPal-Request-Id`` derived from the
   operation, so any resend (a double-click, a retry after a timeout) is
   de-duplicated by PayPal instead of charging twice;
3. record the answer with another compare-and-set, so a concurrent resend
   cannot record it twice.

A write whose outcome is unknown stays claimed; repeating the request resends
it under the same key, which is how the outcome is looked up.
"""

import hashlib
import logging
import uuid
from datetime import date, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price

from . import gateway as gw
from .models import PayPalCustomer, PayPalPayment, PayPalRefund

logger = logging.getLogger(__name__)

Basket = get_model('basket', 'Basket')
Bankcard = get_model('payment', 'Bankcard')
Order = get_model('order', 'Order')
PaymentEventType = get_model('order', 'PaymentEventType')
Product = get_model('catalogue', 'Product')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Transaction = get_model('payment', 'Transaction')
EventHandler = get_class('order.processing', 'EventHandler')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
Selector = get_class('partner.strategy', 'Selector')

# PayPal's authorization windows (reauthorize_payment documentation): a hold is
# honoured for 3 days; it can be reauthorized from day 4 to day 29; from day 30
# a new authorization is needed.
HONOR_PERIOD = timedelta(days=3)
REAUTHORIZE_LIMIT = timedelta(days=29)

STATUS_AWAITING_PAYMENT = 'Pending'
STATUS_PAID = 'Being processed'
STATUS_FULFILLED = 'Complete'
STATUS_CANCELLED = 'Cancelled'

CAPTURABLE_AUTH_STATUSES = ('CREATED',)
CAPTURED_AUTH_STATUSES = ('CAPTURED', 'PARTIALLY_CAPTURED')

SOURCE_TYPE_NAME = 'PayPal'


class ApiProblem(Exception):
    """A request this API refuses, with the status and code to answer with."""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def problem_from_paypal(exc, message=None, **extra):
    """Present a PayPal failure to our caller without leaking provider internals."""
    details = dict(extra)
    if exc.issues:
        details['paypalIssues'] = list(exc.issues)
    if exc.debug_id:
        details['paypalDebugId'] = exc.debug_id
    if exc.outcome_unknown:
        details['outcomeUnknown'] = True
    return ApiProblem(exc.status_code, exc.code, message or exc.message, **details)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def paypal_config():
    return gw.PayPalConfig(
        client_id=settings.PAYPAL_CLIENT_ID,
        client_secret=settings.PAYPAL_CLIENT_SECRET,
        base_url=gw.resolve_base_url(settings.PAYPAL_ENVIRONMENT, settings.PAYPAL_BASE_URL),
        currency=configured_currency(),
        timeout=settings.PAYPAL_TIMEOUT,
        reference_prefix=settings.PAYPAL_REFERENCE_PREFIX,
    )


def paypal():
    return gw.get_gateway(paypal_config)


def configured_currency():
    return settings.PAYPAL_CURRENCY.strip().upper()


def _ref(*parts):
    return '-'.join([settings.PAYPAL_REFERENCE_PREFIX] + [str(p) for p in parts])


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _order_queryset():
    return Order.objects.select_related('paypal_payment', 'user')


def get_order_for_user(user, number):
    """An order the caller owns. Anyone else's order is reported as not found."""
    order = _order_queryset().filter(number=number, user=user).first()
    if order is None:
        raise ApiProblem(404, 'ORDER_NOT_FOUND', 'No such order.')
    return order


def get_order_for_operator(number):
    order = _order_queryset().filter(number=number).first()
    if order is None:
        raise ApiProblem(404, 'ORDER_NOT_FOUND', 'No such order.')
    return order


def _payment_for(order):
    payment, _ = PayPalPayment.objects.get_or_create(order=order)
    return payment


def _claim(payment, from_states, **changes):
    """Compare-and-set: move ``payment`` out of ``from_states``; True if this call won."""
    changes['updated_at'] = timezone.now()
    won = PayPalPayment.objects.filter(pk=payment.pk, state__in=from_states).update(**changes) == 1
    payment.refresh_from_db()
    return won


def _payment_event(order, name, amount, reference):
    event_type, _ = PaymentEventType.objects.get_or_create(name=name)
    EventHandler().create_payment_event(order, event_type, amount, reference=reference)


def _set_order_status(order, status):
    order.refresh_from_db(fields=['status'])
    if order.status != status:
        order.set_status(status)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def place_order(user, lines, request=None):
    """
    Place an order for ``lines`` (``[(product_id, quantity), ...]``) through
    Oscar's basket and ``OrderCreator``. Amounts come from the catalogue; the
    currency is the configured PayPal currency. The order awaits payment.
    """
    quantities = {}
    for product_id, quantity in lines:
        quantities[product_id] = quantities.get(product_id, 0) + quantity

    strategy = Selector().strategy(request=request, user=user)
    currency = configured_currency()
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in quantities.items():
            product = Product.objects.filter(pk=product_id, is_public=True).first()
            if product is None:
                raise ApiProblem(422, 'PRODUCT_NOT_FOUND', 'Product %s does not exist.' % product_id,
                                 productId=product_id)
            info = strategy.fetch_for_product(product)
            if info.stockrecord is None or not info.price.exists or not info.price.is_tax_known:
                raise ApiProblem(422, 'PRODUCT_NOT_PURCHASABLE',
                                 'Product %s cannot be bought on its own.' % product_id, productId=product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ApiProblem(422, 'PRODUCT_UNAVAILABLE', str(reason), productId=product_id)
            allowed, reason = basket.is_quantity_allowed(quantity)
            if not allowed:
                raise ApiProblem(422, 'QUANTITY_NOT_ALLOWED', str(reason), productId=product_id)
            basket.add_product(product, quantity)

        basket.reset_offer_applications()
        total_incl_tax = basket.total_incl_tax
        if total_incl_tax <= 0:
            raise ApiProblem(422, 'ORDER_TOTAL_INVALID', 'The order total must be greater than zero.')
        shipping_method = NoShippingRequired()
        zero = Decimal('0.00')
        order = OrderCreator().place_order(
            basket=basket,
            total=Price(currency=currency, excl_tax=basket.total_excl_tax, incl_tax=total_incl_tax),
            shipping_method=shipping_method,
            shipping_charge=Price(currency=currency, excl_tax=zero, incl_tax=zero),
            user=user,
            order_number=OrderNumberGenerator().order_number(basket),
            status=STATUS_AWAITING_PAYMENT,
            request=request,
        )
        basket.submit()
        PayPalPayment.objects.create(order=order)
    return order


# ---------------------------------------------------------------------------
# Authorize (pay)
# ---------------------------------------------------------------------------


def pay_order(user, number, *, card=None, payment_method_id=None):
    """
    Put a hold on the order total, paid by ``card`` (a one-off
    :class:`gateway.CardDetails`) or by one of the caller's saved cards.
    Repeating the request never authorizes twice.
    """
    order = get_order_for_user(user, number)
    payment = _payment_for(order)

    if payment.state in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING, PayPalPayment.CAPTURED,
                         PayPalPayment.CAPTURE_FAILED, PayPalPayment.VOIDING):
        return order  # already paid: a repeat is a no-op
    if payment.state == PayPalPayment.VOIDED or order.status == STATUS_CANCELLED:
        raise ApiProblem(409, 'ORDER_CANCELLED', 'This order has been cancelled and cannot be paid.')

    saved_card = None
    if payment_method_id is not None:
        saved_card = _saved_cards(user).filter(pk=payment_method_id).first()
        if saved_card is None:
            raise ApiProblem(404, 'PAYMENT_METHOD_NOT_FOUND', 'No such saved card.')

    if payment.state in (PayPalPayment.UNPAID, PayPalPayment.DECLINED):
        attempt = payment.attempt + 1
        _claim(
            payment, [payment.state],
            state=PayPalPayment.AUTHORIZING, attempt=attempt,
            auth_request_id=_ref(order.number, 'auth', attempt),
            invoice_id=_ref(order.number, attempt, uuid.uuid4().hex[:8]),
            saved_card=saved_card, last_error_code='', last_error_message='')
        # Whether we won or a concurrent request did, the state is now
        # AUTHORIZING (or already past it); fall through and act on it.
        if payment.state != PayPalPayment.AUTHORIZING:
            return pay_order(user, number, card=card, payment_method_id=payment_method_id)

    # AUTHORIZING: send, or resend under the same request id. PayPal returns
    # the original result for a repeated id, so this never holds funds twice.
    total = order.total_incl_tax
    items = [
        gw.LineItem(name=line.title, sku=line.partner_sku or line.upc or '', unit_amount=line.unit_price_incl_tax,
                    quantity=line.quantity)
        for line in order.lines.all()
    ]
    try:
        result = paypal().authorize(
            request_id=payment.auth_request_id,
            reference=order.number,
            invoice_id=payment.invoice_id,
            description='Order %s' % order.number,
            amount=total,
            currency=order.currency,
            items=items,
            card=card,
            vault_id=saved_card.partner_reference if saved_card is not None else None,
        )
    except gw.PayPalRejected as exc:
        _claim(payment, [PayPalPayment.AUTHORIZING], state=PayPalPayment.DECLINED,
               last_error_code=exc.code, last_error_message=exc.message)
        raise problem_from_paypal(exc, 'PayPal declined the payment: %s' % exc.message)
    except gw.PayPalError as exc:
        # Unknown or not attempted: the claim stays, and repeating the
        # request resends under the same key.
        PayPalPayment.objects.filter(pk=payment.pk).update(
            last_error_code=exc.code, last_error_message=exc.message, updated_at=timezone.now())
        raise problem_from_paypal(
            exc, retry='Repeat this request: it is safe and will not charge the shopper twice.')
    _record_authorization(order, payment, result)
    order.refresh_from_db()
    return order


def _record_authorization(order, payment, result):
    auth = result.authorization
    common = {
        'paypal_order_id': result.paypal_order_id,
        'card_brand': result.card_brand or '',
        'card_last_digits': result.card_last_digits or '',
    }
    if result.order_status == 'PAYER_ACTION_REQUIRED':
        _claim(payment, [PayPalPayment.AUTHORIZING], state=PayPalPayment.DECLINED,
               last_error_code='PAYER_ACTION_REQUIRED',
               last_error_message='PayPal requires the shopper to approve this card payment in a browser.',
               **common)
        raise ApiProblem(
            422, 'PAYER_ACTION_REQUIRED',
            'PayPal requires the shopper to approve this card payment in a browser (for example a 3-D Secure '
            'challenge). This API does not support browser approval; pay with a different card.')
    if auth is None:
        _claim(payment, [PayPalPayment.AUTHORIZING], state=PayPalPayment.DECLINED,
               last_error_code='AUTHORIZATION_MISSING',
               last_error_message='PayPal order %s has status %s and no authorization.' % (
                   result.paypal_order_id, result.order_status),
               **common)
        raise ApiProblem(402, 'PAYMENT_NOT_AUTHORIZED',
                         'PayPal did not authorize the payment (order status %s).' % result.order_status)
    if auth.status == 'DENIED':
        _claim(payment, [PayPalPayment.AUTHORIZING], state=PayPalPayment.DECLINED,
               authorization_id=auth.authorization_id, authorization_status=auth.status,
               last_error_code='PAYMENT_DECLINED',
               last_error_message='Authorization denied%s.' % (
                   ' (%s)' % auth.status_reason if auth.status_reason else ''),
               **common)
        raise ApiProblem(402, 'PAYMENT_DECLINED', 'The card was declined.', reason=auth.status_reason)

    amount_ok = (auth.amount is not None and auth.amount.currency == order.currency
                 and auth.amount.value == order.total_incl_tax)
    if auth.status not in ('CREATED', 'PENDING') or not amount_ok:
        # Never keep a hold we cannot vouch for: release it straight away.
        try:
            paypal().void(auth.authorization_id, request_id=_ref(order.number, 'void', auth.authorization_id))
        except gw.PayPalError:
            logger.exception('Could not void unexpected authorization %s for order %s',
                             auth.authorization_id, order.number)
        _claim(payment, [PayPalPayment.AUTHORIZING], state=PayPalPayment.DECLINED,
               authorization_id=auth.authorization_id, authorization_status=auth.status,
               last_error_code='AUTHORIZATION_UNEXPECTED',
               last_error_message='Authorization %s status %s amount %s did not match order total %s %s.' % (
                   auth.authorization_id, auth.status, auth.amount, order.total_incl_tax, order.currency),
               **common)
        raise ApiProblem(502, 'AUTHORIZATION_UNEXPECTED',
                         'PayPal answered with an authorization that does not match this order; it was released.')

    with transaction.atomic():
        won = _claim(
            payment, [PayPalPayment.AUTHORIZING], state=PayPalPayment.AUTHORIZED,
            authorization_id=auth.authorization_id, authorization_status=auth.status,
            authorized_amount=auth.amount.value, authorized_at=auth.created_at or timezone.now(),
            authorization_expires_at=auth.expires_at, **common)
        if not won:
            return  # a concurrent resend recorded it
        source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
        last_digits = '...' + result.card_last_digits if result.card_last_digits else None
        label = ' '.join(filter(None, [result.card_brand, last_digits]))
        source = Source.objects.create(
            order=order, source_type=source_type, currency=order.currency,
            reference=result.paypal_order_id, label=label[:128])
        source.allocate(auth.amount.value, reference=auth.authorization_id, status=auth.status)
        PayPalPayment.objects.filter(pk=payment.pk).update(source=source)
        _payment_event(order, 'Authorised', auth.amount.value, auth.authorization_id)
        _set_order_status(order, STATUS_PAID)


# ---------------------------------------------------------------------------
# Fulfil (capture)
# ---------------------------------------------------------------------------


def fulfil_order(number):
    """Mark the order fulfilled and take the money held for it."""
    order = get_order_for_operator(number)
    payment = _payment_for(order)
    if payment.state == PayPalPayment.CAPTURED:
        return order
    if payment.state not in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURE_FAILED, PayPalPayment.CAPTURING):
        raise ApiProblem(409, 'ORDER_NOT_AUTHORIZED',
                         'Only an order whose payment is authorized can be fulfilled (payment is %s).'
                         % payment.get_state_display().lower(), paymentState=payment.state)
    previous_state = payment.state
    if payment.state != PayPalPayment.CAPTURING:
        if not _claim(payment, [previous_state], state=PayPalPayment.CAPTURING):
            return fulfil_order(number)

    def release(error_code='', error_message=''):
        _claim(payment, [PayPalPayment.CAPTURING], state=PayPalPayment.AUTHORIZED,
               last_error_code=error_code, last_error_message=error_message)

    gateway = paypal()
    try:
        authorization_id = _renew_if_stale(order, payment, gateway)
    except ApiProblem as problem:
        release(problem.code, problem.message)
        raise
    except gw.PayPalError as exc:
        if exc.outcome_unknown:
            raise problem_from_paypal(exc, retry='Repeat the fulfilment; it is safe to repeat.')
        release(exc.code, exc.message)
        raise problem_from_paypal(exc)

    try:
        capture = gateway.capture(
            authorization_id, request_id=_ref(order.number, 'capture', authorization_id),
            amount=order.total_incl_tax, currency=order.currency, invoice_id=payment.invoice_id)
    except gw.PayPalRejected as exc:
        release(exc.code, exc.message)
        raise problem_from_paypal(
            exc, 'PayPal refused to capture the payment: %s. The hold is unchanged; you can retry the '
                 'fulfilment, or cancel the order to release the shopper\'s funds.' % exc.message)
    except gw.PayPalError as exc:
        if exc.outcome_unknown:
            # The capture may have happened. Keep the claim: repeating the
            # fulfilment resends under the same key and returns the capture.
            raise problem_from_paypal(exc, retry='Repeat the fulfilment; it will not capture twice.')
        release(exc.code, exc.message)
        raise problem_from_paypal(exc)
    _record_capture(order, payment, capture)
    order.refresh_from_db()
    return order


def _renew_if_stale(order, payment, gateway):
    """
    The authorization id to capture: the current one if it is still within
    its honour period, a renewed one if it has gone stale, or an operator-
    actionable :class:`ApiProblem` if it can no longer be renewed.
    """
    info = gateway.get_authorization(payment.authorization_id)
    PayPalPayment.objects.filter(pk=payment.pk).update(
        authorization_status=info.status, authorization_expires_at=info.expires_at or payment.authorization_expires_at)
    if info.status in CAPTURED_AUTH_STATUSES:
        # A previous capture landed; resending it under the same key returns it.
        return payment.authorization_id
    if info.status == 'PENDING':
        raise ApiProblem(
            409, 'AUTHORIZATION_PENDING',
            'PayPal is still reviewing this payment%s, so it cannot be captured yet. Try the fulfilment again later.'
            % (' (%s)' % info.status_reason if info.status_reason else ''))
    if info.status not in CAPTURABLE_AUTH_STATUSES:
        raise ApiProblem(
            409, 'AUTHORIZATION_NOT_CAPTURABLE',
            'The payment hold is %s at PayPal and can no longer be captured or renewed. Cancel this order and ask '
            'the shopper to place and pay for a new one.' % info.status, authorizationStatus=info.status)

    now = timezone.now()
    originally_authorized = payment.authorized_at or info.created_at or now
    expired = info.expires_at is not None and now >= info.expires_at
    if expired or now - originally_authorized >= REAUTHORIZE_LIMIT:
        raise ApiProblem(
            409, 'AUTHORIZATION_EXPIRED',
            'The payment hold placed on %s has expired and PayPal cannot renew holds older than 29 days. Cancel '
            'this order and ask the shopper to place and pay for a new one.'
            % originally_authorized.date().isoformat(), authorizedAt=originally_authorized.isoformat())

    honour_started = payment.reauthorized_at or originally_authorized
    if now - honour_started <= HONOR_PERIOD:
        return payment.authorization_id

    stale_id = payment.authorization_id
    try:
        renewed = gateway.reauthorize(stale_id, request_id=_ref(order.number, 'reauth', stale_id))
    except gw.PayPalRejected as exc:
        raise ApiProblem(
            409, 'AUTHORIZATION_RENEWAL_REFUSED',
            'The payment hold from %s is past its 3-day honour period and PayPal refused to renew it: %s. Cancel '
            'this order to release the hold and ask the shopper to pay again.'
            % (originally_authorized.date().isoformat(), exc.message),
            paypalIssues=list(exc.issues), paypalDebugId=exc.debug_id)
    PayPalPayment.objects.filter(pk=payment.pk).update(
        original_authorization_id=payment.original_authorization_id or stale_id,
        authorization_id=renewed.authorization_id, authorization_status=renewed.status,
        authorization_expires_at=renewed.expires_at, reauthorized_at=renewed.created_at or now,
        updated_at=now)
    if payment.source_id:
        Transaction.objects.create(
            source_id=payment.source_id, txn_type='Reauthorise', amount=payment.authorized_amount or 0,
            reference=renewed.authorization_id, status=renewed.status)
    payment.refresh_from_db()
    logger.info('Order %s: authorization %s renewed as %s', order.number, stale_id, renewed.authorization_id)
    return renewed.authorization_id


def _record_capture(order, payment, capture):
    fields = {
        'capture_id': capture.capture_id,
        'capture_status': capture.status,
        'captured_amount': capture.amount.value if capture.amount else None,
        'paypal_fee': capture.paypal_fee.value if capture.paypal_fee else None,
        'net_amount': capture.net_amount.value if capture.net_amount else None,
        'captured_at': timezone.now(),
    }
    if capture.status not in ('COMPLETED', 'PENDING', 'PARTIALLY_REFUNDED', 'REFUNDED'):
        _claim(payment, [PayPalPayment.CAPTURING], state=PayPalPayment.CAPTURE_FAILED,
               last_error_code='CAPTURE_%s' % capture.status,
               last_error_message='Capture %s is %s%s.' % (
                   capture.capture_id, capture.status,
                   ' (%s)' % capture.status_reason if capture.status_reason else ''),
               **fields)
        raise ApiProblem(402, 'CAPTURE_FAILED', 'PayPal did not complete the capture (status %s).' % capture.status,
                         reason=capture.status_reason)
    with transaction.atomic():
        if not _claim(payment, [PayPalPayment.CAPTURING], state=PayPalPayment.CAPTURED,
                      authorization_status='CAPTURED', last_error_code='', last_error_message='', **fields):
            return
        amount = fields['captured_amount'] or order.total_incl_tax
        if payment.source_id:
            payment.source.debit(amount, reference=capture.capture_id, status=capture.status)
        _payment_event(order, 'Captured', amount, capture.capture_id)
        EventHandler().consume_stock_allocations(order)
        _set_order_status(order, STATUS_FULFILLED)


# ---------------------------------------------------------------------------
# Cancel (void)
# ---------------------------------------------------------------------------


def cancel_order(number):
    """Cancel before fulfilment: release any hold, so no money ever moves."""
    order = get_order_for_operator(number)
    payment = _payment_for(order)
    if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.CAPTURING):
        raise ApiProblem(409, 'ORDER_ALREADY_FULFILLED',
                         'This order has been fulfilled and its payment captured; refund it instead.')
    if payment.state == PayPalPayment.AUTHORIZING:
        raise ApiProblem(409, 'PAYMENT_IN_FLIGHT',
                         'A payment for this order is still being confirmed with PayPal. Ask the shopper to repeat '
                         'the payment request so its outcome is settled, then cancel.')
    if payment.state in (PayPalPayment.UNPAID, PayPalPayment.DECLINED, PayPalPayment.VOIDED):
        if order.status != STATUS_CANCELLED:
            with transaction.atomic():
                EventHandler().cancel_stock_allocations(order)
                _set_order_status(order, STATUS_CANCELLED)
        order.refresh_from_db()
        return order

    previous_state = payment.state
    if payment.state != PayPalPayment.VOIDING:
        if not _claim(payment, [previous_state], state=PayPalPayment.VOIDING):
            return cancel_order(number)

    gateway = paypal()
    authorization_id = payment.authorization_id
    try:
        voided = gateway.void(authorization_id, request_id=_ref(order.number, 'void', authorization_id))
        voided_status = voided.status
    except gw.PayPalRejected as exc:
        # Perhaps it is already void (an earlier attempt landed); ask PayPal.
        try:
            voided_status = gateway.get_authorization(authorization_id).status
        except gw.PayPalError:
            voided_status = None
        if voided_status != 'VOIDED':
            _claim(payment, [PayPalPayment.VOIDING], state=PayPalPayment.AUTHORIZED,
                   last_error_code=exc.code, last_error_message=exc.message)
            raise problem_from_paypal(
                exc, 'PayPal refused to release the hold: %s' % exc.message, authorizationStatus=voided_status)
    except gw.PayPalError as exc:
        if exc.outcome_unknown:
            raise problem_from_paypal(exc, retry='Repeat the cancellation; it is safe to repeat.')
        _claim(payment, [PayPalPayment.VOIDING], state=previous_state,
               last_error_code=exc.code, last_error_message=exc.message)
        raise problem_from_paypal(exc)

    with transaction.atomic():
        if _claim(payment, [PayPalPayment.VOIDING], state=PayPalPayment.VOIDED,
                  authorization_status=voided_status or 'VOIDED', last_error_code='', last_error_message=''):
            amount = payment.authorized_amount or order.total_incl_tax
            if payment.source_id:
                source = payment.source
                source.amount_allocated = max(source.amount_allocated - amount, Decimal('0.00'))
                source.save()
                Transaction.objects.create(source=source, txn_type='Void', amount=amount,
                                           reference=authorization_id, status=voided_status or 'VOIDED')
            _payment_event(order, 'Voided', amount, authorization_id)
            EventHandler().cancel_stock_allocations(order)
            _set_order_status(order, STATUS_CANCELLED)
    order.refresh_from_db()
    return order


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def refund_order(user, number, *, amount, idempotency_key):
    """
    Refund all (``amount=None``) or part of the captured payment. Returns
    ``(refund, created)``. The same key never refunds twice; distinct keys
    may refund again, never beyond what was captured.
    """
    order = get_order_for_user(user, number)
    payment = _payment_for(order)
    if amount is not None:
        amount = amount.quantize(Decimal('0.01'))

    refund = payment.refunds.filter(idempotency_key=idempotency_key).first()
    if refund is not None:
        if amount is not None and refund.amount != amount:
            raise ApiProblem(422, 'IDEMPOTENCY_KEY_REUSED',
                             'This idempotency key was already used for a refund of %s.' % refund.amount)
        if refund.state != PayPalRefund.REQUESTED:
            return refund, False
    else:
        if payment.state != PayPalPayment.CAPTURED:
            raise ApiProblem(409, 'ORDER_NOT_CAPTURED',
                             'Only a fulfilled order can be refunded; an unfulfilled order is cancelled instead.')
        try:
            refund = _reserve_refund(payment, amount, idempotency_key)
        except IntegrityError:
            # A concurrent request with the same key reserved it first.
            return refund_order(user, number, amount=amount, idempotency_key=idempotency_key)

    try:
        info = paypal().refund(payment.capture_id, request_id=refund.request_id, amount=refund.amount,
                               currency=order.currency)
    except gw.PayPalRejected as exc:
        _settle_refund(refund, state=PayPalRefund.FAILED, error_code=exc.code, error_message=exc.message)
        raise problem_from_paypal(exc, 'PayPal refused the refund: %s' % exc.message)
    except gw.PayPalError as exc:
        if exc.outcome_unknown:
            # The refund may have happened: keep the reservation so it cannot
            # be refunded again; repeating with the same key settles it.
            PayPalRefund.objects.filter(pk=refund.pk).update(error_code=exc.code, error_message=exc.message)
            raise problem_from_paypal(exc, retry='Repeat this request with the same idempotency key.')
        _settle_refund(refund, state=PayPalRefund.FAILED, error_code=exc.code, error_message=exc.message)
        raise problem_from_paypal(exc)
    _record_refund(order, payment, refund, info)
    refund.refresh_from_db()
    if refund.state == PayPalRefund.FAILED:
        raise ApiProblem(402, 'REFUND_FAILED',
                         'PayPal could not complete the refund (status %s).' % refund.paypal_status,
                         refundId=str(refund.pk))
    return refund, True


def _reserve_refund(payment, amount, idempotency_key):
    with transaction.atomic():
        # Take the payment row's write lock first (select_for_update where the
        # database supports it; the no-op update serialises writers on SQLite)
        # so concurrent refunds see each other's reservations.
        PayPalPayment.objects.filter(pk=payment.pk).update(updated_at=timezone.now())
        locked = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        refundable = locked.refundable_amount()
        requested = refundable if amount is None else amount
        if requested <= 0:
            raise ApiProblem(422, 'NOTHING_TO_REFUND', 'Nothing is left to refund on this order.',
                             refundableAmount=str(refundable))
        if requested > refundable:
            raise ApiProblem(422, 'REFUND_EXCEEDS_CAPTURED',
                             'A refund of %s exceeds the %s still refundable on this order.' % (requested, refundable),
                             refundableAmount=str(refundable))
        return PayPalRefund.objects.create(
            payment=locked, idempotency_key=idempotency_key, amount=requested,
            request_id=_ref(locked.order.number, 'refund', _digest(idempotency_key)))


def _settle_refund(refund, **changes):
    changes['updated_at'] = timezone.now()
    return PayPalRefund.objects.filter(pk=refund.pk, state=PayPalRefund.REQUESTED).update(**changes) == 1


def _record_refund(order, payment, refund, info):
    state = {
        'COMPLETED': PayPalRefund.COMPLETED,
        'PENDING': PayPalRefund.PENDING,
        'FAILED': PayPalRefund.FAILED,
        'CANCELLED': PayPalRefund.CANCELLED,
    }.get(info.status, PayPalRefund.PENDING)
    with transaction.atomic():
        won = _settle_refund(
            refund, state=state, paypal_refund_id=info.refund_id, paypal_status=info.status,
            paypal_fee=info.paypal_fee.value if info.paypal_fee else None,
            net_amount=info.net_amount.value if info.net_amount else None,
            error_code='', error_message='')
        if not won or state in PayPalRefund.RELEASED_STATES:
            return
        if payment.source_id:
            payment.source.refund(refund.amount, reference=info.refund_id, status=info.status)
        _payment_event(order, 'Refunded', refund.amount, info.refund_id)


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


def _saved_cards(user):
    # Cards saved through PayPal carry their vault token as partner_reference.
    return Bankcard.objects.filter(user=user).exclude(partner_reference='')


def list_saved_cards(user):
    return list(_saved_cards(user).order_by('-pk'))


def save_card(user, card, idempotency_key=None):
    """Vault ``card`` with PayPal and keep only its token and a masked description."""
    customer = PayPalCustomer.objects.filter(user=user).first()
    key = _digest(idempotency_key) if idempotency_key else uuid.uuid4().hex
    vaulted = paypal().vault_card(
        card, request_id=_ref('user%s' % user.pk, 'card', key),
        customer_id=customer.customer_id if customer else None)

    existing = _saved_cards(user).filter(partner_reference=vaulted.token_id).first()
    if existing is not None:
        return existing, False  # a repeat of the same request
    if customer is None and vaulted.customer_id:
        PayPalCustomer.objects.get_or_create(user=user, defaults={'customer_id': vaulted.customer_id})

    bankcard = Bankcard(
        user=user,
        number='XXXX-XXXX-XXXX-%s' % vaulted.last_digits,
        expiry_date=_last_day_of_month(vaulted.expiry or card.expiry),
        partner_reference=vaulted.token_id,
    )
    bankcard.card_type = (vaulted.brand or 'CARD').upper()
    bankcard.save()
    return bankcard, True


def delete_card(user, payment_method_id):
    bankcard = _saved_cards(user).filter(pk=payment_method_id).first()
    if bankcard is None:
        raise ApiProblem(404, 'PAYMENT_METHOD_NOT_FOUND', 'No such saved card.')
    try:
        paypal().delete_vaulted_card(bankcard.partner_reference)
    except gw.PayPalError as exc:
        raise problem_from_paypal(exc, 'PayPal could not remove the saved card: %s' % exc.message)
    bankcard.delete()


def _last_day_of_month(expiry):
    year, month = (int(part) for part in expiry.split('-')[:2])
    first_of_next = date(year + month // 12, month % 12 + 1, 1)
    return first_of_next - timedelta(days=1)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile(start, end):
    """
    PayPal's own record of transactions between ``start`` and ``end``, lined
    up against the captures and refunds this site recorded.
    """
    result = paypal().search_transactions(start, end)

    captures = {}
    refunds = {}
    invoices = {}
    payments = PayPalPayment.objects.select_related('order').prefetch_related('refunds')
    for payment in payments.exclude(paypal_order_id=''):
        if payment.invoice_id:
            invoices[payment.invoice_id] = payment
        if payment.capture_id:
            captures[payment.capture_id] = payment
        for refund in payment.refunds.all():
            if refund.paypal_refund_id:
                refunds[refund.paypal_refund_id] = refund

    matched, paypal_only = [], []
    seen = set()
    for tx in result.transactions:
        entry = _transaction_json(tx)
        payment, kind, expected = None, None, None
        if tx.transaction_id in captures:
            payment, kind = captures[tx.transaction_id], 'capture'
            expected = payment.captured_amount
        elif tx.transaction_id in refunds:
            refund = refunds[tx.transaction_id]
            payment, kind, expected = refund.payment, 'refund', -refund.amount
        elif tx.invoice_id in invoices:
            payment, kind = invoices[tx.invoice_id], 'invoice'
        elif tx.reference_id in captures:
            payment, kind = captures[tx.reference_id], 'related_to_capture'
        if payment is None:
            paypal_only.append(entry)
            continue
        seen.add(tx.transaction_id)
        entry.update({'orderId': payment.order.number, 'matchedBy': kind, 'appPaymentState': payment.state})
        if expected is not None and tx.amount is not None:
            entry['expectedAmount'] = str(expected)
            entry['amountMatches'] = tx.amount.value == expected
        matched.append(entry)

    app_only = []
    for payment in payments.filter(captured_at__gte=start, captured_at__lt=end).exclude(capture_id=''):
        if payment.capture_id not in seen:
            app_only.append({'kind': 'capture', 'orderId': payment.order.number, 'paypalId': payment.capture_id,
                             'amount': _money(payment.captured_amount), 'at': payment.captured_at.isoformat()})
    refund_qs = PayPalRefund.objects.select_related('payment__order').filter(
        created_at__gte=start, created_at__lt=end,
        state__in=[PayPalRefund.COMPLETED, PayPalRefund.PENDING, PayPalRefund.REQUESTED])
    for refund in refund_qs:
        if refund.paypal_refund_id not in seen:
            app_only.append({'kind': 'refund', 'orderId': refund.payment.order.number,
                             'paypalId': refund.paypal_refund_id or None, 'amount': _money(-refund.amount),
                             'state': refund.state, 'at': refund.created_at.isoformat()})

    mismatches = [m for m in matched if m.get('amountMatches') is False]
    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'paypalLastRefreshed': result.last_refreshed,
        'searchWindows': result.windows,
        'pagesFetched': result.pages,
        'paypalDataUnavailable': [
            {'from': window_start.isoformat(), 'to': window_end.isoformat()}
            for window_start, window_end in result.unavailable_windows
        ],
        'summary': {
            'paypalTransactions': len(result.transactions),
            'matched': len(matched),
            'paypalOnly': len(paypal_only),
            'appOnly': len(app_only),
            'amountMismatches': len(mismatches),
        },
        'matched': matched,
        'paypalOnly': paypal_only,
        'appOnly': app_only,
        'note': 'PayPal lists transactions up to three hours after they happen; very recent activity '
                'can appear under appOnly until PayPal reports it. Windows listed under '
                'paypalDataUnavailable have no PayPal report data yet.',
    }


def _transaction_json(tx):
    return {
        'paypalTransactionId': tx.transaction_id,
        'paypalReferenceId': tx.reference_id,
        'eventCode': tx.event_code,
        'status': tx.status,
        'amount': _amount_json(tx.amount),
        'fee': _amount_json(tx.fee),
        'invoiceId': tx.invoice_id,
        'customField': tx.custom_field,
        'initiatedAt': tx.initiated_at,
    }


def _amount_json(amount):
    if amount is None:
        return None
    return {'value': str(amount.value), 'currency': amount.currency}


def _money(value):
    return None if value is None else str(value)


def parse_iso_datetime(value, name):
    parsed = parse_datetime(value or '')
    if parsed is None:
        raise ApiProblem(400, 'INVALID_DATETIME', '%s must be an ISO-8601 date-time.' % name, parameter=name)
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def validate_range(start, end):
    if start >= end:
        raise ApiProblem(400, 'INVALID_RANGE', '"from" must be earlier than "to".')
    now = timezone.now()
    if end > now:
        end = now
    if now - start > timedelta(days=3 * 365):
        raise ApiProblem(400, 'RANGE_TOO_OLD', 'PayPal reports transactions from the last three years only.')
    return start, end
