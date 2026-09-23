"""
Order, payment and saved-card flows.

Every PayPal write follows the same shape: claim a durable row first (a unique
constraint or a conditional UPDATE decides the single winner), call PayPal with
a PayPal-Request-Id stored on that row, verify what PayPal echoed, then settle
the row from PayPal's own status. A request that loses a claim never starts a
fresh PayPal write; a claim left "unknown" (or abandoned mid-flight) is only
ever resolved by resending under the *same* PayPal-Request-Id, which PayPal
de-duplicates.
"""
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import gateway as gw
from .models import PayPalCustomer, PayPalPayment, PayPalRefund, SavedCard

logger = logging.getLogger("apps.paypal_payments")

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")

# Oscar order statuses (sandbox OSCAR_ORDER_STATUS_PIPELINE).
STATUS_AWAITING_PAYMENT = settings.OSCAR_INITIAL_ORDER_STATUS  # "Pending"
STATUS_PAID = "Being processed"
STATUS_FULFILLED = "Complete"
STATUS_CANCELLED = "Cancelled"

# PayPal's honor period for an authorization (ReauthorizeRequest docstring): after
# three days it must be reauthorized, which is allowed once, up to day 29.
HONOR_PERIOD = timedelta(days=3)
MAX_ITEMS_PER_LINE = 100
MAX_LINES = 50
MAX_RECONCILIATION_RANGE = timedelta(days=1100)
SOURCE_TYPE_NAME = "PayPal"


class ServiceError(Exception):
    def __init__(self, http_status, code, message, **extra):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def paypal_config():
    return gw.PayPalConfig(
        client_id=settings.PAYPAL_CLIENT_ID,
        client_secret=settings.PAYPAL_CLIENT_SECRET,
        environment=settings.PAYPAL_ENVIRONMENT,
        currency=settings.PAYPAL_CURRENCY,
        base_url=settings.PAYPAL_BASE_URL,
        timeout=settings.PAYPAL_TIMEOUT,
    )


def get_gateway():
    try:
        return gw.get_gateway(paypal_config())
    except gw.ConfigurationError as exc:
        logger.error("PayPal is not configured: %s", exc)
        raise ServiceError(503, "payments_not_configured", str(exc))


def currency():
    code = (settings.PAYPAL_CURRENCY or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ServiceError(503, "payments_not_configured", "PAYPAL_CURRENCY must be an ISO-4217 code.")
    return code


def send_window():
    """How long a claim may legitimately be in flight before it counts as abandoned."""
    return timedelta(seconds=2 * settings.PAYPAL_TIMEOUT + 10)


def provider_failure(exc, what):
    """ServiceError for a ProviderError, keeping PayPal's issue codes visible."""
    extra = {}
    if exc.issues:
        extra["providerIssues"] = list(exc.issues)
    if exc.debug_id:
        extra["providerDebugId"] = exc.debug_id
    if exc.outcome_unknown:
        return ServiceError(exc.http_status, "outcome_unknown",
                            "%s: %s Repeat the same request to confirm the outcome." % (what, exc.message),
                            **extra)
    if exc.provider_status in (400, 404, 409, 422):
        return ServiceError(exc.http_status, "provider_rejected", "%s: %s" % (what, exc.message), **extra)
    return ServiceError(exc.http_status, "provider_unavailable", "%s: %s" % (what, exc.message), **extra)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_amount(raw, currency_code):
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ServiceError(400, "invalid_amount", "amount must be a decimal string such as \"10.00\".")
    if not value.is_finite() or value <= 0:
        raise ServiceError(400, "invalid_amount", "amount must be greater than zero.")
    if gw.quantize(value, currency_code) != value:
        raise ServiceError(400, "invalid_amount",
                           "amount has more decimal places than %s allows." % currency_code)
    return gw.quantize(value, currency_code)


def parse_card(data):
    """Validate one-off card details. Error messages never echo the input."""
    if not isinstance(data, dict):
        raise ServiceError(400, "invalid_card", "card must be an object.")
    number = re.sub(r"[\s-]", "", str(data.get("number", "")))
    if not re.fullmatch(r"\d{12,19}", number):
        raise ServiceError(400, "invalid_card", "card.number must be 12-19 digits.")
    expiry = str(data.get("expiry", "")).strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})", expiry) or re.fullmatch(r"(\d{2})/(\d{2,4})", expiry)
    if not match:
        raise ServiceError(400, "invalid_card", "card.expiry must be YYYY-MM (or MM/YY).")
    if "/" in expiry:
        month, year = int(match.group(1)), int(match.group(2))
        year = year + 2000 if year < 100 else year
    else:
        year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise ServiceError(400, "invalid_card", "card.expiry month must be 01-12.")
    today = date.today()
    if (year, month) < (today.year, today.month):
        raise ServiceError(400, "invalid_card", "card has expired.")
    security_code = str(data.get("securityCode", data.get("cvc", ""))).strip()
    if not re.fullmatch(r"\d{3,4}", security_code):
        raise ServiceError(400, "invalid_card", "card.securityCode must be 3 or 4 digits.")
    name = str(data.get("name", "")).strip()[:300]
    address = data.get("billingAddress")
    billing = None
    if address is not None:
        if not isinstance(address, dict) or not re.fullmatch(r"[A-Za-z]{2}", str(address.get("countryCode", ""))):
            raise ServiceError(400, "invalid_card", "card.billingAddress.countryCode must be a 2-letter code.")
        billing = gw.BillingAddress(
            country_code=str(address["countryCode"]).upper(),
            address_line_1=str(address.get("line1", ""))[:300],
            address_line_2=str(address.get("line2", ""))[:300],
            admin_area_2=str(address.get("city", ""))[:120],
            admin_area_1=str(address.get("state", ""))[:300],
            postal_code=str(address.get("postalCode", ""))[:60],
        )
    return gw.CardInput(number=number, expiry="%04d-%02d" % (year, month), security_code=security_code,
                        name=name, billing_address=billing)


