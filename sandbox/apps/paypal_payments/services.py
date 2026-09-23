"""
Order and payment use-cases behind the JSON API.

Every PayPal call goes through ``gateway``. Views call these functions and turn
``ServiceError`` into an HTTP response; nothing here knows about HTTP.

Concurrency / idempotency model:

* each payment action on an order first *claims* the ``PayPalPayment`` row
  with a conditional UPDATE (``lock_until`` lease), so a double-click cannot
  start a second PayPal call while one is in flight;
* every PayPal write carries a PayPal-Request-Id derived from the payment and
  the action, so repeating an action whose outcome was unknown (timeout,
  crash) replays PayPal's original result instead of acting twice.
"""
import calendar
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_class, get_model

from . import gateway
from .gateway import (
    PayPalConfigurationError, PayPalError, PayPalOutcomeUnknown, PayPalRejected)
from .models import PayPalCustomer, PayPalPayment, PayPalRefund

logger = logging.getLogger(__name__)

Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
Basket = get_model('basket', 'Basket')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Transaction = get_model('payment', 'Transaction')
Bankcard = get_model('payment', 'Bankcard')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
SurchargeApplicator = get_class('checkout.applicator', 'SurchargeApplicator')
Selector = get_class('partner.strategy', 'Selector')
Applicator = get_class('offer.applicator', 'Applicator')
ShippingRepository = get_class('shipping.repository', 'Repository')

# Order statuses (see OSCAR_ORDER_STATUS_PIPELINE in sandbox/settings.py)
STATUS_AWAITING_PAYMENT = 'Awaiting payment'
STATUS_AUTHORISED = 'Payment authorised'
STATUS_COMPLETE = 'Complete'
STATUS_CANCELLED = 'Cancelled'

SOURCE_TYPE_NAME = 'PayPal'

# PayPal's documented authorization lifecycle (reauthorize_payment docstring):
# funds are honoured for 3 days; a reauthorization is allowed once, from day 4
# to day 29; after that a new authorization is required.
HONOR_PERIOD = timedelta(days=3)

# Must outlast the longest single action: fulfil makes up to four PayPal calls,
# each possibly retried once, at PAYPAL_TIMEOUT_SECONDS apiece.
LOCK_TTL = timedelta(minutes=5)

MAX_ITEMS = 50
MAX_QUANTITY = 99
REPORT_WINDOW = timedelta(days=31)     # search_transactions: "maximum supported range is 31 days"
REPORT_MAX_RANGE = timedelta(days=3 * 365)
REPORT_MAX_PAGES = 500
CENT = Decimal('0.01')


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class ServiceError(Exception):
    status = 400
    code = 'invalid_request'

    def __init__(self, message, *, code=None, status=None, **details):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details


class InvalidRequest(ServiceError):
    pass


class NotFound(ServiceError):
    status = 404
    code = 'not_found'


class Conflict(ServiceError):
    status = 409
    code = 'conflict'


class Unprocessable(ServiceError):
    status = 422
    code = 'unprocessable'


class PaymentDeclined(ServiceError):
    status = 402
    code = 'payment_declined'


class ProviderFailure(ServiceError):
    status = 502
    code = 'payment_provider_error'


class ProviderOutcomeUnknown(ServiceError):
    status = 503
    code = 'payment_provider_unavailable'


def _provider_failure(exc):
    """Map a gateway failure that the caller cannot fix to a ServiceError."""
    if isinstance(exc, PayPalOutcomeUnknown):
        return ProviderOutcomeUnknown(
            'PayPal could not be reached or its answer could not be read, so the '
            'outcome is unknown. Repeating the same request is safe.')
    if isinstance(exc, PayPalConfigurationError):
        return ProviderFailure('The payment provider is not configured correctly.',
                               code='payment_provider_misconfigured')
    return ProviderFailure('The payment provider failed: %s' % exc.message)


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------

def get_order(user, order_id, *, allow_staff=False):
    order = (Order._default_manager.select_related('paypal_payment')
             .filter(pk=order_id).first())
    if order is None or not (order.user_id == user.pk or (allow_staff and user.is_staff)):
        # Same answer whether it does not exist or belongs to someone else.
        raise NotFound('Order not found')
    return order


def payment_for(order):
    try:
        return order.paypal_payment
    except PayPalPayment.DoesNotExist:
        raise Conflict('This order was not placed through the payments API.',
                       code='not_an_api_order') from None


def _get_saved_card(user, payment_method_id):
    card = (Bankcard._default_manager
            .filter(pk=payment_method_id, user=user).exclude(partner_reference='').first())
    if card is None:
        raise NotFound('Payment method not found', code='payment_method_not_found')
    return card


# --------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------

def _lease_free(now):
    return Q(lock_until__isnull=True) | Q(lock_until__lt=now)


