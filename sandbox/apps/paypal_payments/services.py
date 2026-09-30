"""
Payment flows over Oscar's own order and payment models.

Every PayPal write follows one order: take a claim this database refuses to
grant twice, call PayPal with the claim's request id, record the result. A
refused call releases the claim; a call whose outcome is unknown keeps it, so
a retry resumes the same PayPal request instead of starting a second one.
"""

import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_class, get_model

from . import claims, gateway
from .errors import PaymentError, unknown_outcome
from .models import PayPalCustomer, PayPalPayment, PayPalRefund, SavedCard
from .money import currency, parse_amount, quantize, quantum

logger = logging.getLogger(__name__)

Basket = get_model("basket", "Basket")
Order = get_model("order", "Order")
PaymentEventType = get_model("order", "PaymentEventType")
Product = get_model("catalogue", "Product")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
EventHandler = get_class("order.processing", "EventHandler")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Repository = get_class("shipping.repository", "Repository")
Selector = get_class("partner.strategy", "Selector")

# Oscar order statuses, from the sandbox's OSCAR_ORDER_STATUS_PIPELINE.
STATUS_AWAITING_PAYMENT = settings.OSCAR_INITIAL_ORDER_STATUS  # "Pending"
STATUS_PAID = "Being processed"
STATUS_FULFILLED = "Complete"
STATUS_CANCELLED = "Cancelled"

# PayPal honours an authorization for 3 days; after that it must be
# reauthorized (possible from day 4 to day 29) to keep the funds guaranteed.
HONOR_PERIOD = timedelta(days=3)

MAX_QUANTITY = 99
SOURCE_TYPE_NAME = "PayPal"


# ---------------------------------------------------------------------------
# Lookups and validation
# ---------------------------------------------------------------------------


def shopper_order(user, order_number):
    """The caller's own order; anyone else's is indistinguishable from a missing one."""
    try:
        return Order.objects.get(number=order_number, user=user)
    except Order.DoesNotExist:
        raise PaymentError(404, "order_not_found", "No such order.") from None


def any_order(order_number):
    try:
        return Order.objects.get(number=order_number)
    except Order.DoesNotExist:
        raise PaymentError(404, "order_not_found", "No such order.") from None


def _payment(order):
    try:
        return order.paypal_payment
    except PayPalPayment.DoesNotExist:
        return None


def _bad_request(message, code="invalid_request"):
    return PaymentError(400, code, message)


_EXPIRY = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


@sensitive_variables("data", "number", "security_code")
def parse_card(data):
    """Validate card details from a request body. Card data is never stored or logged."""
    if not isinstance(data, dict):
        raise _bad_request("card must be an object with number, expiry, securityCode and optional name.")
    number = re.sub(r"[\s-]", "", str(data.get("number", "")))
    if not number.isdigit() or not 12 <= len(number) <= 19:
        raise _bad_request("card.number must be 12-19 digits.")
    expiry = str(data.get("expiry", ""))
    match = _EXPIRY.match(expiry)
    if not match:
        raise _bad_request("card.expiry must be in YYYY-MM format.")
    today = timezone.now().date()
    if (int(match.group(1)), int(match.group(2))) < (today.year, today.month):
        raise _bad_request("card.expiry is in the past.")
    security_code = str(data.get("securityCode", ""))
    if not security_code.isdigit() or not 3 <= len(security_code) <= 4:
        raise _bad_request("card.securityCode must be 3 or 4 digits.")
    name = data.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 300):
        raise _bad_request("card.name must be a string of at most 300 characters.")
    return gateway.CardDetails(number=number, expiry=expiry, security_code=security_code, name=name or None)


def _event_type(name):
    return PaymentEventType.objects.get_or_create(name=name)[0]


