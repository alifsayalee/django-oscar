"""
Business flows of the PayPal API: placing orders, authorizing, capturing at
fulfilment, voiding at cancellation, refunding, saved cards and reconciliation.

Every PayPal write follows the same shape:

1. **claim** — a row (or a conditional status update) committed *before* the
   PayPal call, so a double-click or an overlapping retry finds the claim and
   never sends a second authorization, capture, void or refund;
2. **call** — with a PayPal-Request-Id derived from the claim, so a resend of
   a write whose outcome is unknown is de-duplicated by PayPal;
3. **reconcile** — an unknown outcome is kept as ``unknown``/in-flight and
   resolved by resending under the *same* request id, never a new one;
4. **verify** — the amount PayPal echoes must equal what was asked;
5. **settle** — the stored state comes from the status PayPal reported.

These functions run outside the request-wide transaction (the views opt out of
``ATOMIC_REQUESTS``) so that each claim is committed before PayPal is called.
"""
import calendar
import datetime
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price

from . import gateway
from .gateway import CardDetails, PayPalError
from .models import PayPalPayment, PayPalRefund, PayPalSavedCard

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
Bankcard = get_model("payment", "Bankcard")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Repository = get_class("shipping.repository", "Repository")
Selector = get_class("partner.strategy", "Selector")
InvalidOrderStatus = get_class("order.exceptions", "InvalidOrderStatus")

# Oscar order statuses (see OSCAR_ORDER_STATUS_PIPELINE in sandbox/settings.py).
AWAITING_PAYMENT = getattr(settings, "OSCAR_INITIAL_ORDER_STATUS", "Pending")
PAYMENT_AUTHORIZED = "Being processed"
FULFILLED = "Complete"
CANCELLED = "Cancelled"

# A claim younger than this is assumed to still have its request in flight.
# It must exceed the PayPal timeout (no retries inside a claim).
IN_FLIGHT_WINDOW = datetime.timedelta(seconds=90)
# PayPal keeps PayPal-Request-Id keys for 3 hours or more depending on the API;
# an unknown write is only resent (de-duplicated) inside the smallest window.
RESEND_WINDOW = datetime.timedelta(hours=3)
# Card authorizations: 3-day honour period, renewable until expiry (29 days).
HONOR_PERIOD = datetime.timedelta(days=3)

# Namespace for deterministic PayPal-Request-Id values derived from our claims.
REQUEST_ID_NAMESPACE = uuid.UUID("7d0c8bd4-4bb1-4d5b-9a0e-2f4a3c0b9e61")

MAX_ORDER_ITEMS = 50
MAX_QUANTITY = 99
MAX_RECONCILIATION_DAYS = 400
RECONCILIATION_WINDOW = datetime.timedelta(days=31)
RECONCILIATION_MAX_PAGES = 50


