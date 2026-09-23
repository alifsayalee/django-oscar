"""
Order placement and the PayPal payment, saved-card and refund flows.

Every PayPal write follows the same shape (python-configuration-resilience):

1. claim - a row is inserted, or conditionally updated, and committed *before*
   PayPal is called, so a double-click or an overlapping retry loses the claim
   and never calls PayPal a second time;
2. call - with a PayPal-Request-Id derived from that row, so a resend of the
   same logical action is collapsed by PayPal;
3. settle - from the status PayPal reported, by name; money PayPal echoes back is
   compared with what was asked before the row is marked done. A call whose
   outcome is unknown leaves the row ``unknown``; invoking the same endpoint
   again resends under the same request id.

Views call these functions with the request user; ownership is enforced here.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from oscar.apps.partner.strategy import Selector
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price
from paypal.core import UNSET, ApiError, Failure, Success, UnsetType
from paypal.models import (
    AmountWithBreakdown,
    CaptureRequest,
    CardRequest,
    Customer,
    OrderRequest,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    RefundRequest,
)
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CheckoutPaymentIntent,
    OrderStatus,
    RefundStatus,
)
from pydantic import ValidationError

from . import gateway
from .gateway import PayPalError, PayPalRejected
from .models import OrderRequestKey, PayPalCustomer, PayPalPayment, PayPalRefund, SavedCard

logger = logging.getLogger("apps.payments")

Basket = get_model("basket", "Basket")
Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Applicator = get_class("offer.applicator", "Applicator")
Free = get_class("shipping.methods", "Free")
NoShippingRequired = get_class("shipping.methods", "NoShippingRequired")

# Oscar order statuses used by this API (see OSCAR_ORDER_STATUS_PIPELINE).
AWAITING_PAYMENT = "Awaiting payment"
PAYMENT_AUTHORISED = "Payment authorised"
FULFILLED = "Fulfilled"
CANCELLED = "Cancelled"

SOURCE_TYPE_NAME = "PayPal"

# PayPal's honor period for an authorization (payments.reauthorize_payment docstring).
HONOR_PERIOD = timedelta(days=3)

MAX_ITEMS_PER_ORDER = 50
MAX_QUANTITY_PER_LINE = 100


class ServiceError(Exception):
    """A request we refuse ourselves, with the HTTP status and body to answer."""

    def __init__(self, http_status, code, message, **extra):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self):
        body = {"error": self.code, "message": self.message}
        body.update(self.extra)
        return body


def send_window():
    """How long a claimed-but-unanswered row is presumed still in flight."""
    return timedelta(seconds=2 * float(getattr(settings, "PAYPAL_TIMEOUT_SECONDS", 20.0)) + 10)


def _now():
    return timezone.now()


# ---------------------------------------------------------------------------
# Status mapping - the one place a PayPal status becomes ours
# ---------------------------------------------------------------------------


def authorization_outcome(status):
    match status:
        case AuthorizationStatus.CREATED:
            return "authorized"
        case AuthorizationStatus.PENDING:
            return "pending"
        case AuthorizationStatus.DENIED:
            return "failed"
        case AuthorizationStatus.VOIDED:
            return "voided"
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return "captured"
        case _:
            return "unknown"


def capture_outcome(status):
    match status:
        case CaptureStatus.COMPLETED | CaptureStatus.PARTIALLY_REFUNDED | CaptureStatus.REFUNDED:
            return "captured"
        case CaptureStatus.PENDING:
            return "pending"
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return "failed"
        case _:
            return "unknown"


def refund_outcome(status):
    match status:
        case RefundStatus.COMPLETED:
            return PayPalRefund.COMPLETED
        case RefundStatus.PENDING:
            return PayPalRefund.PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return PayPalRefund.FAILED
        case _:
            return PayPalRefund.UNKNOWN


# ---------------------------------------------------------------------------
# Lookups with ownership
# ---------------------------------------------------------------------------


def get_own_order(user, order_number):
    try:
        return Order.objects.get(number=order_number, user=user)
    except Order.DoesNotExist:
        # Another shopper's order is indistinguishable from a missing one.
        raise ServiceError(404, "order_not_found", "Order not found.")


def get_order_for_staff(order_number):
    try:
        return Order.objects.get(number=order_number)
    except Order.DoesNotExist:
        raise ServiceError(404, "order_not_found", "Order not found.")


def live_payment(order):
    return PayPalPayment.objects.filter(order=order).exclude(state=PayPalPayment.FAILED).first()


def _set_order_status(order, status):
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status != status:
        order.set_status(status)
    return order


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def purchasable_products(request, query, limit, offset):
    strategy = Selector().strategy(request=request, user=request.user)
    code = gateway.currency()
    products = Product.objects.browsable().exclude(structure=Product.PARENT).order_by("pk")
    if query:
        products = products.filter(title__icontains=query[:100])
    items: list[dict] = []
    skipped = 0
    for product in products.iterator():
        info = strategy.fetch_for_product(product)
        if not info.availability.is_available_to_buy or info.price.incl_tax is None:
            continue
        if skipped < offset:
            skipped += 1
            continue
        if len(items) == limit:
            return items, True
        items.append({
            "productId": product.pk,
            "title": product.get_title(),
            "price": gateway.quantize(info.price.incl_tax, code),
            "currency": code,
        })
    return items, False


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def _parse_items(items):
    if not isinstance(items, list) or not items:
        raise ServiceError(400, "invalid_request", "items must be a non-empty list.")
    if len(items) > MAX_ITEMS_PER_ORDER:
        raise ServiceError(400, "invalid_request", "Too many items.")
    parsed: dict[int, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ServiceError(400, "invalid_request", "items[%d] must be an object." % index)
        product_id = item.get("productId")
        quantity = item.get("quantity", 1)
        if isinstance(product_id, bool) or not isinstance(product_id, (int, str)) or not str(product_id).isdigit():
            raise ServiceError(400, "invalid_request", "items[%d].productId must be a catalogue item id." % index)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY_PER_LINE:
            raise ServiceError(400, "invalid_request",
                               "items[%d].quantity must be an integer from 1 to %d." % (index, MAX_QUANTITY_PER_LINE))
        parsed[int(product_id)] = parsed.get(int(product_id), 0) + quantity
    return parsed


def _shipping_address(data):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ServiceError(400, "invalid_request", "shippingAddress must be an object.")
    required = ("firstName", "lastName", "line1", "city", "postcode", "countryCode")
    missing = [key for key in required if not isinstance(data.get(key), str) or not data.get(key).strip()]
    if missing:
        raise ServiceError(400, "invalid_request", "shippingAddress is missing: %s." % ", ".join(missing))
    try:
        country = Country.objects.get(iso_3166_1_a2=data["countryCode"].strip().upper())
    except Country.DoesNotExist:
        raise ServiceError(400, "invalid_request", "shippingAddress.countryCode is not a known country.")
    return ShippingAddress(
        first_name=data["firstName"].strip()[:255],
        last_name=data["lastName"].strip()[:255],
        line1=data["line1"].strip()[:255],
        line2=(data.get("line2") or "").strip()[:255] if isinstance(data.get("line2"), str) else "",
        line4=data["city"].strip()[:255],
        state=(data.get("state") or "").strip()[:255] if isinstance(data.get("state"), str) else "",
        postcode=data["postcode"].strip()[:64],
        country=country,
        phone_number=None,
    )


def place_order(request, user, items, shipping_address=None, idempotency_key=None):
    """
    Place an Oscar order from catalogue items, priced by Oscar's strategy, in
    the configured currency, and leave it awaiting payment.

    Returns ``(order, created)``.
    """
    quantities = _parse_items(items)
    address = _shipping_address(shipping_address)
    code = gateway.currency()

    if idempotency_key:
        try:
            with transaction.atomic():
                claim = OrderRequestKey.objects.create(user=user, key=idempotency_key)
        except IntegrityError:
            claim = OrderRequestKey.objects.get(user=user, key=idempotency_key)
            if claim.order_id:
                return claim.order, False
            raise ServiceError(409, "order_in_progress", "An order under this Idempotency-Key is being placed.")
    else:
        claim = None

    try:
        with transaction.atomic():
            products = Product.objects.in_bulk(list(quantities))
            missing = [str(pk) for pk in quantities if pk not in products]
            if missing:
                raise ServiceError(404, "product_not_found", "Unknown catalogue item(s): %s." % ", ".join(missing))

            basket = Basket.objects.create(owner=user)
            basket.strategy = Selector().strategy(request=request, user=user)
            for pk, quantity in quantities.items():
                product = products[pk]
                if not product.is_public or product.is_parent:
                    raise ServiceError(422, "product_not_purchasable",
                                       "Catalogue item %s cannot be bought directly." % pk)
                info = basket.strategy.fetch_for_product(product)
                allowed, reason = info.availability.is_purchase_permitted(quantity)
                if not allowed or info.price.excl_tax is None:
                    raise ServiceError(422, "product_not_purchasable",
                                       "Catalogue item %s: %s" % (pk, reason or "no price"))
                basket.add_product(product, quantity)
            Applicator().apply(basket, user, request)

            method = Free() if basket.is_shipping_required() else NoShippingRequired()
            shipping_charge = method.calculate(basket)
            total = OrderTotalCalculator().calculate(basket, shipping_charge)
            if total.incl_tax is None:
                raise ServiceError(422, "price_unknown", "Order total could not be determined.")
            # Catalogue prices are the amounts; the currency comes from configuration.
            incl_tax = gateway.quantize(total.incl_tax, code)
            if incl_tax <= 0:
                raise ServiceError(422, "zero_total", "Order total must be greater than zero.")
            order_total = Price(currency=code, excl_tax=gateway.quantize(total.excl_tax, code), incl_tax=incl_tax)

            if address is not None:
                address.save()
            order = OrderCreator().place_order(
                basket=basket,
                total=order_total,
                shipping_method=method,
                shipping_charge=shipping_charge,
                user=user,
                shipping_address=address,
                status=AWAITING_PAYMENT,
                request=request,
            )
            basket.submit()
            if claim is not None:
                claim.order = order
                claim.save(update_fields=["order"])
    except BaseException:
        if claim is not None:
            claim.delete()  # release the key: nothing was placed
        raise
    logger.info("Order %s placed for user %s (%s %s)", order.number, user.pk, order.total_incl_tax, code)
    return order, True


# ---------------------------------------------------------------------------
# Pay (authorize)
# ---------------------------------------------------------------------------


def _card_request(card=None, vault_id=None):
    try:
        if vault_id:
            return CardRequest(vault_id=vault_id)
        return CardRequest(**card)
    except ValidationError:
        # Never surface pydantic's message: it echoes the input (card data).
        raise ServiceError(400, "invalid_card", "Card details are invalid.")


def _payment_source_for(user, card=None, payment_method_id=None):
    """Returns (PaymentSource, SavedCard or None, label)."""
    if payment_method_id:
        saved = SavedCard.objects.filter(
            user=user, public_id=payment_method_id, state=SavedCard.ACTIVE
        ).first()
        if saved is None:
            raise ServiceError(404, "payment_method_not_found", "Saved card not found.")
        return PaymentSource(card=_card_request(vault_id=saved.paypal_token_id)), saved, _label(saved.brand, saved.last_digits)
    return PaymentSource(card=_card_request(card=card)), None, _label("CARD", card["number"][-4:])


def _label(brand, last_digits):
    return "%s ****%s" % (brand or "CARD", last_digits)


def pay(user, order_number, card=None, payment_method_id=None):
    """Authorize the order total. Idempotent: a repeat returns the payment already made."""
    order = get_own_order(user, order_number)
    existing = live_payment(order)
    if existing is not None:
        return _resume_pay(existing, user, card, payment_method_id)
    if order.status != AWAITING_PAYMENT:
        raise ServiceError(409, "order_not_payable", "Order is %s and cannot be paid." % order.status)
    if card is None and payment_method_id is None:
        raise ServiceError(400, "invalid_request", "Provide either card or paymentMethodId.")

    source, saved, label = _payment_source_for(user, card, payment_method_id)
    code = order.currency
    if code != gateway.currency():
        raise ServiceError(409, "currency_mismatch", "Order currency %s is not the configured currency." % code)

    attempt = PayPalPayment.objects.filter(order=order).count() + 1
    try:
        with transaction.atomic():
            payment = PayPalPayment.objects.create(
                order=order,
                attempt=attempt,
                state=PayPalPayment.CREATING,
                currency=code,
                amount=gateway.quantize(order.total_incl_tax, code),
                saved_card=saved,
                card_label=label,
            )
    except IntegrityError:
        # Lost the claim to a concurrent request: answer from its row, never call PayPal.
        winner = live_payment(order)
        if winner is None:
            raise ServiceError(409, "payment_in_progress", "A payment for this order is being processed.")
        return _resume_pay(winner, user, None, None)
    return _create_and_authorize(payment, source)


def _resume_pay(payment, user, card, payment_method_id):
    state = payment.state
    if state == PayPalPayment.CREATING and payment.date_updated > _now() - send_window():
        raise ServiceError(409, "payment_in_progress", "A payment for this order is being processed.",
                           paymentState=state)
    if state in (PayPalPayment.CREATING, PayPalPayment.UNKNOWN):
        if payment.paypal_order_id:
            return _reconcile_paypal_order(payment)
        # PayPal may or may not have the order; resend under the same request id,
        # which PayPal collapses if the first attempt landed.
        if card is None and payment_method_id is None:
            raise ServiceError(409, "payment_outcome_unknown",
                               "The previous payment attempt has an unknown outcome; repeat the pay request "
                               "with the same payment details to resolve it.", paymentState=state)
        source, _, _ = _payment_source_for(user, card, payment_method_id)
        _touch(payment, PayPalPayment.CREATING)
        return _create_and_authorize(payment, source)
    if state == PayPalPayment.AUTH_PENDING:
        return _refresh_authorization(payment)
    return payment  # authorized, captured, voided, needs_review... - the outcome already known


def _touch(payment, state):
    payment.state = state
    payment.save(update_fields=["state", "date_updated"])


def _fail(payment, reason, state=PayPalPayment.FAILED):
    payment.state = state
    payment.failure_reason = reason[:255]
    payment.save(update_fields=["state", "failure_reason", "date_updated"])


def _create_and_authorize(payment, source):
    order = payment.order
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=order.number,
                custom_id=order.number,
                # Unique per PayPal account, even across installs sharing it.
                invoice_id="%s-%s" % (order.number, payment.ref.hex[:12]),
                description=("Order %s" % order.number)[:127],
                amount=AmountWithBreakdown(
                    currency_code=payment.currency,
                    value=str(gateway.quantize(payment.amount, payment.currency)),
                ),
            )
        ],
        payment_source=source,
    )
    client = gateway.get_client()
    try:
        result = gateway.call(
            "orders.create_order", client.orders.create_order, body,
            pay_pal_request_id=payment.request_id("create"), prefer="return=representation",
        )
    except PayPalError as exc:
        _record_write_failure(payment, exc)
        raise
    paypal_order_id = gateway.text_or_empty(result.id)
    if not paypal_order_id:
        _fail(payment, "create_order returned no id", PayPalPayment.UNKNOWN)
        raise gateway.unreadable("orders.create_order", "id")
    payment.paypal_order_id = paypal_order_id
    payment.paypal_order_status = gateway.text_or_empty(result.status)
    payment.save(update_fields=["paypal_order_id", "paypal_order_status", "date_updated"])
    return _settle_paypal_order(payment, result)


def _record_write_failure(payment, exc):
    if exc.outcome_unknown:
        _fail(payment, exc.code, PayPalPayment.UNKNOWN)  # may have landed: holds the order
    else:
        _fail(payment, exc.issue or exc.code)  # definitely did not happen: releases the order


def _first_authorization(paypal_order):
    units = paypal_order.purchase_units
    if isinstance(units, UnsetType) or not units:
        return None
    payments = units[0].payments
    if isinstance(payments, UnsetType):
        return None
    authorizations = payments.authorizations
    if isinstance(authorizations, UnsetType) or not authorizations:
        return None
    return authorizations[-1]


def _settle_paypal_order(payment, paypal_order):
    status = paypal_order.status
    authorization = _first_authorization(paypal_order)
    if authorization is not None:
        return _settle_authorization(payment, authorization)
    match status:
        case OrderStatus.APPROVED:
            # Approved but not yet authorized: authorize it (same request id on resend).
            client = gateway.get_client()
            try:
                result = gateway.call(
                    "orders.authorize_order", client.orders.authorize_order, payment.paypal_order_id,
                    pay_pal_request_id=payment.request_id("authorize"), prefer="return=representation",
                )
            except PayPalError as exc:
                _record_write_failure(payment, exc)
                raise
            authorization = _first_authorization(result)
            if authorization is None:
                _fail(payment, "authorize_order returned no authorization", PayPalPayment.UNKNOWN)
                raise gateway.unreadable("orders.authorize_order", "authorization")
            return _settle_authorization(payment, authorization)
        case OrderStatus.PAYER_ACTION_REQUIRED:
            # A browser challenge (e.g. 3-D Secure). Out of scope by design: fail clearly.
            _fail(payment, "payer_action_required")
            raise ServiceError(
                422, "payer_action_required",
                "PayPal requires the shopper to approve this card payment in a browser; "
                "that flow is not supported by this API. No money was taken.",
            )
        case OrderStatus.VOIDED:
            _fail(payment, "paypal_order_voided")
            raise ServiceError(422, "payment_declined", "PayPal voided the payment. No money was taken.")
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.COMPLETED:
            _fail(payment, "no authorization in PayPal order", PayPalPayment.UNKNOWN)
            raise ServiceError(502, "payment_outcome_unknown",
                               "PayPal accepted the order but reported no authorization yet; retry to resolve.")
        case _:
            _fail(payment, "unmapped PayPal order status %s" % gateway.text_or_empty(status), PayPalPayment.UNKNOWN)
            raise ServiceError(502, "payment_outcome_unknown",
                               "PayPal reported an unrecognised order status; retry to resolve.")


def _settle_authorization(payment, authorization):
    auth_id = gateway.text_or_empty(authorization.id)
    if not auth_id:
        _fail(payment, "authorization without id", PayPalPayment.UNKNOWN)
        raise gateway.unreadable("authorization", "id")
    payment.authorization_id = auth_id
    payment.authorization_status = gateway.text_or_empty(authorization.status)
    payment.authorization_time = gateway.parse_time(authorization.create_time) or payment.authorization_time
    payment.authorization_expires = gateway.parse_time(authorization.expiration_time) or payment.authorization_expires

    outcome = authorization_outcome(authorization.status)
    echoed = gateway.amount_of(authorization.amount)
    if outcome in ("authorized", "pending") and echoed != (payment.amount, payment.currency):
        # PayPal holds money, but not the amount we asked for: visible, never "done".
        payment.state = PayPalPayment.NEEDS_REVIEW
        payment.failure_reason = "authorized amount %s differs from order total %s %s" % (
            echoed, payment.amount, payment.currency)
        payment.save()
        logger.error("Payment %s: %s", payment.ref, payment.failure_reason)
        raise ServiceError(502, "amount_mismatch",
                           "PayPal authorized a different amount than the order total; an operator must review it.")

    if outcome == "authorized":
        with transaction.atomic():
            payment.state = PayPalPayment.AUTHORIZED
            payment.failure_reason = ""
            payment.source = _record_allocation(payment)
            payment.save()
            _set_order_status(payment.order, PAYMENT_AUTHORISED)
        logger.info("Order %s authorized: %s %s (authorization %s)", payment.order.number,
                    payment.amount, payment.currency, auth_id)
        return payment
    if outcome == "pending":
        payment.state = PayPalPayment.AUTH_PENDING
        payment.save()
        return payment
    if outcome == "failed" or outcome == "voided":
        payment.state = PayPalPayment.FAILED
        payment.failure_reason = "authorization %s" % payment.authorization_status
        payment.save()
        raise ServiceError(402, "payment_declined", "The card was declined. No money was taken.",
                           paypalStatus=payment.authorization_status)
    payment.state = PayPalPayment.UNKNOWN
    payment.failure_reason = "unmapped authorization status %s" % payment.authorization_status
    payment.save()
    raise ServiceError(502, "payment_outcome_unknown",
                       "PayPal reported an unrecognised authorization status; retry to resolve.")


def _record_allocation(payment):
    source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
    source = payment.source or Source.objects.create(
        order=payment.order, source_type=source_type, currency=payment.currency,
        reference=payment.authorization_id, label=payment.card_label,
    )
    if not source.transactions.filter(txn_type=Transaction.AUTHORISE, reference=payment.authorization_id).exists():
        source.allocate(payment.amount, reference=payment.authorization_id, status=payment.authorization_status)
    return source


def _reconcile_paypal_order(payment):
    """Resolve an unknown create by reading the PayPal order it may have produced."""
    client = gateway.get_client()
    try:
        result = gateway.call("orders.get_order", client.orders.get_order, payment.paypal_order_id)
    except PayPalError:
        _touch(payment, PayPalPayment.UNKNOWN)
        raise
    payment.paypal_order_status = gateway.text_or_empty(result.status)
    payment.save(update_fields=["paypal_order_status", "date_updated"])
    return _settle_paypal_order(payment, result)


def _refresh_authorization(payment):
    client = gateway.get_client()
    result = gateway.call("payments.get_authorized_payment", client.payments.get_authorized_payment,
                          payment.authorization_id)
    return _settle_authorization(payment, result)


# ---------------------------------------------------------------------------
# Fulfil (capture)
# ---------------------------------------------------------------------------


def fulfil(order_number):
    order = get_order_for_staff(order_number)
    payment = live_payment(order)
    if payment is None:
        raise ServiceError(409, "order_not_paid", "Order has no authorized payment to capture.")
    if payment.state == PayPalPayment.CAPTURED:
        return payment
    if payment.state == PayPalPayment.CAPTURE_PENDING:
        return _refresh_capture(payment)
    stale = _now() - send_window()
    claimed = PayPalPayment.objects.filter(pk=payment.pk).filter(
        Q(state__in=[PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURE_UNKNOWN])
        | Q(state=PayPalPayment.CAPTURING, date_updated__lt=stale)
    ).update(state=PayPalPayment.CAPTURING, date_updated=_now())
    if not claimed:
        payment.refresh_from_db()
        if payment.state == PayPalPayment.CAPTURED:
            return payment
        if payment.state == PayPalPayment.CAPTURING:
            raise ServiceError(409, "capture_in_progress", "The capture is already being processed.")
        raise ServiceError(409, "order_not_capturable",
                           "Payment is %s and cannot be captured." % payment.state, paymentState=payment.state)
    payment.refresh_from_db()
    if order.status == CANCELLED:
        _touch(payment, PayPalPayment.AUTHORIZED)
        raise ServiceError(409, "order_cancelled", "Order is cancelled.")
    try:
        _ensure_fresh_authorization(payment)
    except BaseException:
        if payment.state == PayPalPayment.CAPTURING:
            _touch(payment, PayPalPayment.AUTHORIZED)
        raise
    return _capture(payment)


def _ensure_fresh_authorization(payment):
    """Renew an authorization past its honor period; refuse one that has expired."""
    client = gateway.get_client()
    current = gateway.call("payments.get_authorized_payment", client.payments.get_authorized_payment,
                           payment.authorization_id)
    outcome = authorization_outcome(current.status)
    payment.authorization_status = gateway.text_or_empty(current.status)
    payment.authorization_expires = gateway.parse_time(current.expiration_time) or payment.authorization_expires
    payment.authorization_time = gateway.parse_time(current.create_time) or payment.authorization_time
    payment.save(update_fields=["authorization_status", "authorization_expires", "authorization_time", "date_updated"])
    if outcome == "captured":
        return  # an earlier capture landed; the capture call below returns it (same request id)
    if outcome != "authorized":
        _touch(payment, PayPalPayment.AUTHORIZED)
        raise ServiceError(
            409, "authorization_not_capturable",
            "PayPal reports the authorization as %s, so it cannot be captured. Cancel the order and ask the "
            "shopper to pay again." % (payment.authorization_status or "unknown"),
            paypalStatus=payment.authorization_status,
        )
    now = _now()
    if payment.authorization_expires and payment.authorization_expires <= now:
        _touch(payment, PayPalPayment.AUTHORIZED)
        raise ServiceError(
            409, "authorization_expired",
            "The payment authorization expired on %s and can no longer be renewed. Cancel this order and ask "
            "the shopper to place and pay for a new one." % payment.authorization_expires.isoformat(),
        )
    if payment.authorization_time and payment.authorization_time + HONOR_PERIOD <= now:
        _reauthorize(payment)


def _reauthorize(payment):
    client = gateway.get_client()
    attempt = payment.reauthorization_count + 1
    try:
        result = gateway.call(
            "payments.reauthorize_payment", client.payments.reauthorize_payment, payment.authorization_id,
            pay_pal_request_id=payment.request_id("reauth-%d" % attempt), prefer="return=representation",
            body=ReauthorizeRequest(amount=gateway.money(payment.amount, payment.currency)),
        )
    except PayPalRejected as exc:
        _touch(payment, PayPalPayment.AUTHORIZED)
        raise ServiceError(
            409, "authorization_renewal_failed",
            "The authorization is past PayPal's 3-day honor period and PayPal refused to renew it (%s). "
            "Cancel this order and ask the shopper to pay again." % (exc.issue or exc.message),
            paypalIssue=exc.issue, paypalDebugId=exc.debug_id,
        )
    new_id = gateway.text_or_empty(result.id)
    if not new_id or authorization_outcome(result.status) != "authorized":
        _touch(payment, PayPalPayment.AUTHORIZED)
        raise ServiceError(
            409, "authorization_renewal_failed",
            "PayPal did not renew the authorization (status %s). Cancel this order and ask the shopper to pay "
            "again." % (gateway.text_or_empty(result.status) or "unknown"),
        )
    logger.info("Payment %s reauthorized: %s -> %s", payment.ref, payment.authorization_id, new_id)
    payment.authorization_id = new_id
    payment.authorization_status = gateway.text_or_empty(result.status)
    payment.authorization_time = gateway.parse_time(result.create_time) or _now()
    payment.authorization_expires = gateway.parse_time(result.expiration_time) or payment.authorization_expires
    payment.reauthorization_count = attempt
    payment.save()
    if payment.source_id:
        Transaction.objects.create(source=payment.source, txn_type="Reauthorise", amount=payment.amount,
                                   reference=new_id, status=payment.authorization_status)


def _capture(payment):
    client = gateway.get_client()
    try:
        result = gateway.call(
            "payments.capture_authorized_payment", client.payments.capture_authorized_payment,
            payment.authorization_id,
            pay_pal_request_id=payment.request_id("capture-%s" % payment.authorization_id),
            prefer="return=representation",
            body=CaptureRequest(amount=gateway.money(payment.amount, payment.currency), final_capture=True),
        )
    except PayPalError as exc:
        if exc.outcome_unknown:
            _fail(payment, exc.code, PayPalPayment.CAPTURE_UNKNOWN)
        else:
            _touch(payment, PayPalPayment.AUTHORIZED)  # definitely not captured; can be retried or cancelled
        raise
    return _settle_capture(payment, result)


def _settle_capture(payment, capture):
    capture_id = gateway.text_or_empty(capture.id)
    if not capture_id:
        _fail(payment, "capture without id", PayPalPayment.CAPTURE_UNKNOWN)
        raise gateway.unreadable("payments.capture_authorized_payment", "id")
    payment.capture_id = capture_id
    payment.capture_status = gateway.text_or_empty(capture.status)
    payment.capture_time = gateway.parse_time(capture.create_time) or payment.capture_time
    breakdown = capture.seller_receivable_breakdown
    if not isinstance(breakdown, UnsetType):
        gross = gateway.amount_of(breakdown.gross_amount)
        fee = gateway.amount_of(breakdown.paypal_fee)
        net = gateway.amount_of(breakdown.net_amount)
        payment.captured_amount = gross[0] if gross else payment.captured_amount
        payment.paypal_fee = fee[0] if fee else payment.paypal_fee
        payment.net_amount = net[0] if net else payment.net_amount
    echoed = gateway.amount_of(capture.amount)
    if echoed is not None and payment.captured_amount is None:
        payment.captured_amount = echoed[0]

    outcome = capture_outcome(capture.status)
    if outcome in ("captured", "pending") and echoed != (payment.amount, payment.currency):
        payment.state = PayPalPayment.NEEDS_REVIEW
        payment.failure_reason = "captured amount %s differs from %s %s" % (echoed, payment.amount, payment.currency)
        payment.save()
        logger.error("Payment %s: %s", payment.ref, payment.failure_reason)
        raise ServiceError(502, "amount_mismatch",
                           "PayPal captured a different amount than authorized; an operator must review it.")
    if outcome == "captured":
        with transaction.atomic():
            payment.state = PayPalPayment.CAPTURED
            payment.failure_reason = ""
            payment.save()
            source = payment.source
            if source and not source.transactions.filter(txn_type=Transaction.DEBIT, reference=capture_id).exists():
                source.debit(payment.captured_amount or payment.amount, reference=capture_id,
                             status=payment.capture_status)
            _set_order_status(payment.order, FULFILLED)
        logger.info("Order %s captured %s %s (fee %s, net %s; capture %s)", payment.order.number,
                    payment.captured_amount, payment.currency, payment.paypal_fee, payment.net_amount, capture_id)
        return payment
    if outcome == "pending":
        payment.state = PayPalPayment.CAPTURE_PENDING
        payment.save()
        return payment
    if outcome == "failed":
        payment.state = PayPalPayment.NEEDS_REVIEW
        payment.failure_reason = "capture %s" % payment.capture_status
        payment.save()
        raise ServiceError(422, "capture_declined",
                           "PayPal declined the capture (%s); an operator must review the order." %
                           payment.capture_status, paypalStatus=payment.capture_status)
    payment.state = PayPalPayment.CAPTURE_UNKNOWN
    payment.failure_reason = "unmapped capture status %s" % payment.capture_status
    payment.save()
    raise ServiceError(502, "capture_outcome_unknown", "PayPal reported an unrecognised capture status.")


def _refresh_capture(payment):
    client = gateway.get_client()
    result = gateway.call("payments.get_captured_payment", client.payments.get_captured_payment, payment.capture_id)
    return _settle_capture(payment, result)


# ---------------------------------------------------------------------------
# Cancel (void)
# ---------------------------------------------------------------------------


VOIDABLE = [PayPalPayment.AUTHORIZED, PayPalPayment.AUTH_PENDING, PayPalPayment.VOID_UNKNOWN]


def cancel(order_number):
    order = get_order_for_staff(order_number)
    if order.status == FULFILLED:
        raise ServiceError(409, "order_fulfilled", "Order is fulfilled; refund it instead of cancelling.")
    payment = live_payment(order)
    if payment is None:
        if order.status == CANCELLED:
            return order, None
        if CANCELLED not in order.available_statuses():
            raise ServiceError(409, "order_not_cancellable", "Order is %s and cannot be cancelled." % order.status)
        with transaction.atomic():
            _set_order_status(order, CANCELLED)
        return Order.objects.get(pk=order.pk), None
    if payment.state == PayPalPayment.VOIDED:
        return order, payment
    if payment.state in (PayPalPayment.CREATING, PayPalPayment.UNKNOWN):
        raise ServiceError(409, "payment_outcome_unknown",
                           "The payment attempt has not resolved yet; resolve it before cancelling.")
    if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.CAPTURING, PayPalPayment.CAPTURE_PENDING,
                         PayPalPayment.CAPTURE_UNKNOWN):
        raise ServiceError(409, "payment_captured", "Money has been (or may have been) captured; refund instead.")
    stale = _now() - send_window()
    claimed = PayPalPayment.objects.filter(pk=payment.pk).filter(
        Q(state__in=VOIDABLE) | Q(state=PayPalPayment.VOIDING, date_updated__lt=stale)
    ).update(state=PayPalPayment.VOIDING, date_updated=_now())
    if not claimed:
        payment.refresh_from_db()
        if payment.state == PayPalPayment.VOIDED:
            return order, payment
        raise ServiceError(409, "cancel_in_progress" if payment.state == PayPalPayment.VOIDING
                           else "order_not_cancellable",
                           "Payment is %s and cannot be voided now." % payment.state)
    payment.refresh_from_db()
    previous = PayPalPayment.AUTHORIZED
    client = gateway.get_client()
    try:
        result = gateway.call(
            "payments.void_payment", client.payments.void_payment, payment.authorization_id,
            pay_pal_request_id=payment.request_id("void"), prefer="return=representation",
        )
    except PayPalError as exc:
        _fail(payment, exc.issue or exc.code, PayPalPayment.VOID_UNKNOWN if exc.outcome_unknown else previous)
        raise
    status = result.status
    payment.authorization_status = gateway.text_or_empty(status)
    if authorization_outcome(status) != "voided":
        _fail(payment, "void answered %s" % payment.authorization_status, PayPalPayment.VOID_UNKNOWN)
        raise ServiceError(502, "void_outcome_unknown",
                           "PayPal did not confirm the release of funds (status %s); retry the cancel." %
                           (payment.authorization_status or "none"))
    with transaction.atomic():
        payment.state = PayPalPayment.VOIDED
        payment.failure_reason = ""
        payment.save()
        if payment.source_id:
            source = Source.objects.select_for_update().get(pk=payment.source_id)
            if not source.transactions.filter(txn_type="Void", reference=payment.authorization_id).exists():
                source.amount_allocated -= payment.amount
                source.save()
                Transaction.objects.create(source=source, txn_type="Void", amount=payment.amount,
                                           reference=payment.authorization_id, status=payment.authorization_status)
        _set_order_status(order, CANCELLED)
    logger.info("Order %s cancelled; authorization %s voided", order.number, payment.authorization_id)
    return Order.objects.get(pk=order.pk), payment


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def refundable_remaining(payment):
    captured = payment.captured_amount or Decimal("0")
    return gateway.quantize(captured - payment.refund_reserved, payment.currency)


def refund(user, order_number, idempotency_key, amount=None, reason=""):
    """Refund all or part of the capture. Returns ``(refund, created)``."""
    order = get_own_order(user, order_number)
    payment = live_payment(order)
    if payment is None or payment.state != PayPalPayment.CAPTURED or not payment.capture_id:
        raise ServiceError(409, "order_not_refundable", "Only a fulfilled (captured) order can be refunded.")
    code = payment.currency

    existing = PayPalRefund.objects.filter(payment=payment, idempotency_key=idempotency_key).first()
    if existing is not None:
        return _replay_refund(existing, amount), False

    value = gateway.quantize(amount, code) if amount is not None else refundable_remaining(payment)
    if amount is not None and value != amount:
        raise ServiceError(400, "invalid_amount", "amount has more decimal places than %s allows." % code)
    if value <= 0:
        raise ServiceError(409, "nothing_to_refund", "Nothing remains to be refunded on this order.")
    try:
        with transaction.atomic():
            refund_row = PayPalRefund.objects.create(
                payment=payment, idempotency_key=idempotency_key, amount=value, currency=code, reason=reason[:255],
            )
            # The cap, enforced by one conditional write: concurrent refunds cannot overshoot.
            reserved = PayPalPayment.objects.filter(
                pk=payment.pk, refund_reserved__lte=F("captured_amount") - value,
            ).update(refund_reserved=F("refund_reserved") + value)
            if not reserved:
                raise _OverRefund()
    except IntegrityError:
        existing = PayPalRefund.objects.get(payment=payment, idempotency_key=idempotency_key)
        return _replay_refund(existing, amount), False
    except _OverRefund:
        payment.refresh_from_db()
        raise ServiceError(422, "refund_exceeds_captured",
                           "Refund of %s exceeds the refundable remainder of %s %s." % (
                               value, refundable_remaining(payment), code),
                           refundableRemaining=str(refundable_remaining(payment)))
    return _send_refund(refund_row), True


class _OverRefund(Exception):
    pass


def _replay_refund(refund_row, amount):
    if amount is not None and gateway.quantize(amount, refund_row.currency) != refund_row.amount:
        raise ServiceError(409, "idempotency_key_reused",
                           "This idempotencyKey was already used for a refund of a different amount.")
    stale = refund_row.date_updated <= _now() - send_window()
    if refund_row.state == PayPalRefund.UNKNOWN or (refund_row.state == PayPalRefund.SENDING and stale):
        claimed = PayPalRefund.objects.filter(pk=refund_row.pk, date_updated=refund_row.date_updated).update(
            state=PayPalRefund.SENDING, date_updated=_now())
        if claimed:
            refund_row.refresh_from_db()
            return _send_refund(refund_row)  # same PayPal-Request-Id: PayPal collapses a duplicate
    if refund_row.state == PayPalRefund.SENDING:
        raise ServiceError(409, "refund_in_progress", "This refund is being processed.",
                           refundId=str(refund_row.public_id))
    if refund_row.state == PayPalRefund.PENDING and refund_row.paypal_refund_id:
        client = gateway.get_client()
        result = gateway.call("payments.get_refund", client.payments.get_refund, refund_row.paypal_refund_id)
        return _settle_refund(refund_row, result)
    return refund_row


def _release_reservation(refund_row):
    PayPalPayment.objects.filter(pk=refund_row.payment_id).update(
        refund_reserved=F("refund_reserved") - refund_row.amount)


def _send_refund(refund_row):
    payment = refund_row.payment
    client = gateway.get_client()
    try:
        result = gateway.call(
            "payments.refund_captured_payment", client.payments.refund_captured_payment, payment.capture_id,
            pay_pal_request_id=refund_row.request_id, prefer="return=representation",
            body=RefundRequest(amount=gateway.money(refund_row.amount, refund_row.currency)),
        )
    except PayPalError as exc:
        with transaction.atomic():
            if exc.outcome_unknown:
                refund_row.state = PayPalRefund.UNKNOWN  # keeps its reservation: it may have happened
            else:
                refund_row.state = PayPalRefund.FAILED
                _release_reservation(refund_row)
            refund_row.failure_reason = (exc.issue or exc.code)[:255]
            refund_row.save()
        raise
    return _settle_refund(refund_row, result)


def _settle_refund(refund_row, result):
    paypal_id = gateway.text_or_empty(result.id)
    if not paypal_id:
        refund_row.state = PayPalRefund.UNKNOWN
        refund_row.save()
        raise gateway.unreadable("payments.refund_captured_payment", "id")
    outcome = refund_outcome(result.status)
    echoed = gateway.amount_of(result.amount)
    with transaction.atomic():
        was_completed = refund_row.state == PayPalRefund.COMPLETED
        refund_row.paypal_refund_id = paypal_id
        refund_row.paypal_status = gateway.text_or_empty(result.status)
        refund_row.refund_time = gateway.parse_time(result.create_time) or refund_row.refund_time
        if outcome in (PayPalRefund.COMPLETED, PayPalRefund.PENDING) and echoed is not None \
                and echoed != (refund_row.amount, refund_row.currency):
            refund_row.state = PayPalRefund.UNKNOWN
            refund_row.failure_reason = "refunded amount %s differs from %s" % (echoed, refund_row.amount)
            refund_row.save()
            logger.error("Refund %s: %s", refund_row.public_id, refund_row.failure_reason)
            raise ServiceError(502, "amount_mismatch",
                               "PayPal refunded a different amount than requested; an operator must review it.")
        if outcome == PayPalRefund.FAILED and refund_row.state != PayPalRefund.FAILED:
            _release_reservation(refund_row)
        refund_row.state = outcome
        refund_row.save()
        if outcome == PayPalRefund.COMPLETED and not was_completed:
            source = refund_row.payment.source
            if source is not None:
                source.refund(refund_row.amount, reference=paypal_id, status=refund_row.paypal_status)
    logger.info("Refund %s for order %s: %s %s -> %s (PayPal %s)", refund_row.public_id,
                refund_row.payment.order.number, refund_row.amount, refund_row.currency, refund_row.state, paypal_id)
    if outcome == PayPalRefund.FAILED:
        raise ServiceError(422, "refund_failed", "PayPal did not complete the refund (%s)." % refund_row.paypal_status,
                           refundId=str(refund_row.public_id))
    return refund_row


# ---------------------------------------------------------------------------
# Saved cards (PayPal vault)
# ---------------------------------------------------------------------------


def save_card(user, card, client_key):
    try:
        with transaction.atomic():
            saved = SavedCard.objects.create(user=user, client_key=client_key, state=SavedCard.CREATING)
    except IntegrityError:
        saved = SavedCard.objects.get(user=user, client_key=client_key)
        stale = saved.date_updated <= _now() - send_window()
        if saved.state == SavedCard.ACTIVE:
            return saved, False
        if saved.state in (SavedCard.DELETING, SavedCard.DELETED):
            raise ServiceError(409, "payment_method_deleted", "The card saved under this key was deleted.")
        if saved.state == SavedCard.FAILED:
            raise ServiceError(422, "card_rejected", "PayPal rejected this card earlier (%s)." % saved.failure_reason)
        if saved.state == SavedCard.CREATING and not stale:
            raise ServiceError(409, "save_in_progress", "This card is being saved.")
        claimed = SavedCard.objects.filter(pk=saved.pk, date_updated=saved.date_updated).update(
            state=SavedCard.CREATING, date_updated=_now())
        if not claimed:
            raise ServiceError(409, "save_in_progress", "This card is being saved.")
        saved.refresh_from_db()

    customer = PayPalCustomer.objects.filter(user=user).first()
    try:
        body = PaymentTokenRequest(
            payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(**card)),
            customer=Customer(id=customer.paypal_customer_id) if customer else UNSET,
        )
    except ValidationError:
        saved.state = SavedCard.FAILED
        saved.failure_reason = "invalid card"
        saved.save()
        raise ServiceError(400, "invalid_card", "Card details are invalid.")

    client = gateway.get_client()
    try:
        token = gateway.call("vault.create_payment_token", client.vault.create_payment_token, body,
                             pay_pal_request_id=saved.request_id)
    except PayPalError as exc:
        saved.state = SavedCard.UNKNOWN if exc.outcome_unknown else SavedCard.FAILED
        saved.failure_reason = (exc.issue or exc.code)[:255]
        saved.save()
        raise

    token_id = gateway.text_or_empty(token.id)
    if not token_id:
        saved.state = SavedCard.UNKNOWN
        saved.save()
        raise gateway.unreadable("vault.create_payment_token", "id")
    customer_id = ""
    if not isinstance(token.customer, UnsetType):
        customer_id = gateway.text_or_empty(token.customer.id)
    brand = last_digits = expiry = ""
    source = token.payment_source
    if not isinstance(source, UnsetType) and not isinstance(source.card, UnsetType):
        brand = gateway.text_or_empty(source.card.brand)
        last_digits = gateway.text_or_empty(source.card.last_digits)
        expiry = gateway.text_or_empty(source.card.expiry)
    with transaction.atomic():
        saved.paypal_token_id = token_id
        saved.paypal_customer_id = customer_id
        saved.brand = brand
        saved.last_digits = last_digits or card["number"][-4:]
        saved.expiry = expiry or card["expiry"]
        saved.state = SavedCard.ACTIVE
        saved.failure_reason = ""
        saved.save()
        if customer_id and customer is None:
            PayPalCustomer.objects.get_or_create(user=user, defaults={"paypal_customer_id": customer_id})
    logger.info("User %s saved card %s (%s ****%s)", user.pk, saved.public_id, saved.brand, saved.last_digits)
    return saved, True


def list_cards(user):
    return SavedCard.objects.filter(user=user, state=SavedCard.ACTIVE)


def delete_card(user, public_id):
    saved = SavedCard.objects.filter(
        user=user, public_id=public_id, state__in=[SavedCard.ACTIVE, SavedCard.DELETING]
    ).first()
    if saved is None:
        raise ServiceError(404, "payment_method_not_found", "Saved card not found.")
    stale = _now() - send_window()
    # Claim; from here on the card is neither listed nor usable to pay.
    claimed = SavedCard.objects.filter(pk=saved.pk).filter(
        Q(state=SavedCard.ACTIVE) | Q(state=SavedCard.DELETING, date_updated__lt=stale)
    ).update(state=SavedCard.DELETING, date_updated=_now())
    if not claimed:
        raise ServiceError(409, "delete_in_progress", "This card is being deleted.")
    client = gateway.get_client()
    try:
        result = gateway.call("vault.delete_payment_token", client.vault.with_raw_response.delete_payment_token,
                              saved.paypal_token_id)
    except PayPalError:
        raise  # stays DELETING (hidden, unusable); a repeat DELETE resumes it
    match result:
        case Success():
            pass
        case Failure(response=response) if response.status_code == 404:
            pass  # already gone at PayPal
        case Failure():
            try:
                result.unwrap()
            except ApiError as exc:
                raise gateway.translate(exc, "vault.delete_payment_token") from exc
    SavedCard.objects.filter(pk=saved.pk).update(state=SavedCard.DELETED, date_updated=_now())
    logger.info("User %s deleted card %s", user.pk, saved.public_id)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def orders_for(user):
    return (
        Order.objects.filter(user=user)
        .prefetch_related("lines", "paypal_payments__refunds")
        .order_by("-date_placed")
    )


def refund_totals(payment):
    totals = {state: Decimal("0") for state in (PayPalRefund.COMPLETED, PayPalRefund.PENDING)}
    for row in payment.refunds.values("state").annotate(total=Sum("amount")):
        if row["state"] in totals:
            totals[row["state"]] = row["total"]
    return totals