def _minor(amount, currency_code):
    return int((amount / quantum(currency_code)).to_integral_value())


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def _parse_items(items):
    if not isinstance(items, list) or not items:
        raise _bad_request("items must be a non-empty list of {productId, quantity}.")
    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise _bad_request("each item must be an object with productId and quantity.")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            raise _bad_request("productId must be an integer catalogue product id.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
            raise _bad_request(f"quantity must be an integer from 1 to {MAX_QUANTITY}.")
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    return quantities


def place_order(user, items, *, idempotency_key=None, request=None):
    """Write an Oscar order (via Oscar's own basket and OrderCreator) awaiting payment."""
    quantities = _parse_items(items)
    claim = None
    if idempotency_key:
        claim, fresh = claims.claim_or_existing(f"order:{user.pk}:{idempotency_key}", "place-order")
        if not fresh:
            return Order.objects.get(pk=claim.result["orderPk"])
    try:
        with transaction.atomic():
            order = _create_order(user, quantities, request)
    except Exception as e:
        if claim is not None:
            claims.release(claim)  # nothing left this process
        raise e
    if claim is not None:
        claims.complete(claim, orderPk=order.pk)
    return order


def _create_order(user, quantities, request):
    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(request=request, user=user)
    for product_id, quantity in quantities.items():
        product = Product.objects.filter(pk=product_id, is_public=True).first()
        if product is None:
            raise PaymentError(422, "unknown_product", f"Product {product_id} does not exist.")
        info = basket.strategy.fetch_for_product(product)
        if info.price is None or not info.price.exists:
            raise PaymentError(422, "not_for_sale", f"Product {product_id} ({product.get_title()}) is not for sale.")
        permitted, reason = info.availability.is_purchase_permitted(quantity)
        if not permitted:
            raise PaymentError(
                422, "not_available", f"Product {product_id} ({product.get_title()}) cannot be bought: {reason}"
            )
        basket.add_product(product, quantity)

    shipping_method = Repository().get_default_shipping_method(basket=basket, user=user, request=request)
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        user=user,
        request=request,
        status=STATUS_AWAITING_PAYMENT,
        # Amounts are the catalogue's; the currency is the configured one.
        currency=currency(),
    )
    basket.submit()
    return order


# ---------------------------------------------------------------------------
# Pay: authorize (hold) the order total
# ---------------------------------------------------------------------------


@sensitive_variables("card")
def pay_order(user, order_number, *, card=None, payment_method_id=None):
    order = shopper_order(user, order_number)
    if (card is None) == (payment_method_id is None):
        raise _bad_request("Send either card details or a paymentMethodId, not both.")
    saved_card = None
    if payment_method_id is not None:
        saved_card = SavedCard.objects.active().filter(pk=payment_method_id, user=user).first()
        if saved_card is None:
            raise PaymentError(404, "payment_method_not_found", "No such saved payment method.")

    existing = _payment(order)
    if existing is not None:
        return order  # already authorized: a repeat is a no-op
    if order.status != STATUS_AWAITING_PAYMENT:
        raise PaymentError(409, "order_not_payable", f"Order is '{order.status}', not awaiting payment.")

    claim, fresh = claims.claim_or_existing(f"authorize:{order.pk}", "authorize")
    if not fresh:
        return order

    amount = quantize(order.total_incl_tax, order.currency)
    invoice_id = f"{settings.PAYPAL_INVOICE_PREFIX}{order.number}-{claim.request_id[:8]}"
    try:
        result = gateway.authorize(
            amount=amount,
            currency=order.currency,
            invoice_id=invoice_id,
            custom_id=str(order.number),
            description=f"Order {order.number}",
            request_id=claim.request_id,
            card=card,
            vault_id=saved_card.paypal_token_id if saved_card else None,
        )
        _check_authorization(order, result, amount)
    except Exception as e:
        claims.settle_failure(claim, e)
        raise

    with transaction.atomic():
        payment = PayPalPayment.objects.create(
            order=order,
            state=PayPalPayment.AUTHORIZED,
            currency=order.currency,
            amount=amount,
            paypal_order_id=result.paypal_order_id,
            invoice_id=invoice_id,
            saved_card=saved_card,
            card_brand=result.card_brand or (saved_card.brand if saved_card else ""),
            card_last_digits=result.card_last_digits or (saved_card.last_digits if saved_card else ""),
            authorization_id=result.authorization_id,
            authorization_status=result.authorization_status,
            authorized_at=result.created_at or timezone.now(),
            authorization_expires_at=result.expires_at,
        )
        source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
        source = Source.objects.create(
            order=order,
            source_type=source_type,
            currency=order.currency,
            reference=result.paypal_order_id,
            label=f"{payment.card_brand} ending {payment.card_last_digits}".strip(),
        )
        source.allocate(amount, reference=result.authorization_id, status=result.authorization_status)
        payment.source = source
        payment.save(update_fields=["source"])
        EventHandler(user).create_payment_event(
            order, _event_type("Authorised"), amount, reference=result.authorization_id
        )
        order.set_status(STATUS_PAID)
        claims.complete(claim, paypalOrderId=result.paypal_order_id)
    logger.info("Order %s authorized: %s %s (authorization %s)", order.number, amount, order.currency,
                result.authorization_id)
    return order


