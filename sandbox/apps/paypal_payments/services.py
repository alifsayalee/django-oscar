"""
The payment flows: place an order, authorize it, fulfil (capture), cancel
(void), refund, saved cards, and reconciliation.

Every PayPal write goes through ``safe_write``. Views must not run these
inside a request-wide transaction: the claim has to be committed before
PayPal is called.
"""
from __future__ import annotations

import hashlib
import hmac
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from paypal.core import ApiError, UnsetType
from paypal.models import PaymentAuthorization

from . import gateway
from .gateway import DONE, FAILED, PENDING, Answer, CardInput, ProviderError
from .models import OrderPayment, PayPalRefund, PayPalWrite, SavedCard
from .safe_write import OutcomeUnknown, WriteRefused, WriteResult, attempt_ref, safe_write, try_claim

Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
Basket = get_model('basket', 'Basket')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Transaction = get_model('payment', 'Transaction')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Repository = get_class('shipping.repository', 'Repository')
Selector = get_class('partner.strategy', 'Selector')
Applicator = get_class('offer.applicator', 'Applicator')

# Reauthorize docstring: a 3-day honor period; reauthorization allowed from day 4 to day 29 of the
# original authorization; after that a new authorization is required.
HONOR_PERIOD = timedelta(days=3)
REAUTHORIZE_LIMIT = timedelta(days=29)

MAX_LINES = 50
MAX_QUANTITY = 100


class ApiProblem(Exception):
    """A request our API rejects before (or instead of) calling PayPal."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


@dataclass
class FlowResult:
    """What a flow did: the outcome of the write THIS request made or found."""

    outcome: str
    payment: OrderPayment | None = None
    detail: str = ''
    failed_status: int = 409  # the HTTP status a failed outcome answers with
    code: str = ''


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def amount_str(value: Decimal | None, code: str) -> str | None:
    return None if value is None else gateway.format_amount(value, code)


def refund_json(r: PayPalRefund) -> dict[str, Any]:
    code = r.payment.currency
    return {
        'refundId': r.pk,
        'paypalRefundId': r.paypal_refund_id or None,
        'amount': amount_str(r.amount, code),
        'currency': code,
        'status': r.status or None,
        'outcome': r.outcome,
        'refundedAt': r.refunded_at.isoformat() if r.refunded_at else None,
    }


def payment_json(p: OrderPayment | None) -> dict[str, Any] | None:
    if p is None:
        return None
    code = p.currency
    captured = p.captured_amount or Decimal('0')
    return {
        'state': p.state,
        'amount': amount_str(p.amount, code),
        'currency': code,
        'card': {'brand': p.card_brand, 'lastDigits': p.card_last_digits} if p.card_last_digits else None,
        'paymentMethodId': p.payment_method_id,
        'paypalOrderId': p.paypal_order_id or None,
        'authorization': {
            'id': p.authorization_id,
            'status': p.authorization_status,
            'authorizedAt': p.authorized_at.isoformat() if p.authorized_at else None,
            'expiresAt': p.authorization_expires_at.isoformat() if p.authorization_expires_at else None,
            'reauthorizedAt': p.reauthorized_at.isoformat() if p.reauthorized_at else None,
        } if p.authorization_id else None,
        'capture': {
            'id': p.capture_id,
            'status': p.capture_status,
            'capturedAmount': amount_str(p.captured_amount, code),
            'paypalFee': amount_str(p.paypal_fee, code),
            'netAmount': amount_str(p.net_amount, code),
            'capturedAt': p.captured_at.isoformat() if p.captured_at else None,
        } if p.capture_id else None,
        'refundedAmount': amount_str(p.refunded_amount, code),
        'refundableAmount': amount_str(max(captured - reserved_refunds(p), Decimal('0')), code),
        'refunds': [refund_json(r) for r in p.refunds.all()],
        'detail': p.detail or None,
    }


def order_json(order: Any) -> dict[str, Any]:
    payment = OrderPayment.objects.filter(order=order).first()
    return {
        'orderId': order.pk,
        'number': order.number,
        'status': order.status,
        'placedAt': order.date_placed.isoformat() if order.date_placed else None,
        'total': amount_str(payment.amount if payment else order.total_incl_tax, gateway.currency()),
        'currency': payment.currency if payment else gateway.currency(),
        'lines': [{'productId': line.product_id, 'title': line.title, 'quantity': line.quantity,
                   'lineTotal': amount_str(line.line_price_incl_tax, gateway.currency())}
                  for line in order.lines.all()],
        'payment': payment_json(payment),
    }


def card_json(card: SavedCard) -> dict[str, Any]:
    return {
        'paymentMethodId': card.pk,
        'brand': card.brand,
        'lastDigits': card.last_digits,
        'expiry': card.expiry,
        'createdAt': card.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Oscar bookkeeping
# ---------------------------------------------------------------------------

def paypal_source(payment: OrderPayment) -> Any:
    source_type, _ = SourceType.objects.get_or_create(name='PayPal')
    source, _ = Source.objects.get_or_create(
        order=payment.order, source_type=source_type,
        defaults={'currency': payment.currency, 'reference': payment.paypal_order_id})
    label = '%s ending %s' % (payment.card_brand or 'Card', payment.card_last_digits) if payment.card_last_digits else ''
    if label and source.label != label:
        source.label = label
        source.save()
    return source


def advance_order_status(order: Any, target: str) -> None:
    """Walk Oscar's status pipeline to ``target`` (e.g. Pending -> Being processed -> Complete)."""
    if order.status == target:
        return
    if target not in order.available_statuses():
        for step in order.available_statuses():
            if target in order.pipeline.get(step, ()):
                order.set_status(step)
                break
    order.set_status(target)