def parse_datetime(raw, name):
    if not raw:
        raise ServiceError(400, "invalid_range", "%s is required (ISO-8601 date-time)." % name)
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise ServiceError(400, "invalid_range", "%s must be an ISO-8601 date-time." % name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


# ---------------------------------------------------------------------------
# Lookups (ownership is enforced here: another shopper's rows are "not found")
# ---------------------------------------------------------------------------


def order_for_user(user, number):
    try:
        order = Order.objects.get(number=number)
    except Order.DoesNotExist:
        raise ServiceError(404, "order_not_found", "Order not found.")
    if order.user_id != user.id and not user.is_staff:
        raise ServiceError(404, "order_not_found", "Order not found.")
    return order


def order_for_operator(number):
    try:
        return Order.objects.get(number=number)
    except Order.DoesNotExist:
        raise ServiceError(404, "order_not_found", "Order not found.")


def live_payment(order):
    return PayPalPayment.objects.filter(order=order).exclude(status=PayPalPayment.FAILED).first()


def order_amount(order):
    code = currency()
    total = Decimal(order.total_incl_tax)
    if gw.quantize(total, code) != total:
        raise ServiceError(422, "amount_not_representable",
                           "Order total %s cannot be charged in %s." % (total, code))
    return gw.quantize(total, code), code


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _fmt(value, code):
    return None if value is None else gw.format_amount(Decimal(value), code)


def _iso(value):
    return value.isoformat() if value else None


def payment_state(order, payment):
    if payment is None:
        return "cancelled" if order.status == STATUS_CANCELLED else "awaiting_payment"
    if payment.status == PayPalPayment.CAPTURED:
        refunded = sum((r.amount for r in payment.refunds.all() if r.status == PayPalRefund.DONE), Decimal(0))
        if refunded and refunded >= payment.captured_amount:
            return "refunded"
        if refunded:
            return "partially_refunded"
        return "captured"
    if payment.status == PayPalPayment.AUTHORIZED:
        if payment.capture_state in (PayPalPayment.STEP_SENDING, PayPalPayment.STEP_UNKNOWN,
                                     PayPalPayment.STEP_PENDING):
            return "capture_" + payment.capture_state
        if payment.void_state in (PayPalPayment.STEP_SENDING, PayPalPayment.STEP_UNKNOWN):
            return "void_" + payment.void_state
        return "authorized"
    return {
        PayPalPayment.SENDING: "authorizing",
        PayPalPayment.PENDING: "authorization_pending",
        PayPalPayment.UNKNOWN: "authorization_unknown",
        PayPalPayment.NEEDS_REVIEW: "needs_review",
        PayPalPayment.VOIDED: "voided",
    }.get(payment.status, payment.status)


def serialize_refund(refund):
    return {
        "refundId": str(refund.public_id),
        "status": refund.status,
        "amount": _fmt(refund.amount, refund.currency),
        "currency": refund.currency,
        "paypalRefundId": refund.paypal_refund_id or None,
        "paypalStatus": refund.paypal_status or None,
        "refundedAt": _iso(refund.refunded_at),
        "createdAt": _iso(refund.date_created),
    }


def serialize_payment(payment):
    if payment is None:
        return None
    code = payment.currency
    refunded = sum((r.amount for r in payment.refunds.all() if r.status == PayPalRefund.DONE), Decimal(0))
    return {
        "paymentId": str(payment.public_id),
        "status": payment.status,
        "amount": _fmt(payment.amount, code),
        "currency": code,
        "card": {
            "brand": payment.card_brand or None,
            "lastDigits": payment.card_last_digits or None,
            "paymentMethodId": str(payment.saved_card.public_id) if payment.saved_card_id else None,
        },
        "paypalOrderId": payment.paypal_order_id or None,
        "authorization": {
            "id": payment.authorization_id or None,
            "status": payment.authorization_status or None,
            "authorizedAt": _iso(payment.authorized_at),
            "expiresAt": _iso(payment.authorization_expires_at),
            "reauthorizedAt": _iso(payment.reauthorized_at),
        },
        "capture": {
            "state": payment.capture_state or None,
            "id": payment.capture_id or None,
            "status": payment.capture_status or None,
            "amount": _fmt(payment.captured_amount, code),
            "paypalFee": _fmt(payment.paypal_fee, code),
            "netAmount": _fmt(payment.net_amount, code),
            "capturedAt": _iso(payment.captured_at),
        },
        "void": {"state": payment.void_state or None, "voidedAt": _iso(payment.voided_at)},
        "refundedAmount": _fmt(refunded, code),
        "refundableAmount": _fmt(
            (payment.captured_amount - payment.refund_reserved) if payment.captured_amount is not None
            else Decimal(0), code),
        "refunds": [serialize_refund(r) for r in payment.refunds.all()],
        "lastError": payment.last_error or None,
    }


def serialize_order(order):
    payment = live_payment(order)
    code = order.currency
    return {
        "orderId": str(order.number),
        "status": order.status,
        "paymentStatus": payment_state(order, payment),
        "total": _fmt(order.total_incl_tax, code),
        "currency": code,
        "datePlaced": _iso(order.date_placed),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _fmt(line.unit_price_incl_tax, code),
                "linePrice": _fmt(line.line_price_incl_tax, code),
            }
            for line in order.lines.all()
        ],
        "payment": serialize_payment(payment),
    }


def serialize_card(card):
    return {
        "paymentMethodId": str(card.public_id),
        "brand": card.brand or None,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "name": card.name or None,
        "createdAt": _iso(card.date_created),
    }


# ---------------------------------------------------------------------------
# Oscar ledger mirror (payment.Source / payment.Transaction)
# ---------------------------------------------------------------------------


def _source(order, code):
    source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
    source, _ = Source.objects.get_or_create(order=order, source_type=source_type,
                                             defaults={"currency": code})
    return source


# ---------------------------------------------------------------------------
# Flow 1a — place an order
# ---------------------------------------------------------------------------