def _check_authorization(order, result, amount):
    if result.order_status == "PAYER_ACTION_REQUIRED":
        raise PaymentError(
            402, "payer_action_required",
            "PayPal requires the shopper to approve this card payment in a browser (for example 3-D Secure). "
            "This integration does not support browser approval, so the card cannot be used here.",
        )
    if result.authorization_id is None or result.authorization_status is None:
        if result.order_status == "COMPLETED":
            raise unknown_outcome("the card authorization", "an authorization")
        raise PaymentError(
            402, "payment_declined", f"PayPal did not authorize the payment (order status {result.order_status})."
        )
    if result.authorization_status not in ("CREATED", "PENDING"):
        raise PaymentError(
            402, "payment_declined",
            f"The card was declined (authorization status {result.authorization_status}).",
        )
    if result.amount != amount or (result.currency or "").upper() != order.currency.upper():
        # Should never happen: PayPal holds exactly what we asked for.
        logger.error("Order %s: PayPal authorized %s %s, expected %s %s", order.number, result.amount,
                     result.currency, amount, order.currency)
        raise PaymentError(502, "amount_mismatch", "PayPal authorized an unexpected amount.", outcome_unknown=True)


# ---------------------------------------------------------------------------
# Fulfil: capture, renewing a stale authorization first
# ---------------------------------------------------------------------------


def fulfil_order(operator, order_number):
    order = any_order(order_number)
    payment = _payment(order)
    if payment is None:
        raise PaymentError(409, "order_not_paid", "The order has not been paid, so there is nothing to capture.")
    if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        return order  # already fulfilled
    if payment.state == PayPalPayment.VOIDED:
        raise PaymentError(409, "order_cancelled", "The order was cancelled and its payment released.")

    claim, fresh = claims.claim_or_existing(f"settle:{order.pk}", "capture")
    if not fresh:
        order.refresh_from_db()
        return order

    try:
        authorization_id = _renew_if_stale(order, payment, claim)
        result = gateway.capture(
            authorization_id,
            amount=payment.amount,
            currency=payment.currency,
            invoice_id=payment.invoice_id,
            request_id=claim.request_id,
        )
        if result.status in ("DECLINED", "FAILED"):
            raise PaymentError(
                402, "capture_declined",
                f"PayPal declined the capture (status {result.status}). The order was not fulfilled.",
            )
    except Exception as e:
        claims.settle_failure(claim, e)
        raise

    with transaction.atomic():
        payment.refresh_from_db()
        payment.state = PayPalPayment.CAPTURED
        payment.capture_id = result.capture_id
        payment.capture_status = result.status
        payment.captured_amount = result.amount
        payment.captured_minor = _minor(result.amount, payment.currency)
        payment.paypal_fee = result.paypal_fee
        payment.net_amount = result.net_amount
        payment.captured_at = result.created_at or timezone.now()
        payment.authorization_status = "CAPTURED"
        payment.save()
        if payment.source:
            payment.source.debit(result.amount, reference=result.capture_id, status=result.status)
        handler = EventHandler(operator)
        handler.create_payment_event(order, _event_type("Settled"), result.amount, reference=result.capture_id)
        handler.consume_stock_allocations(order)
        order.set_status(STATUS_FULFILLED)
        claims.complete(claim, captureId=result.capture_id)
    logger.info("Order %s fulfilled: captured %s %s (capture %s, fee %s, net %s)", order.number, result.amount,
                result.currency, result.capture_id, result.paypal_fee, result.net_amount)
    return order


