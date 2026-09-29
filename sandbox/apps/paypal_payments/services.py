"""
Order, payment and saved-card operations.

Every PayPal write carries a PayPal-Request-Id that is committed to the
database *before* the call is made. A repeated request (double click, client
retry after a timeout) therefore replays the same key, and PayPal answers with
the original result instead of acting twice. A key is only discarded once
PayPal has definitively rejected the request.

Callers must run these functions outside a surrounding transaction (the views
are ``non_atomic_requests``) so a committed key survives a failed PayPal call.
"""

import calendar
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import gateway as gw
from .gateway import CardInput, PayPalError
from .models import PayPalCustomer, PayPalPayment, PayPalRefund
from .strategy import ConfiguredCurrencyStrategy

logger = logging.getLogger("apps.paypal_payments")

Order = get_model("order", "Order")
ShippingAddress = get_model("order", "ShippingAddress")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
Bankcard = get_model("payment", "Bankcard")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Repository = get_class("shipping.repository", "Repository")
Applicator = get_class("offer.applicator", "Applicator")
EventHandler = get_class("order.processing", "EventHandler")

# Order statuses (see OSCAR_ORDER_STATUS_PIPELINE in sandbox/settings.py)
PENDING_PAYMENT = "Pending payment"
AUTHORISED = "Authorised"
COMPLETE = "Complete"
CANCELLED = "Cancelled"

SOURCE_TYPE_NAME = "PayPal"

# PayPal honours an authorization for three days; after that it has to be
# reauthorized (possible up to day 29) before it is captured.
HONOR_PERIOD = timedelta(days=3)

# Transaction Search can lag live activity by up to three hours.
REPORTING_LAG = timedelta(hours=3)

_KEY_NAMESPACE = uuid.UUID("0b8a7c1e-58c5-4d0f-9a8c-2f1d8b2c6e11")

MAX_LINE_QUANTITY = 100


class ServiceError(Exception):
    def __init__(self, http_status, code, message, **extra):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.extra = extra


def _from_paypal(e, *, retry_hint):
    """Translate a gateway failure into the API's error shape."""
    extra = {}
    if e.issue:
        extra["paypalIssue"] = e.issue
    if e.debug_id:
        extra["paypalDebugId"] = e.debug_id
    message = e.message
    if e.outcome_unknown:
        extra["outcomeUnknown"] = True
        message = "%s %s" % (message, retry_hint)
    return ServiceError(e.http_status, e.code, message, **extra)


def _lock_payment(payment_pk):
    """
    Take the write lock on a payment row for the rest of the transaction.

    The no-op UPDATE makes SQLite take its write lock first (so no two
    transactions read the same pre-state), and ``select_for_update`` does the
    same on databases that support row locks.
    """
    PayPalPayment.objects.filter(pk=payment_pk).update(date_updated=timezone.now())
    return (
        PayPalPayment.objects.select_for_update()
        .select_related("order")
        .get(pk=payment_pk)
    )


def _lock_order(order_pk):
    Order.objects.filter(pk=order_pk).update(status=F("status"))
    return Order.objects.select_for_update().get(pk=order_pk)


# ======
# Orders
# ======


@dataclass
class AddressData:
    first_name: str
    last_name: str
    line1: str
    line2: str
    city: str
    state: str
    postcode: str
    country_code: str
    phone_number: str = ""


def owned_order(user, number):
    try:
        return Order.objects.get(number=number, user=user)
    except Order.DoesNotExist:
        # Not found and not yours look the same to the caller.
        raise ServiceError(404, "order_not_found", "No such order.")


def any_order(number):
    try:
        return Order.objects.get(number=number)
    except Order.DoesNotExist:
        raise ServiceError(404, "order_not_found", "No such order.")