def _claim(payment, states, **updates):
    """Take the in-flight lease on ``payment`` if it is in one of ``states``."""
    now = timezone.now()
    claimed = (PayPalPayment.objects
               .filter(pk=payment.pk, state__in=states)
               .filter(_lease_free(now))
               .update(lock_until=now + LOCK_TTL, **updates))
    payment.refresh_from_db()
    if not claimed:
        if payment.lock_until and payment.lock_until >= now:
            raise Conflict('Another payment action on this order is in progress; '
                           'retry shortly.', code='payment_in_progress')
        return False
    return True


def _release(payment, *, save=True):
    payment.lock_until = None
    if save:
        payment.save()


def _record_error(payment, code, message, debug_id=''):
    payment.last_error_code = code[:128]
    payment.last_error_message = message
    payment.last_error_debug_id = debug_id[:64]


# --------------------------------------------------------------------------
# Ledger (Oscar payment.Source / Transaction)
# --------------------------------------------------------------------------

def _source_for(payment):
    if payment.source_id:
        return payment.source
    source_type, __ = SourceType._default_manager.get_or_create(name=SOURCE_TYPE_NAME)
    source = Source._default_manager.create(
        order=payment.order, source_type=source_type, currency=payment.currency,
        reference=payment.paypal_order_id)
    payment.source = source
    return source


def _release_allocation(payment, txn_type, reference, status):
    source = payment.source
    if source is None:
        return
    source.amount_allocated = max(source.amount_allocated - payment.amount, Decimal('0.00'))
    source.save()
    Transaction._default_manager.create(
        source=source, txn_type=txn_type, amount=payment.amount,
        reference=reference, status=status)


# --------------------------------------------------------------------------
# Place an order
# --------------------------------------------------------------------------

def _parse_items(items):
    if not isinstance(items, list) or not items:
        raise InvalidRequest('"items" must be a non-empty list', code='invalid_items')
    if len(items) > MAX_ITEMS:
        raise InvalidRequest('At most %d items per order' % MAX_ITEMS, code='invalid_items')
    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise InvalidRequest('Each item must be an object', code='invalid_items')
        product_id = item.get('productId')
        quantity = item.get('quantity', 1)
        if (not isinstance(product_id, int) or isinstance(product_id, bool)
                or not isinstance(quantity, int) or isinstance(quantity, bool)):
            raise InvalidRequest('productId and quantity must be integers', code='invalid_items')
        if quantity < 1:
            raise InvalidRequest('quantity must be at least 1', code='invalid_items')
        quantities[product_id] = quantities.get(product_id, 0) + quantity
        if quantities[product_id] > MAX_QUANTITY:
            raise InvalidRequest('At most %d of one product per order' % MAX_QUANTITY,
                                 code='invalid_items')
    return quantities


def order_amount(order):
    return order.total_incl_tax.quantize(CENT)


def place_order(user, items, request=None):
    quantities = _parse_items(items)
    products = {p.pk: p for p in Product._default_manager.filter(pk__in=quantities)}
    missing = sorted(set(quantities) - set(products))
    if missing:
        raise InvalidRequest('Unknown catalogue item(s): %s' % ', '.join(map(str, missing)),
                             code='unknown_product', productIds=missing)
    currency_code = gateway.currency()

    with transaction.atomic():
        basket = Basket._default_manager.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for product_id, quantity in quantities.items():
            product = products[product_id]
            info = basket.strategy.fetch_for_product(product)
            if product.is_parent or not info.availability.is_available_to_buy:
                raise InvalidRequest('Product %s is not available to buy' % product_id,
                                     code='product_unavailable', productId=product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise InvalidRequest('Product %s: %s' % (product_id, reason),
                                     code='product_unavailable', productId=product_id)
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, request)

        shipping_method = ShippingRepository().get_default_shipping_method(
            basket=basket, user=user, request=request)
        shipping_charge = shipping_method.calculate(basket)
        surcharges = SurchargeApplicator(request).get_applicable_surcharges(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge,
                                                        surcharges=surcharges)
        if total.incl_tax <= 0:
            raise InvalidRequest('Order total must be greater than zero', code='zero_total')

        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user, request=request,
            surcharges=surcharges, status=STATUS_AWAITING_PAYMENT)
        basket.submit()
        PayPalPayment.objects.create(
            order=order, currency=currency_code, amount=order_amount(order))
    logger.info('Order %s placed by user %s', order.number, user.pk)
    return order


# --------------------------------------------------------------------------
# Authorize (pay)
# --------------------------------------------------------------------------

