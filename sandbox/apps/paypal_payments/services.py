"""
Payment flows: placing an order, holding the money, capturing it at
fulfilment, releasing or refunding it, saved cards and reconciliation.

Every PayPal write follows one order — claim, call, record:

* the claim is a write of its own, committed before PayPal is called, and
  refused by the database (a conditional UPDATE that matches no row, or a
  unique constraint) when another request already holds it;
* the PayPal call carries a PayPal-Request-Id stored with the claim, so a
  claim that is re-taken after a crash or an unreadable answer replays the
  original request instead of acting twice;
* the result is recorded against the claim, or the claim is released when
  PayPal definitely refused the call.

Views call these functions outside a request-wide transaction so that each
claim is committed before PayPal is contacted.
"""
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import gateway
from .gateway import ProviderError
from .models import PayPalCustomer, PayPalPayment, PayPalRefund, SavedCard
from .money import is_representable, parse_amount

logger = logging.getLogger(__name__)

Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
Basket = get_model('basket', 'Basket')
SourceType = get_model('payment', 'SourceType')
Source = get_model('payment', 'Source')
Transaction = get_model('payment', 'Transaction')
OrderCreator = get_class('order.utils', 'OrderCreator')
EventHandler = get_class('order.processing', 'EventHandler')
Selector = get_class('partner.strategy', 'Selector')
Repository = get_class('shipping.repository', 'Repository')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Applicator = get_class('offer.applicator', 'Applicator')

# Oscar order statuses (see OSCAR_ORDER_STATUS_PIPELINE in settings).
STATUS_AWAITING_PAYMENT = 'Awaiting payment'
STATUS_AUTHORIZED = 'Payment authorized'
STATUS_COMPLETE = 'Complete'
STATUS_CANCELLED = 'Cancelled'

SOURCE_TYPE_NAME = 'PayPal'

# How long a claim shuts out a second request. It must outlast the slowest
# chain of PayPal calls one operation makes (reads are retried by the SDK).
CLAIM_TTL = timedelta(minutes=5)

MAX_ORDER_LINES = 50
MAX_LINE_QUANTITY = 99


class ServiceError(Exception):
    def __init__(self, status_code, code, message, **details):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class ClaimRejected(Exception):
    """Another request holds (or has finished) this operation."""


class RefundRejected(Exception):
    """The refund would take the capture past what was captured."""


class AuthorizationNotRenewable(Exception):
    pass


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

def _claim(queryset, *, fresh_from, claiming, fresh_updates=None):
    """Take the claim on the single row in ``queryset``.

    Re-takes an expired claim in ``claiming`` state unchanged (so its stored
    PayPal-Request-Id is replayed) and returns True; otherwise moves the row
    from one of ``fresh_from`` into ``claiming`` and returns False. Both are
    single conditional UPDATEs, so the database decides which of two racing
    requests wins. Raises ClaimRejected when neither matches.
    """
    now = timezone.now()
    resumed = queryset.filter(state=claiming, claim_expires_at__lte=now).update(
        claim_expires_at=now + CLAIM_TTL)
    if resumed:
        return True
    taken = queryset.filter(state__in=fresh_from).update(
        state=claiming, claim_expires_at=now + CLAIM_TTL, **(fresh_updates or {}))
    if taken:
        return False
    raise ClaimRejected()


def _release_after_error(model, pk, claiming, exc, *, back_to, **updates):
    """Record a failed provider call against its claim. A definite refusal
    returns the row to ``back_to``; an unknown outcome keeps the claim (and its
    PayPal-Request-Id) but lets the next request re-take it at once."""
    fields = dict(last_error_code=exc.issue or exc.code, last_error_message=exc.message[:512],
                  **updates)
    if hasattr(model, 'last_debug_id'):
        fields['last_debug_id'] = exc.debug_id or ''
    if exc.outcome_unknown:
        fields.update(claim_expires_at=_reclaimable_now(), outcome_unknown=True)
    else:
        fields.update(state=back_to, claim_expires_at=None, outcome_unknown=False)
    model.objects.filter(pk=pk, state=claiming).update(**fields)


def _reclaimable_now():
    # An expiry already in the past, so the very next request may re-take it.
    return timezone.now() - timedelta(seconds=1)


def _new_request_id():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def _payment_for_owner(user, order_id):
    payment = (PayPalPayment.objects.select_related('order')
               .filter(order__number=order_id, order__user=user).first())
    if payment is None:
        raise ServiceError(404, 'order_not_found', 'No such order.')
    return payment