# ---------------------------------------------------------------------------
# Place an order
# ---------------------------------------------------------------------------

def place_order(user: Any, items: list[dict[str, Any]], request: Any) -> Any:
    if not items or len(items) > MAX_LINES:
        raise ApiProblem(400, 'invalid_items', 'Send between 1 and %d items.' % MAX_LINES)
    wanted: dict[int, int] = defaultdict(int)
    for item in items:
        try:
            product_id = int(item['productId'])
            quantity = int(item['quantity'])
        except (KeyError, TypeError, ValueError):
            raise ApiProblem(400, 'invalid_items', 'Each item needs an integer productId and quantity.') from None
        if not 1 <= quantity <= MAX_QUANTITY:
            raise ApiProblem(400, 'invalid_quantity', 'Quantity must be between 1 and %d.' % MAX_QUANTITY)
        wanted[product_id] += quantity

    strategy = Selector().strategy(request=request, user=user)
    products = {p.pk: p for p in Product.objects.filter(pk__in=wanted.keys())}
    for product_id, quantity in wanted.items():
        product = products.get(product_id)
        if product is None or not product.is_public or product.is_parent:
            raise ApiProblem(422, 'product_unavailable', 'Product %s cannot be ordered.' % product_id)
        info = strategy.fetch_for_product(product)
        if not info.price.exists:
            raise ApiProblem(422, 'product_unavailable', 'Product %s has no price.' % product_id)
        permitted, reason = info.availability.is_purchase_permitted(quantity)
        if not permitted:
            raise ApiProblem(422, 'product_unavailable', 'Product %s: %s' % (product_id, reason))

    code = gateway.currency()
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in wanted.items():
            basket.add_product(products[product_id], quantity)
        Applicator().apply(basket, user, request)
        shipping_method = Repository().get_default_shipping_method(basket=basket, user=user, request=request)
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method, shipping_charge=shipping_charge,
            user=user, order_number=OrderNumberGenerator().order_number(basket),
            status=getattr(settings, 'OSCAR_INITIAL_ORDER_STATUS', None), request=request)
        basket.submit()
        amount = Decimal(total.incl_tax).quantize(gateway.quantum(code))
        if amount <= 0:
            raise ApiProblem(422, 'nothing_to_pay', 'The order total is zero.')
        OrderPayment.objects.create(order=order, user=user, currency=code, amount=amount)
    return order


def get_payment_for(user: Any, order_id: int, *, staff: bool = False) -> OrderPayment:
    """The order's payment, only if the caller may see it (else 404, never 403: no existence leak)."""
    qs = OrderPayment.objects.select_related('order')
    if not staff:
        qs = qs.filter(user=user)
    payment = qs.filter(order_id=order_id).first()
    if payment is None:
        raise ApiProblem(404, 'order_not_found', 'No such order.')
    return payment


# ---------------------------------------------------------------------------
# Pay (authorize)
# ---------------------------------------------------------------------------

def authorize_refusal(e: ApiError[Any]) -> ProviderError:
    issue = gateway.error_issue(e.error)
    if e.status_code == 422:
        return ProviderError(402, 'payment_declined', 'The payment was declined: %s' % (issue or 'no reason given'))
    if e.status_code == 400:
        return ProviderError(422, 'invalid_card', 'PayPal rejected the card details: %s' % (issue or 'invalid request'))
    if e.status_code in (403, 404):
        # Smoke-observed: an unusable (e.g. deleted) vault token answers 403 on create order.
        return ProviderError(409, 'payment_method_unusable', 'PayPal would not charge this payment method.')
    return gateway.provider_error(e.status_code, e.error)