class ServiceError(Exception):
    """A failure to report to the API caller: HTTP status, stable code, message."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


@dataclass
class Outcome:
    """The result of a flow: the object acted on and the HTTP status to answer."""

    obj: Any
    status_code: int = 200
    notice: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def request_id(*parts: object) -> str:
    return str(uuid.uuid5(REQUEST_ID_NAMESPACE, ":".join(str(p) for p in parts)))


def _paypal_failure(exc: PayPalError, code: str, **extra: Any) -> ServiceError:
    detail = dict(extra)
    if exc.issue:
        detail["paypalIssue"] = exc.issue
    if exc.debug_id:
        detail["paypalDebugId"] = exc.debug_id
    if exc.outcome_unknown:
        detail["outcomeUnknown"] = True
    return ServiceError(exc.status_code, code, exc.message, **detail)


def _is_fresh(updated_at: datetime.datetime) -> bool:
    return timezone.now() - updated_at < IN_FLIGHT_WINDOW


def _source_type():
    source_type, _ = SourceType.objects.get_or_create(name="PayPal")
    return source_type


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def parse_items(raw: Any) -> list[tuple[int, int]]:
    if not isinstance(raw, list) or not raw:
        raise ServiceError(400, "invalid_items", "items must be a non-empty list of {productId, quantity}.")
    if len(raw) > MAX_ORDER_ITEMS:
        raise ServiceError(400, "invalid_items", "An order can have at most %d items." % MAX_ORDER_ITEMS)
    merged: dict[int, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ServiceError(400, "invalid_items", "Each item must be an object.")
        product_id = item.get("productId", item.get("product_id"))
        quantity = item.get("quantity", 1)
        if isinstance(product_id, str) and product_id.isdigit():
            product_id = int(product_id)
        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise ServiceError(400, "invalid_items", "productId must be a positive integer catalogue id.")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise ServiceError(400, "invalid_items", "quantity must be an integer from 1 to %d." % MAX_QUANTITY)
        merged[product_id] = merged.get(product_id, 0) + quantity
        if merged[product_id] > MAX_QUANTITY:
            raise ServiceError(400, "invalid_items", "quantity must be an integer from 1 to %d." % MAX_QUANTITY)
    return list(merged.items())


def place_order(user, items: list[tuple[int, int]], request=None):
    """Place an Oscar order for catalogue items, priced from the catalogue."""
    currency = settings.PAYPAL_CURRENCY
    strategy = Selector().strategy(request=request, user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in items:
            product = Product.objects.filter(pk=product_id).first()
            if product is None:
                raise ServiceError(422, "unknown_product", "Catalogue item %d does not exist." % product_id)
            info = strategy.fetch_for_product(product)
            if info.stockrecord is None or not info.price.exists:
                raise ServiceError(422, "not_for_sale", "Catalogue item %d has no price." % product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ServiceError(
                    422, "not_available", "Catalogue item %d cannot be bought: %s" % (product_id, reason)
                )
            basket.add_product(product, quantity)
        shipping_method = Repository().get_default_shipping_method(basket=basket, user=user, request=request)
        shipping_charge = shipping_method.calculate(basket)
        basket_total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        # Amounts come from the catalogue; the currency charged comes from configuration.
        total = Price(currency=currency, excl_tax=basket_total.excl_tax, incl_tax=basket_total.incl_tax)
        if total.incl_tax is None or total.incl_tax <= 0:
            raise ServiceError(422, "invalid_total", "The order total must be greater than zero.")
        if gateway.quantize(total.incl_tax, currency) != total.incl_tax:
            raise ServiceError(
                422, "invalid_total", "The order total %s cannot be expressed in %s." % (total.incl_tax, currency)
            )
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            request=request,
        )
        basket.submit()
    logger.info("Order %s placed by user %s for %s %s", order.number, user.pk, total.incl_tax, currency)
    return order


def get_order_for(user, order_id, *, allow_staff: bool = False):
    orders = Order.objects.all()
    if not (allow_staff and user.is_staff):
        orders = orders.filter(user=user)
    order = orders.filter(pk=order_id).first()
    if order is None:
        raise ServiceError(404, "order_not_found", "Order not found.")
    return order


def live_payment(order) -> PayPalPayment | None:
    return (
        PayPalPayment.objects.filter(order=order)
        .exclude(status__in=PayPalPayment.CLOSED_STATUSES)
        .select_related("saved_card")
        .first()
    )


def _advance_order(order, new_status: str) -> None:
    """Move the Oscar order along its pipeline (idempotent; never skips the history)."""
    order.refresh_from_db()
    if order.status == new_status:
        return
    if new_status == FULFILLED and order.status == AWAITING_PAYMENT:
        order.set_status(PAYMENT_AUTHORIZED)
    order.set_status(new_status)


# ---------------------------------------------------------------------------
# Pay (authorize)
# ---------------------------------------------------------------------------


@sensitive_variables("card")
def pay(user, order_id, *, card: CardDetails | None, payment_method_id: str | None) -> Outcome:
    order = get_order_for(user, order_id)
    if order.status not in (AWAITING_PAYMENT, PAYMENT_AUTHORIZED):
        raise ServiceError(409, "order_not_payable",
                           "Order %s is %s and cannot be paid." % (order.number, order.status))

    saved = None
    if payment_method_id:
        saved = _usable_card(user, payment_method_id)

    amount = order.total_incl_tax
    currency = order.currency
    try:
        with transaction.atomic():
            reference = uuid.uuid4()
            payment = PayPalPayment.objects.create(
                reference=reference, order=order, user=user, amount=amount, currency=currency,
                saved_card=saved, status=PayPalPayment.SENDING,
                invoice_id="%s-%s" % (order.number, reference.hex[:12]),
            )
    except IntegrityError:
        existing = live_payment(order)
        if existing is None:  # closed between the insert and this read: let the caller retry
            raise ServiceError(409, "payment_in_progress", "A payment for this order is being processed; retry.")
        return _resume_payment(existing, card=card)
    return _send_authorization(payment, card=card, resending=False)


def _usable_card(user, payment_method_id: str) -> PayPalSavedCard:
    try:
        public_id = uuid.UUID(str(payment_method_id))
    except ValueError:
        public_id = None
    saved = None
    if public_id is not None:
        saved = PayPalSavedCard.objects.filter(
            public_id=public_id, user=user, status=PayPalSavedCard.ACTIVE
        ).first()
    if saved is None:
        raise ServiceError(422, "payment_method_unusable", "That saved card does not exist or can no longer be used.")
    return saved


@sensitive_variables("card")
def _resume_payment(payment: PayPalPayment, *, card: CardDetails | None) -> Outcome:
    """A /pay request that lost the claim: answer from the existing payment."""
    if payment.status == PayPalPayment.PENDING and payment.authorization_id:
        return _refresh_authorization(payment)
    if payment.status not in (PayPalPayment.SENDING, PayPalPayment.UNKNOWN):
        return Outcome(payment, 200, "This order already has a payment.")
    if payment.status == PayPalPayment.SENDING and _is_fresh(payment.updated_at):
        return Outcome(payment, 202, "The payment is being processed.")
    # A stale or unknown authorization: resolve it by resending the identical
    # request under the SAME PayPal-Request-Id (PayPal returns the original).
    if timezone.now() - payment.created_at > RESEND_WINDOW:
        PayPalPayment.objects.filter(pk=payment.pk, status=payment.status).update(
            status=PayPalPayment.NEEDS_REVIEW, updated_at=timezone.now(),
            failure_message="Outcome unknown past PayPal's de-duplication window; check PayPal before retrying.",
        )
        payment.refresh_from_db()
        return Outcome(payment, 409, "The payment's outcome is unknown and needs an operator to review it.")
    if payment.saved_card is None and card is None:
        raise ServiceError(
            409, "payment_outcome_unknown",
            "The previous payment attempt's outcome is unknown. Resubmit the same card details to resolve it.",
        )
    taken = PayPalPayment.objects.filter(
        pk=payment.pk, status=payment.status, updated_at=payment.updated_at
    ).update(status=PayPalPayment.SENDING, updated_at=timezone.now())
    if not taken:
        payment.refresh_from_db()
        return Outcome(payment, 202, "The payment is being processed.")
    payment.refresh_from_db()
    return _send_authorization(payment, card=card, resending=True)


@sensitive_variables("card")
def _send_authorization(payment: PayPalPayment, *, card: CardDetails | None, resending: bool) -> Outcome:
    order = payment.order
    saved = payment.saved_card
    if saved is not None and saved.status != PayPalSavedCard.ACTIVE:
        payment.status = PayPalPayment.FAILED
        payment.failure_code = "payment_method_unusable"
        payment.failure_message = "The saved card was removed before the payment was sent."
        payment.save()
        raise ServiceError(422, "payment_method_unusable", payment.failure_message)
    try:
        result = gateway.create_authorized_order(
            request_id=str(payment.reference),
            amount=payment.amount,
            currency=payment.currency,
            invoice_id=payment.invoice_id,
            custom_id=str(payment.reference),
            description="Order %s" % order.number,
            card=None if saved is not None else card,
            vault_id=saved.paypal_token_id if saved is not None else None,
        )
    except PayPalError as exc:
        definitive = exc.is_rejection if resending else not exc.outcome_unknown
        payment.status = PayPalPayment.FAILED if definitive else PayPalPayment.UNKNOWN
        payment.failure_code = exc.issue[:64]
        payment.failure_message = exc.message
        payment.paypal_debug_id = exc.debug_id[:64]
        payment.save()
        logger.warning("Authorization for order %s: %s (%s)",
                       order.number, payment.status, exc.issue or exc.status_code)
        raise _paypal_failure(exc, "payment_failed" if definitive else "payment_outcome_unknown",
                              orderId=order.pk, paymentStatus=payment.status)

    payment.paypal_order_id = result.paypal_order_id
    payment.paypal_order_status = result.paypal_order_status
    payment.authorization_id = result.authorization_id or ""
    payment.authorization_status = result.authorization_status
    payment.card_brand = result.card_brand[:32]
    payment.card_last_digits = result.card_last_digits[:4]
    payment.authorization_expires_at = result.expiration_time
    payment.authorized_at = result.create_time or timezone.now()
    payment.failure_code = ""
    payment.failure_message = result.reason

    expected = (gateway.quantize(payment.amount, payment.currency), payment.currency)
    if result.outcome == "authorized" and result.amount is not None and (
        gateway.quantize(result.amount[0], payment.currency), result.amount[1]
    ) != expected:
        payment.status = PayPalPayment.NEEDS_REVIEW
        payment.failure_code = "amount_mismatch"
        payment.failure_message = "PayPal authorized %s %s instead of %s %s." % (
            result.amount[0], result.amount[1], expected[0], expected[1])
        payment.save()
        logger.error("Order %s: %s", order.number, payment.failure_message)
        return Outcome(payment, 409, "The authorization needs an operator to review it.")

    if result.outcome == "authorized":
        with transaction.atomic():
            payment.status = PayPalPayment.AUTHORIZED
            source = Source.objects.create(
                order=order, source_type=_source_type(), currency=payment.currency,
                reference=payment.authorization_id,
                label=("%s ending %s" % (payment.card_brand, payment.card_last_digits)).strip(),
            )
            source.allocate(payment.amount, reference=payment.authorization_id, status=result.authorization_status)
            payment.source = source
            payment.save()
            _advance_order(order, PAYMENT_AUTHORIZED)
        logger.info("Order %s authorized: %s", order.number, payment.authorization_id)
        return Outcome(payment, 200)
    if result.outcome == "pending":
        payment.status = PayPalPayment.PENDING
        payment.save()
        return Outcome(payment, 202, "PayPal accepted the payment but has not finished authorizing it.")
    if result.outcome in ("failed", "voided"):
        payment.status = PayPalPayment.FAILED
        payment.failure_code = payment.failure_code or "declined"
        payment.failure_message = result.reason or "The card was declined."
        payment.save()
        raise ServiceError(402, "payment_declined", payment.failure_message, orderId=order.pk,
                           paymentStatus=payment.status)
    payment.status = PayPalPayment.UNKNOWN
    payment.save()
    raise ServiceError(502, "payment_outcome_unknown",
                       result.reason or "PayPal returned an authorization state this app does not recognise.",
                       orderId=order.pk, paymentStatus=payment.status, outcomeUnknown=True)


def _refresh_authorization(payment: PayPalPayment) -> Outcome:
    try:
        result = gateway.get_authorization(payment.authorization_id)
    except PayPalError as exc:
        raise _paypal_failure(exc, "provider_unavailable")
    payment.authorization_status = result.authorization_status
    if result.expiration_time:
        payment.authorization_expires_at = result.expiration_time
    if result.outcome == "authorized":
        with transaction.atomic():
            payment.status = PayPalPayment.AUTHORIZED
            if payment.source is None:
                source = Source.objects.create(
                    order=payment.order, source_type=_source_type(), currency=payment.currency,
                    reference=payment.authorization_id,
                )
                source.allocate(payment.amount, reference=payment.authorization_id,
                                status=result.authorization_status)
                payment.source = source
            payment.save()
            _advance_order(payment.order, PAYMENT_AUTHORIZED)
        return Outcome(payment, 200)
    if result.outcome == "failed":
        payment.status = PayPalPayment.FAILED
        payment.failure_message = result.reason or "The authorization was denied."
        payment.save()
        return Outcome(payment, 200)
    payment.save()
    return Outcome(payment, 202, "PayPal has not finished authorizing the payment.")


# ---------------------------------------------------------------------------
# Fulfil (capture)
# ---------------------------------------------------------------------------


def fulfil(order_id) -> Outcome:
    order, payment = _payment_to_fulfil(order_id)
    if payment.status in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        _advance_order(order, FULFILLED)
        return Outcome(payment, 200, "The order was already fulfilled.")
    if payment.status in (PayPalPayment.CAPTURE_PENDING, PayPalPayment.UNKNOWN) and payment.capture_id:
        return _refresh_capture(payment)
    if payment.status == PayPalPayment.CAPTURING:
        # A stale capture claim is resolved by resending under the same request id.
        if not _take_over_stale_claim(payment, PayPalPayment.CAPTURING):
            return Outcome(payment, 202, "The capture is being processed.")
        return _capture(payment, resending=True)
    if payment.status != PayPalPayment.AUTHORIZED:
        raise ServiceError(
            409, "not_capturable",
            "The payment is %s; only an authorized payment can be captured." % payment.status,
            paymentStatus=payment.status,
        )
    taken = PayPalPayment.objects.filter(pk=payment.pk, status=PayPalPayment.AUTHORIZED).update(
        status=PayPalPayment.CAPTURING, updated_at=timezone.now()
    )
    payment.refresh_from_db()
    if not taken:
        return Outcome(payment, 202, "The capture is being processed.")
    return _capture(payment, resending=False)


def _payment_to_fulfil(order_id):
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        raise ServiceError(404, "order_not_found", "Order not found.")
    if order.status == CANCELLED:
        raise ServiceError(409, "order_cancelled", "Order %s was cancelled." % order.number)
    payment = live_payment(order)
    if payment is None:
        expired = PayPalPayment.objects.filter(order=order, status=PayPalPayment.EXPIRED).first()
        if expired is not None:
            raise ServiceError(409, "authorization_expired", expired.failure_message, orderId=order.pk)
        raise ServiceError(409, "not_authorized", "Order %s has no authorized payment to capture." % order.number)
    return order, payment


def _take_over_stale_claim(payment: PayPalPayment, status: str) -> bool:
    """Claim an in-flight step whose sender went quiet; False while it may still be running."""
    if _is_fresh(payment.updated_at):
        return False
    taken = PayPalPayment.objects.filter(
        pk=payment.pk, status=status, updated_at=payment.updated_at
    ).update(updated_at=timezone.now())
    payment.refresh_from_db()
    return bool(taken)


def _release_capture_claim(payment: PayPalPayment) -> None:
    PayPalPayment.objects.filter(pk=payment.pk, status=PayPalPayment.CAPTURING).update(
        status=PayPalPayment.AUTHORIZED, updated_at=timezone.now()
    )


def _expire(payment: PayPalPayment, detail: str) -> ServiceError:
    order = payment.order
    message = (
        "The card authorization %s for order %s can no longer be captured or renewed (%s). No money was "
        "taken. Ask the shopper to pay again with POST /api/orders/%s/pay, or cancel the order."
        % (payment.authorization_id, order.number, detail, order.pk)
    )
    payment.status = PayPalPayment.EXPIRED
    payment.failure_code = "authorization_expired"
    payment.failure_message = message
    payment.save()
    logger.warning("Order %s: authorization %s expired", order.number, payment.authorization_id)
    return ServiceError(409, "authorization_expired", message, orderId=order.pk)


def _renew_if_stale(payment: PayPalPayment) -> str:
    """Reauthorize an authorization past its honour period. Returns a note for the operator."""
    authorized_at = payment.authorized_at or payment.created_at
    if timezone.now() - authorized_at <= HONOR_PERIOD:
        return ""
    old_id = payment.authorization_id
    try:
        renewed = gateway.reauthorize(old_id, request_id=request_id(payment.reference, "reauthorize", old_id))
    except PayPalError as exc:
        if exc.outcome_unknown or not exc.is_rejection:
            _release_capture_claim(payment)
            raise _paypal_failure(exc, "renewal_outcome_unknown" if exc.outcome_unknown else "provider_unavailable")
        logger.warning("Reauthorization of %s refused (%s); capturing the original", old_id, exc.issue)
        return "PayPal refused to renew the authorization (%s: %s)" % (exc.issue or exc.status_code, exc.message)
    if renewed.outcome != "authorized" or not renewed.authorization_id:
        return "PayPal did not renew the authorization (status %s)" % (renewed.authorization_status or "unknown")
    payment.previous_authorization_ids = [*payment.previous_authorization_ids, old_id]
    payment.authorization_id = renewed.authorization_id
    payment.authorization_status = renewed.authorization_status
    payment.authorized_at = renewed.create_time or timezone.now()
    if renewed.expiration_time:
        payment.authorization_expires_at = renewed.expiration_time
    payment.save()
    if payment.source is not None:
        Transaction.objects.create(
            source=payment.source, txn_type="Reauthorise", amount=payment.amount,
            reference=renewed.authorization_id, status=renewed.authorization_status,
        )
    logger.info("Authorization %s renewed as %s", old_id, renewed.authorization_id)
    return ""


def _capture(payment: PayPalPayment, *, resending: bool) -> Outcome:
    now = timezone.now()
    if payment.authorization_expires_at and now >= payment.authorization_expires_at:
        raise _expire(payment, "it expired on %s" % payment.authorization_expires_at.isoformat())

    renewal_note = _renew_if_stale(payment)
    auth_id = payment.authorization_id
    try:
        result = gateway.capture(auth_id, request_id=request_id(payment.reference, "capture", auth_id))
    except PayPalError as exc:
        if exc.is_rejection:
            return _capture_rejected(payment, exc, renewal_note)
        if exc.outcome_unknown or resending:
            # May have landed: keep the capture claim; the next fulfil resends it
            # under the same PayPal-Request-Id, which PayPal de-duplicates.
            PayPalPayment.objects.filter(pk=payment.pk).update(updated_at=timezone.now())
            raise _paypal_failure(exc, "capture_outcome_unknown", orderId=payment.order_id,
                                  hint="Retry POST /api/orders/%s/fulfil to resolve it." % payment.order_id)
        _release_capture_claim(payment)
        raise _paypal_failure(exc, "provider_unavailable", orderId=payment.order_id)
    return _settle_capture(payment, result)


def _capture_rejected(payment: PayPalPayment, exc: PayPalError, renewal_note: str) -> Outcome:
    """PayPal refused the capture: find out what state the authorization is in."""
    try:
        current = gateway.get_authorization(payment.authorization_id)
    except PayPalError:
        current = None
    now = timezone.now()
    if current is not None and current.outcome == "captured":
        payment.status = PayPalPayment.NEEDS_REVIEW
        payment.failure_message = "PayPal reports the authorization as already captured."
        payment.save()
        raise ServiceError(409, "needs_review", payment.failure_message, orderId=payment.order_id)
    expired = (
        current is not None and current.expiration_time is not None and current.expiration_time <= now
    ) or (current is not None and current.outcome in ("voided", "failed"))
    if expired or renewal_note:
        detail = "PayPal refused the capture: %s: %s" % (exc.issue or exc.status_code, exc.message)
        if renewal_note:
            detail = "%s; %s" % (renewal_note, detail)
        raise _expire(payment, detail)
    _release_capture_claim(payment)
    raise _paypal_failure(exc, "capture_refused", orderId=payment.order_id,
                          hint="The authorization is still open; nothing was captured.")


def _settle_capture(payment: PayPalPayment, result: gateway.CaptureResult) -> Outcome:
    order = payment.order
    payment.capture_id = result.capture_id
    payment.capture_status = result.capture_status
    if result.amount is not None:
        payment.captured_amount = result.amount[0]
    if result.paypal_fee is not None:
        payment.paypal_fee = result.paypal_fee[0]
    if result.net_amount is not None:
        payment.net_amount = result.net_amount[0]
    payment.captured_at = result.create_time or timezone.now()

    expected = (gateway.quantize(payment.amount, payment.currency), payment.currency)
    if result.amount is None or (gateway.quantize(result.amount[0], payment.currency), result.amount[1]) != expected:
        payment.status = PayPalPayment.NEEDS_REVIEW
        payment.failure_code = "amount_mismatch"
        payment.failure_message = "PayPal captured %s instead of %s %s." % (
            "%s %s" % result.amount if result.amount else "an unreported amount", expected[0], expected[1])
        payment.save()
        logger.error("Order %s: %s", order.number, payment.failure_message)
        return Outcome(payment, 409, "The capture needs an operator to review it.")

    if result.outcome == "captured":
        with transaction.atomic():
            payment.status = PayPalPayment.CAPTURED
            payment.failure_code = ""
            payment.failure_message = ""
            payment.save()
            if payment.source is not None:
                payment.source.debit(payment.captured_amount, reference=result.capture_id,
                                     status=result.capture_status)
            _advance_order(order, FULFILLED)
        logger.info("Order %s captured: %s", order.number, result.capture_id)
        return Outcome(payment, 200)
    if result.outcome == "capture_pending":
        payment.status = PayPalPayment.CAPTURE_PENDING
        payment.failure_message = result.reason
        payment.save()
        return Outcome(payment, 202, "PayPal accepted the capture but has not completed it (%s)."
                       % (result.reason or "pending"))
    if result.outcome == "capture_failed":
        payment.status = PayPalPayment.NEEDS_REVIEW
        payment.failure_code = "capture_declined"
        payment.failure_message = "PayPal declined the capture (%s)." % (result.reason or result.capture_status)
        payment.save()
        raise ServiceError(402, "capture_declined", payment.failure_message, orderId=order.pk)
    payment.status = PayPalPayment.UNKNOWN
    payment.save()
    raise ServiceError(502, "capture_outcome_unknown",
                       "PayPal reported capture status %r, which this app does not recognise." % result.capture_status,
                       orderId=order.pk, outcomeUnknown=True)


def _refresh_capture(payment: PayPalPayment) -> Outcome:
    try:
        result = gateway.get_capture(payment.capture_id)
    except PayPalError as exc:
        raise _paypal_failure(exc, "provider_unavailable")
    return _settle_capture(payment, result)


# ---------------------------------------------------------------------------
# Cancel (void)
# ---------------------------------------------------------------------------


def cancel(order_id) -> Outcome:
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        raise ServiceError(404, "order_not_found", "Order not found.")
    payment = live_payment(order)
    if payment is not None and payment.status in (
        PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED,
        PayPalPayment.CAPTURING, PayPalPayment.CAPTURE_PENDING,
    ):
        raise ServiceError(409, "already_fulfilled",
                           "Order %s has been captured; use POST /api/orders/%s/refunds instead."
                           % (order.number, order.pk))
    if payment is None:
        return _cancel_order(order, None)
    if payment.status == PayPalPayment.VOIDING:
        if not _take_over_stale_claim(payment, PayPalPayment.VOIDING):
            return Outcome(payment, 202, "The cancellation is being processed.")
        return _void(payment, resending=True)
    if payment.status not in (PayPalPayment.AUTHORIZED, PayPalPayment.PENDING) or not payment.authorization_id:
        raise ServiceError(
            409, "payment_unresolved",
            "The payment is %s; resolve it before cancelling." % payment.status, paymentStatus=payment.status,
        )
    taken = PayPalPayment.objects.filter(pk=payment.pk, status=payment.status).update(
        status=PayPalPayment.VOIDING, updated_at=timezone.now()
    )
    if not taken:
        payment.refresh_from_db()
        return Outcome(payment, 202, "The cancellation is being processed.")
    payment.refresh_from_db()
    return _void(payment, resending=False)


def _cancel_order(order, payment: PayPalPayment | None) -> Outcome:
    with transaction.atomic():
        order.refresh_from_db()
        if order.status != CANCELLED:
            try:
                order.set_status(CANCELLED)
            except InvalidOrderStatus:
                raise ServiceError(409, "order_not_cancellable",
                                   "Order %s is %s and cannot be cancelled." % (order.number, order.status))
    return Outcome(payment if payment is not None else order, 200)


def _void(payment: PayPalPayment, *, resending: bool) -> Outcome:
    auth_id = payment.authorization_id
    try:
        result = gateway.void(auth_id, request_id=request_id(payment.reference, "void", auth_id))
    except PayPalError as exc:
        if exc.is_rejection:
            try:
                current = gateway.get_authorization(auth_id)
            except PayPalError:
                current = None
            if current is not None and current.outcome == "voided":
                return _settle_void(payment, current.authorization_status)
            PayPalPayment.objects.filter(pk=payment.pk, status=PayPalPayment.VOIDING).update(
                status=PayPalPayment.AUTHORIZED, updated_at=timezone.now()
            )
            raise _paypal_failure(exc, "void_refused", orderId=payment.order_id)
        if exc.outcome_unknown or resending:
            PayPalPayment.objects.filter(pk=payment.pk).update(updated_at=timezone.now())
            raise _paypal_failure(exc, "void_outcome_unknown", orderId=payment.order_id,
                                  hint="Retry POST /api/orders/%s/cancel to resolve it." % payment.order_id)
        PayPalPayment.objects.filter(pk=payment.pk, status=PayPalPayment.VOIDING).update(
            status=PayPalPayment.AUTHORIZED, updated_at=timezone.now()
        )
        raise _paypal_failure(exc, "provider_unavailable", orderId=payment.order_id)
    if result.outcome != "voided":
        PayPalPayment.objects.filter(pk=payment.pk).update(
            status=PayPalPayment.UNKNOWN, authorization_status=result.authorization_status,
            updated_at=timezone.now(),
        )
        raise ServiceError(502, "void_outcome_unknown",
                           "PayPal reported authorization status %r after the void." % result.authorization_status,
                           orderId=payment.order_id, outcomeUnknown=True)
    return _settle_void(payment, result.authorization_status)


def _settle_void(payment: PayPalPayment, authorization_status: str) -> Outcome:
    with transaction.atomic():
        payment.status = PayPalPayment.VOIDED
        payment.authorization_status = authorization_status
        payment.voided_at = timezone.now()
        payment.save()
        if payment.source is not None:
            Transaction.objects.create(
                source=payment.source, txn_type="Void", amount=payment.amount,
                reference=payment.authorization_id, status=authorization_status,
            )
    # The void is recorded first: it happened at PayPal whatever the order does next.
    logger.info("Order %s: authorization %s voided", payment.order.number, payment.authorization_id)
    return _cancel_order(payment.order, payment)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def parse_amount(raw: Any, currency: str) -> Decimal | None:
    if raw is None:
        return None
    try:
        amount = Decimal(str(raw))
    except InvalidOperation:
        raise ServiceError(400, "invalid_amount", "amount must be a decimal string such as \"10.00\".")
    if not amount.is_finite() or amount <= 0:
        raise ServiceError(400, "invalid_amount", "amount must be greater than zero.")
    if gateway.quantize(amount, currency) != amount:
        raise ServiceError(400, "invalid_amount", "amount has more decimal places than %s allows." % currency)
    return amount


def refund(user, order_id, *, amount_raw: Any, idempotency_key: str) -> Outcome:
    order = get_order_for(user, order_id, allow_staff=True)
    if not idempotency_key or len(idempotency_key) > 128:
        raise ServiceError(400, "idempotency_key_required",
                           "Send an Idempotency-Key header (or idempotencyKey field) of at most 128 characters.")
    payment = (
        PayPalPayment.objects.filter(
            order=order,
            status__in=[PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED],
        ).first()
    )
    if payment is None:
        raise ServiceError(409, "not_refundable",
                           "Order %s has no captured payment; cancel it instead of refunding." % order.number)
    amount = parse_amount(amount_raw, payment.currency)

    existing = PayPalRefund.objects.filter(payment=payment, idempotency_key=idempotency_key).first()
    if existing is not None:
        return _resume_refund(existing, amount)

    try:
        with transaction.atomic():
            payment.refresh_from_db()
            if amount is None:
                amount = payment.refundable_amount
                if amount <= 0:
                    raise ServiceError(422, "nothing_to_refund", "The payment has been fully refunded already.")
                reserved = PayPalPayment.objects.filter(
                    pk=payment.pk, refund_reserved=payment.refund_reserved
                ).update(refund_reserved=F("captured_amount"))
            else:
                reserved = PayPalPayment.objects.filter(
                    pk=payment.pk, refund_reserved__lte=F("captured_amount") - amount
                ).update(refund_reserved=F("refund_reserved") + amount)
            if not reserved:
                payment.refresh_from_db()
                raise ServiceError(
                    422, "refund_exceeds_captured",
                    "Refund of %s %s exceeds what is still refundable (%s %s)." % (
                        amount, payment.currency,
                        gateway.format_amount(payment.refundable_amount, payment.currency), payment.currency),
                    refundableAmount=gateway.format_amount(payment.refundable_amount, payment.currency),
                )
            record = PayPalRefund.objects.create(
                payment=payment, idempotency_key=idempotency_key, amount=amount,
                currency=payment.currency, requested_by=user,
            )
    except IntegrityError:
        # The same key was claimed concurrently; our reservation rolled back with it.
        existing = PayPalRefund.objects.get(payment=payment, idempotency_key=idempotency_key)
        return _resume_refund(existing, amount)
    return _send_refund(record, resending=False)


def _resume_refund(record: PayPalRefund, amount: Decimal | None) -> Outcome:
    if amount is not None and amount != record.amount:
        raise ServiceError(422, "idempotency_key_reused",
                           "This Idempotency-Key was already used for a refund of %s %s."
                           % (record.amount, record.currency),
                           refundId=str(record.public_id))
    if record.status in (PayPalRefund.DONE, PayPalRefund.FAILED, PayPalRefund.PENDING):
        answers = {PayPalRefund.DONE: 200, PayPalRefund.PENDING: 202, PayPalRefund.FAILED: 422}
        return Outcome(record, answers[record.status])
    if record.status == PayPalRefund.SENDING and _is_fresh(record.updated_at):
        return Outcome(record, 202, "The refund is being processed.")
    if timezone.now() - record.created_at > RESEND_WINDOW:
        return Outcome(record, 409, "The refund's outcome is unknown and needs an operator to review it.")
    taken = PayPalRefund.objects.filter(
        pk=record.pk, status=record.status, updated_at=record.updated_at
    ).update(status=PayPalRefund.SENDING, updated_at=timezone.now())
    if not taken:
        record.refresh_from_db()
        return Outcome(record, 202, "The refund is being processed.")
    record.refresh_from_db()
    return _send_refund(record, resending=True)


def _release_reservation(record: PayPalRefund) -> None:
    PayPalPayment.objects.filter(pk=record.payment_id).update(refund_reserved=F("refund_reserved") - record.amount)


def _send_refund(record: PayPalRefund, *, resending: bool) -> Outcome:
    payment = record.payment
    try:
        result = gateway.refund(
            payment.capture_id,
            request_id=request_id(payment.reference, "refund", record.idempotency_key),
            amount=record.amount,
            currency=record.currency,
        )
    except PayPalError as exc:
        definitive = exc.is_rejection if resending else not exc.outcome_unknown
        with transaction.atomic():
            record.status = PayPalRefund.FAILED if definitive else PayPalRefund.UNKNOWN
            record.failure_code = exc.issue[:64]
            record.failure_message = exc.message
            record.save()
            if definitive:
                _release_reservation(record)
        raise _paypal_failure(exc, "refund_failed" if definitive else "refund_outcome_unknown",
                              refundId=str(record.public_id), refundStatus=record.status)

    record.paypal_refund_id = result.refund_id
    record.paypal_status = result.refund_status
    record.refunded_at = result.create_time or timezone.now()
    if result.amount is None or (gateway.quantize(result.amount[0], record.currency), result.amount[1]) != (
        record.amount, record.currency
    ):
        record.status = PayPalRefund.UNKNOWN
        record.failure_code = "amount_mismatch"
        record.failure_message = "PayPal refunded %s instead of %s %s." % (
            "%s %s" % result.amount if result.amount else "an unreported amount", record.amount, record.currency)
        record.save()
        logger.error("Refund %s: %s", record.public_id, record.failure_message)
        return Outcome(record, 409, "The refund needs an operator to review it.")

    if result.outcome == "done":
        with transaction.atomic():
            record.status = PayPalRefund.DONE
            record.save()
            PayPalPayment.objects.filter(pk=payment.pk).update(refunded_amount=F("refunded_amount") + record.amount)
            payment.refresh_from_db()
            payment.status = (
                PayPalPayment.REFUNDED
                if payment.captured_amount is not None and payment.refunded_amount >= payment.captured_amount
                else PayPalPayment.PARTIALLY_REFUNDED
            )
            payment.save(update_fields=["status", "updated_at"])
            if payment.source is not None:
                payment.source.refund(record.amount, reference=result.refund_id, status=result.refund_status)
        logger.info("Refund %s of %s %s on order %s completed", result.refund_id, record.amount,
                    record.currency, payment.order.number)
        return Outcome(record, 201)
    if result.outcome == "pending":
        record.status = PayPalRefund.PENDING
        record.save()
        return Outcome(record, 202, "PayPal accepted the refund but has not completed it.")
    if result.outcome == "failed":
        with transaction.atomic():
            record.status = PayPalRefund.FAILED
            record.failure_message = "PayPal reported the refund as %s." % result.refund_status
            record.save()
            _release_reservation(record)
        raise ServiceError(422, "refund_failed", record.failure_message, refundId=str(record.public_id))
    record.status = PayPalRefund.UNKNOWN
    record.save()
    raise ServiceError(502, "refund_outcome_unknown",
                       "PayPal reported refund status %r, which this app does not recognise." % result.refund_status,
                       refundId=str(record.public_id), outcomeUnknown=True)


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


def _card_expiry_date(expiry: str) -> datetime.date:
    year, month = (int(part) for part in expiry.split("-")[:2])
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


@sensitive_variables("card")
def save_card(user, card: CardDetails, *, idempotency_key: str) -> Outcome:
    try:
        with transaction.atomic():
            record = PayPalSavedCard.objects.create(user=user, idempotency_key=idempotency_key)
    except IntegrityError:
        record = PayPalSavedCard.objects.get(user=user, idempotency_key=idempotency_key)
        if record.status == PayPalSavedCard.ACTIVE:
            return Outcome(record, 200, "This card was already saved.")
        if record.status == PayPalSavedCard.SENDING and _is_fresh(record.updated_at):
            return Outcome(record, 202, "The card is being saved.")
        if record.status in (PayPalSavedCard.SENDING, PayPalSavedCard.UNKNOWN):
            if timezone.now() - record.created_at > RESEND_WINDOW:
                return Outcome(record, 409, "The card's outcome is unknown; use a new Idempotency-Key.")
            taken = PayPalSavedCard.objects.filter(
                pk=record.pk, status=record.status, updated_at=record.updated_at
            ).update(status=PayPalSavedCard.SENDING, updated_at=timezone.now())
            if not taken:
                return Outcome(record, 202, "The card is being saved.")
            record.refresh_from_db()
            return _send_save_card(record, card, resending=True)
        raise ServiceError(409, "idempotency_key_reused",
                           "This Idempotency-Key was already used for a card that is %s." % record.status)
    return _send_save_card(record, card, resending=False)


@sensitive_variables("card")
def _send_save_card(record: PayPalSavedCard, card: CardDetails, *, resending: bool) -> Outcome:
    customer_id = (
        PayPalSavedCard.objects.filter(user=record.user).exclude(paypal_customer_id="")
        .values_list("paypal_customer_id", flat=True).first()
    )
    try:
        token = gateway.save_card(card, request_id=str(record.request_id), customer_id=customer_id)
    except PayPalError as exc:
        definitive = exc.is_rejection if resending else not exc.outcome_unknown
        record.status = PayPalSavedCard.FAILED if definitive else PayPalSavedCard.UNKNOWN
        record.failure_message = exc.message
        record.save()
        raise _paypal_failure(exc, "card_rejected" if definitive else "card_outcome_unknown")

    with transaction.atomic():
        record.paypal_token_id = token.token_id
        record.paypal_customer_id = token.customer_id
        record.brand = token.brand[:32]
        record.last_digits = token.last_digits[:4]
        record.expiry = token.expiry[:7]
        bankcard = Bankcard(
            user=record.user,
            name=token.name[:255],
            number="XXXX-XXXX-XXXX-%s" % token.last_digits,
            expiry_date=_card_expiry_date(token.expiry) if token.expiry else datetime.date.today(),
            partner_reference=token.token_id,
        )
        bankcard.card_type = token.brand or bankcard.card_type
        bankcard.save()
        record.bankcard = bankcard
        record.status = PayPalSavedCard.ACTIVE
        record.failure_message = ""
        record.save()
    logger.info("User %s saved a %s card ending %s", record.user_id, record.brand, record.last_digits)
    return Outcome(record, 201)


def list_cards(user):
    return PayPalSavedCard.objects.filter(user=user, status=PayPalSavedCard.ACTIVE).select_related("bankcard")


def delete_card(user, payment_method_id: str) -> Outcome:
    try:
        public_id = uuid.UUID(str(payment_method_id))
    except ValueError:
        raise ServiceError(404, "payment_method_not_found", "Saved card not found.")
    record = PayPalSavedCard.objects.filter(public_id=public_id, user=user).first()
    if record is None or record.status in (PayPalSavedCard.FAILED, PayPalSavedCard.SENDING, PayPalSavedCard.UNKNOWN):
        raise ServiceError(404, "payment_method_not_found", "Saved card not found.")
    if record.status == PayPalSavedCard.DELETED:
        return Outcome(record, 204)
    if record.status == PayPalSavedCard.ACTIVE:
        with transaction.atomic():
            taken = PayPalSavedCard.objects.filter(pk=record.pk, status=PayPalSavedCard.ACTIVE).update(
                status=PayPalSavedCard.DELETING, updated_at=timezone.now()
            )
            if taken and record.bankcard_id:
                Bankcard.objects.filter(pk=record.bankcard_id).delete()
        record.refresh_from_db()
    # DELETING: unusable and hidden locally; remove it at PayPal (safe to repeat).
    try:
        gateway.delete_token(record.paypal_token_id)
    except PayPalError as exc:
        logger.warning("Saved card %s removed locally; PayPal deletion pending (%s)", record.public_id, exc.message)
        return Outcome(record, 202, "The card was removed and can no longer be used; removal at PayPal will be "
                                    "retried by repeating this request.")
    PayPalSavedCard.objects.filter(pk=record.pk).update(status=PayPalSavedCard.DELETED, updated_at=timezone.now())
    record.refresh_from_db()
    logger.info("User %s deleted saved card %s", user.pk, record.public_id)
    return Outcome(record, 204)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def parse_datetime_param(raw: str | None, name: str) -> datetime.datetime:
    if not raw:
        raise ServiceError(400, "invalid_range", "Query parameter %s (ISO-8601 date-time) is required." % name)
    try:
        value = datetime.datetime.fromisoformat(raw.strip().replace(" ", "+").replace("Z", "+00:00"))
    except ValueError:
        raise ServiceError(400, "invalid_range", "%s must be an ISO-8601 date-time." % name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


def reconcile(start: datetime.datetime, end: datetime.datetime) -> dict[str, Any]:
    if end <= start:
        raise ServiceError(400, "invalid_range", "from must be before to.")
    if end - start > datetime.timedelta(days=MAX_RECONCILIATION_DAYS):
        raise ServiceError(400, "invalid_range", "The range can cover at most %d days." % MAX_RECONCILIATION_DAYS)

    provider, truncated, last_refreshed = _fetch_provider_transactions(start, end)
    # The PayPal filter is inclusive and second-granular: narrow back to [start, end).
    provider_records = [t for t in provider if t.initiated_at is None or start <= t.initiated_at < end]
    local_events = _local_events(start, end)
    matched, provider_only, unmatched_local = _match(provider_records, local_events, start)

    # A local event PayPal's report has not caught up with yet is not a discrepancy.
    local_only, not_yet_reported = [], []
    for event in unmatched_local:
        if last_refreshed is not None and event["at"] > last_refreshed:
            not_yet_reported.append(_event_json(event))
        else:
            local_only.append(_event_json(event))
    unsettled = _unsettled(start, end)

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalDataRefreshedAt": last_refreshed.isoformat() if last_refreshed else None,
        "truncated": truncated,
        "summary": {
            "paypalTransactions": len(provider_records),
            "matched": len(matched),
            "paypalOnly": len(provider_only),
            "appOnly": len(local_only),
            "notYetReportedByPaypal": len(not_yet_reported),
            "unsettled": len(unsettled),
            "amountMismatches": sum(1 for m in matched if not m["amountsAgree"]),
        },
        "matched": matched,
        "paypalOnly": provider_only,
        "appOnly": local_only,
        "notYetReportedByPaypal": not_yet_reported,
        "unsettled": unsettled,
    }


def _fetch_provider_transactions(start: datetime.datetime, end: datetime.datetime):
    """Every page of PayPal's transactions in [start, end], in <= 31-day windows (the API's maximum)."""
    provider: dict[str, gateway.ProviderTransaction] = {}
    truncated = False
    last_refreshed: datetime.datetime | None = None
    window_start = start
    while window_start < end:
        window_end = min(window_start + RECONCILIATION_WINDOW, end)
        # Windows share a boundary; end all but the last one a second early.
        query_end = window_end - datetime.timedelta(seconds=1) if window_end < end else window_end
        for page in range(1, RECONCILIATION_MAX_PAGES + 1):
            try:
                result = gateway.search_transactions(window_start, query_end, page=page)
            except PayPalError as exc:
                raise _paypal_failure(exc, "provider_unavailable")
            if result.last_refreshed and (last_refreshed is None or result.last_refreshed < last_refreshed):
                last_refreshed = result.last_refreshed
            provider.update((t.transaction_id, t) for t in result.transactions if t.transaction_id)
            if page >= result.total_pages or not result.transactions:
                break
        else:
            truncated = True
            logger.warning("Reconciliation stopped after %d pages for %s..%s",
                           RECONCILIATION_MAX_PAGES, window_start, window_end)
        window_start = window_end
    return list(provider.values()), truncated, last_refreshed


def _local_events(start: datetime.datetime, end: datetime.datetime) -> list[dict[str, Any]]:
    """Captures and refunds this app recorded, on PayPal's clock (the times PayPal reported)."""
    events: list[dict[str, Any]] = []
    captures = PayPalPayment.objects.filter(
        captured_at__gte=start, captured_at__lt=end).exclude(capture_id="").select_related("order")
    for p in captures:
        events.append({
            "type": "capture", "paypalId": p.capture_id, "orderId": p.order_id, "orderNumber": p.order.number,
            "amount": p.captured_amount, "currency": p.currency, "at": p.captured_at, "status": p.status,
        })
    refunds = PayPalRefund.objects.filter(
        refunded_at__gte=start, refunded_at__lt=end).exclude(paypal_refund_id="").select_related("payment__order")
    for r in refunds:
        events.append({
            "type": "refund", "paypalId": r.paypal_refund_id, "orderId": r.payment.order_id,
            "orderNumber": r.payment.order.number, "amount": -r.amount, "currency": r.currency,
            "at": r.refunded_at, "status": r.status,
        })
    return events