def place_order(user, items, shipping_address=None):
    """
    Place an Oscar order for catalogue items, awaiting payment.

    ``items`` is a list of (product_id, quantity). Prices come from the
    catalogue; the currency from configuration.
    """
    currency = gw.configured_currency()
    quantities = {}
    for product_id, quantity in items:
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    if any(q > MAX_LINE_QUANTITY for q in quantities.values()):
        raise ServiceError(
            422,
            "quantity_too_large",
            "At most %d of an item per order." % MAX_LINE_QUANTITY,
        )

    products = {p.pk: p for p in Product.objects.filter(pk__in=quantities.keys())}
    missing = [pid for pid in quantities if pid not in products]
    if missing:
        raise ServiceError(
            422, "unknown_items", "Unknown catalogue items.", itemIds=missing
        )

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = ConfiguredCurrencyStrategy()
        for product_id, quantity in quantities.items():
            product = products[product_id]
            if not product.is_public or product.is_parent:
                raise ServiceError(
                    422,
                    "item_not_purchasable",
                    "Item %s cannot be bought directly." % product_id,
                    itemId=product_id,
                )
            info = basket.strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted or not info.price.exists:
                raise ServiceError(
                    409,
                    "item_unavailable",
                    str(reason or "Item is not available."),
                    itemId=product_id,
                )
            basket.add_product(product, quantity)

        Applicator().apply(basket, user)

        address = None
        if shipping_address is not None:
            try:
                country = Country.objects.get(
                    iso_3166_1_a2=shipping_address.country_code.upper()
                )
            except Country.DoesNotExist:
                raise ServiceError(422, "invalid_country", "Unknown shipping country.")
            address = ShippingAddress(
                first_name=shipping_address.first_name,
                last_name=shipping_address.last_name,
                line1=shipping_address.line1,
                line2=shipping_address.line2,
                line4=shipping_address.city,
                state=shipping_address.state,
                postcode=shipping_address.postcode,
                country=country,
                phone_number=shipping_address.phone_number or "",
            )
        elif basket.is_shipping_required():
            raise ServiceError(
                422, "shipping_address_required", "These items need a shippingAddress."
            )

        method = Repository().get_default_shipping_method(
            basket=basket, user=user, shipping_addr=address
        )
        shipping_charge = method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        if total.incl_tax is None or total.incl_tax <= 0:
            raise ServiceError(
                422, "nothing_to_pay", "The order total must be positive."
            )
        if basket.currency != currency:
            raise ServiceError(
                409,
                "currency_mismatch",
                "Basket currency does not match configuration.",
            )
        if address is not None:
            address.save()

        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=address,
            status=PENDING_PAYMENT,
        )
        basket.submit()
    logger.info("Placed order %s for user %s", order.number, user.pk)
    return order


# =====================
# Pay (authorize / hold)
# =====================


def _source_type():
    source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
    return source_type


def _record_txn(source, txn_type, amount, reference, status):
    Transaction.objects.create(
        source=source,
        txn_type=txn_type,
        amount=amount,
        reference=reference,
        status=status,
    )