@sensitive_variables('card')
def pay(user, order_id, *, card=None, payment_method_id=None):
    """Put a hold on the order total, with a one-off card or a saved card."""
    if (card is None) == (payment_method_id is None):
        raise InvalidRequest('Provide either "card" or "paymentMethodId"',
                             code='invalid_payment_source')
    order = get_order(user, order_id)
    payment = payment_for(order)
    if order.status == STATUS_CANCELLED:
        raise Conflict('Order is cancelled', code='order_cancelled')
    if payment.state == PayPalPayment.AUTHORIZED:
        return payment                        # double-click: already held
    if payment.state not in PayPalPayment.PAYABLE_STATES + (PayPalPayment.AUTHORIZING,):
        raise Conflict('Order is not awaiting payment (payment is %s)' % payment.state,
                       code='not_payable')
    saved_card = _get_saved_card(user, payment_method_id) if payment_method_id else None

    # A lapsed AUTHORIZING lease means an earlier attempt's outcome is unknown:
    # resume it under the same attempt number (same PayPal-Request-Id) so PayPal
    # replays that attempt instead of creating a second hold.
    resuming = payment.state == PayPalPayment.AUTHORIZING
    claimed = _claim(
        payment, [payment.state], state=PayPalPayment.AUTHORIZING,
        attempt=F('attempt') if resuming else F('attempt') + 1)
    if not claimed:
        if payment.state == PayPalPayment.AUTHORIZED:
            return payment
        raise Conflict('Order is not awaiting payment (payment is %s)' % payment.state,
                       code='not_payable')

    outcome = _authorize_at_paypal(order, payment, card, saved_card)
    auth = _accepted_authorization(order, payment, outcome)
    _record_authorization(order, payment, outcome, auth, saved_card)
    return payment


@sensitive_variables('card')
def _authorize_at_paypal(order, payment, card, saved_card):
    request_id = '%s-authorize-%d' % (payment.uuid, payment.attempt)
    invoice_id = '%s-%s-%d' % (order.number, payment.uuid.hex[:8], payment.attempt)
    try:
        outcome = gateway.create_authorized_order(
            amount=payment.amount, currency_code=payment.currency,
            reference_id=order.number, invoice_id=invoice_id, custom_id=order.number,
            description='Order %s' % order.number, request_id=request_id + '-create',
            card=card, vault_id=saved_card.partner_reference if saved_card else None)
        payment.paypal_order_id = outcome.paypal_order_id
        payment.invoice_id = invoice_id
        if outcome.status == 'APPROVED' and outcome.authorization is None:
            outcome = gateway.authorize_order(outcome.paypal_order_id,
                                              request_id=request_id + '-authorize')
    except PayPalRejected as exc:
        _fail_authorization(payment, exc.code, exc.message, exc.debug_id)
        raise PaymentDeclined('The payment was declined: %s' % exc.message,
                              paypalIssue=exc.code, paypalDebugId=exc.debug_id) from exc
    except PayPalConfigurationError as exc:
        _fail_authorization(payment, 'PAYPAL_CONFIGURATION', exc.message)
        raise _provider_failure(exc) from exc
    except PayPalOutcomeUnknown as exc:
        # Leave AUTHORIZING: a retry resumes this attempt with the same request id.
        _record_error(payment, 'OUTCOME_UNKNOWN', exc.message)
        _release(payment)
        raise _provider_failure(exc) from exc
    return outcome


def _accepted_authorization(order, payment, outcome):
    """The authorization in ``outcome`` if it is a hold for the order total."""
    if outcome.status == 'PAYER_ACTION_REQUIRED':
        message = ('The card issuer requires the shopper to approve this payment in a '
                   'browser (3-D Secure); this API does not support that.')
        _fail_authorization(payment, 'PAYER_ACTION_REQUIRED', message)
        raise PaymentDeclined(message, code='payer_action_required')
    auth = outcome.authorization
    if auth is None or auth.status not in ('CREATED', 'PENDING'):
        status = auth.status if auth else outcome.status
        message = 'PayPal did not authorize the payment (status %s)' % status
        _fail_authorization(payment, 'AUTHORIZATION_%s' % status, message)
        raise PaymentDeclined(message, paypalStatus=status)
    if auth.amount != payment.amount or auth.currency != payment.currency:
        # Never keep a hold for anything but the order total.
        logger.error('Order %s: PayPal held %s %s, expected %s %s; voiding',
                     order.number, auth.amount, auth.currency, payment.amount, payment.currency)
        try:
            gateway.void(auth.id, request_id='%s-void-%s' % (payment.uuid, auth.id))
        except PayPalError:
            logger.exception('Order %s: could not void mismatched authorization %s',
                             order.number, auth.id)
        _fail_authorization(payment, 'AMOUNT_MISMATCH', 'Held amount did not match order total')
        raise ProviderFailure('PayPal held an amount different from the order total; '
                              'the hold was released.', code='amount_mismatch')
    return auth


