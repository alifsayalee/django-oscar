"""
Payment flows: place an order, authorize it, capture at fulfilment, void on
cancel, refund after fulfilment, save/remove cards, reconcile with PayPal.

Every PayPal write follows the same order: claim (a write to this app's DB that
the DB itself refuses the second time) -> PayPal call -> record the result.
A refused call releases its claim; a call whose outcome is unknown is settled
by re-reading PayPal, here or later (``settle_*`` / ``paypal_settle`` command).
"""

import calendar
import logging
import uuid
from datetime import date, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core import prices
from oscar.core.loading import get_class, get_model

from . import gateway, money
from .client import get_client
from .errors import ProviderError
from .models import CardSaveClaim, PayPalCustomer, PayPalPayment, PayPalRefund, PayPalVaultCard

logger = logging.getLogger("apps.paypal_payments")

Order = get_model("order", "Order")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
Bankcard = get_model("payment", "Bankcard")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
EventHandler = get_class("order.processing", "EventHandler")
Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")

AWAITING_PAYMENT_STATUS = "Awaiting payment"
PAID_STATUS = "Pending"
PROCESSING_STATUS = "Being processed"
COMPLETE_STATUS = "Complete"
CANCELLED_STATUS = "Cancelled"

# From the reauthorize operation's description: a 3-day honor period, after which the
# authorization can be reauthorized within its 29-day authorization period.
HONOR_PERIOD = timedelta(days=3)
AUTHORIZATION_PERIOD = timedelta(days=29)

MAX_LINES = 50
MAX_QUANTITY = 100


# --------------------------------------------------------------------------
# Errors raised to the views
# --------------------------------------------------------------------------


class PaymentError(Exception):
    """A request this app refuses itself (validation, state, ownership)."""

    def __init__(self, status_code, message, code=None, payment=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.payment = payment


class ClaimRejected(PaymentError):
    def __init__(self, payment, action):
        super().__init__(
            409,
            f"Cannot {action} while the payment is {payment.status}.",
            code="INVALID_PAYMENT_STATE",
            payment=payment,
        )


def currency():
    return str(settings.PAYPAL_CURRENCY).upper()


def _short_error(e):
    return (e.provider_error or e.message)[:255]


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------


def _claim(payment, from_statuses, to_status, action, *, new_attempt=False, **extra):
    """Atomically move the payment into an in-flight status; the DB refuses a second caller."""
    updates = {"status": to_status, "updated": timezone.now(), "last_error": "", **extra}
    if new_attempt:
        updates["attempt"] = F("attempt") + 1
    claimed = PayPalPayment.objects.filter(pk=payment.pk, status__in=from_statuses).update(**updates)
    payment.refresh_from_db()
    if not claimed:
        raise ClaimRejected(payment, action)
    return payment


def _save(payment, **fields):
    for name, value in fields.items():
        setattr(payment, name, value)
    payment.save(update_fields=[*fields.keys(), "updated"])


# --------------------------------------------------------------------------
# Oscar ledger (payment.Source / payment.Transaction)
# --------------------------------------------------------------------------


def _source(payment):
    if payment.source_id:
        return payment.source
    source_type, _ = SourceType.objects.get_or_create(name="PayPal")
    source = Source.objects.create(
        order=payment.order,
        source_type=source_type,
        currency=payment.currency,
        reference=payment.paypal_order_id,
        label=_card_label(payment.card_brand, payment.card_last_digits),
    )
    _save(payment, source=source)
    return source


def _card_label(brand, last_digits):
    if not last_digits:
        return ""
    return f"{brand or 'Card'} ending {last_digits}"


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


def place_order(user, items):
    """Place an Oscar order for ``user`` from ``[(product_id, quantity), ...]``, awaiting payment."""
    if not items:
        raise PaymentError(400, "An order needs at least one item.", code="EMPTY_ORDER")
    if len(items) > MAX_LINES:
        raise PaymentError(400, f"An order can have at most {MAX_LINES} items.", code="TOO_MANY_ITEMS")

    strategy = Selector().strategy(user=user)
    basket = Basket(owner=user)
    basket.strategy = strategy
    with transaction.atomic():
        basket.save()
        for product_id, quantity in items:
            if quantity < 1 or quantity > MAX_QUANTITY:
                raise PaymentError(400, f"Quantity must be between 1 and {MAX_QUANTITY}.", code="INVALID_QUANTITY")
            product = Product.objects.filter(pk=product_id, is_public=True).first()
            if product is None:
                raise PaymentError(400, f"Catalogue item {product_id} does not exist.", code="UNKNOWN_ITEM")
            info = strategy.fetch_for_product(product)
            if product.is_parent or not info.price.exists or info.stockrecord is None:
                raise PaymentError(400, f"Catalogue item {product_id} cannot be bought.", code="NOT_PURCHASABLE")
            allowed, reason = info.availability.is_purchase_permitted(quantity)
            if not allowed:
                raise PaymentError(409, f"Catalogue item {product_id}: {reason}", code="NOT_AVAILABLE")
            basket.add_product(product, quantity)

        shipping_method = Free()
        shipping_charge = shipping_method.calculate(basket)
        total_excl = basket.total_excl_tax + shipping_charge.excl_tax
        total_incl = basket.total_incl_tax + shipping_charge.incl_tax
        # Amounts come from catalogue prices; the currency is the configured PayPal currency.
        cur = currency()
        total = prices.Price(
            currency=cur,
            excl_tax=money.quantize(total_excl, cur),
            incl_tax=money.quantize(total_incl, cur),
        )
        if total.incl_tax <= 0:
            raise PaymentError(400, "The order total must be positive.", code="ZERO_TOTAL")
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            order_number=OrderNumberGenerator().order_number(basket),
            status=AWAITING_PAYMENT_STATUS,
        )
        basket.submit()
        PayPalPayment.objects.create(order=order, currency=cur, amount=total.incl_tax)
    return order