def pay(user, number, *, card=None, payment_method_id=None):
    """Authorize (hold) the order total on a one-off card or a saved card."""
    if (card is None) == (payment_method_id is None):
        raise ServiceError(
            422, "payment_source_required", "Send either card or paymentMethodId."
        )
    order = owned_order(user, number)
    currency = gw.configured_currency()

    bankcard = None
    if payment_method_id is not None:
        bankcard = _owned_bankcard(user, payment_method_id)

    with transaction.atomic():
        payment, _ = PayPalPayment.objects.get_or_create(
            order=order,
            defaults={"amount": order.total_incl_tax, "currency": order.currency},
        )
        payment = _lock_payment(payment.pk)
        order = payment.order
        if payment.status not in (
            PayPalPayment.PENDING,
            PayPalPayment.DECLINED,
            PayPalPayment.EXPIRED,
        ):
            # Already paid for: a repeat is answered with the current state.
            return payment, False
        if order.status != PENDING_PAYMENT:
            raise ServiceError(409, "order_not_payable", "Order is %s." % order.status)
        if order.currency != currency:
            raise ServiceError(
                409,
                "currency_mismatch",
                "Order is in %s but payments are configured for %s."
                % (order.currency, currency),
            )
        payment.amount = order.total_incl_tax
        payment.currency = order.currency
        if not payment.authorize_request_id:
            payment.authorize_request_id = str(uuid.uuid4())
            payment.invoice_id = "%s-%s" % (
                order.number,
                payment.authorize_request_id[:8],
            )
        payment.status = PayPalPayment.PENDING
        payment.bankcard = bankcard
        payment.save()
        request_id = payment.authorize_request_id
        invoice_id = payment.invoice_id

    try:
        result = gw.get_gateway().authorize(
            request_id=request_id,
            amount=payment.amount,
            currency=payment.currency,
            order_number=order.number,
            invoice_id=invoice_id,
            description="Order %s" % order.number,
            card=card,
            vault_id=bankcard.partner_reference if bankcard else None,
        )
    except PayPalError as e:
        if e.outcome_unknown:
            PayPalPayment.objects.filter(pk=payment.pk).update(
                last_error=e.message[:255]
            )
            raise _from_paypal(
                e, retry_hint="Repeat the same pay request to settle it."
            )
        _fail_attempt(payment.pk, e.message)
        if e.http_status == 422:
            raise ServiceError(
                402,
                "payment_declined",
                "PayPal declined the payment: %s" % e.message,
                **({"paypalIssue": e.issue} if e.issue else {}),
            )
        raise _from_paypal(e, retry_hint="")

    if result.order_status == "PAYER_ACTION_REQUIRED":
        _fail_attempt(payment.pk, "PAYER_ACTION_REQUIRED")
        raise ServiceError(
            402,
            "card_requires_browser_authentication",
            "PayPal requires the cardholder to complete a browser-based authentication "
            "challenge for this card, which this API does not support.",
        )
    if result.authorization_id is None or result.authorization_status not in (
        "CREATED",
        "PENDING",
    ):
        _fail_attempt(payment.pk, "authorization %s" % result.authorization_status)
        raise ServiceError(
            402,
            "payment_declined",
            "PayPal did not authorize the payment (status %s)."
            % (result.authorization_status or result.order_status),
        )
    if (
        result.currency != payment.currency
        or gw.quantize(result.amount, payment.currency) != payment.amount
    ):
        # Never keep a hold that differs from the order total.
        logger.error(
            "Authorization %s for order %s held %s %s, expected %s %s; voiding",
            result.authorization_id,
            order.number,
            result.amount,
            result.currency,
            payment.amount,
            payment.currency,
        )
        try:
            gw.get_gateway().void(result.authorization_id, request_id=str(uuid.uuid4()))
        except PayPalError:
            logger.exception(
                "Could not void mismatched authorization %s", result.authorization_id
            )
        _fail_attempt(payment.pk, "amount mismatch")
        raise ServiceError(
            502,
            "amount_mismatch",
            "PayPal held a different amount; the hold was released.",
        )

    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        if payment.authorization_id == result.authorization_id:
            return payment, False  # a concurrent duplicate already recorded it
        order = _lock_order(payment.order_id)
        source = Source.objects.create(
            order=order,
            source_type=_source_type(),
            currency=payment.currency,
            reference=result.authorization_id,
            label=_card_label(result.card_brand, result.card_last_digits),
        )
        source.allocate(
            payment.amount,
            reference=result.authorization_id,
            status=result.authorization_status,
        )
        now = timezone.now()
        payment.source = source
        payment.status = PayPalPayment.AUTHORIZED
        payment.paypal_order_id = result.paypal_order_id
        payment.authorization_id = result.authorization_id
        payment.authorization_status = result.authorization_status
        payment.authorized_at = result.created_at or now
        payment.honor_period_started_at = result.created_at or now
        payment.authorization_expires_at = result.expires_at
        payment.card_brand = result.card_brand or (
            bankcard.card_type if bankcard else ""
        )
        payment.card_last_digits = result.card_last_digits
        payment.last_error = ""
        payment.save()
        order.set_status(AUTHORISED)
    logger.info("Order %s authorized: %s", order.number, result.authorization_id)
    return payment, True


def _fail_attempt(payment_pk, reason):
    """A definitive rejection: the next attempt must use a fresh request id."""
    PayPalPayment.objects.filter(pk=payment_pk, status=PayPalPayment.PENDING).update(
        status=PayPalPayment.DECLINED,
        authorize_request_id="",
        last_error=str(reason)[:255],
    )


def _card_label(brand, last_digits):
    return (
        ("%s ending %s" % (brand or "Card", last_digits)).strip()
        if last_digits
        else (brand or "Card")
    )


# ===================
# Fulfil (capture)
# ===================