def _record_authorization(order, payment, outcome, auth, saved_card):
    with transaction.atomic():
        payment.state = PayPalPayment.AUTHORIZED
        payment.authorization_id = payment.original_authorization_id = auth.id
        payment.authorization_status = auth.status
        payment.authorized_at = auth.created_at or timezone.now()
        payment.authorization_expires_at = auth.expires_at
        payment.reauthorized_at = None
        payment.payment_method = saved_card
        payment.card_brand = outcome.card_brand or (saved_card.card_type if saved_card else '')
        payment.card_last_digits = (outcome.card_last_digits
                                    or (saved_card.number[-4:] if saved_card else ''))
        payment.clear_error()
        source = _source_for(payment)
        source.reference = payment.paypal_order_id
        source.label = ('%s ending %s' % (payment.card_brand, payment.card_last_digits)).strip()
        source.allocate(payment.amount, reference=auth.id, status=auth.status)
        _release(payment)
        order.set_status(STATUS_AUTHORISED)
    logger.info('Order %s authorized: %s %s (authorization %s)',
                order.number, payment.amount, payment.currency, auth.id)


def _fail_authorization(payment, code, message, debug_id=''):
    payment.state = PayPalPayment.FAILED
    _record_error(payment, code, message, debug_id)
    _release(payment)


# --------------------------------------------------------------------------
# Fulfil (capture)
# --------------------------------------------------------------------------

def fulfil(user, order_id):
    order = get_order(user, order_id, allow_staff=True)
    payment = payment_for(order)
    if payment.state in PayPalPayment.CAPTURED_STATES:
        return payment                        # already fulfilled
    if payment.state != PayPalPayment.AUTHORIZED:
        raise Conflict('Only an order with authorized payment can be fulfilled '
                       '(payment is %s)' % payment.state, code='not_authorized')
    if not _claim(payment, [PayPalPayment.AUTHORIZED]):
        if payment.state in PayPalPayment.CAPTURED_STATES:
            return payment
        raise Conflict('Order cannot be fulfilled (payment is %s)' % payment.state,
                       code='not_authorized')
    try:
        return _fulfil_claimed(order, payment)
    finally:
        if payment.lock_until is not None:
            _release(payment)


def _fulfil_claimed(order, payment):
    auth = _refresh_hold(order, payment)
    renew_first = auth.status == 'CREATED' and _is_stale(payment, timezone.now())
    captured = _capture_renewing(payment, renew_first)
    if captured.status in ('DECLINED', 'FAILED'):
        _record_error(payment, 'CAPTURE_%s' % captured.status,
                      'PayPal reported the capture as %s' % captured.status)
        payment.save()
        raise Unprocessable(
            'PayPal %s the capture. Ask the shopper to pay again or cancel the order.'
            % captured.status.lower(), code='capture_%s' % captured.status.lower())
    _record_capture(order, payment, captured)
    return payment


def _refresh_hold(order, payment):
    """Re-read the hold from PayPal; raise with operator guidance if it is unusable."""
    try:
        auth = gateway.get_authorization(payment.authorization_id)
    except PayPalRejected as exc:
        raise _operator_error(payment, exc, 'look up the payment hold') from exc
    except PayPalError as exc:
        raise _provider_failure(exc) from exc
    payment.authorization_status = auth.status
    if auth.expires_at:
        payment.authorization_expires_at = auth.expires_at

    if auth.status in ('VOIDED', 'DENIED'):
        _hold_lost(order, payment, auth.status)
        raise Conflict(
            'PayPal reports the payment hold as %s, so there is nothing to capture. The '
            'order is back to awaiting payment: ask the shopper to pay again '
            '(POST /api/orders/%s/pay), or cancel the order.' % (auth.status, order.pk),
            code='authorization_%s' % auth.status.lower())
    if auth.status == 'PENDING':
        payment.save()
        raise Conflict('PayPal is still reviewing this payment hold; fulfil again later.',
                       code='authorization_pending')

    now = timezone.now()
    if auth.status not in ('CAPTURED', 'PARTIALLY_CAPTURED'):
        if auth.expires_at and now >= auth.expires_at:
            _hold_lost(order, payment, 'EXPIRED')
            raise Conflict(
                'The payment hold expired on %s and can no longer be renewed. The order '
                'is back to awaiting payment: ask the shopper to pay again '
                '(POST /api/orders/%s/pay), then fulfil.' % (
                    auth.expires_at.isoformat(), order.pk),
                code='authorization_expired')
    return auth


def _capture_renewing(payment, renew_first):
    """Capture the hold, renewing a stale one first. A refused capture of a hold
    that was never renewed gets one renewal and one more capture attempt."""
    if renew_first:
        _try_reauthorize(payment)
    try:
        return _capture(payment)
    except PayPalRejected as exc:
        if renew_first or payment.reauthorized_at or not _try_reauthorize(payment):
            raise _operator_error(payment, exc, 'capture the payment') from exc
    except PayPalError as exc:
        raise _provider_failure(exc) from exc
    try:
        return _capture(payment)
    except PayPalRejected as exc:
        raise _operator_error(payment, exc, 'capture the renewed payment hold') from exc
    except PayPalError as exc:
        raise _provider_failure(exc) from exc