def orders_for(user):
    return (
        Order.objects.filter(user=user)
        .select_related("paypal_payment")
        .prefetch_related("lines", "paypal_payment__refunds")
        .order_by("-date_placed")
    )


def get_order_for(user, number):
    """The caller's own order. Another shopper's order is indistinguishable from a missing one."""
    order = Order.objects.filter(number=number, user=user).select_related("paypal_payment").first()
    if order is None:
        raise PaymentError(404, "Order not found.", code="NOT_FOUND")
    return order


def get_order_any(number):
    order = Order.objects.filter(number=number).select_related("paypal_payment").first()
    if order is None:
        raise PaymentError(404, "Order not found.", code="NOT_FOUND")
    return order


def payment_for(order):
    try:
        return order.paypal_payment
    except PayPalPayment.DoesNotExist:
        raise PaymentError(409, "This order was not placed for PayPal payment.", code="NOT_A_PAYPAL_ORDER") from None


# --------------------------------------------------------------------------
# Flow 1 - authorize
# --------------------------------------------------------------------------


def pay_order(user, order, *, card=None, bankcard_id=None):
    payment = payment_for(order)
    if payment.status == PayPalPayment.UNKNOWN and payment.pending_operation != "create":
        settle_payment(payment)

    vault_id = None
    bankcard = None
    if bankcard_id is not None:
        vault = (
            PayPalVaultCard.objects.select_related("bankcard")
            .filter(bankcard_id=bankcard_id, bankcard__user=user, state=PayPalVaultCard.ACTIVE)
            .first()
        )
        if vault is None:
            raise PaymentError(404, "Saved card not found.", code="PAYMENT_METHOD_NOT_FOUND")
        vault_id, bankcard = vault.token_id, vault.bankcard
    elif card is None:
        raise PaymentError(400, "Provide card details or a saved card.", code="NO_PAYMENT_SOURCE")

    if payment.status == PayPalPayment.UNKNOWN and payment.pending_operation == "create":
        # The previous create may have landed: re-send under the SAME PayPal-Request-Id,
        # which PayPal answers with the original order instead of creating another.
        _claim(payment, [PayPalPayment.UNKNOWN], PayPalPayment.AUTHORIZING, "pay")
    else:
        _claim(
            payment,
            [PayPalPayment.AWAITING_PAYMENT, PayPalPayment.DECLINED],
            PayPalPayment.AUTHORIZING,
            "pay",
            new_attempt=True,
            paypal_order_id="",
            paypal_order_status="",
        )

    client = get_client()
    try:
        snapshot = _create_paypal_order(client, payment, card=card, vault_id=vault_id)
    except ProviderError as e:
        if e.outcome_unknown:
            _save(payment, status=PayPalPayment.UNKNOWN, pending_operation="create", last_error=_short_error(e))
        else:
            _save(payment, status=PayPalPayment.DECLINED, pending_operation="", last_error=_short_error(e))
        raise

    _save(
        payment,
        paypal_order_id=snapshot.id,
        paypal_order_status=snapshot.status or "",
        pending_operation="",
        bankcard=bankcard,
        card_brand=snapshot.card_brand or (bankcard.card_type if bankcard else "") or "",
        card_last_digits=snapshot.card_last_digits or (bankcard.number[-4:] if bankcard else ""),
    )

    if snapshot.status == "PAYER_ACTION_REQUIRED":
        # A 3-D Secure challenge needs the shopper in a browser; this integration does not do that.
        _save(payment, status=PayPalPayment.DECLINED, last_error="PAYER_ACTION_REQUIRED")
        raise PaymentError(
            422,
            "PayPal requires the shopper to approve this card payment in a browser (3-D Secure), "
            "which this API does not support. Use a different card.",
            code="PAYER_ACTION_REQUIRED",
            payment=payment,
        )

    if not snapshot.authorizations:
        if snapshot.status not in ("APPROVED", "CREATED", "SAVED"):
            _save(payment, status=PayPalPayment.DECLINED, last_error=f"ORDER_{snapshot.status}")
            raise PaymentError(422, f"PayPal did not accept the payment (order {snapshot.status}).",
                               code="PAYMENT_DECLINED", payment=payment)
        try:
            snapshot = gateway.authorize_order(client, snapshot.id, request_id=payment.request_id("authorize"))
        except ProviderError as e:
            if not e.outcome_unknown:
                _save(payment, status=PayPalPayment.DECLINED, last_error=_short_error(e))
                raise
            _save(payment, status=PayPalPayment.UNKNOWN, pending_operation="authorize", last_error=_short_error(e))
            _settle_or_raise(payment, e, "authorize the payment")
        else:
            _apply_authorization(payment, snapshot)
    else:
        _apply_authorization(payment, snapshot)
    if payment.status == PayPalPayment.DECLINED:
        raise PaymentError(422, "The card was declined.", code="PAYMENT_DECLINED", payment=payment)
    return payment