def fulfil(number):
    order = any_order(number)
    payment = _payment_of(order)
    if payment.status in (
        PayPalPayment.CAPTURED,
        PayPalPayment.CAPTURE_PENDING,
        PayPalPayment.PARTIALLY_REFUNDED,
        PayPalPayment.REFUNDED,
    ):
        return payment, False
    if payment.status != PayPalPayment.AUTHORIZED or order.status != AUTHORISED:
        raise ServiceError(
            409,
            "order_not_fulfillable",
            "Order is %s with payment %s; only an authorised order can be fulfilled."
            % (order.status, payment.status),
        )
    gateway = gw.get_gateway()

    payment = _ensure_capturable(payment, gateway)

    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        if not payment.capture_request_id:
            payment.capture_request_id = str(uuid.uuid4())
            payment.save(update_fields=["capture_request_id", "date_updated"])
        capture_request_id = payment.capture_request_id

    try:
        capture = gateway.capture(
            payment.authorization_id,
            request_id=capture_request_id,
            amount=payment.amount,
            currency=payment.currency,
        )
    except PayPalError as e:
        if e.outcome_unknown:
            PayPalPayment.objects.filter(pk=payment.pk).update(
                last_error=e.message[:255]
            )
            raise _from_paypal(e, retry_hint="Repeat the fulfil request to settle it.")
        PayPalPayment.objects.filter(pk=payment.pk).update(
            capture_request_id="", last_error=e.message[:255]
        )
        raise ServiceError(
            409,
            "capture_failed",
            "PayPal refused to capture the authorised payment: %s. The order was not "
            "fulfilled; if the authorization is no longer valid, cancel the order and ask the "
            "shopper to place and pay for it again." % e.message,
            **({"paypalIssue": e.issue} if e.issue else {}),
        )

    if capture.status in ("DECLINED", "FAILED"):
        PayPalPayment.objects.filter(pk=payment.pk).update(
            capture_request_id="",
            capture_status=capture.status,
            last_error="capture %s" % capture.status,
        )
        raise ServiceError(
            409,
            "capture_declined",
            "PayPal declined the capture (status %s). The order was not fulfilled; cancel it "
            "and ask the shopper to pay again." % capture.status,
        )

    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        if payment.capture_id == capture.capture_id:
            return payment, False
        order = _lock_order(payment.order_id)
        payment.capture_id = capture.capture_id
        payment.capture_status = capture.status
        payment.captured_amount = capture.amount
        payment.paypal_fee = capture.paypal_fee
        payment.net_amount = capture.net_amount
        payment.captured_at = timezone.now()
        payment.status = (
            PayPalPayment.CAPTURED
            if capture.status == "COMPLETED"
            else PayPalPayment.CAPTURE_PENDING
        )
        payment.authorization_status = "CAPTURED"
        payment.last_error = ""
        payment.save()
        if payment.source_id:
            payment.source.debit(
                capture.amount, reference=capture.capture_id, status=capture.status
            )
        EventHandler().consume_stock_allocations(order)
        order.set_status(COMPLETE)
    logger.info("Order %s fulfilled; capture %s", order.number, capture.capture_id)
    return payment, True


def _ensure_capturable(payment, gateway):
    """
    Make sure the authorization can still be captured, renewing it if its
    honor period has lapsed. Raises an operator-actionable error otherwise.
    """
    try:
        state = gateway.get_authorization(payment.authorization_id)
    except PayPalError as e:
        raise _from_paypal(e, retry_hint="Retry the fulfil request.")
    now = timezone.now()

    if state.status in ("CAPTURED", "PARTIALLY_CAPTURED"):
        return payment  # captured by an earlier attempt: the capture replay settles it
    if state.status in ("VOIDED", "DENIED"):
        _expire(payment, "authorization %s" % state.status)
        raise ServiceError(
            409,
            "authorization_not_capturable",
            "The payment authorization is %s at PayPal and can no longer be captured. The order "
            "was returned to '%s'; the shopper must pay again before it can be fulfilled, or "
            "the order can be cancelled." % (state.status, PENDING_PAYMENT),
        )
    expires_at = state.expires_at or payment.authorization_expires_at
    if expires_at and now >= expires_at:
        _expire(payment, "authorization expired")
        raise ServiceError(
            409,
            "authorization_expired",
            "The payment authorization expired on %s and can no longer be renewed. The order was "
            "returned to '%s'; the shopper must pay again before it can be fulfilled, or the "
            "order can be cancelled." % (expires_at.isoformat(), PENDING_PAYMENT),
        )
    started = (
        payment.honor_period_started_at or state.created_at or payment.authorized_at
    )
    if started is None or now - started < HONOR_PERIOD:
        return payment

    # The honor period has lapsed: reauthorize before capturing.
    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        if not payment.reauthorize_request_id:
            payment.reauthorize_request_id = str(uuid.uuid4())
            payment.save(update_fields=["reauthorize_request_id", "date_updated"])
        request_id = payment.reauthorize_request_id
    try:
        renewed = gateway.reauthorize(payment.authorization_id, request_id=request_id)
    except PayPalError as e:
        if e.outcome_unknown:
            raise _from_paypal(
                e, retry_hint="Repeat the fulfil request to settle the renewal."
            )
        PayPalPayment.objects.filter(pk=payment.pk).update(reauthorize_request_id="")
        _expire(payment, "reauthorization refused: %s" % (e.issue or e.message))
        raise ServiceError(
            409,
            "authorization_renewal_failed",
            "The payment authorization is past its %d-day honor period and PayPal refused to "
            "renew it (%s). No money was taken. The order was returned to '%s'; the shopper "
            "must pay again before it can be fulfilled, or the order can be cancelled."
            % (HONOR_PERIOD.days, e.message, PENDING_PAYMENT),
            **({"paypalIssue": e.issue} if e.issue else {}),
        )
    if renewed.status not in ("CREATED", "PENDING"):
        _expire(payment, "reauthorization %s" % renewed.status)
        raise ServiceError(
            409,
            "authorization_renewal_failed",
            "PayPal did not renew the authorization (status %s). The order was returned to '%s'; "
            "the shopper must pay again." % (renewed.status, PENDING_PAYMENT),
        )
    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        previous = payment.authorization_id
        payment.authorization_id = renewed.authorization_id
        payment.authorization_status = renewed.status
        payment.honor_period_started_at = renewed.created_at or timezone.now()
        if renewed.expires_at:
            payment.authorization_expires_at = renewed.expires_at
        payment.reauthorize_request_id = ""
        payment.capture_request_id = ""  # a new authorization gets a new capture key
        payment.save()
        if payment.source_id:
            _record_txn(
                payment.source,
                "Reauthorise",
                payment.amount,
                renewed.authorization_id,
                renewed.status,
            )
    logger.info("Reauthorized %s as %s", previous, renewed.authorization_id)
    return payment