def _payment_for_operator(order_id):
    payment = PayPalPayment.objects.select_related('order').filter(order__number=order_id).first()
    if payment is None:
        raise ServiceError(404, 'order_not_found', 'No such order.')
    return payment


def _source_for(order, payment):
    source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
    source, _ = Source.objects.get_or_create(
        order=order, source_type=source_type,
        defaults={'currency': payment.currency, 'reference': payment.authorization_id,
                  'label': _card_label(payment.card_brand, payment.card_last_digits)})
    return source


def _card_label(brand, last_digits):
    return ('%s ending %s' % (brand or 'Card', last_digits))[:128] if last_digits else ''


def _set_order_status(order, status, note=None):
    if order.status != status:
        order.set_status(status)
    if note:
        EventHandler().create_note(order, note)


# ---------------------------------------------------------------------------
# Placing an order
# ---------------------------------------------------------------------------

def parse_order_items(data):
    items = data.get('items') if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ServiceError(422, 'invalid_items', '"items" must be a non-empty list.')
    if len(items) > MAX_ORDER_LINES:
        raise ServiceError(422, 'invalid_items', 'An order may have at most %d lines.' % MAX_ORDER_LINES)
    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise ServiceError(422, 'invalid_items', 'Each item must be an object.')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
            raise ServiceError(422, 'invalid_items', '"productId" must be a catalogue item id.')
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_LINE_QUANTITY:
            raise ServiceError(422, 'invalid_items',
                               '"quantity" must be an integer from 1 to %d.' % MAX_LINE_QUANTITY)
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    return quantities


@transaction.atomic
def place_order(request, quantities):
    """Create an Oscar order (through Oscar's own basket, pricing, offers and
    OrderCreator) that awaits payment."""
    user = request.user
    currency = settings.PAYPAL_CURRENCY
    products = Product.objects.in_bulk(list(quantities))
    missing = sorted(set(quantities) - set(products))
    if missing:
        raise ServiceError(422, 'unknown_product', 'Unknown catalogue item(s).', productIds=missing)

    basket = Basket(owner=user)
    basket.strategy = Selector().strategy(request=request, user=user)
    basket.save()
    for product_id, quantity in quantities.items():
        product = products[product_id]
        info = basket.strategy.fetch_for_product(product)
        permitted, reason = (info.availability.is_purchase_permitted(quantity)
                             if info.availability else (False, 'unavailable'))
        if product.is_parent or not permitted or info.price is None or not info.price.exists:
            raise ServiceError(422, 'not_purchasable',
                               'Catalogue item %d cannot be bought: %s' % (
                                   product_id, reason or 'not available'),
                               productId=product_id)
        basket.add_product(product, quantity)
    Applicator().apply(basket, user, request)

    shipping_method = Repository().get_default_shipping_method(basket=basket, user=user, request=request)
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
    if total.incl_tax is None:
        raise ServiceError(422, 'total_unknown', 'The order total could not be determined.')
    if total.incl_tax <= 0 or not is_representable(total.incl_tax, currency):
        raise ServiceError(422, 'total_not_payable',
                           'The order total %s cannot be charged in %s.' % (total.incl_tax, currency))

    order = OrderCreator().place_order(
        basket=basket, total=total, shipping_method=shipping_method,
        shipping_charge=shipping_charge, user=user, status=STATUS_AWAITING_PAYMENT,
        request=request)
    basket.submit()
    # Catalogue prices are charged in the configured PayPal currency.
    Order.objects.filter(pk=order.pk).update(currency=currency)
    order.currency = currency
    payment = PayPalPayment.objects.create(order=order, currency=currency, amount=order.total_incl_tax)
    logger.info('Order %s placed, awaiting payment of %s %s', order.number, payment.amount, currency)
    return payment


# ---------------------------------------------------------------------------
# Authorize (hold)
# ---------------------------------------------------------------------------