def place_order(user, items):
    if not isinstance(items, list) or not items:
        raise ServiceError(400, "invalid_items", "items must be a non-empty list.")
    if len(items) > MAX_LINES:
        raise ServiceError(400, "invalid_items", "At most %d items per order." % MAX_LINES)
    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise ServiceError(400, "invalid_items", "Each item needs productId and quantity.")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool) or \
                not isinstance(quantity, int) or isinstance(quantity, bool):
            raise ServiceError(400, "invalid_items", "productId and quantity must be integers.")
        if not 1 <= quantity <= MAX_ITEMS_PER_LINE:
            raise ServiceError(400, "invalid_items", "quantity must be 1-%d." % MAX_ITEMS_PER_LINE)
        quantities[product_id] = quantities.get(product_id, 0) + quantity

    code = currency()
    products = {p.id: p for p in Product.objects.filter(id__in=quantities, is_public=True)}
    missing = sorted(set(quantities) - set(products))
    if missing:
        raise ServiceError(404, "product_not_found", "Unknown product(s): %s" % missing)

    strategy = Selector().strategy(user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in quantities.items():
            product = products[product_id]
            info = strategy.fetch_for_product(product)
            if not product.is_public or product.is_parent or not info.price.exists:
                raise ServiceError(422, "product_not_purchasable",
                                   "Product %s cannot be bought directly." % product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ServiceError(422, "product_not_purchasable", "Product %s: %s" % (product_id, reason))
            basket.add_product(product, quantity)
        method = Free()
        shipping_charge = method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        number = OrderNumberGenerator().order_number(basket)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=method, shipping_charge=shipping_charge,
            user=user, order_number=number, status=STATUS_AWAITING_PAYMENT,
            currency=code,  # amounts come from catalogue prices, currency from configuration
        )
        basket.submit()
        order_amount(order)  # reject totals the currency cannot represent, before anything is charged
    logger.info("Order %s placed by user %s", order.number, user.id)
    return order


# ---------------------------------------------------------------------------
# Flow 1b — authorize
# ---------------------------------------------------------------------------


def _saved_card_for(user, payment_method_id):
    try:
        public_id = uuid.UUID(str(payment_method_id))
    except ValueError:
        raise ServiceError(404, "payment_method_not_found", "Payment method not found.")
    card = SavedCard.objects.filter(public_id=public_id, user=user, status=SavedCard.ACTIVE).first()
    if card is None:
        raise ServiceError(404, "payment_method_not_found", "Payment method not found.")
    return card


def _stored_payment_answer(payment):
    if payment.status in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURED):
        return payment
    if payment.status == PayPalPayment.VOIDED:
        raise ServiceError(409, "order_cancelled", "This order was cancelled and its hold released.")
    if payment.status == PayPalPayment.PENDING:
        return payment  # 202 at the view
    if payment.status == PayPalPayment.NEEDS_REVIEW:
        raise ServiceError(409, "needs_review", "This order's payment needs operator review: %s"
                           % payment.last_error)
    raise ServiceError(409, "payment_in_progress", "A payment for this order is already in progress.")


def authorize_order(user, number, card_data=None, payment_method_id=None):
    if (card_data is None) == (payment_method_id is None):
        raise ServiceError(400, "invalid_payment_source", "Send either card or paymentMethodId.")
    order = order_for_user(user, number)
    if order.user_id != user.id:
        raise ServiceError(404, "order_not_found", "Order not found.")
    card = parse_card(card_data) if card_data is not None else None
    saved = _saved_card_for(user, payment_method_id) if payment_method_id is not None else None
    amount, code = order_amount(order)

    # 1. Claim: one live payment per order (partial unique constraint).
    payment = None
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != STATUS_AWAITING_PAYMENT:
            existing = live_payment(locked)
            if existing is None:
                raise ServiceError(409, "order_not_payable", "Order is %s." % locked.status)
        try:
            with transaction.atomic():
                payment = PayPalPayment.objects.create(
                    order=locked, amount=amount, currency=code, saved_card=saved,
                    card_brand=saved.brand if saved else "",
                    card_last_digits=saved.last_digits if saved else card.number[-4:],
                )
        except IntegrityError:
            payment = None
    if payment is None:
        payment = _take_over_authorization(order, card, saved)

    # 2. Call PayPal under the claim's PayPal-Request-Id.
    source_card = None
    vault_id = None
    if payment.saved_card_id:
        vault_id = payment.saved_card.paypal_token_id
    else:
        source_card = card
    try:
        result = get_gateway().authorize(
            amount=payment.amount, currency=payment.currency, custom_id=payment.custom_id,
            request_id=str(payment.request_id), card=source_card, vault_id=vault_id)
    except gw.PayerActionRequired as exc:
        _settle_payment(payment, PayPalPayment.FAILED, paypal_order_id=exc.paypal_order_id,
                        last_error="PayPal requires shopper approval in a browser (unsupported).")
        raise ServiceError(409, "payer_action_required", exc.message)
    except gw.ProviderError as exc:
        if exc.outcome_unknown:
            _settle_payment(payment, PayPalPayment.UNKNOWN, last_error=exc.message)
        else:
            _settle_payment(payment, PayPalPayment.FAILED, last_error=exc.message)
        raise provider_failure(exc, "Card authorization failed")

    # 3. Verify the echoed amount before keeping it.
    fields = dict(
        paypal_order_id=result.paypal_order_id,
        authorization_id=result.authorization_id,
        authorization_status=result.provider_status,
        authorized_at=result.created_at or timezone.now(),
        authorization_expires_at=result.expires_at,
    )
    if (result.amount, result.currency) != (payment.amount, payment.currency):
        _settle_payment(payment, PayPalPayment.NEEDS_REVIEW,
                        last_error="PayPal authorized %s %s, expected %s %s"
                        % (result.amount, result.currency, payment.amount, payment.currency), **fields)
        raise ServiceError(409, "needs_review", "PayPal authorized a different amount; flagged for review.")

    # 4. Settle from PayPal's status.
    state = {"authorized": PayPalPayment.AUTHORIZED, "pending": PayPalPayment.PENDING,
             "failed": PayPalPayment.FAILED}.get(result.state, PayPalPayment.UNKNOWN)
    if result.card_brand and not payment.card_brand:
        fields["card_brand"] = result.card_brand
    if state == PayPalPayment.AUTHORIZED:
        with transaction.atomic():
            _settle_payment(payment, state, last_error="", **fields)
            order.refresh_from_db()
            if order.status == STATUS_AWAITING_PAYMENT:
                order.set_status(STATUS_PAID)
            source = _source(order, payment.currency)
            source.allocate(payment.amount, reference=result.authorization_id, status=result.provider_status)
        logger.info("Order %s authorized %s %s (%s)", order.number, payment.amount, payment.currency,
                    result.authorization_id)
        return payment
    if state == PayPalPayment.FAILED:
        _settle_payment(payment, state, last_error="Authorization %s" % result.provider_status, **fields)
        raise ServiceError(402, "card_declined", "PayPal declined the authorization (%s)." % result.provider_status)
    _settle_payment(payment, state, last_error="Authorization status %s" % result.provider_status, **fields)
    return payment