def _expire(payment, reason):
    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        order = _lock_order(payment.order_id)
        payment.status = PayPalPayment.EXPIRED
        payment.authorize_request_id = ""
        payment.capture_request_id = ""
        payment.last_error = reason[:255]
        payment.save()
        if payment.source_id:
            source = payment.source
            _record_txn(
                source,
                "Expired",
                source.amount_allocated,
                payment.authorization_id,
                reason[:128],
            )
            source.amount_allocated = Decimal("0.00")
            source.save()
        order.set_status(PENDING_PAYMENT)


def _payment_of(order):
    try:
        return order.paypal_payment
    except PayPalPayment.DoesNotExist:
        raise ServiceError(
            409, "order_not_paid", "Order %s has no payment." % order.number
        )


# ======
# Cancel
# ======


def cancel(number):
    order = any_order(number)
    if order.status == CANCELLED:
        return order, False
    payment = PayPalPayment.objects.filter(order=order).first()
    if payment is not None:
        if payment.capture_id or payment.status in (
            PayPalPayment.CAPTURED,
            PayPalPayment.CAPTURE_PENDING,
            PayPalPayment.PARTIALLY_REFUNDED,
            PayPalPayment.REFUNDED,
        ):
            raise ServiceError(
                409,
                "already_fulfilled",
                "Order %s has been fulfilled and paid; return money with a refund instead."
                % order.number,
            )
        if payment.status == PayPalPayment.PENDING and payment.authorize_request_id:
            raise ServiceError(
                409,
                "payment_in_flight",
                "A payment attempt for this order has an unknown outcome; it must be settled "
                "(the shopper repeats the pay request) before the order can be cancelled.",
            )
        if payment.status == PayPalPayment.AUTHORIZED:
            _void(payment)
    if order.status not in (PENDING_PAYMENT, AUTHORISED):
        raise ServiceError(409, "order_not_cancellable", "Order is %s." % order.status)

    with transaction.atomic():
        order = _lock_order(order.pk)
        if order.status == CANCELLED:
            return order, False
        EventHandler().cancel_stock_allocations(order)
        order.set_status(CANCELLED)
    logger.info("Order %s cancelled", order.number)
    return order, True