def pay_order(user, order_id, *, card=None, payment_method_id=None):
    payment = _payment_for_owner(user, order_id)
    saved_card = None
    if payment_method_id is not None:
        saved_card = SavedCard.objects.filter(
            user=user, public_id=payment_method_id, state=SavedCard.ACTIVE).first()
        if saved_card is None:
            raise ServiceError(404, 'payment_method_not_found', 'No such saved card.')

    if payment.state in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING, PayPalPayment.CAPTURED):
        raise ServiceError(409, 'already_paid', 'This order has already been paid.')
    if payment.state in (PayPalPayment.VOIDING, PayPalPayment.VOIDED, PayPalPayment.CANCELLED):
        raise ServiceError(409, 'order_cancelled', 'This order has been cancelled.')

    try:
        _claim(PayPalPayment.objects.filter(pk=payment.pk),
               fresh_from=(PayPalPayment.AWAITING_PAYMENT, PayPalPayment.FAILED),
               claiming=PayPalPayment.AUTHORIZING,
               fresh_updates={'authorize_request_id': _new_request_id(), 'saved_card': saved_card,
                              'outcome_unknown': False, 'last_error_code': '',
                              'last_error_message': ''})
    except ClaimRejected:
        payment.refresh_from_db()
        if payment.state == PayPalPayment.AUTHORIZED:
            raise ServiceError(409, 'already_paid', 'This order has already been paid.')
        raise ServiceError(409, 'payment_in_progress',
                           'A payment for this order is already being processed.')
    payment.refresh_from_db()

    try:
        result = gateway.authorize_order_total(
            reference=payment.order.number,
            description='Order %s' % payment.order.number,
            amount=payment.amount, currency=payment.currency,
            request_id=payment.authorize_request_id,
            card=card if saved_card is None else None,
            vault_id=saved_card.vault_token_id if saved_card is not None else None)
    except ProviderError as exc:
        _release_after_error(PayPalPayment, payment.pk, PayPalPayment.AUTHORIZING, exc,
                             back_to=PayPalPayment.FAILED)
        raise

    return _record_authorization(payment, result)


def _record_authorization(payment, result):
    fields = dict(
        paypal_order_id=result.paypal_order_id, authorization_id=result.authorization_id,
        authorization_status=result.authorization_status,
        authorized_at=result.created_at or timezone.now(),
        authorization_expires_at=result.expires_at,
        card_brand=result.card_brand[:32], card_last_digits=result.card_last_digits[:4],
        claim_expires_at=None, outcome_unknown=False)

    held_exactly = result.amount == payment.amount and result.currency == payment.currency
    if result.authorization_status in ('CREATED', 'PENDING') and not held_exactly:
        # Never keep a hold that differs from the order total.
        logger.error('PayPal held %s %s for order %s, expected %s %s; voiding', result.amount,
                     result.currency, payment.order.number, payment.amount, payment.currency)
        try:
            gateway.void_authorization(result.authorization_id,
                                       request_id=gateway.derive_request_id(
                                           payment.authorize_request_id, 'void-mismatch'))
        except ProviderError:
            logger.exception('Could not void mismatched authorization for order %s',
                             payment.order.number)
        PayPalPayment.objects.filter(pk=payment.pk, state=PayPalPayment.AUTHORIZING).update(
            state=PayPalPayment.FAILED, last_error_code='amount_mismatch',
            last_error_message='PayPal held a different amount than the order total.', **fields)
        raise ServiceError(502, 'amount_mismatch',
                           'PayPal held a different amount than the order total; the hold was released.')

    if result.authorization_status not in ('CREATED', 'PENDING'):
        reason = result.decline_reason or result.authorization_status
        PayPalPayment.objects.filter(pk=payment.pk, state=PayPalPayment.AUTHORIZING).update(
            state=PayPalPayment.FAILED, last_error_code='card_declined',
            last_error_message=('Card declined (%s).' % reason)[:512], **fields)
        raise ServiceError(402, 'card_declined', 'The card was declined (%s).' % reason)

    with transaction.atomic():
        updated = PayPalPayment.objects.filter(
            pk=payment.pk, state=PayPalPayment.AUTHORIZING,
            authorize_request_id=payment.authorize_request_id,
        ).update(state=PayPalPayment.AUTHORIZED, last_error_code='', last_error_message='', **fields)
        payment.refresh_from_db()
        if updated:
            order = payment.order
            source = _source_for(order, payment)
            source.reference = payment.authorization_id
            source.label = _card_label(payment.card_brand, payment.card_last_digits)
            source.allocate(payment.amount, reference=payment.authorization_id,
                            status=payment.authorization_status)
            _set_order_status(order, STATUS_AUTHORIZED,
                              'PayPal authorization %s holds %s %s.' % (
                                  payment.authorization_id, payment.amount, payment.currency))
    logger.info('Order %s authorized (%s)', payment.order.number, payment.authorization_id)
    return payment


# ---------------------------------------------------------------------------
# Fulfil (capture)
# ---------------------------------------------------------------------------