def _match(provider_records, local_events, start):
    """
    Pair PayPal's records with this app's events: by PayPal id first, then by the
    invoice id this app sent plus the signed amount. An order owns all of its
    records (capture and every refund), so matching is against the whole set.
    """
    invoices = {t.invoice_id for t in provider_records if t.invoice_id}
    known_orders = dict(PayPalPayment.objects.filter(invoice_id__in=invoices).values_list("invoice_id", "order_id"))
    unmatched = {e["paypalId"]: e for e in local_events}
    matched, provider_only = [], []
    for txn in sorted(provider_records, key=lambda t: t.initiated_at or start):
        event = unmatched.pop(txn.transaction_id, None)
        order_id = known_orders.get(txn.invoice_id)
        if event is None and order_id is not None and txn.amount is not None:
            candidate = next((e for e in unmatched.values()
                              if e["orderId"] == order_id and e["amount"] == txn.amount[0]), None)
            if candidate is not None:
                event = unmatched.pop(candidate["paypalId"])
        if event is not None:
            agree = txn.amount is not None and txn.amount == (event["amount"], event["currency"])
            matched.append({"paypal": _txn_json(txn), "local": _event_json(event), "amountsAgree": agree})
            continue
        entry = _txn_json(txn)
        if order_id is not None:
            entry["orderId"] = order_id
        provider_only.append(entry)
    return matched, provider_only, list(unmatched.values())