def _void(payment):
    gateway = gw.get_gateway()
    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        if not payment.void_request_id:
            payment.void_request_id = str(uuid.uuid4())
            payment.save(update_fields=["void_request_id", "date_updated"])
        request_id = payment.void_request_id
    try:
        state = gateway.void(payment.authorization_id, request_id=request_id)
        status = state.status
    except PayPalError as e:
        if e.outcome_unknown:
            raise _from_paypal(e, retry_hint="Repeat the cancel request to settle it.")
        # Rejected: find out whether anything is still held.
        try:
            current = gateway.get_authorization(payment.authorization_id)
        except PayPalError as lookup_error:
            raise _from_paypal(lookup_error, retry_hint="Retry the cancel request.")
        expired = (
            current.expires_at is not None and timezone.now() >= current.expires_at
        )
        if current.status in ("VOIDED", "DENIED") or expired:
            status = current.status if not expired else "EXPIRED"
        else:
            PayPalPayment.objects.filter(pk=payment.pk).update(void_request_id="")
            raise ServiceError(
                409,
                "void_failed",
                "PayPal refused to release the held funds: %s" % e.message,
                **({"paypalIssue": e.issue} if e.issue else {}),
            )
    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        payment.status = PayPalPayment.VOIDED
        payment.authorization_status = status
        payment.save()
        if payment.source_id:
            source = payment.source
            _record_txn(
                source,
                "Void",
                source.amount_allocated,
                payment.authorization_id,
                status,
            )
            source.amount_allocated = Decimal("0.00")
            source.save()
    logger.info("Voided authorization %s", payment.authorization_id)


# =======
# Refunds
# =======


def refund(user, number, *, idempotency_key, amount=None, reason=""):
    order = owned_order(user, number)
    payment = _payment_of(order)

    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if (
                amount is not None
                and gw.quantize(amount, payment.currency) != existing.amount
            ):
                raise ServiceError(
                    422,
                    "idempotency_key_reused",
                    "This Idempotency-Key was already used for a refund of %s."
                    % existing.amount,
                )
            if existing.status == PayPalRefund.FAILED:
                raise ServiceError(
                    422,
                    "refund_failed",
                    "The refund under this Idempotency-Key failed: %s"
                    % existing.last_error,
                    refundId=existing.paypal_refund_id or None,
                )
            if existing.status != PayPalRefund.REQUESTED:
                return existing, False
            refund_row = (
                existing  # outcome unknown earlier: replay the same PayPal-Request-Id
            )
        else:
            if payment.status == PayPalPayment.CAPTURE_PENDING:
                raise ServiceError(
                    409, "capture_pending", "The capture is still pending at PayPal."
                )
            if payment.status not in (
                PayPalPayment.CAPTURED,
                PayPalPayment.PARTIALLY_REFUNDED,
            ):
                raise ServiceError(
                    409,
                    "not_refundable",
                    "Only a fulfilled (captured) order can be refunded; payment is %s."
                    % payment.status,
                )
            refundable = payment.refundable_amount
            value = (
                refundable if amount is None else gw.quantize(amount, payment.currency)
            )
            if value <= 0:
                raise ServiceError(
                    422,
                    "invalid_amount" if amount is not None else "nothing_to_refund",
                    (
                        "Refund amount must be positive."
                        if amount is not None
                        else "Nothing is left to refund."
                    ),
                )
            if value > refundable:
                raise ServiceError(
                    422,
                    "refund_exceeds_captured",
                    "Refund of %s exceeds the refundable %s %s."
                    % (value, refundable, payment.currency),
                    refundableAmount=str(refundable),
                )
            request_id = str(
                uuid.uuid5(
                    _KEY_NAMESPACE, "refund:%s:%s" % (payment.pk, idempotency_key)
                )
            )
            try:
                with transaction.atomic():
                    refund_row = PayPalRefund.objects.create(
                        payment=payment,
                        idempotency_key=idempotency_key,
                        paypal_request_id=request_id,
                        amount=value,
                        reason=reason[:255],
                        requested_by=user,
                    )
            except IntegrityError:
                raise ServiceError(
                    409, "refund_in_progress", "A refund with this key is in progress."
                )

    try:
        result = gw.get_gateway().refund(
            payment.capture_id,
            request_id=refund_row.paypal_request_id,
            amount=refund_row.amount,
            currency=payment.currency,
        )
    except PayPalError as e:
        if e.outcome_unknown:
            PayPalRefund.objects.filter(pk=refund_row.pk).update(
                last_error=e.message[:255]
            )
            raise _from_paypal(
                e,
                retry_hint="Repeat the request with the same Idempotency-Key to settle it.",
            )
        PayPalRefund.objects.filter(pk=refund_row.pk).update(
            status=PayPalRefund.FAILED, last_error=e.message[:255]
        )
        raise ServiceError(
            422 if e.http_status in (409, 422) else e.http_status,
            "refund_rejected",
            "PayPal rejected the refund: %s" % e.message,
            **({"paypalIssue": e.issue} if e.issue else {}),
        )

    with transaction.atomic():
        payment = _lock_payment(payment.pk)
        refund_row = PayPalRefund.objects.select_for_update().get(pk=refund_row.pk)
        if refund_row.paypal_refund_id:
            return refund_row, False
        failed = result.status in ("FAILED", "CANCELLED")
        refund_row.paypal_refund_id = result.refund_id
        refund_row.paypal_status = result.status
        refund_row.status = (
            PayPalRefund.FAILED
            if failed
            else (
                PayPalRefund.COMPLETED
                if result.status == "COMPLETED"
                else PayPalRefund.PENDING
            )
        )
        refund_row.last_error = "refund %s" % result.status if failed else ""
        refund_row.save()
        if not failed:
            if payment.source_id:
                payment.source.refund(
                    refund_row.amount, reference=result.refund_id, status=result.status
                )
            payment.status = (
                PayPalPayment.REFUNDED
                if payment.refundable_amount <= 0
                else PayPalPayment.PARTIALLY_REFUNDED
            )
            payment.save(update_fields=["status", "date_updated"])
    if failed:
        raise ServiceError(
            422,
            "refund_failed",
            "PayPal reports the refund as %s." % result.status,
            refundId=result.refund_id,
        )
    logger.info(
        "Refund %s of %s on order %s", result.refund_id, refund_row.amount, number
    )
    return refund_row, True