def _settle_payment(payment, status, **fields):
    fields["status"] = status
    for key, value in fields.items():
        setattr(payment, key, value)
    payment.save(update_fields=list(fields) + ["date_updated"])


def _take_over_authorization(order, card, saved):
    """The order already has a live payment: answer from it, or resend under its key."""
    payment = live_payment(order)
    if payment is None:  # the holder failed between our insert and this read
        raise ServiceError(409, "payment_in_progress", "Payment state changed; retry.")
    stale = timezone.now() - send_window()
    resumable = payment.status == PayPalPayment.UNKNOWN or (
        payment.status == PayPalPayment.SENDING and payment.date_updated < stale)
    if not resumable:
        return _raise_or_return(payment)
    # Resending must reproduce the original request under the original key.
    if payment.saved_card_id:
        if saved is None or saved.pk != payment.saved_card_id:
            raise ServiceError(409, "outcome_unknown",
                               "An earlier payment attempt with saved card %s has an unknown outcome; "
                               "repeat that request to confirm it." % payment.saved_card.public_id)
    elif card is None or card.number[-4:] != payment.card_last_digits:
        raise ServiceError(409, "outcome_unknown",
                           "An earlier card payment attempt has an unknown outcome; repeat it with the "
                           "same card to confirm it.")
    won = PayPalPayment.objects.filter(pk=payment.pk, status=payment.status,
                                       date_updated=payment.date_updated).update(
        status=PayPalPayment.SENDING, date_updated=timezone.now())
    if not won:
        raise ServiceError(409, "payment_in_progress", "A payment for this order is already in progress.")
    payment.refresh_from_db()
    return payment


class _Answered(Exception):
    def __init__(self, payment):
        self.payment = payment


def _raise_or_return(payment):
    raise _Answered(_stored_payment_answer(payment))


def pay(user, number, card_data=None, payment_method_id=None):
    """Authorize; a repeat of a settled request answers with the stored payment."""
    try:
        return authorize_order(user, number, card_data, payment_method_id)
    except _Answered as answered:
        return answered.payment


# ---------------------------------------------------------------------------
# Flow 1c — fulfil (capture), with authorization renewal
# ---------------------------------------------------------------------------


def _claim_step(payment, state_field, request_field, claimed_field, allowed_from, **conditions):
    """Conditional UPDATE: exactly one request moves the step to 'sending'."""
    now = timezone.now()
    won = PayPalPayment.objects.filter(pk=payment.pk, **{state_field + "__in": allowed_from}, **conditions).update(
        **{state_field: PayPalPayment.STEP_SENDING, request_field: uuid.uuid4(), claimed_field: now})
    if won:
        payment.refresh_from_db()
        return True
    # Abandoned in flight, or unknown: resume under the SAME request id.
    stale = now - send_window()
    resumed = PayPalPayment.objects.filter(pk=payment.pk, **{state_field: PayPalPayment.STEP_UNKNOWN}).update(
        **{state_field: PayPalPayment.STEP_SENDING, claimed_field: now}) or \
        PayPalPayment.objects.filter(pk=payment.pk, **{state_field: PayPalPayment.STEP_SENDING,
                                                       claimed_field + "__lt": stale}).update(
        **{claimed_field: now})
    payment.refresh_from_db()
    return bool(resumed)