def pay(user: Any, order_id: int, *, card: CardInput | None, payment_method_id: int | None) -> FlowResult:
    payment = get_payment_for(user, order_id)
    if payment.state not in (OrderPayment.AWAITING_PAYMENT, OrderPayment.AUTHORIZATION_PENDING):
        return FlowResult(DONE if payment.state != OrderPayment.CANCELLED else FAILED, payment,
                          code='already_paid' if payment.state != OrderPayment.CANCELLED else 'order_cancelled')

    saved: SavedCard | None = None
    if payment_method_id is not None:
        saved = SavedCard.objects.filter(pk=payment_method_id, user=user, state=SavedCard.ACTIVE).first()
        if saved is None:
            raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')
    elif card is None:
        raise ApiProblem(400, 'payment_source_required', 'Send either card details or a paymentMethodId.')

    order = payment.order
    code = payment.currency
    ref = attempt_ref('auth', order.number)
    attempt = ref.rsplit('-', 1)[1]

    def apply(record: PayPalWrite, result: Any, got: Answer) -> None:
        auth = gateway.first_authorization(result)
        p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        p.paypal_order_id = gateway._str(result.id)
        if saved is not None:
            p.payment_method = saved
        card_resp = None if isinstance(result.payment_source, UnsetType) else result.payment_source.card
        if card_resp is not None and not isinstance(card_resp, UnsetType):
            p.card_brand = gateway._str(card_resp.brand)
            p.card_last_digits = gateway._str(card_resp.last_digits)
        if auth is not None:
            p.authorization_id = gateway._str(auth.id)
            p.authorization_status = gateway._str(auth.status)
            p.authorized_at = got.provider_time
            p.original_authorized_at = got.provider_time
            p.authorization_expires_at = gateway.authorization_expiry(auth)
        p.detail = record.detail
        if record.outcome == DONE:
            p.state = OrderPayment.AUTHORIZED
            p.save()
            paypal_source(p).allocate(p.amount, reference=p.authorization_id, status=p.authorization_status)
            advance_order_status(p.order, 'Being processed')
        elif record.outcome == PENDING:
            p.state = OrderPayment.AUTHORIZATION_PENDING
            p.save()
        elif record.outcome == PayPalWrite.NEEDS_REVIEW:
            p.state = OrderPayment.NEEDS_REVIEW
            p.save()
        else:  # failed: nothing is held; the shopper may try again (a new attempt, a new reference)
            p.state = OrderPayment.AWAITING_PAYMENT
            p.save()

    try:
        write = safe_write(
            ref=ref, step=PayPalWrite.AUTHORIZE,
            send=lambda key: gateway.authorize(
                key, amount=payment.amount, code=code, order_number=order.number,
                invoice_id='%s-%s' % (gateway.deterministic_ref('inv', order.number), attempt),
                card=card, vault_id=saved.paypal_token_id if saved else None),
            # a first answer is the order; a pending one re-read by id is the authorization itself
            read=lambda result: (gateway.read_authorization(result) if isinstance(result, PaymentAuthorization)
                                 else gateway.read_authorize(result)),
            outcome_of=gateway.authorize_outcome,
            sent=(payment.amount, code),
            lookup=lambda auth_id: gateway.get_authorization(auth_id),
            refusal=authorize_refusal,
            apply=lambda record, result, got: (
                apply_pending_authorization(payment, record, result, got) if isinstance(result, PaymentAuthorization)
                else apply(record, result, got)),
            claim_fields={'order': order, 'user': user, 'amount': payment.amount, 'currency': code},
        )
    except WriteRefused as e:
        payment.refresh_from_db()
        return FlowResult(FAILED, payment, detail=e.error.message, failed_status=e.error.status_code, code=e.error.code)
    payment.refresh_from_db()
    return FlowResult(write.outcome, payment, detail=write.record.detail, failed_status=402,
                      code=authorize_code(write))


def authorize_code(write: WriteResult) -> str:
    if write.record.provider_status == gateway.PAYER_ACTION_REQUIRED:
        return 'payer_action_required'
    return {DONE: 'authorized', PENDING: 'authorization_pending', FAILED: 'payment_declined',
            PayPalWrite.NEEDS_REVIEW: 'needs_review', PayPalWrite.SENDING: 'in_progress'}.get(write.outcome, 'unknown')


def apply_pending_authorization(payment: OrderPayment, record: PayPalWrite, auth: Any, got: Answer) -> None:
    """A pending authorization re-read by its id (GET authorization) has settled."""
    p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
    p.authorization_status = gateway._str(auth.status)
    p.authorization_expires_at = gateway.authorization_expiry(auth) or p.authorization_expires_at
    p.detail = record.detail
    if record.outcome == DONE:
        p.state = OrderPayment.AUTHORIZED
        p.save()
        paypal_source(p).allocate(p.amount, reference=p.authorization_id, status=p.authorization_status)
        advance_order_status(p.order, 'Being processed')
    elif record.outcome == FAILED:
        p.state = OrderPayment.AWAITING_PAYMENT
        p.save()
    else:
        p.save()


# ---------------------------------------------------------------------------
# Fulfil (capture), renewing a stale authorization first
# ---------------------------------------------------------------------------