def parse_amount(raw):
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ServiceError(
            400, "invalid_amount", 'amount must be a decimal string, e.g. "10.00".'
        )
    if not value.is_finite():
        raise ServiceError(400, "invalid_amount", "amount must be a finite number.")
    currency = gw.configured_currency()
    if value != gw.quantize(value, currency):
        raise ServiceError(
            400,
            "invalid_amount",
            "amount has more decimal places than %s allows." % currency,
        )
    return value


# ===========
# Saved cards
# ===========


def _owned_bankcard(user, payment_method_id):
    try:
        return Bankcard.objects.get(
            pk=int(payment_method_id), user=user, partner_reference__gt=""
        )
    except (Bankcard.DoesNotExist, ValueError, TypeError):
        raise ServiceError(404, "payment_method_not_found", "No such saved card.")


def saved_cards(user):
    return Bankcard.objects.filter(user=user, partner_reference__gt="").order_by("pk")


def save_card(user, card: CardInput, idempotency_key=None):
    customer = PayPalCustomer.objects.filter(user=user).first()
    request_id = (
        str(uuid.uuid5(_KEY_NAMESPACE, "vault:%s:%s" % (user.pk, idempotency_key)))
        if idempotency_key
        else str(uuid.uuid4())
    )
    try:
        vaulted = gw.get_gateway().vault_card(
            request_id=request_id,
            card=card,
            customer_id=customer.paypal_customer_id if customer else None,
            merchant_customer_id="oscar-user-%s" % user.pk,
        )
    except PayPalError as e:
        if e.outcome_unknown:
            hint = (
                "Repeat the request with the same Idempotency-Key to settle it."
                if idempotency_key
                else "Check your saved cards before trying again."
            )
            raise _from_paypal(e, retry_hint=hint)
        if e.http_status == 422:
            raise ServiceError(
                422,
                "card_rejected",
                "PayPal could not save the card: %s" % e.message,
                **({"paypalIssue": e.issue} if e.issue else {}),
            )
        raise _from_paypal(e, retry_hint="")

    with transaction.atomic():
        if customer is None:
            PayPalCustomer.objects.get_or_create(
                user=user, defaults={"paypal_customer_id": vaulted.customer_id}
            )
        existing = Bankcard.objects.filter(
            user=user, partner_reference=vaulted.token_id
        ).first()
        if existing is not None:
            return existing, False
        bankcard = Bankcard(
            user=user,
            number="XXXX-XXXX-XXXX-%s" % vaulted.last_digits,
            expiry_date=_month_end(vaulted.expiry),
            partner_reference=vaulted.token_id,
        )
        bankcard.card_type = vaulted.brand
        bankcard.save()
    logger.info("Saved card %s for user %s", bankcard.pk, user.pk)
    return bankcard, True


def delete_card(user, payment_method_id):
    bankcard = _owned_bankcard(user, payment_method_id)
    try:
        gw.get_gateway().delete_vaulted_card(bankcard.partner_reference)
    except PayPalError as e:
        raise _from_paypal(e, retry_hint="Repeat the delete request.")
    bankcard.delete()
    logger.info("Deleted saved card %s for user %s", payment_method_id, user.pk)