def _record_capture(order, payment, captured):
    with transaction.atomic():
        amount = captured.amount if captured.amount is not None else payment.amount
        if amount != payment.amount:
            logger.error('Order %s: captured %s but order total is %s',
                         order.number, amount, payment.amount)
            _record_error(payment, 'CAPTURE_AMOUNT_MISMATCH',
                          'Captured %s, order total %s' % (amount, payment.amount))
        else:
            payment.clear_error()
        payment.state = PayPalPayment.CAPTURED
        payment.capture_id = captured.id
        payment.capture_status = captured.status
        payment.captured_at = captured.created_at or timezone.now()
        payment.captured_amount = amount
        payment.paypal_fee = captured.paypal_fee
        payment.net_amount = captured.net_amount
        payment.authorization_status = 'CAPTURED'
        _source_for(payment).debit(amount, reference=captured.id, status=captured.status)
        _release(payment)
        order.set_status(STATUS_COMPLETE)
    logger.info('Order %s fulfilled: captured %s (capture %s, fee %s, net %s)',
                order.number, amount, captured.id, captured.paypal_fee, captured.net_amount)


def _is_stale(payment, now):
    return payment.authorized_at is not None and now - payment.authorized_at >= HONOR_PERIOD


def _capture(payment):
    return gateway.capture(
        payment.authorization_id, amount=payment.amount, currency_code=payment.currency,
        request_id='%s-capture-%s' % (payment.uuid, payment.authorization_id))


def _try_reauthorize(payment):
    """Renew a stale hold. Returns False (and records why) if PayPal refuses; the
    original hold may still be capturable until it expires."""
    original = payment.original_authorization_id or payment.authorization_id
    try:
        renewed = gateway.reauthorize(
            payment.authorization_id, amount=payment.amount, currency_code=payment.currency,
            request_id='%s-reauthorize-%s' % (payment.uuid, original))
    except PayPalRejected as exc:
        logger.warning('Order %s: reauthorization refused (%s)', payment.order.number, exc.code)
        _record_error(payment, exc.code, 'Renewing the payment hold was refused: %s' % exc.message,
                      exc.debug_id)
        payment.save()
        return False
    except PayPalError as exc:
        raise _provider_failure(exc) from exc
    with transaction.atomic():
        old = payment.authorization_id
        payment.authorization_id = renewed.id
        payment.authorization_status = renewed.status
        payment.authorized_at = renewed.created_at or timezone.now()
        if renewed.expires_at:
            payment.authorization_expires_at = renewed.expires_at
        payment.reauthorized_at = timezone.now()
        payment.save()
        if payment.source is not None:
            Transaction._default_manager.create(
                source=payment.source, txn_type='Reauthorise', amount=payment.amount,
                reference=renewed.id, status=renewed.status)
    logger.info('Order %s: authorization %s renewed as %s', payment.order.number, old, renewed.id)
    return True


def _hold_lost(order, payment, status):
    with transaction.atomic():
        payment.state = PayPalPayment.EXPIRED
        payment.authorization_status = status
        _record_error(payment, 'AUTHORIZATION_%s' % status,
                      'The payment hold is %s; the shopper must pay again.' % status.lower())
        _release_allocation(payment, 'Expire', payment.authorization_id, status)
        _release(payment)
        order.set_status(STATUS_AWAITING_PAYMENT)


def _operator_error(payment, exc, action):
    _record_error(payment, exc.code, exc.message, exc.debug_id)
    payment.save()
    return Unprocessable(
        'PayPal refused to %s: %s (%s). If the hold can no longer be used, ask the shopper '
        'to pay again or cancel the order.' % (action, exc.message, exc.code),
        code='paypal_refused', paypalIssue=exc.code, paypalDebugId=exc.debug_id)


# --------------------------------------------------------------------------
# Cancel (void)
# --------------------------------------------------------------------------