def not_renewable(p: OrderPayment, why: str) -> ProviderError:
    since = p.original_authorized_at or p.authorized_at
    return ProviderError(
        409, 'authorization_not_renewable',
        'The payment hold from %s can no longer be renewed (%s). Cancel this order to release the hold '
        'and ask the shopper to pay again.' % (since.date().isoformat() if since else 'an unknown date', why))


def fulfil(order_id: int) -> FlowResult:
    payment = get_payment_for(None, order_id, staff=True)
    if payment.state in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED):
        return FlowResult(DONE, payment, code='already_fulfilled')
    if payment.state not in (OrderPayment.AUTHORIZED, OrderPayment.CAPTURE_PENDING):
        raise ApiProblem(409, 'not_authorized',
                         'The order has no payment hold to capture (payment state: %s).' % payment.state)

    if payment.state == OrderPayment.AUTHORIZED:
        now = timezone.now()
        original = payment.original_authorized_at or payment.authorized_at
        if payment.authorization_expires_at and now >= payment.authorization_expires_at:
            raise ApiProblem(409, *_problem(not_renewable(payment, 'it expired on %s' % (
                payment.authorization_expires_at.date().isoformat()))))
        if original and now - original > REAUTHORIZE_LIMIT:
            raise ApiProblem(409, *_problem(not_renewable(payment, 'PayPal renews a hold only up to 29 days')))
        if payment.authorized_at and now - payment.authorized_at > HONOR_PERIOD and not payment.reauthorized_at:
            renewed = reauthorize(payment)
            if renewed is not None:
                return renewed

    try:
        return capture(payment)
    except WriteRefused as e:
        payment.refresh_from_db()
        stale = payment.authorized_at and timezone.now() - payment.authorized_at > HONOR_PERIOD
        if stale and not payment.reauthorized_at:
            renewed = reauthorize(payment)
            if renewed is not None:
                return renewed
            return capture(payment)
        return FlowResult(FAILED, payment, detail=e.error.message, failed_status=e.error.status_code,
                          code=e.error.code)


def _problem(error: ProviderError) -> tuple[str, str]:
    return error.code, error.message


def reauthorize(payment: OrderPayment) -> FlowResult | None:
    """Renew a stale hold. Returns None when renewed (go on to capture), else the answer to give."""
    old_id = payment.authorization_id
    code = payment.currency

    def refusal(e: ApiError[Any]) -> ProviderError:
        return not_renewable(payment, 'PayPal refused: %s' % (gateway.error_issue(e.error) or 'HTTP %s' % e.status_code))

    def apply(record: PayPalWrite, auth: Any, got: Answer) -> None:
        p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        if record.outcome in (DONE, PENDING):
            p.authorization_id = got.provider_id
            p.authorization_status = gateway._str(auth.status)
            p.authorized_at = got.provider_time or timezone.now()
            p.authorization_expires_at = gateway.authorization_expiry(auth) or p.authorization_expires_at
            p.reauthorized_at = timezone.now()
        p.detail = record.detail
        p.save()
        if record.outcome == DONE:
            paypal_source(p).transactions.create(
                txn_type='Reauthorise', amount=p.amount, reference=p.authorization_id, status=p.authorization_status)

    try:
        write = safe_write(
            ref=attempt_ref('reauth', payment.order.number, old_id), step=PayPalWrite.REAUTHORIZE,
            send=lambda key: gateway.reauthorize(key, old_id, amount=payment.amount, code=code),
            read=gateway.read_authorization, outcome_of=gateway.authorization_outcome,
            sent=(payment.amount, code), lookup=gateway.get_authorization,
            refusal=refusal, apply=apply,
            claim_fields={'order': payment.order, 'amount': payment.amount, 'currency': code},
        )
    except WriteRefused as e:
        payment.refresh_from_db()
        return FlowResult(FAILED, payment, detail=e.error.message, code=e.error.code)
    payment.refresh_from_db()
    if write.outcome == DONE:
        return None
    if write.outcome == FAILED:
        return FlowResult(FAILED, payment, detail=not_renewable(payment, write.record.detail or
                                                                'PayPal did not renew it').message,
                          code='authorization_not_renewable')
    return FlowResult(write.outcome, payment, detail=write.record.detail, code='reauthorization_' + write.outcome)