def _create_paypal_order(client, payment, *, card, vault_id):
    order = payment.order
    kwargs = dict(
        amount=payment.amount,
        currency=payment.currency,
        reference_id=order.number,
        invoice_id=f"{_invoice_prefix(payment)}-{payment.attempt}",
        # Globally unique (the PayPal account may serve other stores whose order numbers collide).
        custom_id=str(payment.request_ns),
        description=f"Order {order.number}",
        request_id=payment.request_id("create"),
        card=card,
        vault_id=vault_id,
    )
    try:
        return gateway.create_order(client, **kwargs)
    except ProviderError as e:
        if not e.outcome_unknown:
            raise
        # Settle now: the same PayPal-Request-Id returns the original order if the first one landed.
        logger.warning("Re-sending create order for %s under the same request id", order.number)
        return gateway.create_order(client, **kwargs)


def _invoice_prefix(payment):
    return f"{payment.order.number}-{payment.request_ns.hex[:8]}"


def _latest(items):
    dated = [i for i in items if i.create_time is not None]
    if dated:
        return max(dated, key=lambda i: i.create_time)
    return items[-1] if items else None


def _apply_authorization(payment, snapshot):
    auth = _latest(snapshot.authorizations)
    if auth is None:
        _save(payment, status=PayPalPayment.UNKNOWN, pending_operation="authorize",
              last_error="PayPal returned no authorization")
        raise ProviderError(502, "PayPal returned no authorization; the outcome is not yet known.",
                            outcome_unknown=True)
    _record_authorization(payment, auth)


def _record_authorization(payment, auth, *, reauthorized=False):
    fields = dict(
        authorization_id=auth.id,
        authorization_status=auth.status or "",
        authorized_amount=auth.amount,
        authorized_at=auth.create_time,
        authorization_expires_at=auth.expiration_time,
        pending_operation="",
    )
    if auth.status in ("CREATED",):
        if not money.same_amount(payment.amount, payment.currency, auth.amount, auth.currency):
            logger.error("Authorization %s amount %s %s != order total %s %s", auth.id, auth.amount,
                         auth.currency, payment.amount, payment.currency)
            _save(payment, status=PayPalPayment.NEEDS_REVIEW,
                  last_error="Authorized amount differs from the order total", **fields)
            return
        _save(payment, status=PayPalPayment.AUTHORIZED, **fields)
        source = _source(payment)
        if reauthorized:
            Transaction.objects.create(source=source, txn_type="Reauthorise", amount=auth.amount or payment.amount,
                                       reference=auth.id, status=auth.status or "")
        else:
            source.allocate(auth.amount, reference=auth.id, status=auth.status or "")
            order = payment.order
            if order.status == AWAITING_PAYMENT_STATUS:
                order.set_status(PAID_STATUS)
    elif auth.status == "PENDING":
        # Not held yet; re-read later.
        _save(payment, status=PayPalPayment.UNKNOWN, **{**fields, "pending_operation": "authorize"})
    elif auth.status in ("DENIED", "VOIDED"):
        _save(payment, status=PayPalPayment.DECLINED, last_error=f"AUTHORIZATION_{auth.status}", **fields)
    else:
        _save(payment, status=PayPalPayment.NEEDS_REVIEW,
              last_error=f"Unexpected authorization status {auth.status}", **fields)


# --------------------------------------------------------------------------
# Flow 1 - fulfil (capture)
# --------------------------------------------------------------------------


def fulfil_order(order):
    payment = payment_for(order)
    if payment.status in (PayPalPayment.UNKNOWN, PayPalPayment.CAPTURE_PENDING):
        settle_payment(payment)
    _claim(payment, [PayPalPayment.AUTHORIZED], PayPalPayment.CAPTURING, "fulfil", new_attempt=True)
    client = get_client()
    now = timezone.now()

    if payment.authorization_expires_at and now >= payment.authorization_expires_at:
        _save(payment, status=PayPalPayment.EXPIRED, last_error="AUTHORIZATION_EXPIRED")
        raise PaymentError(
            409,
            f"The payment authorization expired on {payment.authorization_expires_at.isoformat()} and can no "
            "longer be renewed or captured. Cancel this order and ask the shopper to place and pay a new one.",
            code="AUTHORIZATION_EXPIRED",
            payment=payment,
        )

    renewal_refused = None
    if payment.authorized_at and now - payment.authorized_at > HONOR_PERIOD:
        renewal_refused = _renew_authorization(client, payment)

    try:
        captured = gateway.capture(
            client,
            payment.authorization_id,
            amount=payment.amount,
            currency=payment.currency,
            invoice_id=f"{_invoice_prefix(payment)}-C",
            request_id=payment.request_id("capture"),
        )
    except ProviderError as e:
        if e.outcome_unknown:
            _save(payment, status=PayPalPayment.UNKNOWN, pending_operation="capture", last_error=_short_error(e))
            return _settle_or_raise(payment, e, "capture the payment")
        _save(payment, status=PayPalPayment.AUTHORIZED, last_error=_short_error(e))
        if renewal_refused is not None:
            raise PaymentError(
                409,
                "The payment authorization is past its honor period, PayPal refused to renew it "
                f"({renewal_refused}) and refused the capture ({e.provider_error or e.status_code}). "
                "Cancel this order to release the shopper's funds and ask them to pay again.",
                code="AUTHORIZATION_STALE",
                payment=payment,
            ) from e
        raise
    _record_capture(payment, captured)
    return payment