def cancel(user, order_id):
    order = get_order(user, order_id, allow_staff=True)
    payment = payment_for(order)
    if order.status == STATUS_CANCELLED:
        return payment
    if payment.state in PayPalPayment.CAPTURED_STATES:
        raise Conflict('The order is already fulfilled and paid; refund it instead.',
                       code='already_captured')
    if payment.state in PayPalPayment.PAYABLE_STATES:
        # No hold exists, so no money to release.
        if not _claim(payment, PayPalPayment.PAYABLE_STATES):
            raise Conflict('Order cannot be cancelled (payment is %s)' % payment.state,
                           code='not_cancellable')
        with transaction.atomic():
            _release(payment)
            order.set_status(STATUS_CANCELLED)
        return payment
    if payment.state == PayPalPayment.AUTHORIZING:
        raise Conflict('A payment attempt for this order has an unknown outcome; repeat '
                       'the pay request to settle it, then cancel.', code='payment_in_progress')
    if not _claim(payment, [PayPalPayment.AUTHORIZED]):
        raise Conflict('Order cannot be cancelled (payment is %s)' % payment.state,
                       code='not_cancellable')
    try:
        status = _void(payment)
    except ServiceError:
        _release(payment)
        raise

    with transaction.atomic():
        payment.state = PayPalPayment.VOIDED
        payment.authorization_status = status
        payment.voided_at = timezone.now()
        payment.clear_error()
        _release_allocation(payment, 'Void', payment.authorization_id, status)
        _release(payment)
        order.set_status(STATUS_CANCELLED)
    logger.info('Order %s cancelled; authorization %s voided', order.number,
                payment.authorization_id)
    return payment


def _void(payment):
    request_id = '%s-void-%s' % (payment.uuid, payment.authorization_id)
    try:
        return gateway.void(payment.authorization_id, request_id=request_id).status
    except PayPalRejected as exc:
        # Already voided (e.g. an earlier attempt whose answer was lost) is success.
        try:
            current = gateway.get_authorization(payment.authorization_id)
        except PayPalError:
            current = None
        if current is not None and current.status == 'VOIDED':
            return current.status
        raise _operator_error(payment, exc, 'release the payment hold') from exc
    except PayPalError as exc:
        raise _provider_failure(exc) from exc


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------

def parse_amount(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidRequest('amount must be a decimal string such as "5.00"', code='invalid_amount')
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        raise InvalidRequest('amount must be a decimal string such as "5.00"',
                             code='invalid_amount') from None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT):
        raise InvalidRequest('amount must be positive with at most 2 decimal places',
                             code='invalid_amount')
    return amount


def refund(user, order_id, *, idempotency_key, amount=None, note=''):
    if not idempotency_key or len(idempotency_key) > 255:
        raise InvalidRequest('An Idempotency-Key header (1-255 characters) is required',
                             code='idempotency_key_required')
    order = get_order(user, order_id, allow_staff=True)
    payment = payment_for(order)

    existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if amount is not None and amount != existing.amount:
            raise Unprocessable('This Idempotency-Key was already used for a refund of %s'
                                % existing.amount, code='idempotency_key_reused')
        if existing.status != PayPalRefund.SUBMITTING:
            return existing, False
    elif payment.state not in PayPalPayment.CAPTURED_STATES:
        raise Conflict('Only a fulfilled order can be refunded (payment is %s); cancel it '
                       'instead.' % payment.state, code='not_captured')

    if not _claim(payment, PayPalPayment.CAPTURED_STATES):
        raise Conflict('Order cannot be refunded (payment is %s)' % payment.state,
                       code='not_captured')
    try:
        if existing is None:
            existing = _new_refund(payment, user, idempotency_key, amount, note)
        return _submit_refund(payment, existing), True
    finally:
        if payment.lock_until is not None:
            _release(payment)


def _new_refund(payment, user, idempotency_key, amount, note):
    """Record a refund request, never beyond what is still refundable. The
    caller holds the payment lease, so concurrent refunds are serialised."""
    refundable = payment.refundable_amount()
    requested = amount if amount is not None else refundable
    if requested <= 0:
        raise Conflict('Nothing left to refund on this order.', code='fully_refunded')
    if requested > refundable:
        raise Unprocessable(
            'Refund of %s exceeds the refundable amount %s' % (requested, refundable),
            code='refund_exceeds_captured', refundable=str(refundable))
    try:
        return PayPalRefund.objects.create(
            payment=payment, idempotency_key=idempotency_key, amount=requested,
            currency=payment.currency, note=note[:255], requested_by=user)
    except IntegrityError:
        raise Conflict('A refund with this Idempotency-Key is being processed; retry.',
                       code='payment_in_progress') from None