def capture(payment: OrderPayment) -> FlowResult:
    """Capture the full order total against the current authorization. Raises WriteRefused on a 4xx."""
    auth_id = payment.authorization_id
    code = payment.currency

    def apply(record: PayPalWrite, result: Any, got: Answer) -> None:
        p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        p.capture_id = got.provider_id
        p.capture_status = gateway._str(result.status)
        p.detail = record.detail
        if record.outcome == DONE:
            breakdown = gateway.capture_breakdown(result)
            p.captured_amount = breakdown['gross'] if breakdown['gross'] is not None else got.amount
            p.paypal_fee = breakdown['fee']
            p.net_amount = breakdown['net']
            p.captured_at = got.provider_time
            p.state = OrderPayment.CAPTURED
            p.save()
            paypal_source(p).debit(p.captured_amount, reference=p.capture_id, status=p.capture_status)
            advance_order_status(p.order, 'Complete')
        elif record.outcome == PENDING:
            p.state = OrderPayment.CAPTURE_PENDING
            p.save()
        elif record.outcome == PayPalWrite.NEEDS_REVIEW:
            p.state = OrderPayment.NEEDS_REVIEW
            p.save()
        else:
            p.state = OrderPayment.AUTHORIZED  # declined/failed: the hold may still be there
            p.save()

    write = safe_write(
        ref=attempt_ref('capture', payment.order.number, auth_id), step=PayPalWrite.CAPTURE,
        send=lambda key: gateway.capture(key, auth_id, amount=payment.amount, code=code),
        read=gateway.read_capture, outcome_of=gateway.capture_outcome,
        sent=(payment.amount, code), lookup=gateway.get_capture, apply=apply,
        claim_fields={'order': payment.order, 'amount': payment.amount, 'currency': code},
    )
    payment.refresh_from_db()
    codes = {DONE: 'captured', PENDING: 'capture_pending', FAILED: 'capture_failed',
             PayPalWrite.NEEDS_REVIEW: 'needs_review', PayPalWrite.SENDING: 'in_progress'}
    return FlowResult(write.outcome, payment, detail=write.record.detail, code=codes.get(write.outcome, 'unknown'))


# ---------------------------------------------------------------------------
# Cancel (void) before fulfilment
# ---------------------------------------------------------------------------

def cancel(order_id: int) -> FlowResult:
    payment = get_payment_for(None, order_id, staff=True)
    if payment.state == OrderPayment.CANCELLED:
        return FlowResult(DONE, payment, code='already_cancelled')
    if payment.state in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED,
                         OrderPayment.CAPTURE_PENDING):
        raise ApiProblem(409, 'already_fulfilled', 'The payment was already captured; refund it instead.')
    if payment.state == OrderPayment.AWAITING_PAYMENT:
        in_doubt = PayPalWrite.objects.filter(
            order=payment.order, step=PayPalWrite.AUTHORIZE,
            outcome__in=(PayPalWrite.SENDING, PayPalWrite.UNKNOWN, PayPalWrite.PENDING)).exists()
        if in_doubt:
            raise ApiProblem(409, 'payment_in_doubt', 'A payment attempt for this order has not settled yet; '
                             'retry the pay request (it re-checks PayPal) before cancelling.')
        with transaction.atomic():
            p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
            p.state = OrderPayment.CANCELLED
            p.save()
            advance_order_status(p.order, 'Cancelled')
        payment.refresh_from_db()
        return FlowResult(DONE, payment, code='cancelled')
    if not payment.authorization_id:
        raise ApiProblem(409, 'needs_review', 'This payment needs operator review: %s' % payment.detail)

    auth_id = payment.authorization_id

    def found(e: ApiError[Any]) -> Any:
        # Refused: read the authorization itself - it may already be voided (done) or captured (too late).
        try:
            return gateway.get_authorization(auth_id)
        except (ApiError, ValueError):
            return None

    def apply(record: PayPalWrite, auth: Any, got: Answer) -> None:
        p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        p.authorization_status = gateway._str(auth.status)
        p.detail = record.detail
        if record.outcome == DONE:
            p.state = OrderPayment.CANCELLED
            p.save()
            paypal_source(p).transactions.create(
                txn_type='Void', amount=p.amount, reference=auth_id, status=p.authorization_status)
            advance_order_status(p.order, 'Cancelled')
        else:
            p.save()

    try:
        write = safe_write(
            ref=attempt_ref('void', payment.order.number, auth_id), step=PayPalWrite.VOID,
            send=lambda key: gateway.void(key, auth_id),
            read=gateway.read_authorization, outcome_of=gateway.void_outcome,
            lookup=gateway.get_authorization, on_refused=found, apply=apply,
            claim_fields={'order': payment.order, 'amount': payment.amount, 'currency': payment.currency},
        )
    except WriteRefused as e:
        payment.refresh_from_db()
        return FlowResult(FAILED, payment, detail=e.error.message, failed_status=e.error.status_code, code=e.error.code)
    payment.refresh_from_db()
    if write.outcome == FAILED:
        return FlowResult(FAILED, payment, code='already_captured',
                          detail='PayPal reports the hold was already captured; refund it instead.')
    return FlowResult(write.outcome, payment, detail=write.record.detail,
                      code='cancelled' if write.outcome == DONE else 'cancel_' + write.outcome)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def reserved_refunds(p: OrderPayment) -> Decimal:
    """Everything refunded or possibly being refunded: all refunds except definite failures."""
    total = p.refunds.exclude(outcome=PayPalWrite.FAILED).aggregate(s=Sum('amount'))['s']
    return total or Decimal('0')