def _month_end(expiry):
    year, month = (int(part) for part in expiry.split("-")[:2])
    return date(year, month, calendar.monthrange(year, month)[1])


# ==============
# Reconciliation
# ==============


def reconciliation(start: datetime, end: datetime):
    """
    Line PayPal's own transaction record for [start, end] up against this
    app's captures and refunds.
    """
    gateway = gw.get_gateway()
    paypal_txns = []
    seen_paypal = set()
    last_refreshed = None
    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=gw.MAX_SEARCH_WINDOW_DAYS), end)
        page = 1
        while True:
            try:
                result = gateway.search_transactions(
                    window_start, window_end, page=page
                )
            except PayPalError as e:
                raise _from_paypal(e, retry_hint="Retry the report.")
            for txn in result.transactions:
                # Window boundaries are inclusive on both sides: never count one twice.
                identity = (txn.transaction_id, txn.event_code, txn.amount)
                if identity not in seen_paypal:
                    seen_paypal.add(identity)
                    paypal_txns.append(txn)
            if result.last_refreshed_at and (
                last_refreshed is None or result.last_refreshed_at < last_refreshed
            ):
                last_refreshed = result.last_refreshed_at
            if page >= result.total_pages:
                break
            page += 1
        window_start = window_end

    # The app's own money movements in the range
    app_entries = {}
    captures = (
        PayPalPayment.objects.filter(captured_at__gte=start, captured_at__lte=end)
        .exclude(capture_id="")
        .select_related("order")
    )
    for p in captures:
        app_entries[p.capture_id] = {
            "kind": "capture",
            "transactionId": p.capture_id,
            "orderId": str(p.order.number),
            "amount": str(p.captured_amount),
            "currency": p.currency,
            "at": p.captured_at,
        }
    refunds = (
        PayPalRefund.objects.filter(date_created__gte=start, date_created__lte=end)
        .exclude(paypal_refund_id="")
        .select_related("payment__order")
    )
    for r in refunds:
        app_entries[r.paypal_refund_id] = {
            "kind": "refund",
            "transactionId": r.paypal_refund_id,
            "orderId": str(r.payment.order.number),
            "amount": str(-r.amount),
            "currency": r.payment.currency,
            "at": r.date_created,
        }

    seen = set()
    matched, paypal_only = [], []
    order_numbers = set(
        Order.objects.filter(
            number__in={t.custom_field for t in paypal_txns if t.custom_field}
        ).values_list("number", flat=True)
    )
    for txn in paypal_txns:
        entry = {
            "transactionId": txn.transaction_id,
            "eventCode": txn.event_code,
            "status": txn.status,
            "at": txn.initiated_at.isoformat() if txn.initiated_at else None,
            "amount": str(txn.amount) if txn.amount is not None else None,
            "currency": txn.currency,
            "fee": str(txn.fee) if txn.fee is not None else None,
            "invoiceId": txn.invoice_id or None,
            "customField": txn.custom_field or None,
        }
        app = app_entries.get(txn.transaction_id)
        if app is not None:
            seen.add(txn.transaction_id)
            entry["orderId"] = app["orderId"]
            entry["kind"] = app["kind"]
            entry["appAmount"] = app["amount"]
            entry["amountMatches"] = (
                txn.amount is not None
                and Decimal(app["amount"]) == txn.amount
                and app["currency"] == txn.currency
            )
            matched.append(entry)
        else:
            entry["orderId"] = (
                txn.custom_field if txn.custom_field in order_numbers else None
            )
            paypal_only.append(entry)

    lag_cutoff = timezone.now() - REPORTING_LAG
    app_only = []
    for txn_id, app in app_entries.items():
        if txn_id in seen:
            continue
        app_only.append(
            {
                **app,
                "at": app["at"].isoformat(),
                "withinReportingLag": app["at"] >= lag_cutoff
                or (last_refreshed is not None and app["at"] > last_refreshed),
            }
        )

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalLastRefreshedAt": last_refreshed.isoformat() if last_refreshed else None,
        "summary": {
            "paypalTransactions": len(paypal_txns),
            "matched": len(matched),
            "amountMismatches": sum(1 for m in matched if not m["amountMatches"]),
            "paypalOnly": len(paypal_only),
            "appOnly": len(app_only),
        },
        "matched": matched,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
    }