def _renew_if_stale(order, payment, claim):
    """Return the authorization to capture, reauthorizing it if its honor period has lapsed."""
    state = gateway.get_authorization(payment.authorization_id)
    now = timezone.now()
    cancel_hint = (
        f"Cancel the order (POST /api/orders/{order.number}/cancel) and ask the shopper to place and pay "
        "for a new order."
    )
    if state.status in ("CAPTURED", "PARTIALLY_CAPTURED"):
        # An earlier capture attempt landed; re-sending it with the same request id returns that capture.
        return state.authorization_id
    if state.status in ("VOIDED", "DENIED"):
        raise PaymentError(
            409, "authorization_not_capturable",
            f"PayPal reports the payment hold as {state.status}, so it can no longer be captured. {cancel_hint}",
        )
    if state.expires_at is not None and now >= state.expires_at:
        raise PaymentError(
            409, "authorization_expired",
            f"The payment hold expired on {state.expires_at.isoformat()} and cannot be renewed. {cancel_hint}",
        )
    authorized_at = state.created_at or payment.authorized_at
    if now < authorized_at + HONOR_PERIOD:
        return state.authorization_id

    logger.info("Order %s: authorization %s is past its honor period; reauthorizing", order.number,
                state.authorization_id)
    try:
        renewed = gateway.reauthorize(
            state.authorization_id,
            amount=payment.amount,
            currency=payment.currency,
            request_id=f"{claim.request_id}-reauth",
        )
    except PaymentError as e:
        if e.outcome_unknown:
            raise
        raise PaymentError(
            409, "authorization_not_renewable",
            f"The payment hold is past PayPal's {HONOR_PERIOD.days}-day honor period and PayPal refused to renew "
            f"it ({e.message}). The shopper's funds are no longer guaranteed. {cancel_hint}",
            details=e.details,
        ) from e
    with transaction.atomic():
        PayPalPayment.objects.filter(pk=payment.pk).update(
            authorization_id=renewed.authorization_id,
            authorization_status=renewed.status,
            authorized_at=renewed.created_at or now,
            authorization_expires_at=renewed.expires_at or payment.authorization_expires_at,
            reauthorization_count=F("reauthorization_count") + 1,
        )
        if payment.source:
            Transaction.objects.create(
                source=payment.source, txn_type="Reauthorise", amount=payment.amount,
                reference=renewed.authorization_id, status=renewed.status,
            )
    payment.refresh_from_db()
    return renewed.authorization_id


# ---------------------------------------------------------------------------
# Cancel: release the hold before fulfilment
# ---------------------------------------------------------------------------


def cancel_order(operator, order_number):
    order = any_order(order_number)
    if order.status == STATUS_CANCELLED:
        return order
    payment = _payment(order)
    if payment is None:
        return _cancel_unpaid(operator, order)
    if payment.state == PayPalPayment.VOIDED:
        return order
    if payment.state != PayPalPayment.AUTHORIZED:
        raise PaymentError(
            409, "order_already_fulfilled",
            "The payment has been captured; return money with POST /api/orders/{orderId}/refunds instead.",
        )

    claim, fresh = claims.claim_or_existing(f"settle:{order.pk}", "void")
    if fresh:
        status = _void_hold(payment, claim)
        with transaction.atomic():
            PayPalPayment.objects.filter(pk=payment.pk).update(
                state=PayPalPayment.VOIDED, authorization_status=status
            )
            if payment.source:
                Transaction.objects.create(
                    source=payment.source, txn_type="Void", amount=payment.amount,
                    reference=payment.authorization_id, status=status,
                )
            handler = EventHandler(operator)
            handler.create_payment_event(order, _event_type("Voided"), payment.amount,
                                         reference=payment.authorization_id)
            handler.cancel_stock_allocations(order)
            order.set_status(STATUS_CANCELLED)
            claims.complete(claim)
        logger.info("Order %s cancelled: authorization %s voided", order.number, payment.authorization_id)
    order.refresh_from_db()
    return order