def _renew_authorization(client, payment):
    """Reauthorize a stale authorization. Returns PayPal's refusal reason, or None when renewed."""
    try:
        renewed = gateway.reauthorize(
            client,
            payment.authorization_id,
            amount=payment.amount,
            currency=payment.currency,
            request_id=payment.request_id("reauthorize"),
        )
    except ProviderError as e:
        if not e.outcome_unknown:
            logger.warning("Reauthorization of %s refused: %s", payment.authorization_id, e.provider_error)
            return e.provider_error or str(e.status_code)
        previous_id = payment.authorization_id
        _save(payment, status=PayPalPayment.UNKNOWN, pending_operation="reauthorize", last_error=_short_error(e))
        _try_settle(payment)
        if payment.status != PayPalPayment.AUTHORIZED:
            raise
        # Settled: PayPal either holds a new authorization or never renewed the old one.
        renewal = None if payment.authorization_id != previous_id else "renewal did not take effect"
        _claim(payment, [PayPalPayment.AUTHORIZED], PayPalPayment.CAPTURING, "fulfil")
        return renewal
    if renewed.status != "CREATED":
        return f"authorization {renewed.status}"
    _save(payment, reauthorization_count=payment.reauthorization_count + 1)
    _record_authorization(payment, renewed, reauthorized=True)
    if payment.status != PayPalPayment.AUTHORIZED:
        raise PaymentError(409, "The renewed authorization needs review before capture.",
                           code="NEEDS_REVIEW", payment=payment)
    # _record_authorization released the claim; take it back atomically before capturing.
    _claim(payment, [PayPalPayment.AUTHORIZED], PayPalPayment.CAPTURING, "fulfil")
    return None


def _record_capture(payment, cap):
    fields = dict(
        capture_id=cap.id,
        capture_status=cap.status or "",
        captured_amount=cap.amount,
        paypal_fee=cap.paypal_fee,
        net_amount=cap.net_amount,
        captured_at=cap.create_time,
        pending_operation="",
    )
    if cap.status in ("COMPLETED", "PENDING", "PARTIALLY_REFUNDED", "REFUNDED"):
        if not money.same_amount(payment.amount, payment.currency, cap.amount, cap.currency):
            logger.error("Capture %s amount %s %s != order total %s", cap.id, cap.amount, cap.currency, payment.amount)
            _save(payment, status=PayPalPayment.NEEDS_REVIEW,
                  last_error="Captured amount differs from the order total", **fields)
            return
        previous = payment.status
        if cap.status == "PENDING":
            _save(payment, status=PayPalPayment.CAPTURE_PENDING, **fields)
            return
        _save(payment, status=PayPalPayment.CAPTURED, **fields)
        _save(payment, status=_refund_status(payment))
        if previous in (PayPalPayment.CAPTURING, PayPalPayment.UNKNOWN, PayPalPayment.CAPTURE_PENDING):
            _source(payment).debit(cap.amount, reference=cap.id, status=cap.status or "")
            order = payment.order
            EventHandler().consume_stock_allocations(order)
            for step in (PROCESSING_STATUS, COMPLETE_STATUS):
                if step in order.available_statuses():
                    order.set_status(step)
    elif cap.status in ("DECLINED", "FAILED"):
        _save(payment, status=PayPalPayment.NEEDS_REVIEW, last_error=f"CAPTURE_{cap.status}", **fields)
    else:
        _save(payment, status=PayPalPayment.NEEDS_REVIEW, last_error=f"Unexpected capture status {cap.status}",
              **fields)


# --------------------------------------------------------------------------
# Flow 1 - cancel (void)
# --------------------------------------------------------------------------


def cancel_order(order):
    payment = payment_for(order)
    if payment.status == PayPalPayment.UNKNOWN:
        settle_payment(payment)
    previous = payment.status
    _claim(
        payment,
        [PayPalPayment.AWAITING_PAYMENT, PayPalPayment.DECLINED, PayPalPayment.AUTHORIZED, PayPalPayment.EXPIRED],
        PayPalPayment.VOIDING,
        "cancel",
        new_attempt=True,
    )
    if (
        previous == PayPalPayment.EXPIRED
        or not payment.authorization_id
        or payment.authorization_status not in ("CREATED", "PENDING")
    ):
        # Nothing is held at PayPal (never authorized, declined, or the hold already lapsed).
        _save(payment, status=PayPalPayment.CANCELLED)
        _cancel_oscar_order(payment)
        return payment

    client = get_client()
    try:
        voided = gateway.void(client, payment.authorization_id, request_id=payment.request_id("void"))
        if voided is None:  # PayPal answered without a body: read the authorization back
            voided = gateway.get_authorization(client, payment.authorization_id)
    except ProviderError as e:
        if e.outcome_unknown:
            _save(payment, status=PayPalPayment.UNKNOWN, pending_operation="void", last_error=_short_error(e))
            return _settle_or_raise(payment, e, "release the held funds")
        _save(payment, status=PayPalPayment.AUTHORIZED, last_error=_short_error(e))
        raise
    _record_void(payment, voided)
    return payment