def parse_amount(raw: Any, code: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except Exception:
        raise ApiProblem(400, 'invalid_amount', 'amount must be a decimal string such as "5.00".') from None
    if not value.is_finite() or value <= 0 or value != value.quantize(gateway.quantum(code)):
        raise ApiProblem(400, 'invalid_amount', 'amount must be positive with at most %d decimals for %s.'
                         % (gateway.EXPONENT.get(code, 2), code))
    return value


@dataclass
class RefundResult(FlowResult):
    refund: PayPalRefund | None = None


def refund(order_id: int, *, idempotency_key: str, amount_raw: Any) -> RefundResult:
    if not idempotency_key or len(idempotency_key) > 255:
        raise ApiProblem(400, 'idempotency_key_required',
                         'Send an Idempotency-Key header (or idempotencyKey field), at most 255 characters.')
    payment = get_payment_for(None, order_id, staff=True)
    code = payment.currency
    requested = parse_amount(amount_raw, code) if amount_raw not in (None, '') else None

    with transaction.atomic():
        # Lock the payment row first so two refunds of one capture are decided one after the other.
        OrderPayment.objects.filter(pk=payment.pk).update(updated_at=timezone.now())
        p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        existing = PayPalRefund.objects.filter(payment=p, idempotency_key=idempotency_key).first()
        if existing is not None:
            if requested is not None and requested != existing.amount:
                raise ApiProblem(422, 'idempotency_key_reused',
                                 'This Idempotency-Key was already used for a refund of %s.'
                                 % amount_str(existing.amount, code))
            refund_row, claimed = existing, False
        else:
            if p.state not in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED) or not p.capture_id:
                raise ApiProblem(409, 'not_refundable', 'Only a captured payment can be refunded (state: %s).' % p.state)
            remaining = (p.captured_amount or Decimal('0')) - reserved_refunds(p)
            amount = requested if requested is not None else remaining
            if amount <= 0 or amount > remaining:
                raise ApiProblem(422, 'refund_exceeds_captured',
                                 'At most %s %s can still be refunded.' % (amount_str(max(remaining, Decimal('0')), code), code),
                                 refundable=amount_str(max(remaining, Decimal('0')), code))
            key_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]
            ref = gateway.deterministic_ref('refund', p.order.number, key_digest)
            refund_row = PayPalRefund.objects.create(payment=p, idempotency_key=idempotency_key, ref=ref, amount=amount)
            claimed = try_claim(ref, PayPalWrite.REFUND, order=p.order, amount=amount, currency=code)

    capture_id = payment.capture_id

    def apply(record: PayPalWrite, result: Any, got: Answer) -> None:
        r = PayPalRefund.objects.select_for_update().get(pk=refund_row.pk)
        was_done = r.outcome == DONE
        r.outcome = record.outcome
        r.paypal_refund_id = got.provider_id or r.paypal_refund_id
        r.status = gateway._str(result.status)
        r.refunded_at = got.provider_time or r.refunded_at
        r.save()
        if record.outcome == DONE and not was_done:
            p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
            p.refunded_amount += r.amount
            p.state = (OrderPayment.REFUNDED if p.refunded_amount >= (p.captured_amount or Decimal('0'))
                       else OrderPayment.PARTIALLY_REFUNDED)
            p.save()
            paypal_source(p).refund(r.amount, reference=r.paypal_refund_id, status=r.status)

    try:
        write = safe_write(
            ref=refund_row.ref, step=PayPalWrite.REFUND, claimed=claimed,
            send=lambda key: gateway.refund(key, capture_id, amount=refund_row.amount, code=code),
            read=gateway.read_refund, outcome_of=gateway.refund_outcome,
            sent=(refund_row.amount, code), lookup=gateway.get_refund, apply=apply,
        )
    except WriteRefused as e:
        PayPalRefund.objects.filter(pk=refund_row.pk).update(outcome=PayPalWrite.FAILED)
        payment.refresh_from_db()
        refund_row.refresh_from_db()
        return RefundResult(FAILED, payment, detail=e.error.message, failed_status=e.error.status_code,
                            code=e.error.code, refund=refund_row)
    except OutcomeUnknown:
        PayPalRefund.objects.filter(pk=refund_row.pk).update(outcome=PayPalWrite.UNKNOWN)
        raise
    refund_row.refresh_from_db()
    if refund_row.outcome != write.outcome and write.result is None:
        PayPalRefund.objects.filter(pk=refund_row.pk).update(outcome=write.outcome)
        refund_row.refresh_from_db()
    payment.refresh_from_db()
    return RefundResult(write.outcome, payment, detail=write.record.detail, code='refund_' + write.outcome,
                        refund=refund_row)


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