def _unsettled(start: datetime.datetime, end: datetime.datetime) -> list[dict[str, Any]]:
    """Payments and refunds whose PayPal outcome this app does not know yet (no PayPal time: created_at)."""
    payments = PayPalPayment.objects.filter(
        created_at__gte=start, created_at__lt=end,
        status__in=[PayPalPayment.SENDING, PayPalPayment.UNKNOWN, PayPalPayment.NEEDS_REVIEW,
                    PayPalPayment.CAPTURING, PayPalPayment.VOIDING, PayPalPayment.CAPTURE_PENDING],
    )
    refunds = PayPalRefund.objects.filter(
        created_at__gte=start, created_at__lt=end,
        status__in=[PayPalRefund.SENDING, PayPalRefund.UNKNOWN, PayPalRefund.PENDING],
    ).select_related("payment")
    return [
        {"type": "payment", "orderId": p.order_id, "paymentStatus": p.status, "createdAt": p.created_at.isoformat()}
        for p in payments
    ] + [
        {"type": "refund", "refundId": str(r.public_id), "orderId": r.payment.order_id,
         "refundStatus": r.status, "createdAt": r.created_at.isoformat()}
        for r in refunds
    ]


def _money_json(value: tuple[Decimal, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"value": str(value[0]), "currency": value[1]}


def _txn_json(txn: gateway.ProviderTransaction) -> dict[str, Any]:
    return {
        "transactionId": txn.transaction_id,
        "eventCode": txn.event_code,
        "status": txn.status,
        "initiatedAt": txn.initiated_at.isoformat() if txn.initiated_at else None,
        "amount": _money_json(txn.amount),
        "fee": _money_json(txn.fee),
        "invoiceId": txn.invoice_id,
        "customField": txn.custom_field,
    }


def _event_json(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": event["type"],
        "paypalId": event["paypalId"],
        "orderId": event["orderId"],
        "orderNumber": event["orderNumber"],
        "amount": {"value": gateway.format_amount(event["amount"], event["currency"]), "currency": event["currency"]}
        if event["amount"] is not None else None,
        "at": event["at"].isoformat() if event["at"] else None,
        "status": event["status"],
    }