def fulfil_order(order_id):
    payment = _payment_for_operator(order_id)
    if payment.state == PayPalPayment.CAPTURED:
        return payment      # already fulfilled: repeat requests see the same capture
    if payment.state not in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING):
        raise ServiceError(409, 'not_authorized',
                           'Only an order with an authorized payment can be fulfilled '
                           '(payment is %s).' % payment.state)
    try:
        _claim(PayPalPayment.objects.filter(pk=payment.pk),
               fresh_from=(PayPalPayment.AUTHORIZED,), claiming=PayPalPayment.CAPTURING,
               fresh_updates={'capture_request_id': _new_request_id()})
    except ClaimRejected:
        payment.refresh_from_db()
        if payment.state == PayPalPayment.CAPTURED:
            return payment
        raise ServiceError(409, 'fulfilment_in_progress', 'This order is already being fulfilled.')
    payment.refresh_from_db()

    try:
        authorization_id = _ensure_live_authorization(payment)
        result = gateway.capture_authorization(
            authorization_id, amount=payment.amount, currency=payment.currency,
            request_id=payment.capture_request_id)
    except AuthorizationNotRenewable as exc:
        message = ('The payment hold on this order can no longer be renewed: %s. Cancel the order '
                   '(POST /api/orders/%s/cancel) to release it and ask the shopper to place and '
                   'pay for a new order.' % (exc, payment.order.number))
        PayPalPayment.objects.filter(pk=payment.pk, state=PayPalPayment.CAPTURING).update(
            state=PayPalPayment.AUTHORIZED, claim_expires_at=None,
            last_error_code='authorization_not_renewable', last_error_message=message[:512])
        raise ServiceError(409, 'authorization_not_renewable', message)
    except ProviderError as exc:
        _release_after_error(PayPalPayment, payment.pk, PayPalPayment.CAPTURING, exc,
                             back_to=PayPalPayment.AUTHORIZED)
        raise

    if result.status in ('DECLINED', 'FAILED'):
        PayPalPayment.objects.filter(pk=payment.pk, state=PayPalPayment.CAPTURING).update(
            state=PayPalPayment.AUTHORIZED, claim_expires_at=None, capture_status=result.status,
            last_error_code='capture_declined',
            last_error_message='PayPal declined the capture (%s).' % result.status)
        raise ServiceError(402, 'capture_declined', 'PayPal declined the capture (%s).' % result.status)

    _record_capture(payment, result)
    logger.info('Order %s captured (%s)', payment.order.number, payment.capture_id)
    return payment


def _record_capture(payment, result):
    captured = result.amount if result.amount is not None else payment.amount
    with transaction.atomic():
        updated = PayPalPayment.objects.filter(pk=payment.pk, state=PayPalPayment.CAPTURING).update(
            state=PayPalPayment.CAPTURED, claim_expires_at=None, outcome_unknown=False,
            authorization_status='CAPTURED', capture_id=result.capture_id, capture_status=result.status,
            captured_amount=captured, paypal_fee=result.paypal_fee, net_amount=result.net_amount,
            captured_at=result.created_at or timezone.now(),
            last_error_code='', last_error_message='')
        payment.refresh_from_db()
        if updated:
            order = payment.order
            _source_for(order, payment).debit(captured, reference=result.capture_id,
                                              status=result.status)
            handler = EventHandler()
            handler.consume_stock_allocations(order)
            _set_order_status(order, STATUS_COMPLETE, 'Fulfilled; PayPal capture %s took %s %s '
                              '(fee %s, net %s).' % (result.capture_id, captured, payment.currency,
                                                     result.paypal_fee, result.net_amount))