def card_digest(user: Any, card: CardInput, idempotency_key: str | None) -> str:
    if idempotency_key:
        return hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]
    # No caller key: whose card, which card (keyed hash - never the number), and how many times
    # this shopper has deleted a card (so re-saving after a delete is a new write).
    deleted = SavedCard.objects.filter(user=user, state=SavedCard.DELETED).count()
    material = '%s|%s|%s' % (card.number, card.expiry, deleted)
    return hmac.new(settings.SECRET_KEY.encode(), material.encode(), hashlib.sha256).hexdigest()[:20]


@dataclass
class CardResult(FlowResult):
    card: SavedCard | None = None


def save_card(user: Any, card: CardInput, idempotency_key: str | None) -> CardResult:
    ref = attempt_ref('card', user.pk, card_digest(user, card, idempotency_key))
    customer_id = (SavedCard.objects.filter(user=user).exclude(paypal_customer_id='')
                   .values_list('paypal_customer_id', flat=True).last())

    def apply(record: PayPalWrite, saved: gateway.SavedToken, got: Answer) -> None:
        if record.outcome != DONE:
            return
        token = saved.token
        if isinstance(token.payment_source, UnsetType) or isinstance(token.payment_source.card, UnsetType):
            return  # unreachable: read_saved_card only answers done when the card is present
        pm = token.payment_source.card
        customer = '' if isinstance(token.customer, UnsetType) else gateway._str(token.customer.id)
        SavedCard.objects.update_or_create(
            paypal_token_id=got.provider_id,
            defaults={'user': user, 'state': SavedCard.ACTIVE, 'paypal_customer_id': customer,
                      'brand': gateway._str(pm.brand), 'last_digits': gateway._str(pm.last_digits),
                      'expiry': gateway._str(pm.expiry), 'save_ref': ref, 'deleted_at': None})

    def refusal(e: ApiError[Any]) -> ProviderError:
        if e.status_code in (400, 422):
            return ProviderError(422, 'card_rejected', 'PayPal could not save this card: %s'
                                 % (gateway.error_issue(e.error) or 'invalid card'))
        return gateway.provider_error(e.status_code, e.error)

    try:
        write = safe_write(
            ref=ref, step=PayPalWrite.SAVE_CARD,
            send=lambda key: gateway.save_card(key, card, customer_id),
            read=gateway.read_saved_card, outcome_of=gateway.save_card_outcome,
            refusal=refusal, apply=apply, claim_fields={'user': user},
        )
    except WriteRefused as e:
        return CardResult(FAILED, detail=e.error.message, failed_status=e.error.status_code, code=e.error.code)
    saved = (SavedCard.objects.filter(user=user, paypal_token_id=write.record.provider_id).first()
             if write.record.provider_id else None)
    if write.outcome == DONE and saved is not None and saved.state != SavedCard.ACTIVE:
        # A repeat of a save whose card has since been deleted: that card is gone.
        return CardResult(FAILED, detail='This card was deleted; save it again with a new Idempotency-Key.',
                          code='card_deleted')
    return CardResult(write.outcome, detail=write.record.detail, failed_status=402,
                      code='card_saved' if write.outcome == DONE else 'card_' + write.outcome, card=saved)


def list_cards(user: Any) -> list[SavedCard]:
    return list(SavedCard.objects.filter(user=user, state=SavedCard.ACTIVE))


def delete_card(user: Any, card_id: int) -> CardResult:
    with transaction.atomic():
        card = (SavedCard.objects.select_for_update()
                .filter(pk=card_id, user=user, state__in=(SavedCard.ACTIVE, SavedCard.DELETING)).first())
        if card is None:
            raise ApiProblem(404, 'payment_method_not_found', 'No such saved card.')
        # Hidden and unusable from this moment on, whatever PayPal answers.
        card.state = SavedCard.DELETING
        card.save()
    token_id = card.paypal_token_id

    def apply(record: PayPalWrite, deleted: Any, got: Answer) -> None:
        c = SavedCard.objects.select_for_update().get(pk=card.pk)
        if record.outcome == DONE:
            c.state = SavedCard.DELETED
            c.deleted_at = timezone.now()
            c.save()

    try:
        write = safe_write(
            ref=attempt_ref('carddel', card.pk), step=PayPalWrite.DELETE_CARD,
            send=lambda key: gateway.delete_card(token_id),
            read=lambda deleted: gateway.read_deleted(deleted, token_id),
            outcome_of=gateway.delete_outcome, apply=apply, claim_fields={'user': user},
        )
    except WriteRefused as e:
        SavedCard.objects.filter(pk=card.pk, state=SavedCard.DELETING).update(state=SavedCard.ACTIVE)
        return CardResult(FAILED, detail=e.error.message, failed_status=e.error.status_code, code=e.error.code)
    card.refresh_from_db()
    return CardResult(write.outcome, detail=write.record.detail,
                      code='deleted' if write.outcome == DONE else 'delete_' + write.outcome, card=card)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