def fulfil_order(number):
    order = order_for_operator(number)
    payment = live_payment(order)
    if payment is None:
        raise ServiceError(409, "order_not_paid", "Order %s has no authorized payment to capture." % number)
    if payment.capture_state == PayPalPayment.STEP_DONE:
        return payment, order
    if payment.void_state:
        raise ServiceError(409, "order_cancelled", "Order %s was cancelled; nothing to capture." % number)
    if payment.status != PayPalPayment.AUTHORIZED:
        raise ServiceError(409, "payment_not_authorized",
                           "Payment is %s; only an authorized payment can be captured." % payment.status)
    if payment.capture_state == PayPalPayment.STEP_NEEDS_REVIEW:
        raise ServiceError(409, "needs_review", "The capture needs operator review: %s" % payment.last_error)

    claimed = _claim_step(payment, "capture_state", "capture_request_id", "capture_claimed_at",
                          ["", PayPalPayment.STEP_FAILED], void_state="", status=PayPalPayment.AUTHORIZED)
    if not claimed:
        if payment.capture_state == PayPalPayment.STEP_DONE:
            return payment, order
        if payment.capture_state == PayPalPayment.STEP_PENDING:
            return payment, order
        if payment.void_state:
            raise ServiceError(409, "order_cancelled", "Order %s was cancelled; nothing to capture." % number)
        raise ServiceError(409, "capture_in_progress", "A capture for this order is already in progress.")

    gateway = get_gateway()
    try:
        _renew_authorization_if_stale(gateway, payment)
    except ServiceError:
        PayPalPayment.objects.filter(pk=payment.pk).update(capture_state=PayPalPayment.STEP_FAILED)
        raise

    try:
        result = gateway.capture(payment.authorization_id, amount=payment.amount, currency=payment.currency,
                                 request_id=str(payment.capture_request_id))
    except gw.ProviderError as exc:
        step = PayPalPayment.STEP_UNKNOWN if exc.outcome_unknown else PayPalPayment.STEP_FAILED
        PayPalPayment.objects.filter(pk=payment.pk).update(capture_state=step, last_error=exc.message[:500])
        if not exc.outcome_unknown and exc.provider_status in (400, 404, 409, 422):
            raise _capture_refused(payment, exc)
        raise provider_failure(exc, "Capture failed")

    fields = dict(capture_id=result.capture_id, capture_status=result.provider_status,
                  captured_amount=result.amount, paypal_fee=result.fee, net_amount=result.net,
                  captured_at=result.captured_at or timezone.now())
    if (result.amount, result.currency) != (payment.amount, payment.currency):
        _update(payment, capture_state=PayPalPayment.STEP_NEEDS_REVIEW,
                last_error="PayPal captured %s %s, expected %s %s"
                % (result.amount, result.currency, payment.amount, payment.currency), **fields)
        raise ServiceError(409, "needs_review", "PayPal captured a different amount; flagged for review.")

    if result.state == "captured":
        with transaction.atomic():
            _update(payment, capture_state=PayPalPayment.STEP_DONE, status=PayPalPayment.CAPTURED,
                    last_error="", **fields)
            order.refresh_from_db()
            if order.status == STATUS_AWAITING_PAYMENT:
                order.set_status(STATUS_PAID)
            if order.status == STATUS_PAID:
                order.set_status(STATUS_FULFILLED)
            source = _source(order, payment.currency)
            source.debit(result.amount, reference=result.capture_id, status=result.provider_status)
        logger.info("Order %s captured %s %s (%s) fee=%s net=%s", order.number, result.amount,
                    result.currency, result.capture_id, result.fee, result.net)
        return payment, order
    step = {"pending": PayPalPayment.STEP_PENDING, "failed": PayPalPayment.STEP_FAILED}.get(
        result.state, PayPalPayment.STEP_UNKNOWN)
    _update(payment, capture_state=step, last_error="Capture status %s" % result.provider_status, **fields)
    if step == PayPalPayment.STEP_FAILED:
        raise ServiceError(402, "capture_declined", "PayPal declined the capture (%s)." % result.provider_status)
    return payment, order


def _update(payment, **fields):
    for key, value in fields.items():
        setattr(payment, key, value)
    payment.save(update_fields=list(fields) + ["date_updated"])


def _capture_refused(payment, exc):
    now = timezone.now()
    base = payment.reauthorized_at or payment.authorized_at
    hint = ""
    if payment.authorization_expires_at and now >= payment.authorization_expires_at:
        hint = " The authorization expired at %s." % payment.authorization_expires_at.isoformat()
    elif base and now - base > HONOR_PERIOD:
        hint = (" The authorization is past its honor period and has already been renewed once, "
                "so it cannot be renewed again.")
    return ServiceError(
        409, "capture_refused",
        "PayPal refused to capture the held funds: %s.%s Cancel the order to release any hold and ask the "
        "shopper to pay again." % (exc.message.rstrip("."), hint),
        providerIssues=list(exc.issues), providerDebugId=exc.debug_id or None)


def _renew_authorization_if_stale(gateway, payment):
    """Reauthorize once when the 3-day honor period has passed; refuse clearly when it cannot be renewed."""
    now = timezone.now()
    if payment.authorization_expires_at and now >= payment.authorization_expires_at:
        raise ServiceError(
            409, "authorization_expired",
            "The payment authorization expired at %s and can no longer be renewed or captured. Cancel the "
            "order and ask the shopper to pay again." % payment.authorization_expires_at.isoformat())
    if payment.reauthorized_at is not None or payment.authorized_at is None:
        return
    if now - payment.authorized_at <= HONOR_PERIOD:
        return
    if payment.reauthorize_request_id is None:
        PayPalPayment.objects.filter(pk=payment.pk, reauthorize_request_id__isnull=True).update(
            reauthorize_request_id=uuid.uuid4())
        payment.refresh_from_db()
    try:
        renewed = gateway.reauthorize(payment.authorization_id, amount=payment.amount,
                                      currency=payment.currency, request_id=str(payment.reauthorize_request_id))
    except gw.ProviderError as exc:
        if exc.outcome_unknown:
            raise provider_failure(exc, "Renewing the stale authorization failed")
        raise ServiceError(
            409, "authorization_renewal_refused",
            "The authorization from %s is past PayPal's 3-day honor period and PayPal refused to renew it: "
            "%s. Cancel the order to release the hold and ask the shopper to pay again."
            % (payment.authorized_at.isoformat(), exc.message.rstrip(".")),
            providerIssues=list(exc.issues), providerDebugId=exc.debug_id or None)
    if renewed.state != "authorized":
        raise ServiceError(
            409, "authorization_renewal_refused",
            "PayPal renewed the authorization with status %s, which cannot be captured. Cancel the order and "
            "ask the shopper to pay again." % renewed.provider_status)
    _update(payment, authorization_id=renewed.authorization_id, authorization_status=renewed.provider_status,
            reauthorized_at=now, authorization_expires_at=renewed.expires_at or payment.authorization_expires_at)
    logger.info("Order %s authorization renewed as %s", payment.order.number, renewed.authorization_id)


# ---------------------------------------------------------------------------
# Flow 1d — cancel (void)
# ---------------------------------------------------------------------------