def _cancel_unpaid(operator, order):
    if order.status != STATUS_AWAITING_PAYMENT:
        raise PaymentError(409, "order_not_cancellable", f"Order is '{order.status}' and cannot be cancelled.")
    # Holding the order's authorize key stops a concurrent pay from authorizing it.
    claim, fresh = claims.claim_or_existing(f"authorize:{order.pk}", "cancel-unpaid")
    if fresh:
        with transaction.atomic():
            EventHandler(operator).cancel_stock_allocations(order)
            order.set_status(STATUS_CANCELLED)
            claims.complete(claim)
    order.refresh_from_db()
    return order


def _void_hold(payment, claim):
    """Void the authorization at PayPal; returns its resulting status."""
    try:
        return gateway.void(payment.authorization_id, request_id=claim.request_id)
    except PaymentError as e:
        if e.outcome_unknown:
            claims.mark_unknown(claim)
            raise
        # Refused: it may already be void (e.g. expired at PayPal). Check before giving up.
        try:
            status = gateway.get_authorization(payment.authorization_id).status
        except PaymentError:
            status = None
        if status != "VOIDED":
            claims.release(claim)
            raise e
        return status
    except Exception as e:
        claims.settle_failure(claim, e)
        raise


# ---------------------------------------------------------------------------
# Refund: return captured money, in full or in part
# ---------------------------------------------------------------------------


class _OverRefund(Exception):
    pass


def refund_order(user, order_number, *, idempotency_key, amount=None, note=None):
    """Returns ``(refund, created)``; a repeated key returns the original refund."""
    order = shopper_order(user, order_number)
    if not idempotency_key or len(idempotency_key) > 255:
        raise _bad_request("An Idempotency-Key header (1-255 characters) is required for refunds.",
                           "idempotency_key_required")
    if note is not None and (not isinstance(note, str) or len(note) > 255):
        raise _bad_request("note must be a string of at most 255 characters.")
    payment = _payment(order)
    if payment is None or payment.state in (PayPalPayment.AUTHORIZED, PayPalPayment.VOIDED):
        raise PaymentError(409, "order_not_refundable", "Only a fulfilled (captured) order can be refunded.")

    requested = None if amount is None else _parse_refund_amount(amount, payment.currency)
    refund, fresh = _claim_refund(payment, idempotency_key, requested, note)
    if not fresh:
        return refund, False
    return _send_refund(order, payment, refund), True


def _parse_refund_amount(raw, currency_code):
    try:
        return parse_amount(raw, currency_code)
    except ValueError as e:
        raise _bad_request(str(e)) from None


def _claim_refund(payment, key, requested, note):
    try:
        with transaction.atomic():
            payment.refresh_from_db()
            q = quantum(payment.currency)
            remaining_minor = payment.captured_minor - payment.refund_reserved_minor
            amount = requested if requested is not None else Decimal(remaining_minor) * q
            amount_minor = _minor(amount, payment.currency)
            if amount_minor <= 0:
                raise _OverRefund(Decimal(0))
            refund = PayPalRefund.objects.create(payment=payment, idempotency_key=key, amount=amount,
                                                 note=note or "")
            reserved = PayPalPayment.objects.filter(
                pk=payment.pk,
                refund_reserved_minor__lte=F("captured_minor") - amount_minor,
            ).update(refund_reserved_minor=F("refund_reserved_minor") + amount_minor)
            if not reserved:
                raise _OverRefund(Decimal(remaining_minor) * q)
        return refund, True
    except _OverRefund as e:
        remaining = e.args[0]
        raise PaymentError(
            409, "refund_exceeds_captured",
            f"The refund exceeds what is left to refund on this order ({remaining} {payment.currency}).",
            details={"refundableAmount": str(remaining)},
        ) from None
    except IntegrityError:
        pass

    existing = PayPalRefund.objects.get(payment=payment, idempotency_key=key)
    if requested is not None and existing.amount != requested:
        raise PaymentError(
            422, "idempotency_key_reused",
            "This Idempotency-Key was already used for a refund of a different amount.",
        )
    if existing.status in (PayPalRefund.COMPLETED, PayPalRefund.PENDING):
        return existing, False
    resumable = PayPalRefund.objects.filter(pk=existing.pk, status=PayPalRefund.OUTCOME_UNKNOWN)
    if resumable.update(status=PayPalRefund.PENDING_SUBMISSION):
        existing.refresh_from_db()
        return existing, True
    stale = PayPalRefund.objects.filter(
        pk=existing.pk, status=PayPalRefund.PENDING_SUBMISSION,
        date_updated__lt=timezone.now() - claims.STALE_AFTER,
    )
    if stale.update(date_updated=timezone.now()):
        existing.refresh_from_db()
        return existing, True
    raise PaymentError(409, "operation_in_progress", "This refund is already being processed; retry in a moment.")