def _ensure_live_authorization(payment):
    """Return an authorization id that can be captured now, renewing a hold
    whose honor period has passed. Raises AuthorizationNotRenewable when the
    hold is gone for good."""
    now = timezone.now()
    state = gateway.get_authorization(payment.authorization_id)
    PayPalPayment.objects.filter(pk=payment.pk).update(
        authorization_status=state.status,
        authorization_expires_at=state.expires_at or payment.authorization_expires_at)

    if state.status in ('CAPTURED', 'PARTIALLY_CAPTURED'):
        # A capture from an earlier, unconfirmed attempt: replaying the same
        # PayPal-Request-Id returns it rather than capturing again.
        return payment.authorization_id
    expires_at = state.expires_at or payment.authorization_expires_at
    if state.status in ('VOIDED', 'DENIED', 'EXPIRED') or (expires_at and now >= expires_at):
        raise AuthorizationNotRenewable(
            'PayPal reports the authorization as %s%s' % (
                state.status, ' (expired %s)' % expires_at.isoformat() if expires_at and now >= expires_at else ''))

    honor_started = payment.authorized_at or state.created_at
    if honor_started is None or now <= honor_started + gateway.HONOR_PERIOD:
        return payment.authorization_id

    try:
        renewed = gateway.reauthorize(
            payment.authorization_id, amount=payment.amount, currency=payment.currency,
            request_id=gateway.derive_request_id(payment.capture_request_id, 'reauthorize'))
    except ProviderError as exc:
        if exc.status_code in (409, 422) and not exc.outcome_unknown:
            raise AuthorizationNotRenewable('PayPal refused to renew it (%s: %s)' % (
                exc.issue or exc.code, exc.message)) from exc
        raise
    if renewed.status not in ('CREATED', 'PENDING'):
        raise AuthorizationNotRenewable('PayPal renewed it as %s' % renewed.status)

    with transaction.atomic():
        PayPalPayment.objects.filter(pk=payment.pk).update(
            authorization_id=renewed.authorization_id, authorization_status=renewed.status,
            authorized_at=renewed.created_at or now, authorization_expires_at=renewed.expires_at,
            reauthorization_count=F('reauthorization_count') + 1)
        source = _source_for(payment.order, payment)
        source.reference = renewed.authorization_id
        source.save()
        Transaction.objects.create(source=source, txn_type='Reauthorise', amount=payment.amount,
                                   reference=renewed.authorization_id, status=renewed.status)
        EventHandler().create_note(payment.order, 'PayPal authorization %s renewed as %s.' % (
            payment.authorization_id, renewed.authorization_id))
    logger.info('Order %s: authorization %s renewed as %s', payment.order.number,
                payment.authorization_id, renewed.authorization_id)
    payment.refresh_from_db()
    return renewed.authorization_id


# ---------------------------------------------------------------------------
# Cancel (void)
# ---------------------------------------------------------------------------

def cancel_order(order_id):
    payment = _payment_for_operator(order_id)
    if payment.state in (PayPalPayment.VOIDED, PayPalPayment.CANCELLED):
        return payment
    if payment.state in (PayPalPayment.CAPTURING, PayPalPayment.CAPTURED):
        raise ServiceError(409, 'already_fulfilled',
                           'This order has been fulfilled; return money with a refund instead.')

    if payment.state in (PayPalPayment.AWAITING_PAYMENT, PayPalPayment.FAILED):
        return _cancel_unpaid(payment)

    try:
        _claim(PayPalPayment.objects.filter(pk=payment.pk),
               fresh_from=(PayPalPayment.AUTHORIZED,), claiming=PayPalPayment.VOIDING,
               fresh_updates={'void_request_id': _new_request_id()})
    except ClaimRejected:
        payment.refresh_from_db()
        if payment.state in (PayPalPayment.VOIDED, PayPalPayment.CANCELLED):
            return payment
        _in_progress()
    payment.refresh_from_db()

    try:
        status = gateway.void_authorization(payment.authorization_id,
                                            request_id=payment.void_request_id)
    except ProviderError as exc:
        _release_after_error(PayPalPayment, payment.pk, PayPalPayment.VOIDING, exc,
                             back_to=PayPalPayment.AUTHORIZED)
        raise

    with transaction.atomic():
        updated = PayPalPayment.objects.filter(pk=payment.pk, state=PayPalPayment.VOIDING).update(
            state=PayPalPayment.VOIDED, authorization_status=status, claim_expires_at=None,
            outcome_unknown=False, last_error_code='', last_error_message='')
        payment.refresh_from_db()
        if updated:
            source = _source_for(payment.order, payment)
            Transaction.objects.create(source=source, txn_type='Void', amount=payment.amount,
                                       reference=payment.authorization_id, status=status)
            _close_order(payment.order, 'Cancelled; PayPal authorization %s voided, %s %s released.'
                         % (payment.authorization_id, payment.amount, payment.currency))
    logger.info('Order %s cancelled, authorization %s voided', payment.order.number,
                payment.authorization_id)
    return payment


def _cancel_unpaid(payment):
    # No money is held: cancelling is purely local. The same conditional
    # UPDATE shuts out a payment attempt racing this cancellation.
    with transaction.atomic():
        if PayPalPayment.objects.filter(
                pk=payment.pk, state__in=(PayPalPayment.AWAITING_PAYMENT, PayPalPayment.FAILED)
        ).update(state=PayPalPayment.CANCELLED, claim_expires_at=None):
            payment.refresh_from_db()
            _close_order(payment.order, 'Cancelled before payment.')
            return payment
    _in_progress()


def _in_progress():
    raise ServiceError(409, 'operation_in_progress',
                       'Another operation on this order is in progress; try again shortly.')