def _record_void(payment, auth):
    if auth.status == "VOIDED":
        _save(payment, status=PayPalPayment.VOIDED, authorization_status="VOIDED", pending_operation="")
        Transaction.objects.create(source=_source(payment), txn_type="Void", amount=payment.authorized_amount or 0,
                                   reference=auth.id, status="VOIDED")
        _cancel_oscar_order(payment)
    else:
        _save(payment, status=PayPalPayment.NEEDS_REVIEW, authorization_status=auth.status or "",
              pending_operation="", last_error=f"Void left authorization {auth.status}")


def _cancel_oscar_order(payment):
    order = payment.order
    if CANCELLED_STATUS in order.available_statuses():
        EventHandler().cancel_stock_allocations(order)
        order.set_status(CANCELLED_STATUS)


# --------------------------------------------------------------------------
# Flow 1 - refunds
# --------------------------------------------------------------------------


def _refund_status(payment):
    if payment.captured_amount is not None and payment.refunded_amount >= payment.captured_amount:
        return PayPalPayment.REFUNDED
    if payment.refunded_amount > 0:
        return PayPalPayment.PARTIALLY_REFUNDED
    return PayPalPayment.CAPTURED


def refund_order(user, order, *, idempotency_key, amount=None):
    """Refund all or part of the capture. Returns (refund, created)."""
    payment = payment_for(order)
    if payment.status == PayPalPayment.UNKNOWN:
        settle_payment(payment)
    refundable_states = [PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED]
    if amount is not None:
        amount = money.quantize(amount, payment.currency)
        if amount <= 0:
            raise PaymentError(400, "The refund amount must be positive.", code="INVALID_AMOUNT")
    requested = amount

    try:
        with transaction.atomic():
            if amount is None:
                # A full refund: whatever remains un-refunded at this moment.
                payment.refresh_from_db()
                if payment.captured_amount is None:
                    raise ClaimRejected(payment, "refund")
                amount = payment.captured_amount - payment.refund_reserved
                if amount <= 0:
                    raise PaymentError(409, "Nothing is left to refund.", code="REFUND_EXCEEDS_CAPTURE",
                                       payment=payment)
            refund = PayPalRefund.objects.create(
                payment=payment, idempotency_key=idempotency_key, amount=amount, requested_by=user
            )
            # The reservation is one conditional UPDATE: the DB refuses any refund that would
            # take the total past what was captured, however many requests race.
            reserved = PayPalPayment.objects.filter(
                pk=payment.pk,
                status__in=refundable_states,
                refund_reserved__lte=F("captured_amount") - amount,
            ).update(refund_reserved=F("refund_reserved") + amount, updated=timezone.now())
            if not reserved:
                payment.refresh_from_db()
                if payment.status not in refundable_states:
                    raise ClaimRejected(payment, "refund")
                remaining = (payment.captured_amount or 0) - payment.refund_reserved
                raise PaymentError(
                    409,
                    f"Refund of {amount} would exceed the captured amount; at most {remaining} can still be refunded.",
                    code="REFUND_EXCEEDS_CAPTURE",
                    payment=payment,
                )
    except IntegrityError as claimed:
        existing = PayPalRefund.objects.get(payment=payment, idempotency_key=idempotency_key)
        if requested is not None and existing.amount != requested:
            raise PaymentError(
                409, "This idempotency key was already used for a different refund.",
                code="IDEMPOTENCY_KEY_REUSED", payment=payment,
            ) from claimed
        if existing.status == PayPalRefund.UNKNOWN:
            settle_refund(existing)
        return existing, False

    payment.refresh_from_db()
    client = get_client()
    try:
        result = gateway.refund(
            client,
            payment.capture_id,
            amount=refund.amount,
            currency=payment.currency,
            custom_id=str(refund.reference),
            request_id=refund.request_id(),
        )
    except ProviderError as e:
        if e.outcome_unknown:
            _save_refund(refund, status=PayPalRefund.UNKNOWN, last_error=_short_error(e))
            _try_settle_refund(refund)
            if refund.status in (PayPalRefund.COMPLETED, PayPalRefund.PENDING):
                return refund, True
            if refund.status == PayPalRefund.UNKNOWN:
                raise
            raise ProviderError(502, "PayPal did not make the refund; nothing was refunded. Try again.") from e
        _save_refund(refund, status=PayPalRefund.FAILED, last_error=_short_error(e))
        _release_refund(refund)
        raise
    _record_refund(refund, result)
    if refund.status in (PayPalRefund.FAILED, PayPalRefund.CANCELLED):
        raise PaymentError(422, f"PayPal did not complete the refund (status {refund.paypal_status}).",
                           code="REFUND_FAILED", payment=payment)
    return refund, True