def _submit_refund(payment, refund_row):
    key_hash = hashlib.sha256(refund_row.idempotency_key.encode()).hexdigest()[:40]
    try:
        result = gateway.refund(
            payment.capture_id, amount=refund_row.amount, currency_code=refund_row.currency,
            request_id='%s-refund-%s' % (payment.uuid, key_hash), note=refund_row.note)
    except PayPalRejected as exc:
        refund_row.status = PayPalRefund.REJECTED
        refund_row.error_code = exc.code[:128]
        refund_row.error_message = exc.message
        refund_row.save()
        raise Unprocessable('PayPal refused the refund: %s (%s)' % (exc.message, exc.code),
                            code='refund_refused', paypalIssue=exc.code,
                            paypalDebugId=exc.debug_id, refundId=refund_row.pk) from exc
    except PayPalError as exc:
        # Stays SUBMITTING; the same Idempotency-Key resubmits the same PayPal request.
        raise _provider_failure(exc) from exc

    with transaction.atomic():
        refund_row.paypal_refund_id = result.id
        refund_row.status = (result.status if result.status in dict(PayPalRefund.STATUS_CHOICES)
                             else PayPalRefund.PENDING)
        refund_row.save()
        if refund_row.status not in PayPalRefund.RELEASED_STATUSES:
            _source_for(payment).refund(refund_row.amount, reference=result.id,
                                        status=result.status)
        refunded = payment.refunded_total()
        if refunded >= payment.captured_amount:
            payment.state = payment.capture_status = PayPalPayment.REFUNDED
        elif refunded > 0:
            payment.state = payment.capture_status = PayPalPayment.PARTIALLY_REFUNDED
        _release(payment)
    logger.info('Order %s: refund %s of %s (%s)', payment.order.number, result.id,
                refund_row.amount, result.status)
    return refund_row


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------

def _expiry_date(expiry):
    year, month = (int(part) for part in expiry.split('-'))
    return date(year, month, calendar.monthrange(year, month)[1])


@sensitive_variables('card')
def save_card(user, card, *, idempotency_key=None):
    customer = PayPalCustomer.objects.filter(user=user).first()
    if idempotency_key:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]
        request_id = 'vault-%s-%s' % (user.pk, digest)
    else:
        request_id = None
    try:
        vaulted = gateway.vault_card(
            card, request_id=request_id or 'vault-%s' % uuid.uuid4(),
            customer_id=customer.paypal_customer_id if customer else None)
    except PayPalRejected as exc:
        raise Unprocessable('PayPal could not save the card: %s' % exc.message,
                            code='card_rejected', paypalIssue=exc.code,
                            paypalDebugId=exc.debug_id) from exc
    except PayPalError as exc:
        raise _provider_failure(exc) from exc

    with transaction.atomic():
        if customer is None and vaulted.customer_id:
            PayPalCustomer.objects.get_or_create(
                user=user, defaults={'paypal_customer_id': vaulted.customer_id})
        existing = Bankcard._default_manager.filter(
            user=user, partner_reference=vaulted.token_id).first()
        if existing is not None:
            return existing
        last4 = vaulted.last_digits or card.number[-4:]
        bankcard = Bankcard(
            user=user, number='XXXX-XXXX-XXXX-%s' % last4,
            name=(vaulted.name or card.name)[:255],
            expiry_date=_expiry_date(vaulted.expiry or card.expiry),
            partner_reference=vaulted.token_id)
        bankcard.card_type = vaulted.brand or 'CARD'
        bankcard.save()
    logger.info('User %s saved card %s (vault token %s)', user.pk, bankcard.pk, vaulted.token_id)
    return bankcard


def list_cards(user):
    return (Bankcard._default_manager.filter(user=user).exclude(partner_reference='')
            .order_by('-pk'))


def delete_card(user, payment_method_id):
    bankcard = _get_saved_card(user, payment_method_id)
    try:
        gateway.delete_vaulted_card(bankcard.partner_reference)
    except PayPalRejected as exc:
        if exc.status_code != 404:            # 404: already gone at PayPal
            raise Unprocessable('PayPal could not delete the card: %s' % exc.message,
                                code='card_delete_refused', paypalIssue=exc.code) from exc
    except PayPalError as exc:
        raise _provider_failure(exc) from exc
    bankcard.delete()
    logger.info('User %s deleted saved card %s', user.pk, payment_method_id)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

@dataclass
class _AppRecord:
    kind: str
    paypal_id: str
    order: object
    amount: Decimal
    currency: str
    at: datetime
    invoice_id: str


def _app_records(start, end):
    records = []
    captured = (PayPalPayment.objects.select_related('order')
                .filter(captured_at__gte=start, captured_at__lt=end).exclude(capture_id=''))
    for payment in captured:
        records.append(_AppRecord('capture', payment.capture_id, payment.order,
                                  payment.captured_amount, payment.currency,
                                  payment.captured_at, payment.invoice_id))
    refunds = (PayPalRefund.objects.select_related('payment__order')
               .filter(date_created__gte=start, date_created__lt=end)
               .exclude(paypal_refund_id='')
               .exclude(status__in=PayPalRefund.RELEASED_STATUSES))
    for refund_row in refunds:
        records.append(_AppRecord('refund', refund_row.paypal_refund_id,
                                  refund_row.payment.order, refund_row.amount,
                                  refund_row.currency, refund_row.date_created,
                                  refund_row.payment.invoice_id))
    return records