def _close_order(order, note):
    EventHandler().cancel_stock_allocations(order)
    _set_order_status(order, STATUS_CANCELLED, note)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def refund_order(user, order_id, *, idempotency_key, amount=None, note=''):
    payment = _payment_for_owner(user, order_id)
    if payment.state != PayPalPayment.CAPTURED:
        raise ServiceError(409, 'not_refundable', 'Only a fulfilled order can be refunded '
                           '(payment is %s).' % payment.payment_status)
    try:
        requested = parse_amount(amount, payment.currency) if amount is not None else None
    except ValueError as exc:
        raise ServiceError(422, 'invalid_amount', str(exc))
    refund, resumed = _claim_refund(payment, idempotency_key, requested, note)
    if not resumed and refund.state != PayPalRefund.REQUESTED:
        return refund, False     # replay of a finished refund under the same key

    try:
        result = gateway.refund_capture(
            payment.capture_id, amount=refund.amount, currency=payment.currency,
            request_id=refund.request_id, custom_id=payment.order.number, note=refund.note or None)
    except ProviderError as exc:
        if exc.outcome_unknown:
            PayPalRefund.objects.filter(pk=refund.pk, state=PayPalRefund.REQUESTED).update(
                claim_expires_at=_reclaimable_now(), outcome_unknown=True,
                last_error_code=exc.code, last_error_message=exc.message[:512])
        else:
            _settle_refund(refund, PayPalRefund.FAILED, error=exc)
        raise

    state = {'COMPLETED': PayPalRefund.COMPLETED, 'PENDING': PayPalRefund.PENDING,
             'CANCELLED': PayPalRefund.CANCELLED}.get(result.status, PayPalRefund.FAILED)
    if result.amount is not None and result.amount != refund.amount:
        logger.error('PayPal refund %s is %s, requested %s', result.refund_id, result.amount,
                     refund.amount)
    refund = _settle_refund(refund, state, refund_id=result.refund_id, paypal_status=result.status)
    if state in (PayPalRefund.FAILED, PayPalRefund.CANCELLED):
        raise ServiceError(422, 'refund_failed', 'PayPal did not complete the refund (%s).'
                           % result.status, refundId=str(refund.public_id))
    logger.info('Order %s refund %s: %s %s (%s)', payment.order.number, result.refund_id,
                refund.amount, payment.currency, result.status)
    return refund, True


def _claim_refund(payment, idempotency_key, requested, note):
    """Insert the refund under its idempotency key and reserve its amount
    against the capture, in one transaction. Returns (refund, resumed)."""
    now = timezone.now()
    existing = PayPalRefund.objects.filter(payment=payment, idempotency_key=idempotency_key).first()
    if existing is not None:
        return _existing_refund(existing, requested, now)
    try:
        with transaction.atomic():
            amount = requested if requested is not None else (
                PayPalPayment.objects.get(pk=payment.pk).refundable_amount)
            if amount <= 0:
                raise RefundRejected()
            refund = PayPalRefund.objects.create(
                payment=payment, idempotency_key=idempotency_key, request_id=_new_request_id(),
                amount=amount, note=note[:255], claim_expires_at=now + CLAIM_TTL)
            reserved = PayPalPayment.objects.filter(
                pk=payment.pk, state=PayPalPayment.CAPTURED,
                refund_reserved__lte=F('captured_amount') - amount,
            ).update(refund_reserved=F('refund_reserved') + amount)
            if not reserved:
                raise RefundRejected()
        return refund, False
    except RefundRejected:
        payment.refresh_from_db()
        raise ServiceError(422, 'exceeds_refundable',
                           'The refund exceeds what remains refundable on this order.',
                           refundable=str(payment.refundable_amount))
    except IntegrityError:
        # A concurrent request inserted the same key first; the unique
        # constraint refused this one.
        existing = PayPalRefund.objects.get(payment=payment, idempotency_key=idempotency_key)
        return _existing_refund(existing, requested, now)


def _existing_refund(existing, requested, now):
    if requested is not None and existing.amount != requested:
        raise ServiceError(422, 'idempotency_key_reused',
                           'This Idempotency-Key was already used for a different refund.')
    if existing.state == PayPalRefund.REQUESTED:
        if PayPalRefund.objects.filter(pk=existing.pk, state=PayPalRefund.REQUESTED,
                                       claim_expires_at__lte=now).update(
                                           claim_expires_at=now + CLAIM_TTL):
            existing.refresh_from_db()
            return existing, True
        raise ServiceError(409, 'refund_in_progress', 'This refund is already being processed.',
                           refundId=str(existing.public_id))
    return existing, False