def _save_refund(refund, **fields):
    for name, value in fields.items():
        setattr(refund, name, value)
    refund.save(update_fields=[*fields.keys(), "updated"])


def _release_refund(refund):
    PayPalPayment.objects.filter(pk=refund.payment_id).update(
        refund_reserved=F("refund_reserved") - refund.amount, updated=timezone.now()
    )


def _record_refund(refund, snap):
    fields = dict(paypal_refund_id=snap.id, paypal_status=snap.status or "", refunded_at=snap.create_time)
    payment = refund.payment
    if snap.status == "COMPLETED":
        with transaction.atomic():
            _save_refund(refund, status=PayPalRefund.COMPLETED, **fields)
            PayPalPayment.objects.filter(pk=payment.pk).update(
                refunded_amount=F("refunded_amount") + refund.amount, updated=timezone.now()
            )
            payment.refresh_from_db()
            _save(payment, status=_refund_status(payment))
            _source(payment).refund(refund.amount, reference=snap.id, status=snap.status)
    elif snap.status == "PENDING":
        _save_refund(refund, status=PayPalRefund.PENDING, **fields)
    elif snap.status in ("FAILED", "CANCELLED"):
        _save_refund(refund, status=getattr(PayPalRefund, snap.status), **fields)
        _release_refund(refund)
    else:
        _save_refund(refund, status=PayPalRefund.UNKNOWN, last_error=f"Unexpected refund status {snap.status}",
                     **fields)


# --------------------------------------------------------------------------
# Settling unknown outcomes (re-read PayPal; never blind re-send)
# --------------------------------------------------------------------------


def _try_settle(payment):
    try:
        settle_payment(payment)
    except ProviderError:
        logger.warning("Could not settle payment %s yet; left %s", payment.pk, payment.status)


def _settle_or_raise(payment, error, action):
    """Called in the except of a write whose outcome is unknown: re-read PayPal right away.

    Returns the payment when PayPal shows the write happened; raises the original
    (unknown-outcome) error when PayPal could not be read; raises a definite error
    when PayPal shows the write did not happen.
    """
    wanted = {
        "authorize the payment": (PayPalPayment.AUTHORIZED, PayPalPayment.NEEDS_REVIEW),
        "capture the payment": (
            PayPalPayment.CAPTURED, PayPalPayment.CAPTURE_PENDING, PayPalPayment.PARTIALLY_REFUNDED,
            PayPalPayment.REFUNDED, PayPalPayment.NEEDS_REVIEW,
        ),
        "release the held funds": (PayPalPayment.VOIDED, PayPalPayment.CANCELLED, PayPalPayment.NEEDS_REVIEW),
    }[action]
    _try_settle(payment)
    if payment.status in wanted:
        return payment
    if payment.status == PayPalPayment.UNKNOWN:
        raise error
    raise ProviderError(502, f"PayPal did not {action}; nothing changed. Try again.") from error


def _try_settle_refund(refund):
    try:
        settle_refund(refund)
    except ProviderError:
        logger.warning("Could not settle refund %s yet", refund.pk)


def settle_payment(payment):
    """Bring a payment whose last PayPal write has an unknown outcome back in line with PayPal."""
    client = get_client()
    op = payment.pending_operation
    if payment.status == PayPalPayment.CAPTURE_PENDING and payment.capture_id:
        _record_capture(payment, gateway.get_capture(client, payment.capture_id))
        return payment
    if payment.status != PayPalPayment.UNKNOWN:
        return payment
    if op == "create" or not payment.paypal_order_id:
        # Nothing to re-read by: the shopper's next pay re-sends under the same request id.
        return payment
    if op == "void":
        auth = gateway.get_authorization(client, payment.authorization_id)
        if auth.status == "VOIDED":
            _record_void(payment, auth)
        else:
            _save(payment, status=PayPalPayment.AUTHORIZED, pending_operation="", authorization_status=auth.status or "")
        return payment

    snapshot = gateway.get_order(client, payment.paypal_order_id)
    _save(payment, paypal_order_status=snapshot.status or "")
    if op == "authorize":
        if snapshot.authorizations:
            _record_authorization(payment, _latest(snapshot.authorizations))
        else:
            # PayPal holds no authorization: the attempt did not happen; the shopper may pay again.
            _save(payment, status=PayPalPayment.DECLINED, pending_operation="",
                  last_error="Authorization did not complete")
        return payment
    if op in ("capture", "reauthorize"):
        if snapshot.captures:
            _record_capture(payment, _latest(snapshot.captures))
        else:
            auth = _latest(snapshot.authorizations)
            if auth is not None and auth.id != payment.authorization_id and auth.status == "CREATED":
                _save(payment, reauthorization_count=payment.reauthorization_count + 1)
                _record_authorization(payment, auth, reauthorized=True)
            else:
                _save(payment, status=PayPalPayment.AUTHORIZED, pending_operation="")
        return payment
    return payment


def settle_refund(refund):
    payment = refund.payment
    snapshot = gateway.get_order(get_client(), payment.paypal_order_id)
    match = next((r for r in snapshot.refunds if r.custom_id == str(refund.reference)), None)
    if match is None:
        # PayPal lists this capture's refunds and ours is not among them: it never happened.
        _save_refund(refund, status=PayPalRefund.FAILED, last_error="Refund not found at PayPal")
        _release_refund(refund)
    else:
        _record_refund(refund, match)
    return refund