def cancel_order(number):
    order = order_for_operator(number)
    payment = live_payment(order)
    if payment is None:
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            if live_payment(locked) is not None:
                raise ServiceError(409, "payment_in_progress", "A payment was just started; retry.")
            if locked.status != STATUS_CANCELLED:
                if locked.status != STATUS_AWAITING_PAYMENT:
                    raise ServiceError(409, "order_not_cancellable", "Order is %s." % locked.status)
                locked.set_status(STATUS_CANCELLED)
        order.refresh_from_db()
        return None, order
    if payment.status == PayPalPayment.VOIDED:
        return payment, order
    if payment.status == PayPalPayment.CAPTURED or payment.capture_state not in ("", PayPalPayment.STEP_FAILED):
        raise ServiceError(409, "already_fulfilled",
                           "Funds for order %s have been (or are being) captured; issue a refund instead." % number)
    if payment.status != PayPalPayment.AUTHORIZED:
        raise ServiceError(409, "payment_not_settled",
                           "Payment is %s; it cannot be voided until its outcome is known." % payment.status)

    claimed = _claim_step(payment, "void_state", "void_request_id", "void_claimed_at",
                          ["", PayPalPayment.STEP_FAILED], capture_state__in=["", PayPalPayment.STEP_FAILED],
                          status=PayPalPayment.AUTHORIZED)
    if not claimed:
        if payment.void_state == PayPalPayment.STEP_DONE:
            return payment, order
        if payment.capture_state not in ("", PayPalPayment.STEP_FAILED):
            raise ServiceError(409, "already_fulfilled", "A capture is in progress; issue a refund instead.")
        raise ServiceError(409, "cancel_in_progress", "A cancellation for this order is already in progress.")

    try:
        result = get_gateway().void(payment.authorization_id, request_id=str(payment.void_request_id))
    except gw.ProviderError as exc:
        step = PayPalPayment.STEP_UNKNOWN if exc.outcome_unknown else PayPalPayment.STEP_FAILED
        PayPalPayment.objects.filter(pk=payment.pk).update(void_state=step, last_error=exc.message[:500])
        raise provider_failure(exc, "Releasing the held funds failed")

    if result.state == "voided":
        with transaction.atomic():
            _update(payment, void_state=PayPalPayment.STEP_DONE, status=PayPalPayment.VOIDED,
                    authorization_status=result.provider_status, voided_at=timezone.now(), last_error="")
            order.refresh_from_db()
            if order.status != STATUS_CANCELLED:
                order.set_status(STATUS_CANCELLED)
            source = _source(order, payment.currency)
            source.transactions.create(txn_type="Void", amount=payment.amount,
                                       reference=payment.authorization_id, status=result.provider_status)
        logger.info("Order %s cancelled; authorization %s voided", order.number, payment.authorization_id)
        return payment, order
    _update(payment, void_state=PayPalPayment.STEP_UNKNOWN, authorization_status=result.provider_status,
            last_error="Void returned status %s" % result.provider_status)
    raise ServiceError(502, "outcome_unknown",
                       "PayPal answered the void with status %s; repeat the request to confirm."
                       % result.provider_status)


# ---------------------------------------------------------------------------
# Flow 1e — refunds
# ---------------------------------------------------------------------------


def refund_order(user, number, idempotency_key, raw_amount):
    if not idempotency_key or len(idempotency_key) > 128:
        raise ServiceError(400, "idempotency_key_required",
                           "Send an Idempotency-Key header (1-128 characters) with every refund request.")
    order = order_for_user(user, number)
    payment = live_payment(order)
    if payment is None or payment.status != PayPalPayment.CAPTURED or not payment.capture_id:
        raise ServiceError(409, "not_refundable", "Order %s has no captured payment to refund." % number)
    code = payment.currency
    amount = parse_amount(raw_amount, code)

    refund = PayPalRefund.objects.filter(payment=payment, idempotency_key=idempotency_key).first()
    if refund is None:
        refund = _claim_refund(payment, idempotency_key, amount)
    else:
        refund = _replay_refund(refund, amount)
        if refund.status != PayPalRefund.SENDING:
            return refund

    try:
        result = get_gateway().refund(payment.capture_id, amount=refund.amount, currency=refund.currency,
                                      request_id=str(refund.request_id))
    except gw.ProviderError as exc:
        if exc.outcome_unknown:
            _update(refund, status=PayPalRefund.UNKNOWN, last_error=exc.message[:500])
        else:
            _fail_refund(refund, exc.message)
        raise provider_failure(exc, "Refund failed")

    fields = dict(paypal_refund_id=result.refund_id, paypal_status=result.provider_status,
                  refunded_at=result.refunded_at or timezone.now())
    if (result.amount, result.currency) != (refund.amount, refund.currency):
        _update(refund, status=PayPalRefund.NEEDS_REVIEW,
                last_error="PayPal refunded %s %s, expected %s %s"
                % (result.amount, result.currency, refund.amount, refund.currency), **fields)
        raise ServiceError(409, "needs_review", "PayPal refunded a different amount; flagged for review.")
    status = {"done": PayPalRefund.DONE, "pending": PayPalRefund.PENDING,
              "failed": PayPalRefund.FAILED}.get(result.state, PayPalRefund.UNKNOWN)
    if status == PayPalRefund.FAILED:
        _update(refund, **fields)
        _fail_refund(refund, "Refund status %s" % result.provider_status)
        raise ServiceError(402, "refund_failed", "PayPal reported the refund as %s." % result.provider_status)
    with transaction.atomic():
        _update(refund, status=status, last_error="", **fields)
        if status == PayPalRefund.DONE:
            _source(order, code).refund(refund.amount, reference=result.refund_id, status=result.provider_status)
    logger.info("Order %s refund %s %s %s (%s)", order.number, refund.amount, code, status, result.refund_id)
    return refund