def _settle_refund(refund, state, *, refund_id='', paypal_status='', error=None):
    with transaction.atomic():
        updated = PayPalRefund.objects.filter(pk=refund.pk, state=PayPalRefund.REQUESTED).update(
            state=state, paypal_refund_id=refund_id, paypal_status=paypal_status,
            claim_expires_at=None, outcome_unknown=False,
            last_error_code=(error.issue or error.code) if error else '',
            last_error_message=error.message[:512] if error else '')
        if updated:
            payments = PayPalPayment.objects.filter(pk=refund.payment_id)
            if state not in PayPalRefund.HOLDING:
                payments.update(refund_reserved=F('refund_reserved') - refund.amount)
            elif state == PayPalRefund.COMPLETED:
                payments.update(refunded_amount=F('refunded_amount') + refund.amount)
                payment = PayPalPayment.objects.select_related('order').get(pk=refund.payment_id)
                _source_for(payment.order, payment).refund(refund.amount, reference=refund_id,
                                                           status=paypal_status)
                EventHandler().create_note(payment.order, 'PayPal refund %s: %s %s.' % (
                    refund_id, refund.amount, payment.currency))
    refund.refresh_from_db()
    return refund


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

def list_saved_cards(user):
    return SavedCard.objects.filter(user=user, state=SavedCard.ACTIVE)


def save_card(user, card, *, idempotency_key=None):
    card_row, resumed = _claim_saved_card(user, idempotency_key)
    if not resumed and card_row.state != SavedCard.SAVING:
        return card_row, False

    customer = PayPalCustomer.objects.filter(user=user).first()
    try:
        vaulted = gateway.vault_card(card, customer_id=customer.customer_id if customer else None,
                                     request_id=card_row.request_id)
    except ProviderError as exc:
        _release_after_error(SavedCard, card_row.pk, SavedCard.SAVING, exc, back_to=SavedCard.FAILED)
        raise

    with transaction.atomic():
        SavedCard.objects.filter(pk=card_row.pk, state=SavedCard.SAVING).update(
            state=SavedCard.ACTIVE, claim_expires_at=None, vault_token_id=vaulted.token_id,
            paypal_customer_id=vaulted.customer_id or '', brand=vaulted.brand[:32],
            last_digits=vaulted.last_digits[:4], expiry=vaulted.expiry[:7])
        if vaulted.customer_id and customer is None:
            try:
                with transaction.atomic():
                    PayPalCustomer.objects.create(user=user, customer_id=vaulted.customer_id)
            except IntegrityError:
                pass    # a concurrent first save recorded the customer already
    card_row.refresh_from_db()
    logger.info('User %s saved card %s', user.pk, card_row.public_id)
    return card_row, True


def _claim_saved_card(user, idempotency_key):
    now = timezone.now()
    try:
        with transaction.atomic():
            return SavedCard.objects.create(
                user=user, idempotency_key=idempotency_key, request_id=_new_request_id(),
                claim_expires_at=now + CLAIM_TTL), False
    except IntegrityError:
        pass
    existing = SavedCard.objects.get(user=user, idempotency_key=idempotency_key)
    if existing.state == SavedCard.SAVING:
        if SavedCard.objects.filter(pk=existing.pk, state=SavedCard.SAVING,
                                    claim_expires_at__lte=now).update(claim_expires_at=now + CLAIM_TTL):
            existing.refresh_from_db()
            return existing, True
        raise ServiceError(409, 'save_in_progress', 'This card is already being saved.')
    if existing.state == SavedCard.FAILED:
        raise ServiceError(422, 'idempotency_key_reused',
                           'Saving a card with this Idempotency-Key already failed; use a new key.')
    if existing.state != SavedCard.ACTIVE:
        raise ServiceError(409, 'idempotency_key_reused',
                           'This Idempotency-Key belongs to a card that has been removed.')
    return existing, False


def delete_saved_card(user, payment_method_id):
    card = SavedCard.objects.filter(user=user, public_id=payment_method_id,
                                    state__in=(SavedCard.ACTIVE, SavedCard.DELETING)).first()
    if card is None:
        raise ServiceError(404, 'payment_method_not_found', 'No such saved card.')
    try:
        _claim(SavedCard.objects.filter(pk=card.pk), fresh_from=(SavedCard.ACTIVE,),
               claiming=SavedCard.DELETING)
    except ClaimRejected:
        raise ServiceError(409, 'delete_in_progress', 'This card is already being removed.')
    try:
        gateway.delete_vaulted_card(card.vault_token_id)
    except ProviderError as exc:
        _release_after_error(SavedCard, card.pk, SavedCard.DELETING, exc, back_to=SavedCard.ACTIVE)
        raise
    SavedCard.objects.filter(pk=card.pk, state=SavedCard.DELETING).update(
        state=SavedCard.DELETED, claim_expires_at=None, deleted_at=timezone.now())
    logger.info('User %s removed card %s', user.pk, card.public_id)