def _release_refund(payment, refund):
    with transaction.atomic():
        PayPalPayment.objects.filter(pk=payment.pk).update(
            refund_reserved_minor=F("refund_reserved_minor") - _minor(refund.amount, payment.currency)
        )
        refund.delete()


def _send_refund(order, payment, refund):
    try:
        result = gateway.refund(
            payment.capture_id,
            amount=refund.amount,
            currency=payment.currency,
            invoice_id=payment.invoice_id,
            note=refund.note or None,
            request_id=refund.request_id,
        )
        if result.status in ("FAILED", "CANCELLED"):
            raise PaymentError(402, "refund_failed", f"PayPal did not complete the refund (status {result.status}).")
    except PaymentError as e:
        if e.outcome_unknown:
            PayPalRefund.objects.filter(pk=refund.pk).update(status=PayPalRefund.OUTCOME_UNKNOWN)
        else:
            _release_refund(payment, refund)
        raise
    except Exception:
        PayPalRefund.objects.filter(pk=refund.pk).update(status=PayPalRefund.OUTCOME_UNKNOWN)
        raise

    with transaction.atomic():
        refund.status = PayPalRefund.COMPLETED if result.status == "COMPLETED" else PayPalRefund.PENDING
        refund.paypal_refund_id = result.refund_id
        refund.paypal_status = result.status
        refund.save()
        PayPalPayment.objects.filter(pk=payment.pk).update(refunded_amount=F("refunded_amount") + refund.amount)
        payment.refresh_from_db()
        payment.state = (
            PayPalPayment.REFUNDED if payment.refunded_amount >= payment.captured_amount
            else PayPalPayment.PARTIALLY_REFUNDED
        )
        payment.save(update_fields=["state", "date_updated"])
        if payment.source:
            payment.source.refund(refund.amount, reference=result.refund_id, status=result.status)
        EventHandler().create_payment_event(order, _event_type("Refunded"), refund.amount,
                                            reference=result.refund_id)
    logger.info("Order %s refunded %s %s (refund %s, %s)", order.number, refund.amount, payment.currency,
                result.refund_id, result.status)
    return refund


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


@sensitive_variables("card")
def save_card(user, card, *, idempotency_key):
    claim, fresh = claims.claim_or_existing(f"vault:{user.pk}:{idempotency_key}", "save-card")
    if not fresh:
        saved = SavedCard.objects.active().filter(pk=claim.result.get("paymentMethodId"), user=user).first()
        if saved is None:
            raise PaymentError(410, "payment_method_deleted", "The card saved under this Idempotency-Key was deleted.")
        return saved, False

    customer = PayPalCustomer.objects.filter(user=user).first()
    try:
        vaulted = gateway.vault_card(
            card, customer_id=customer.paypal_customer_id if customer else None, request_id=claim.request_id
        )
    except Exception as e:
        claims.settle_failure(claim, e)
        raise

    with transaction.atomic():
        if customer is None and vaulted.customer_id:
            PayPalCustomer.objects.get_or_create(user=user, defaults={"paypal_customer_id": vaulted.customer_id})
        saved, _ = SavedCard.objects.get_or_create(
            paypal_token_id=vaulted.token_id,
            defaults={
                "user": user,
                "paypal_customer_id": vaulted.customer_id or "",
                "brand": vaulted.brand,
                "last_digits": vaulted.last_digits,
                "expiry": vaulted.expiry,
                "cardholder_name": vaulted.name,
            },
        )
        claims.complete(claim, paymentMethodId=str(saved.pk))
    logger.info("User %s saved card %s (%s ending %s)", user.pk, saved.pk, saved.brand, saved.last_digits)
    return saved, True