def settle_card_save(claim):
    customer = PayPalCustomer.objects.filter(user=claim.user).first()
    if customer is None:
        return claim  # no customer to list by; stays UNKNOWN for review
    known = set(PayPalVaultCard.objects.values_list("token_id", flat=True))
    for card in gateway.list_vaulted_cards(get_client(), customer.customer_id):
        if card.token_id in known:
            continue
        if card.last_digits == claim.card_last_digits and card.expiry == claim.card_expiry:
            claim.bankcard = _store_vaulted_card(claim.user, card)
            claim.state = CardSaveClaim.SAVED
            claim.save(update_fields=["bankcard", "state", "updated"])
            return claim
    claim.delete()  # PayPal has no such card: the save did not happen
    return None


def settle_all():
    """Re-drive every record left in an unknown or in-between state. Returns a count."""
    count = 0
    for payment in PayPalPayment.objects.filter(status__in=[PayPalPayment.UNKNOWN, PayPalPayment.CAPTURE_PENDING]):
        _try_settle(payment)
        count += 1
    for refund in PayPalRefund.objects.filter(status__in=[PayPalRefund.UNKNOWN, PayPalRefund.PENDING]):
        if refund.status == PayPalRefund.PENDING:
            refund.status = PayPalRefund.UNKNOWN  # re-read it the same way
        _try_settle_refund(refund)
        count += 1
    for claim in CardSaveClaim.objects.filter(state=CardSaveClaim.UNKNOWN):
        try:
            settle_card_save(claim)
        except ProviderError:
            pass
        count += 1
    for vault in PayPalVaultCard.objects.filter(state=PayPalVaultCard.DELETING).select_related("bankcard"):
        try:
            delete_card(vault.bankcard.user, vault.bankcard_id)
        except (ProviderError, PaymentError):
            pass
        count += 1
    return count


# --------------------------------------------------------------------------
# Flow 2 - saved cards
# --------------------------------------------------------------------------


def _expiry_date(expiry):
    year, month = (int(p) for p in expiry.split("-")[:2])
    return date(year, month, calendar.monthrange(year, month)[1])


def _store_vaulted_card(user, card):
    expiry = card.expiry or ""
    bankcard = Bankcard(
        user=user,
        number=f"XXXX-XXXX-XXXX-{card.last_digits or '????'}",
        expiry_date=_expiry_date(expiry) if expiry else date.today(),
    )
    bankcard.card_type = card.brand or "Card"
    bankcard.save()
    PayPalVaultCard.objects.create(bankcard=bankcard, token_id=card.token_id, customer_id=card.customer_id or "")
    return bankcard


def save_card(user, card, idempotency_key=None):
    """Vault a card at PayPal and keep only its masked description here. Returns (bankcard, created)."""
    key = idempotency_key or uuid.uuid4().hex
    try:
        with transaction.atomic():
            claim = CardSaveClaim.objects.create(
                user=user, idempotency_key=key, card_last_digits=card.number[-4:], card_expiry=card.expiry
            )
    except IntegrityError as claimed:
        claim = CardSaveClaim.objects.get(user=user, idempotency_key=key)
        if claim.state == CardSaveClaim.UNKNOWN:
            claim = settle_card_save(claim)
        if claim is not None and claim.state == CardSaveClaim.SAVED and claim.bankcard_id:
            return claim.bankcard, False
        raise PaymentError(409, "A card save with this idempotency key is still in progress.",
                           code="SAVE_IN_PROGRESS") from claimed

    customer = PayPalCustomer.objects.filter(user=user).first()
    client = get_client()
    request_id = f"{claim.request_ns}-vault"
    try:
        try:
            vaulted = gateway.vault_card(
                client, card, customer_id=customer.customer_id if customer else None, request_id=request_id
            )
        except ProviderError as e:
            if not e.outcome_unknown:
                raise
            # Settle now, while the card is still in memory: same PayPal-Request-Id, same body.
            vaulted = gateway.vault_card(
                client, card, customer_id=customer.customer_id if customer else None, request_id=request_id
            )
    except ProviderError as e:
        if e.outcome_unknown:
            claim.state = CardSaveClaim.UNKNOWN
            claim.save(update_fields=["state", "updated"])
        else:
            claim.delete()  # release: PayPal refused, nothing was saved
        raise

    with transaction.atomic():
        if customer is None and vaulted.customer_id:
            PayPalCustomer.objects.get_or_create(user=user, defaults={"customer_id": vaulted.customer_id})
        existing = PayPalVaultCard.objects.filter(token_id=vaulted.token_id).select_related("bankcard").first()
        bankcard = existing.bankcard if existing else _store_vaulted_card(user, vaulted)
        claim.bankcard = bankcard
        claim.state = CardSaveClaim.SAVED
        claim.save(update_fields=["bankcard", "state", "updated"])
    return bankcard, True


def cards_for(user):
    return (
        Bankcard.objects.filter(user=user, paypal_vault__state=PayPalVaultCard.ACTIVE)
        .select_related("paypal_vault")
        .order_by("-paypal_vault__created")
    )