# ---------------------------------------------------------------------------
# Orders listing and reconciliation
# ---------------------------------------------------------------------------

def orders_for(user):
    return (Order.objects.filter(user=user)
            .select_related('paypal_payment')
            .prefetch_related('lines', 'paypal_payment__refunds')
            .order_by('-date_placed'))


MAX_RECONCILIATION_RANGE = timedelta(days=366)


def reconcile(start, end):
    if end <= start:
        raise ServiceError(422, 'invalid_range', '"to" must be after "from".')
    if end - start > MAX_RECONCILIATION_RANGE:
        raise ServiceError(422, 'invalid_range', 'The range may span at most 366 days.')
    report = gateway.search_transactions(start, end)

    payments = list(PayPalPayment.objects.select_related('order').exclude(capture_id=''))
    refunds = list(PayPalRefund.objects.select_related('payment__order').exclude(paypal_refund_id=''))
    by_capture = {p.capture_id: p for p in payments}
    by_refund = {r.paypal_refund_id: r for r in refunds}
    by_number = {p.order.number: p for p in PayPalPayment.objects.select_related('order')}

    matched, unknown_to_app, seen = [], [], set()
    for txn in report.transactions:
        entry = {'paypal': _txn_dict(txn)}
        if txn.transaction_id in by_capture:
            payment = by_capture[txn.transaction_id]
            expected = payment.captured_amount
            entry.update(kind='capture', orderId=payment.order.number,
                         appAmount=str(expected) if expected is not None else None)
        elif txn.transaction_id in by_refund:
            refund = by_refund[txn.transaction_id]
            expected = refund.amount
            entry.update(kind='refund', orderId=refund.payment.order.number,
                         refundId=str(refund.public_id), appAmount=str(expected))
        else:
            payment = by_number.get(txn.custom_field or '')
            entry.update(kind='unknown', orderId=payment.order.number if payment else None)
            entry['status'] = 'unknown_to_app'
            unknown_to_app.append(entry)
            continue
        seen.add(txn.transaction_id)
        amounts_agree = txn.amount is not None and expected is not None and abs(txn.amount) == expected
        entry['status'] = 'matched' if amounts_agree else 'amount_mismatch'
        matched.append(entry)

    missing, not_yet_reported = [], []
    horizon = report.last_refreshed_at
    for payment in payments:
        when = payment.captured_at
        if when and start <= when < end and payment.capture_id not in seen:
            item = {'kind': 'capture', 'orderId': payment.order.number,
                    'paypalId': payment.capture_id, 'amount': str(payment.captured_amount),
                    'at': when.isoformat()}
            (not_yet_reported if horizon and when > horizon else missing).append(item)
    for refund in refunds:
        when = refund.created
        if (refund.state in (PayPalRefund.COMPLETED, PayPalRefund.PENDING)
                and start <= when < end and refund.paypal_refund_id not in seen):
            item = {'kind': 'refund', 'orderId': refund.payment.order.number,
                    'refundId': str(refund.public_id), 'paypalId': refund.paypal_refund_id,
                    'amount': str(refund.amount), 'at': when.isoformat()}
            (not_yet_reported if horizon and when > horizon else missing).append(item)

    return {
        'from': start.isoformat(), 'to': end.isoformat(),
        'paypalDataRefreshedAt': horizon.isoformat() if horizon else None,
        'summary': {
            'paypalTransactions': len(report.transactions),
            'matched': sum(1 for m in matched if m['status'] == 'matched'),
            'amountMismatches': sum(1 for m in matched if m['status'] == 'amount_mismatch'),
            'unknownToApp': len(unknown_to_app),
            'missingFromPayPal': len(missing),
            'notYetReportedByPayPal': len(not_yet_reported),
        },
        'matched': matched,
        'unknownToApp': unknown_to_app,
        'missingFromPayPal': missing,
        'notYetReportedByPayPal': not_yet_reported,
        'paypalTransactions': [_txn_dict(t) for t in report.transactions],
    }


def _txn_dict(txn):
    return {
        'transactionId': txn.transaction_id,
        'referenceId': txn.reference_id,
        'eventCode': txn.event_code,
        'status': txn.status,
        'initiatedAt': txn.initiated_at.isoformat() if txn.initiated_at else None,
        'amount': str(txn.amount) if txn.amount is not None else None,
        'fee': str(txn.fee) if txn.fee is not None else None,
        'currency': txn.currency,
        'customField': txn.custom_field,
        'invoiceId': txn.invoice_id,
    }