def delete_card(user, payment_method_id):
    """Tombstone the card (no longer listed or usable at once), then remove it from PayPal's vault."""
    removed = SavedCard.objects.filter(pk=payment_method_id, user=user, deleted_at__isnull=True).update(
        deleted_at=timezone.now()
    )
    if not removed:
        raise PaymentError(404, "payment_method_not_found", "No such saved payment method.")
    card = SavedCard.objects.get(pk=payment_method_id)
    return purge_vault_token(card)


def purge_vault_token(card):
    """Remove a deleted card's token from PayPal. Returns whether PayPal confirmed it."""
    try:
        gateway.delete_vault_token(card.paypal_token_id)
    except PaymentError as e:
        if e.status_code != 404:
            # Left for `manage.py paypal_purge_deleted_cards` to retry.
            logger.warning("Saved card %s: vault token not yet removed from PayPal (%s)", card.pk, e.code)
            return False
    SavedCard.objects.filter(pk=card.pk).update(vault_token_deleted=True)
    return True


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _amount(value):
    return None if value is None else str(value)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def card_to_dict(card):
    return {
        "paymentMethodId": str(card.pk),
        "type": "card",
        "brand": card.brand,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "cardholderName": card.cardholder_name,
        "createdAt": _iso(card.date_created),
    }


def refund_to_dict(refund):
    return {
        "refundId": str(refund.pk),
        "amount": _amount(refund.amount),
        "status": refund.status,
        "paypalRefundId": refund.paypal_refund_id or None,
        "paypalStatus": refund.paypal_status or None,
        "createdAt": _iso(refund.date_created),
    }


def payment_state(order, payment):
    if payment is not None:
        return payment.state
    return "CANCELLED" if order.status == STATUS_CANCELLED else "AWAITING_PAYMENT"


def order_to_dict(order):
    payment = _payment(order)
    body = {
        "orderId": str(order.number),
        "status": order.status,
        "paymentState": payment_state(order, payment),
        "currency": order.currency,
        "total": _amount(quantize(order.total_incl_tax, order.currency)),
        "placedAt": _iso(order.date_placed),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _amount(line.unit_price_incl_tax),
                "lineTotal": _amount(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": None,
    }
    if payment is not None:
        captured = payment.captured_amount
        body["payment"] = {
            "state": payment.state,
            "amount": _amount(payment.amount),
            "card": {"brand": payment.card_brand, "lastDigits": payment.card_last_digits},
            "paymentMethodId": str(payment.saved_card_id) if payment.saved_card_id else None,
            "paypalOrderId": payment.paypal_order_id,
            "invoiceId": payment.invoice_id,
            "authorization": {
                "id": payment.authorization_id,
                "status": payment.authorization_status,
                "authorizedAt": _iso(payment.authorized_at),
                "expiresAt": _iso(payment.authorization_expires_at),
                "reauthorizations": payment.reauthorization_count,
            },
            "capture": None if not payment.capture_id else {
                "id": payment.capture_id,
                "status": payment.capture_status,
                "amount": _amount(captured),
                "paypalFee": _amount(payment.paypal_fee),
                "netAmount": _amount(payment.net_amount),
                "capturedAt": _iso(payment.captured_at),
            },
            "refundedAmount": _amount(payment.refunded_amount),
            "refundableAmount": _amount(
                Decimal(payment.captured_minor - payment.refund_reserved_minor) * quantum(payment.currency)
            ) if payment.capture_id else None,
            "refunds": [refund_to_dict(r) for r in payment.refunds.all()],
        }
    return body