def delete_card(user, bankcard_id):
    """Remove a saved card. It stops being listable and usable as soon as the claim is taken."""
    vault = PayPalVaultCard.objects.filter(bankcard_id=bankcard_id, bankcard__user=user).first()
    if vault is None:
        raise PaymentError(404, "Saved card not found.", code="PAYMENT_METHOD_NOT_FOUND")
    PayPalVaultCard.objects.filter(pk=vault.pk).update(state=PayPalVaultCard.DELETING, updated=timezone.now())
    try:
        gateway.delete_vaulted_card(get_client(), vault.token_id)
    except ProviderError as e:
        if not e.outcome_unknown:
            PayPalVaultCard.objects.filter(pk=vault.pk).update(state=PayPalVaultCard.ACTIVE, updated=timezone.now())
        raise
    Bankcard.objects.filter(pk=bankcard_id, user=user).delete()


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def _in(ts, start, end):
    return ts is not None and start <= ts < end


def _our_order(row, by_reference):
    if row.custom_field and row.custom_field in by_reference:
        return by_reference[row.custom_field]
    if row.invoice_id and "-" in row.invoice_id:
        return by_reference.get(row.invoice_id.rsplit("-", 1)[0])
    return None


def reconcile(start, end):
    """Line PayPal's transaction record for [start, end) up against this app's payments."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt_timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt_timezone.utc)

    # Our side, keyed by the PayPal id of each money movement; filtered by PayPal's own times.
    local = {}
    unsettled = []
    by_reference = {}
    for p in PayPalPayment.objects.exclude(paypal_order_id="").select_related("order").prefetch_related("refunds"):
        number = p.order.number
        by_reference[str(p.request_ns)] = number
        by_reference[_invoice_prefix(p)] = number
        if p.status in (PayPalPayment.UNKNOWN, PayPalPayment.CAPTURE_PENDING, PayPalPayment.AUTHORIZING,
                        PayPalPayment.CAPTURING, PayPalPayment.VOIDING, PayPalPayment.NEEDS_REVIEW):
            unsettled.append({"orderId": number, "kind": "payment", "status": p.status,
                              "pendingOperation": p.pending_operation or None})
        if p.authorization_id and _in(p.authorized_at, start, end):
            local[p.authorization_id] = {"orderId": number, "kind": "authorization",
                                         "amount": p.authorized_amount, "at": p.authorized_at}
        if p.capture_id and _in(p.captured_at, start, end):
            local[p.capture_id] = {"orderId": number, "kind": "capture", "amount": p.captured_amount,
                                   "fee": p.paypal_fee, "at": p.captured_at}
        for r in p.refunds.all():
            if r.status in (PayPalRefund.UNKNOWN, PayPalRefund.PENDING, PayPalRefund.SUBMITTING):
                unsettled.append({"orderId": number, "kind": "refund", "refundId": str(r.reference),
                                  "status": r.status})
            if r.paypal_refund_id and _in(r.refunded_at, start, end):
                local[r.paypal_refund_id] = {"orderId": number, "kind": "refund", "amount": r.amount,
                                             "at": r.refunded_at}

    matched, provider_only, mismatched = [], [], []
    seen = set()
    for row in gateway.search_transactions(get_client(), start, end):
        if row.initiated_at is not None and not _in(row.initiated_at, start, end):
            continue
        entry = {
            "transactionId": row.transaction_id,
            "eventCode": row.event_code,
            "status": row.status,
            "initiatedAt": row.initiated_at.isoformat() if row.initiated_at else None,
            "amount": str(row.amount) if row.amount is not None else None,
            "currency": row.currency,
            "fee": str(row.fee) if row.fee is not None else None,
            "invoiceId": row.invoice_id,
            "customField": row.custom_field,
        }
        ours = local.get(row.transaction_id or "")
        if ours is not None:
            seen.add(row.transaction_id)
            entry.update(orderId=ours["orderId"], kind=ours["kind"])
            if ours["amount"] is not None and row.amount is not None and abs(row.amount) != ours["amount"]:
                entry["localAmount"] = str(ours["amount"])
                mismatched.append(entry)
            else:
                matched.append(entry)
        elif (number := _our_order(row, by_reference)) is not None:
            # PayPal ties it to one of our payments by reference, under an id we no longer hold
            # (a superseded authorization, a void, a declined attempt).
            entry.update(orderId=number, kind="related")
            matched.append(entry)
        else:
            provider_only.append(entry)

    app_only = [
        {"transactionId": pid, "orderId": v["orderId"], "kind": v["kind"],
         "amount": str(v["amount"]) if v["amount"] is not None else None, "at": v["at"].isoformat()}
        for pid, v in local.items() if pid not in seen
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "currency": currency(),
        "summary": {
            "matched": len(matched),
            "amountMismatches": len(mismatched),
            "paypalOnly": len(provider_only),
            "appOnly": len(app_only),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "amountMismatches": mismatched,
        "paypalOnly": provider_only,
        "appOnly": app_only,
        "unsettled": unsettled,
        "note": "PayPal's transaction reporting can lag live activity by up to three hours; "
                "recent app-side movements may appear under appOnly until PayPal reports them.",
    }