def _claim_refund(payment, key, amount):
    remaining = payment.captured_amount - payment.refund_reserved
    if amount is None:
        amount = remaining
    if amount <= 0:
        raise ServiceError(409, "nothing_to_refund", "The captured amount has already been refunded.")
    try:
        with transaction.atomic():
            # The reservation is one conditional UPDATE: refunds can never exceed the capture.
            reserved = PayPalPayment.objects.filter(
                pk=payment.pk, captured_amount__gte=F("refund_reserved") + amount).update(
                refund_reserved=F("refund_reserved") + amount)
            if not reserved:
                payment.refresh_from_db()
                raise ServiceError(
                    409, "refund_exceeds_captured",
                    "Refund of %s exceeds the refundable amount %s." % (
                        gw.format_amount(amount, payment.currency),
                        gw.format_amount(payment.captured_amount - payment.refund_reserved, payment.currency)))
            return PayPalRefund.objects.create(payment=payment, idempotency_key=key, amount=amount,
                                               currency=payment.currency)
    except IntegrityError:
        refund = PayPalRefund.objects.get(payment=payment, idempotency_key=key)
        replayed = _replay_refund(refund, amount)
        if replayed.status == PayPalRefund.SENDING:
            return replayed
        raise _RefundAnswered(replayed)


class _RefundAnswered(Exception):
    def __init__(self, refund):
        self.refund = refund


def _replay_refund(refund, amount):
    """Same idempotency key again: never a second refund."""
    if amount is not None and amount != refund.amount:
        raise ServiceError(422, "idempotency_key_reused",
                           "This Idempotency-Key was used for a refund of %s." % refund.amount)
    stale = timezone.now() - send_window()
    abandoned = refund.status == PayPalRefund.SENDING and refund.date_updated < stale
    if refund.status == PayPalRefund.UNKNOWN or abandoned:
        won = PayPalRefund.objects.filter(pk=refund.pk, status=refund.status,
                                          date_updated=refund.date_updated).update(
            status=PayPalRefund.SENDING, date_updated=timezone.now())
        if won:
            refund.refresh_from_db()
            return refund  # resend under the same PayPal-Request-Id
    if refund.status == PayPalRefund.SENDING:
        raise ServiceError(409, "refund_in_progress", "This refund is already in progress.")
    return refund


def _fail_refund(refund, message):
    with transaction.atomic():
        released = PayPalRefund.objects.filter(pk=refund.pk).exclude(status=PayPalRefund.FAILED).update(
            status=PayPalRefund.FAILED, last_error=message[:500])
        if released:
            PayPalPayment.objects.filter(pk=refund.payment_id).update(
                refund_reserved=F("refund_reserved") - refund.amount)
    refund.refresh_from_db()


def refund(user, number, idempotency_key, raw_amount):
    try:
        return refund_order(user, number, idempotency_key, raw_amount)
    except _RefundAnswered as answered:
        return answered.refund


# ---------------------------------------------------------------------------
# Flow 2 — saved cards
# ---------------------------------------------------------------------------


def save_card(user, card_data, idempotency_key=None):
    card = parse_card(card_data)
    key = idempotency_key or uuid.uuid4().hex
    if len(key) > 128:
        raise ServiceError(400, "invalid_idempotency_key", "Idempotency-Key must be at most 128 characters.")
    try:
        with transaction.atomic():
            saved = SavedCard.objects.create(user=user, idempotency_key=key, last_digits=card.number[-4:])
    except IntegrityError:
        saved = SavedCard.objects.get(user=user, idempotency_key=key)
        stale = timezone.now() - send_window()
        if saved.status == SavedCard.ACTIVE:
            return saved, False
        if saved.status in (SavedCard.DELETING, SavedCard.DELETED):
            raise ServiceError(409, "idempotency_key_reused", "This Idempotency-Key belongs to a deleted card.")
        resumable = saved.status == "unknown" or (saved.status == "sending" and saved.date_updated < stale)
        if not resumable or saved.last_digits != card.number[-4:] or not SavedCard.objects.filter(
                pk=saved.pk, status=saved.status, date_updated=saved.date_updated).update(
                status="sending", date_updated=timezone.now()):
            raise ServiceError(409, "save_in_progress", "This card is already being saved.")
        saved.refresh_from_db()

    customer = PayPalCustomer.objects.filter(user=user).first()
    try:
        vaulted = get_gateway().vault_card(card, customer_id=customer.paypal_customer_id if customer else None,
                                           request_id=str(saved.request_id))
    except gw.ProviderError as exc:
        if exc.outcome_unknown:
            _update(saved, status="unknown")
        else:
            saved.delete()  # definitively not saved: release the key
        raise provider_failure(exc, "Saving the card failed")
    if customer is None and vaulted.customer_id:
        try:
            with transaction.atomic():
                PayPalCustomer.objects.create(user=user, paypal_customer_id=vaulted.customer_id)
        except IntegrityError:
            pass  # a concurrent save recorded the customer first; this card keeps its own customer id
    _update(saved, status=SavedCard.ACTIVE, paypal_token_id=vaulted.token_id,
            paypal_customer_id=vaulted.customer_id, brand=vaulted.brand, last_digits=vaulted.last_digits,
            expiry=vaulted.expiry, name=vaulted.name)
    logger.info("User %s saved a %s card ending %s", user.id, vaulted.brand, vaulted.last_digits)
    return saved, True


def list_cards(user):
    return SavedCard.objects.filter(user=user, status=SavedCard.ACTIVE)


def delete_card(user, payment_method_id):
    try:
        public_id = uuid.UUID(str(payment_method_id))
    except ValueError:
        raise ServiceError(404, "payment_method_not_found", "Payment method not found.")
    card = SavedCard.objects.filter(public_id=public_id, user=user).first()
    if card is None or card.status not in (SavedCard.ACTIVE, SavedCard.DELETING, SavedCard.DELETED):
        raise ServiceError(404, "payment_method_not_found", "Payment method not found.")
    if card.status == SavedCard.DELETED:
        return
    # Hidden and unusable from this point on, whatever PayPal answers.
    SavedCard.objects.filter(pk=card.pk, status=SavedCard.ACTIVE).update(status=SavedCard.DELETING)
    try:
        get_gateway().delete_vaulted_card(card.paypal_token_id)
    except gw.ProviderError as exc:
        raise provider_failure(exc, "The card was removed here but deleting it at PayPal failed; "
                                    "repeat the request to finish")
    SavedCard.objects.filter(pk=card.pk).update(status=SavedCard.DELETED, date_updated=timezone.now())
    logger.info("User %s deleted saved card %s", user.id, card.public_id)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