def _fetch_paypal_transactions(start, end):
    seen = {}
    last_refreshed = None
    window_start = start
    pages = 0
    while window_start < end:
        window_end = min(window_start + REPORT_WINDOW, end)
        page = 1
        while True:
            pages += 1
            if pages > REPORT_MAX_PAGES:
                raise Unprocessable('The date range holds too many transactions; narrow it.',
                                    code='range_too_large')
            result = gateway.search_transactions(start=window_start, end=window_end, page=page)
            for txn in result.transactions:
                seen.setdefault(txn.transaction_id or id(txn), txn)
            if result.last_refreshed_at and (last_refreshed is None
                                             or result.last_refreshed_at < last_refreshed):
                last_refreshed = result.last_refreshed_at
            if page >= result.total_pages:
                break
            page += 1
        window_start = window_end
    return list(seen.values()), last_refreshed


def _money_str(value):
    return None if value is None else str(value)


def reconcile(start, end):
    if start.tzinfo is None or end.tzinfo is None:
        raise InvalidRequest('from and to must include a UTC offset', code='invalid_range')
    if end <= start:
        raise InvalidRequest('"to" must be after "from"', code='invalid_range')
    if end - start > REPORT_MAX_RANGE:
        raise InvalidRequest('The range may span at most 3 years', code='invalid_range')
    try:
        transactions, last_refreshed = _fetch_paypal_transactions(start, end)
    except PayPalRejected as exc:
        raise Unprocessable('PayPal refused the transaction search: %s' % exc.message,
                            code='paypal_refused', paypalIssue=exc.code) from exc
    except PayPalError as exc:
        raise _provider_failure(exc) from exc

    app_records = _app_records(start, end)
    matched, paypal_only, seen_ids = _match(transactions, app_records, start)
    app_only = [{
        'kind': record.kind, 'paypalId': record.paypal_id, 'orderId': record.order.pk,
        'orderNumber': record.order.number, 'amount': str(record.amount),
        'currency': record.currency, 'at': record.at.isoformat(),
        # PayPal's report lags live activity; newer records are not missing yet.
        'notYetReported': bool(last_refreshed is None or record.at > last_refreshed),
    } for record in app_records if record.paypal_id not in seen_ids]

    return {
        'from': start.isoformat(),
        'to': end.isoformat(),
        'paypalLastRefreshedAt': last_refreshed.isoformat() if last_refreshed else None,
        'summary': {
            'paypalTransactions': len(transactions),
            'appRecords': len(app_records),
            'matched': len(matched),
            'amountMismatches': sum(1 for m in matched if not m['amountMatches']),
            'paypalOnly': len(paypal_only),
            'appOnly': len(app_only),
            'appOnlyNotYetReported': sum(1 for a in app_only if a['notYetReported']),
        },
        'matched': matched,
        'paypalOnly': paypal_only,
        'appOnly': app_only,
    }


def _payments_by_reference(transactions):
    """Payments whose PayPal invoice id or order number a transaction carries."""
    refs = {t.invoice_id for t in transactions if t.invoice_id}
    refs |= {t.custom_field for t in transactions if t.custom_field}
    by_ref = {}
    if refs:
        for payment in (PayPalPayment.objects.select_related('order')
                        .filter(Q(invoice_id__in=refs) | Q(order__number__in=refs))):
            if payment.invoice_id:
                by_ref[payment.invoice_id] = payment
            by_ref.setdefault(payment.order.number, payment)
    return by_ref


def _match(transactions, app_records, start):
    """Line PayPal's transactions up against the app's captures and refunds."""
    by_paypal_id = {r.paypal_id: r for r in app_records}
    payments_by_ref = _payments_by_reference(transactions)
    matched, paypal_only, seen_ids = [], [], set()
    for txn in sorted(transactions, key=lambda t: (t.initiated_at or start)):
        record = by_paypal_id.get(txn.transaction_id)
        entry = {
            'transactionId': txn.transaction_id,
            'eventCode': txn.event_code,
            'status': txn.status,
            'initiatedAt': txn.initiated_at.isoformat() if txn.initiated_at else None,
            'amount': _money_str(txn.amount),
            'fee': _money_str(txn.fee),
            'currency': txn.currency,
            'invoiceId': txn.invoice_id,
        }
        if record is not None:
            seen_ids.add(record.paypal_id)
            app_amount = record.amount if record.kind == 'capture' else -record.amount
            entry.update({
                'orderId': record.order.pk, 'orderNumber': record.order.number,
                'kind': record.kind, 'appAmount': str(app_amount),
                'amountMatches': txn.amount == app_amount and txn.currency == record.currency,
            })
            matched.append(entry)
            continue
        payment = (payments_by_ref.get(txn.invoice_id) or payments_by_ref.get(txn.custom_field))
        entry.update({'orderId': payment.order.pk if payment else None,
                      'orderNumber': payment.order.number if payment else None})
        paypal_only.append(entry)
    return matched, paypal_only, seen_ids