SEARCH_WINDOW = timedelta(days=31)  # search_transactions: maximum supported range is 31 days
MAX_PAGES = 1000


def fetch_paypal_transactions(start: datetime, end: datetime) -> list[Any]:
    """PayPal's record for the whole range: every <=31-day window, every page."""
    records: list[Any] = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + SEARCH_WINDOW, end)
        page = 1
        while True:
            try:
                response = gateway.search_transactions(window_start, window_end, page)
            except ApiError as e:
                raise gateway.provider_error(e.status_code, e.error) from e
            details = [] if isinstance(response.transaction_details, UnsetType) else response.transaction_details
            records.extend(d.transaction_info for d in details if not isinstance(d.transaction_info, UnsetType))
            total_pages = 0 if isinstance(response.total_pages, UnsetType) else response.total_pages
            if page >= total_pages or not details or page >= MAX_PAGES:
                break
            page += 1
        window_start = window_end
    return records


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    if end <= start:
        raise ApiProblem(400, 'invalid_range', '"to" must be after "from".')
    try:
        provider = [t for t in fetch_paypal_transactions(start, end)
                    if (ts := gateway.parse_time(t.transaction_initiation_date)) is None or start <= ts < end]
    except ProviderError:
        raise
    prefix = gateway.reference_prefix()

    # Our side, on PayPal's clock: captures and refunds whose PayPal time falls in the window.
    local: list[dict[str, Any]] = []
    for p in OrderPayment.objects.filter(captured_at__gte=start, captured_at__lt=end).select_related('order'):
        local.append({'kind': 'capture', 'paypalId': p.capture_id, 'orderId': p.order_id,
                      'orderNumber': p.order.number, 'amount': amount_str(p.captured_amount, p.currency),
                      'currency': p.currency, 'status': p.capture_status})
    for r in PayPalRefund.objects.filter(refunded_at__gte=start, refunded_at__lt=end).select_related('payment__order'):
        local.append({'kind': 'refund', 'paypalId': r.paypal_refund_id, 'orderId': r.payment.order_id,
                      'orderNumber': r.payment.order.number, 'amount': amount_str(r.amount, r.payment.currency),
                      'currency': r.payment.currency, 'status': r.status})
    unsettled = [{'reference': w.ref, 'step': w.step, 'outcome': w.outcome, 'orderId': w.order_id,
                  'claimedAt': w.claimed_at.isoformat(), 'detail': w.detail}
                 for w in PayPalWrite.objects.filter(
                     claimed_at__gte=start, claimed_at__lt=end, provider_time__isnull=True,
                     outcome__in=(PayPalWrite.SENDING, PayPalWrite.PENDING, PayPalWrite.UNKNOWN))]

    def txn_json(t: Any) -> dict[str, Any]:
        amount, code = gateway.parse_money(t.transaction_amount)
        fee, _ = gateway.parse_money(t.fee_amount)
        return {'transactionId': gateway._str(t.transaction_id), 'eventCode': gateway._str(t.transaction_event_code),
                'status': gateway._str(t.transaction_status), 'initiatedAt': gateway._str(t.transaction_initiation_date),
                'amount': str(amount) if amount is not None else None, 'currency': code,
                'fee': str(fee) if fee is not None else None, 'invoiceId': gateway._str(t.invoice_id) or None,
                'customField': gateway._str(t.custom_field) or None,
                'referenceId': gateway._str(t.paypal_reference_id) or None}

    by_id: dict[str, list[Any]] = defaultdict(list)
    for t in provider:
        by_id[gateway._str(t.transaction_id)].append(t)
    matched, app_only = [], []
    for record in local:
        found = by_id.pop(record['paypalId'], []) if record['paypalId'] else []
        if found:
            txns = [txn_json(t) for t in found]
            amounts_agree = all(
                x['amount'] is None or abs(Decimal(x['amount'])) == Decimal(record['amount'] or '0') for x in txns)
            matched.append({'app': record, 'paypal': txns, 'amountsAgree': amounts_agree})
        else:
            app_only.append(record)
    paypal_only = []
    for txns in by_id.values():
        for t in txns:
            entry = txn_json(t)
            ours = (entry['invoiceId'] or '').startswith(prefix) or (entry['customField'] or '').startswith(prefix)
            entry['carriesOurReference'] = ours
            paypal_only.append(entry)
    return {
        'from': start.isoformat(), 'to': end.isoformat(),
        'paypalTransactionCount': len(provider),
        'matched': matched,
        'paypalOnly': paypal_only,
        'appOnly': app_only,
        'unsettled': unsettled,
        'note': ('PayPal lists transactions up to 3 hours after they happen; recent activity can '
                 'appear as appOnly until then.'),
    }