CUSTOM_ID_RE = re.compile(r"^(?P<number>[^:]+):(?P<ref>[0-9a-f]{32})$")


@dataclass
class _Local:
    kind: str
    order_number: str
    paypal_id: str
    amount: Decimal
    currency: str
    at: datetime


def reconcile(raw_from, raw_to):
    start, end = parse_datetime(raw_from, "from"), parse_datetime(raw_to, "to")
    if start >= end:
        raise ServiceError(400, "invalid_range", "from must be before to.")
    if end - start > MAX_RECONCILIATION_RANGE:
        raise ServiceError(400, "invalid_range", "Range may span at most %d days." % MAX_RECONCILIATION_RANGE.days)
    try:
        search = get_gateway().search_transactions(start, end)
    except gw.ProviderError as exc:
        raise provider_failure(exc, "Transaction search failed")

    # Local side, on PayPal's clock (the create_time PayPal reported), same [start, end) window.
    local = {}
    captures = PayPalPayment.objects.select_related("order").filter(
        captured_at__gte=start, captured_at__lt=end).exclude(capture_id="")
    for p in captures:
        local[p.capture_id] = _Local("capture", p.order.number, p.capture_id, p.captured_amount, p.currency,
                                     p.captured_at)
    refunds = PayPalRefund.objects.select_related("payment__order").filter(
        refunded_at__gte=start, refunded_at__lt=end).exclude(paypal_refund_id="")
    for r in refunds:
        local[r.paypal_refund_id] = _Local("refund", r.payment.order.number, r.paypal_refund_id, r.amount,
                                           r.currency, r.refunded_at)

    refs = set()
    for txn in search.transactions:
        custom = CUSTOM_ID_RE.match(txn.custom_id or "")
        if custom:
            refs.add(uuid.UUID(custom.group("ref")))
    request_ids = {p.request_id.hex: p.order.number
                   for p in PayPalPayment.objects.select_related("order").filter(request_id__in=refs)}

    matched, provider_only, mismatches = [], [], []
    for txn in search.transactions:
        entry = _txn_json(txn)
        mine = local.pop(txn.transaction_id, None)
        if mine is not None:
            entry.update(kind=mine.kind, orderId=mine.order_number)
            if txn.amount is not None and (abs(txn.amount) != mine.amount or txn.currency != mine.currency):
                entry["localAmount"] = gw.format_amount(mine.amount, mine.currency)
                mismatches.append(entry)
            else:
                matched.append(entry)
            continue
        custom = CUSTOM_ID_RE.match(txn.custom_id or "")
        if custom and custom.group("ref") in request_ids:
            entry["orderId"] = request_ids[custom.group("ref")]
            entry["note"] = "Belongs to one of this app's orders but is not recorded locally."
        provider_only.append(entry)

    # PayPal's reporting lags live activity: a local record newer than PayPal's last refresh is not a
    # discrepancy yet, so it is reported separately rather than as local-only.
    refreshed = gw.parse_time(search.last_refreshed) if search.last_refreshed else None
    local_only, not_yet_reported = [], []
    for m in local.values():
        item = {"kind": m.kind, "orderId": m.order_number, "paypalId": m.paypal_id,
                "amount": gw.format_amount(m.amount, m.currency), "currency": m.currency, "at": _iso(m.at)}
        (not_yet_reported if refreshed is not None and m.at > refreshed else local_only).append(item)
    unsettled = [
        {"orderId": p.order.number, "paymentStatus": p.status, "captureState": p.capture_state or None,
         "voidState": p.void_state or None, "createdAt": _iso(p.date_created)}
        for p in PayPalPayment.objects.select_related("order").filter(date_created__gte=start, date_created__lt=end)
        if p.status in (PayPalPayment.SENDING, PayPalPayment.UNKNOWN, PayPalPayment.PENDING,
                        PayPalPayment.NEEDS_REVIEW)
        or p.capture_state in (PayPalPayment.STEP_SENDING, PayPalPayment.STEP_UNKNOWN,
                               PayPalPayment.STEP_PENDING, PayPalPayment.STEP_NEEDS_REVIEW)
        or p.void_state in (PayPalPayment.STEP_SENDING, PayPalPayment.STEP_UNKNOWN)
    ] + [
        {"orderId": r.payment.order.number, "refundId": str(r.public_id), "refundStatus": r.status,
         "createdAt": _iso(r.date_created)}
        for r in PayPalRefund.objects.select_related("payment__order").filter(
            date_created__gte=start, date_created__lt=end,
            status__in=[PayPalRefund.SENDING, PayPalRefund.UNKNOWN, PayPalRefund.PENDING,
                        PayPalRefund.NEEDS_REVIEW])
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "providerLastRefreshed": search.last_refreshed or None,
        "complete": not search.truncated,
        "summary": {
            "providerTransactions": len(search.transactions),
            "matched": len(matched),
            "providerOnly": len(provider_only),
            "localOnly": len(local_only),
            "notYetReportedByPayPal": len(not_yet_reported),
            "amountMismatches": len(mismatches),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "providerOnly": provider_only,
        "localOnly": local_only,
        "notYetReportedByPayPal": not_yet_reported,
        "amountMismatches": mismatches,
        "unsettled": unsettled,
    }


def _txn_json(txn):
    return {
        "paypalTransactionId": txn.transaction_id,
        "eventCode": txn.event_code,
        "status": txn.status,
        "initiatedAt": _iso(txn.initiated_at),
        "amount": None if txn.amount is None else str(txn.amount),
        "fee": None if txn.fee is None else str(txn.fee),
        "currency": txn.currency,
        "customId": txn.custom_id or None,
    }
