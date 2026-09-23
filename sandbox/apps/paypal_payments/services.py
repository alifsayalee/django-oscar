"""
Order, payment and saved-card flows.

Every action that calls PayPal first *claims* its row with one conditional
UPDATE (or a unique INSERT), so exactly one request — across processes — gets
to call PayPal for a given logical action. Everyone else answers from the row.
Each write carries a deterministic PayPal-Request-Id; when the outcome of a
call is unknown the row says so, and the next request for the same action
re-sends under the same id, which PayPal collapses into the original.
"""

import logging
import re
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_class, get_model

from . import money, paypal_calls
from .gateway import ProviderError, ProviderRejected
from .models import (
    PaymentState,
    PayPalCustomer,
    PayPalPayment,
    PayPalRefund,
    RefundState,
    SavedCard,
    SavedCardStatus,
)

logger = logging.getLogger("apps.paypal_payments")

Order = get_model("order", "Order")
PaymentEventType = get_model("order", "PaymentEventType")
ShippingAddress = get_model("order", "ShippingAddress")
Country = get_model("address", "Country")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Repository = get_class("shipping.repository", "Repository")
Selector = get_class("partner.strategy", "Selector")
Applicator = get_class("offer.applicator", "Applicator")
EventHandler = get_class("order.processing", "EventHandler")
Price = get_class("partner.prices", "FixedPrice")

# Oscar order statuses (see OSCAR_ORDER_STATUS_PIPELINE in sandbox/settings.py)
STATUS_AWAITING_PAYMENT = "Awaiting payment"
STATUS_PAYMENT_AUTHORIZED = "Payment authorized"
STATUS_COMPLETE = "Complete"
STATUS_CANCELLED = "Cancelled"

# A claim older than this with no answer is treated as abandoned (worker died);
# it is larger than one PayPal call's timeout, and the next attempt re-sends
# under the same PayPal-Request-Id, so no second effect can result.
SEND_WINDOW = timedelta(seconds=120)

# From the reauthorize_payment docstring: a 3-day honor period, and
# reauthorization possible until day 29 of the original authorization.
HONOR_PERIOD = timedelta(days=3)
REAUTHORIZE_LIMIT = timedelta(days=29)

MAX_LINES = 50
MAX_QUANTITY = 99


class ApiProblem(Exception):
    """An error our API reports to its caller."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra

    def as_dict(self):
        body = {"error": self.message}
        body.update(self.extra)
        return body


def _from_provider(error, status=None, prefix=""):
    """Wrap a gateway error for our caller, keeping its status and outcome flag."""
    body = error.as_dict()
    if prefix:
        body["error"] = "%s %s" % (prefix, body["error"])
    message = body.pop("error")
    return ApiProblem(status or error.status_code, message, **body)


def _now():
    return timezone.now()


# ==========================================================================
# Input parsing
# ==========================================================================

_EXPIRY = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_CVV = re.compile(r"^\d{3,4}$")


def _luhn_ok(number):
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@sensitive_variables("data", "number", "cvv")
def parse_card(data):
    """Validate card input. Error messages never echo the submitted values."""
    if not isinstance(data, dict):
        raise ApiProblem(400, "card must be an object")
    number = re.sub(r"[\s-]", "", str(data.get("number", "")))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise ApiProblem(422, "card.number is not a valid card number")
    expiry = str(data.get("expiry", ""))
    match = _EXPIRY.match(expiry)
    if not match:
        raise ApiProblem(422, "card.expiry must be YYYY-MM")
    today = _now().date()
    if (int(match.group(1)), int(match.group(2))) < (today.year, today.month):
        raise ApiProblem(422, "card.expiry is in the past")
    cvv = str(data.get("securityCode", ""))
    if not _CVV.match(cvv):
        raise ApiProblem(422, "card.securityCode must be 3 or 4 digits")
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 300:
        raise ApiProblem(422, "card.name is required (max 300 characters)")
    billing = data.get("billingAddress")
    if billing is not None:
        if not isinstance(billing, dict):
            raise ApiProblem(400, "card.billingAddress must be an object")
        billing = {k: str(v) for k, v in billing.items() if v not in (None, "")}
        if not re.fullmatch(r"[A-Z]{2}", billing.get("countryCode", "")):
            raise ApiProblem(
                422, "card.billingAddress.countryCode must be an ISO-3166 alpha-2 code"
            )
    return paypal_calls.CardInput(
        number=number,
        expiry=expiry,
        security_code=cvv,
        name=name,
        billing_address=billing,
    )


def parse_idempotency_key(value, required):
    key = (value or "").strip()
    if not key:
        if required:
            raise ApiProblem(
                400,
                "An idempotency key is required (Idempotency-Key header or idempotencyKey)",
            )
        return None
    if len(key) > 100 or not re.fullmatch(r"[A-Za-z0-9._:-]+", key):
        raise ApiProblem(
            400, "Idempotency key must be 1-100 characters of [A-Za-z0-9._:-]"
        )
    return key


# ==========================================================================
# Orders
# ==========================================================================


def _parse_lines(lines):
    if not isinstance(lines, list) or not lines:
        raise ApiProblem(400, "lines must be a non-empty list of {productId, quantity}")
    if len(lines) > MAX_LINES:
        raise ApiProblem(400, "At most %d lines per order" % MAX_LINES)
    merged = {}
    for item in lines:
        if not isinstance(item, dict):
            raise ApiProblem(400, "each line must be an object")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            raise ApiProblem(400, "productId must be an integer")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ApiProblem(400, "quantity must be a positive integer")
        merged[product_id] = merged.get(product_id, 0) + quantity
        if merged[product_id] > MAX_QUANTITY:
            raise ApiProblem(
                422, "quantity for product %d exceeds %d" % (product_id, MAX_QUANTITY)
            )
    return merged


def _shipping_address(data, user):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ApiProblem(400, "shippingAddress must be an object")
    required = ("firstName", "lastName", "line1", "city", "postcode", "countryCode")
    missing = [f for f in required if not str(data.get(f, "")).strip()]
    if missing:
        raise ApiProblem(422, "shippingAddress is missing: %s" % ", ".join(missing))
    try:
        country = Country.objects.get(iso_3166_1_a2=str(data["countryCode"]).upper())
    except Country.DoesNotExist:
        raise ApiProblem(
            422, "shippingAddress.countryCode is not a known country"
        ) from None
    address = ShippingAddress(
        first_name=str(data["firstName"])[:255],
        last_name=str(data["lastName"])[:255],
        line1=str(data["line1"])[:255],
        line2=str(data.get("line2", ""))[:255],
        line4=str(data["city"])[:255],
        state=str(data.get("state", ""))[:255],
        postcode=str(data["postcode"])[:64],
        country=country,
    )
    address.save()
    return address


def place_order(user, lines, shipping_address=None):
    """Create an Oscar order from catalogue products, awaiting payment."""
    currency = money.currency()
    wanted = _parse_lines(lines)
    products = Product.objects.in_bulk(list(wanted))
    unknown = [pid for pid in wanted if pid not in products]
    if unknown:
        raise ApiProblem(
            404, "Unknown product id(s): %s" % ", ".join(map(str, unknown))
        )

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(user=user)
        for pid, quantity in wanted.items():
            product = products[pid]
            info = basket.strategy.fetch_for_product(product)
            if not product.is_public or not info.availability.is_available_to_buy:
                raise ApiProblem(422, "Product %d is not available to buy" % pid)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ApiProblem(422, "Product %d: %s" % (pid, reason))
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, None)
        if basket.is_empty:
            raise ApiProblem(422, "No purchasable lines")

        address = _shipping_address(shipping_address, user)
        method = Repository().get_default_shipping_method(
            basket=basket, shipping_addr=address
        )
        shipping_charge = method.calculate(basket)
        basket_total = OrderTotalCalculator().calculate(basket, shipping_charge)
        # Amounts come from catalogue prices; the currency comes from configuration.
        total_amount = money.quantize(basket_total.incl_tax, currency)
        if total_amount <= 0:
            raise ApiProblem(422, "Order total must be greater than zero")
        total = Price(
            currency=currency,
            excl_tax=basket_total.excl_tax,
            tax=basket_total.incl_tax - basket_total.excl_tax,
        )
        shipping_charge = Price(
            currency=currency,
            excl_tax=shipping_charge.excl_tax,
            tax=shipping_charge.incl_tax - shipping_charge.excl_tax,
        )
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=address,
            order_number=OrderNumberGenerator().order_number(basket),
            status=STATUS_AWAITING_PAYMENT,
        )
        basket.submit()
        source_type, _ = SourceType.objects.get_or_create(name="PayPal")
        source = Source.objects.create(
            order=order, source_type=source_type, currency=currency
        )
        PayPalPayment.objects.create(
            order=order, source=source, currency=currency, amount=total_amount
        )
    logger.info(
        "Order %s placed by user %s for %s %s",
        order.number,
        user.pk,
        total_amount,
        currency,
    )
    return order


def get_user_order(user, number):
    try:
        return Order.objects.select_related("paypal_payment").get(
            number=number, user=user
        )
    except Order.DoesNotExist:
        raise ApiProblem(404, "Order not found") from None


def get_any_order(number):
    try:
        return Order.objects.select_related("paypal_payment").get(number=number)
    except Order.DoesNotExist:
        raise ApiProblem(404, "Order not found") from None


def _payment(order):
    try:
        return PayPalPayment.objects.get(order=order)
    except PayPalPayment.DoesNotExist:
        raise ApiProblem(
            409, "Order %s was not placed through the payments API" % order.number
        ) from None


def _claim(payment, from_states, to_state, *, stale_state=None, **updates):
    """
    Move ``payment`` into ``to_state`` iff it is in one of ``from_states`` (or
    in ``stale_state`` with an abandoned claim). One UPDATE decides the winner.
    """
    condition = Q(state__in=from_states)
    if stale_state:
        condition |= Q(state=stale_state, date_updated__lt=_now() - SEND_WINDOW)
    changed = (
        PayPalPayment.objects.filter(pk=payment.pk)
        .filter(condition)
        .update(state=to_state, date_updated=_now(), **updates)
    )
    payment.refresh_from_db()
    return changed == 1


def _set_state(payment, state, error="", **fields):
    payment.state = state
    payment.last_error = error
    for name, value in fields.items():
        setattr(payment, name, value)
    payment.save()


def _event(order, name, amount, reference):
    event_type, _ = PaymentEventType.objects.get_or_create(name=name)
    EventHandler().create_payment_event(order, event_type, amount, reference=reference)


def _move_order(order, status, note):
    order.refresh_from_db()
    if order.status != status:
        EventHandler().handle_order_status_change(order, status, note_msg=note)


# ==========================================================================
# Pay (authorize)
# ==========================================================================

_IN_FLIGHT_MESSAGE = "Another request for this order is in progress; retry shortly."


@sensitive_variables("card", "card_data")
def pay(user, number, card_data=None, payment_method_id=None):
    order = get_user_order(user, number)
    payment = _payment(order)
    if (card_data is None) == (payment_method_id is None):
        raise ApiProblem(400, "Provide exactly one of card or paymentMethodId")

    saved = None
    card = None
    if payment_method_id is not None:
        saved = _get_active_card(user, payment_method_id)
    else:
        card = parse_card(card_data)

    if payment.state == PaymentState.AUTHORIZATION_PENDING:
        return _refresh_pending_authorization(order, payment)

    previous = (payment.state, payment.attempt)
    claimed = _claim(
        payment,
        [PaymentState.AWAITING_PAYMENT, PaymentState.PAYMENT_FAILED],
        PaymentState.AUTHORIZING,
        attempt=F("attempt") + 1,
        saved_card=saved,
        last_error="",
    )
    if not claimed:
        # An unknown outcome is re-sent under the SAME request id (no attempt bump).
        claimed = _claim(
            payment,
            [PaymentState.AUTHORIZATION_UNKNOWN],
            PaymentState.AUTHORIZING,
            stale_state=PaymentState.AUTHORIZING,
            saved_card=saved,
        )
    if not claimed:
        return _answer_pay_from_row(payment)

    try:
        result = paypal_calls.create_authorized_order(
            request_id=payment.request_ref("authorize"),
            order_number=order.number,
            custom_id=payment.custom_id,
            invoice_id="%s-A%d" % (payment.custom_id, payment.attempt),
            amount=money.to_wire(payment.amount, payment.currency),
            currency=payment.currency,
            card=card,
            vault_id=saved.paypal_token_id if saved else None,
        )
        if result.order_outcome == "needs_authorize":
            _set_state(
                payment,
                PaymentState.AUTHORIZING,
                paypal_order_id=result.paypal_order_id,
                paypal_order_status=result.raw_status,
            )
            result = paypal_calls.authorize_order(
                paypal_order_id=result.paypal_order_id,
                request_id=payment.request_ref("authorize-order"),
            )
    except ProviderRejected as e:
        _set_state(payment, PaymentState.PAYMENT_FAILED, error=e.message)
        status = 402 if e.provider_status == 422 else 422
        raise _from_provider(
            e, status=status, prefix="Payment was not authorized:"
        ) from e
    except ProviderError as e:
        if e.outcome_unknown:
            _set_state(payment, PaymentState.AUTHORIZATION_UNKNOWN, error=e.message)
            raise _from_provider(
                e,
                prefix="Repeat this request to resolve it (it is safe; PayPal will not hold twice).",
            ) from e
        # Nothing was sent by this request. An abandoned claim may still have
        # landed earlier, so it becomes unknown; otherwise restore the row.
        restore = (
            PaymentState.AUTHORIZATION_UNKNOWN
            if previous[0] == PaymentState.AUTHORIZING
            else previous[0]
        )
        _set_state(payment, restore, error=e.message, attempt=previous[1])
        raise _from_provider(e) from e

    return _settle_authorization(order, payment, result)


def _answer_pay_from_row(payment):
    state = payment.state
    if state in (
        PaymentState.AUTHORIZED,
        PaymentState.CAPTURING,
        PaymentState.CAPTURE_PENDING,
        PaymentState.CAPTURE_UNKNOWN,
        PaymentState.CAPTURED,
    ):
        return payment  # already paid: a double-click gets the same answer
    if state == PaymentState.AUTHORIZING:
        raise ApiProblem(409, _IN_FLIGHT_MESSAGE, state=state)
    raise ApiProblem(409, "Order cannot be paid in state '%s'" % state, state=state)


def _settle_authorization(order, payment, result):
    fields = {
        "paypal_order_id": result.paypal_order_id,
        "paypal_order_status": result.raw_status,
        "card_brand": result.card_brand or payment.card_brand,
        "card_last_digits": result.card_last_digits or payment.card_last_digits,
    }
    if result.order_outcome == "payer_action":
        _set_state(
            payment,
            PaymentState.PAYMENT_FAILED,
            error="PayPal requires the shopper to approve this payment in a browser.",
            **fields,
        )
        raise ApiProblem(
            422,
            "PayPal requires the shopper to approve this card payment in a browser (3-D Secure). "
            "That approval round-trip is not supported by this API; no money was held.",
            state=payment.state,
        )
    auth = result.authorization
    if auth is None:
        if result.order_outcome == "failed":
            _set_state(
                payment,
                PaymentState.PAYMENT_FAILED,
                error="PayPal voided the order",
                **fields,
            )
            raise ApiProblem(
                402,
                "Payment was not authorized: PayPal voided the order.",
                state=payment.state,
            )
        _set_state(
            payment,
            PaymentState.AUTHORIZATION_UNKNOWN,
            error="PayPal answered without an authorization (order status %s)"
            % result.raw_status,
            **fields,
        )
        raise ApiProblem(
            502,
            "PayPal's answer did not include an authorization; repeat the request to resolve it.",
            state=payment.state,
            outcomeUnknown=True,
        )
    return _apply_authorization(order, payment, auth, fields)


def _apply_authorization(order, payment, auth, fields):
    fields.update(
        authorization_id=auth.id,
        authorization_status=auth.raw_status,
        authorization_expires_at=auth.expires_at,
    )
    if auth.amount:
        fields["authorized_amount"] = auth.amount[0]
    if auth.outcome in ("authorized", "pending"):
        # Verify before keeping: PayPal must hold exactly the order total.
        if (
            auth.amount is None
            or auth.amount[0] != payment.amount
            or auth.amount[1] != payment.currency
        ):
            _set_state(
                payment,
                PaymentState.NEEDS_REVIEW,
                error="PayPal held %s, expected %s %s"
                % (auth.amount, payment.amount, payment.currency),
                **fields,
            )
            raise ApiProblem(
                502,
                "PayPal held a different amount than the order total; an operator must review.",
                state=payment.state,
            )
    if auth.outcome == "authorized":
        with transaction.atomic():
            fields.update(authorized_at=auth.created_at or _now())
            if payment.original_authorized_at is None:
                fields["original_authorized_at"] = fields["authorized_at"]
            _set_state(payment, PaymentState.AUTHORIZED, **fields)
            payment.source.allocate(
                payment.amount, reference=auth.id, status=auth.raw_status
            )
            _event(order, "Authorised", payment.amount, auth.id)
            _move_order(
                order, STATUS_PAYMENT_AUTHORIZED, "PayPal authorization %s" % auth.id
            )
        return payment
    if auth.outcome == "pending":
        _set_state(
            payment,
            PaymentState.AUTHORIZATION_PENDING,
            error="PayPal is reviewing the authorization (%s)"
            % (auth.reason or "pending"),
            **fields,
        )
        return payment
    if auth.outcome in ("failed", "voided"):
        reason = auth.reason or auth.raw_status
        _set_state(
            payment,
            PaymentState.PAYMENT_FAILED,
            error="Authorization %s" % reason,
            **fields,
        )
        raise ApiProblem(
            402,
            "Payment was not authorized: the card was declined (%s)." % reason,
            state=payment.state,
        )
    _set_state(
        payment,
        PaymentState.NEEDS_REVIEW,
        error="Unexpected authorization status %s" % auth.raw_status,
        **fields,
    )
    raise ApiProblem(
        502,
        "PayPal returned an unexpected authorization status; an operator must review.",
        state=payment.state,
    )


def _refresh_pending_authorization(order, payment):
    try:
        auth = paypal_calls.get_authorization(payment.authorization_id)
    except ProviderError as e:
        raise _from_provider(e) from e
    return _apply_authorization(order, payment, auth, {})


# ==========================================================================
# Fulfil (capture)
# ==========================================================================


def fulfil(number):
    order = get_any_order(number)
    payment = _payment(order)
    if payment.state == PaymentState.CAPTURED:
        return payment
    if payment.state == PaymentState.CAPTURE_PENDING:
        try:
            info = paypal_calls.get_capture(payment.capture_id)
        except ProviderError as e:
            raise _from_provider(e) from e
        return _settle_capture(order, payment, info)

    previous = payment.state
    claimed = _claim(
        payment,
        [PaymentState.AUTHORIZED, PaymentState.CAPTURE_UNKNOWN],
        PaymentState.CAPTURING,
        stale_state=PaymentState.CAPTURING,
    )
    if not claimed:
        if payment.state == PaymentState.CAPTURING:
            raise ApiProblem(409, _IN_FLIGHT_MESSAGE, state=payment.state)
        messages = {
            PaymentState.AWAITING_PAYMENT: "Order %s has not been paid yet; nothing to capture.",
            PaymentState.PAYMENT_FAILED: "Order %s has no successful payment to capture.",
            PaymentState.AUTHORIZATION_PENDING: "PayPal is still reviewing the payment for order %s; try later.",
            PaymentState.VOIDED: "Order %s was cancelled and its hold released; nothing to capture.",
            PaymentState.CANCELLED: "Order %s was cancelled; nothing to capture.",
        }
        message = messages.get(
            payment.state,
            "Order %%s cannot be fulfilled in state '%s'." % payment.state,
        )
        raise ApiProblem(409, message % order.number, state=payment.state)

    try:
        if previous == PaymentState.AUTHORIZED:
            _ensure_fresh_authorization(order, payment)
        info = paypal_calls.capture(
            authorization_id=payment.authorization_id,
            request_id=payment.request_ref("capture"),
            amount=money.to_wire(payment.amount, payment.currency),
            currency=payment.currency,
        )
    except ApiProblem:
        raise
    except ProviderRejected as e:
        # PayPal answers a repeat of a request id with the original result, so a
        # rejection means no capture exists under this request.
        _set_state(payment, PaymentState.AUTHORIZED, error=e.message)
        raise _from_provider(
            e,
            status=409,
            prefix="PayPal refused to capture the payment for order %s; no money was taken."
            % order.number,
        ) from e
    except ProviderError as e:
        if e.outcome_unknown:
            _set_state(payment, PaymentState.CAPTURE_UNKNOWN, error=e.message)
            raise _from_provider(
                e,
                prefix="Repeat the fulfil request to resolve it (it is safe; PayPal will not capture twice).",
            ) from e
        restore = (
            PaymentState.AUTHORIZED
            if previous == PaymentState.AUTHORIZED
            else PaymentState.CAPTURE_UNKNOWN
        )
        _set_state(payment, restore, error=e.message)
        raise _from_provider(e) from e
    return _settle_capture(order, payment, info)


def _ensure_fresh_authorization(order, payment):
    """
    Renew a hold whose honor period has passed; refuse, in operator terms, one
    that can no longer be renewed. Leaves the payment in CAPTURING on success.
    """
    try:
        auth = paypal_calls.get_authorization(payment.authorization_id)
    except ProviderError as e:
        _set_state(payment, PaymentState.AUTHORIZED, error=e.message)
        raise _from_provider(e) from e
    now = _now()
    original = (
        payment.original_authorized_at or auth.created_at or payment.authorized_at
    )
    current = auth.created_at or payment.authorized_at
    if auth.expires_at:
        payment.authorization_expires_at = auth.expires_at

    if auth.outcome != "authorized":
        _set_state(
            payment,
            PaymentState.AUTHORIZED,
            authorization_status=auth.raw_status,
            error="Authorization is %s at PayPal" % auth.raw_status,
        )
        raise ApiProblem(
            409,
            "Order %s cannot be fulfilled: PayPal reports its authorization %s as %s. No money was taken; "
            "cancel the order and ask the shopper to pay again."
            % (order.number, auth.id, auth.raw_status),
            state=payment.state,
        )

    expired = (auth.expires_at is not None and now >= auth.expires_at) or (
        original is not None and now >= original + REAUTHORIZE_LIMIT
    )
    if expired:
        _set_state(
            payment,
            PaymentState.AUTHORIZED,
            error="Authorization can no longer be renewed",
        )
        raise ApiProblem(
            409,
            "Order %s cannot be fulfilled: the shopper's PayPal authorization was placed on %s and can no longer "
            "be renewed (PayPal allows re-authorizing only within 29 days). No money was taken. Cancel the order "
            "and ask the shopper to pay again."
            % (
                order.number,
                original.date().isoformat() if original else "an unknown date",
            ),
            state=payment.state,
        )

    if current is not None and now >= current + HONOR_PERIOD:
        try:
            renewed = paypal_calls.reauthorize(
                authorization_id=payment.authorization_id,
                request_id="%s-%d"
                % (
                    payment.request_ref("reauthorize"),
                    payment.reauthorization_count + 1,
                ),
                amount=money.to_wire(payment.amount, payment.currency),
                currency=payment.currency,
            )
        except ProviderRejected as e:
            _set_state(payment, PaymentState.AUTHORIZED, error=e.message)
            raise _from_provider(
                e,
                status=409,
                prefix="Order %s cannot be fulfilled: its authorization is past PayPal's 3-day honor period and "
                "PayPal refused to renew it. No money was taken; cancel the order and ask the shopper to pay "
                "again. PayPal said:" % order.number,
            ) from e
        except ProviderError as e:
            _set_state(payment, PaymentState.AUTHORIZED, error=e.message)
            raise _from_provider(
                e, prefix="Renewing the authorization failed; retry the fulfil request."
            ) from e
        if (
            renewed.outcome != "authorized"
            or renewed.amount is None
            or renewed.amount[0] != payment.amount
        ):
            _set_state(
                payment,
                PaymentState.NEEDS_REVIEW,
                error="Reauthorization %s returned %s"
                % (renewed.id, renewed.raw_status),
            )
            raise ApiProblem(
                502,
                "PayPal's renewed authorization is not usable; an operator must review.",
                state=payment.state,
            )
        history = list(payment.previous_authorization_ids) + [payment.authorization_id]
        _set_state(
            payment,
            PaymentState.CAPTURING,
            authorization_id=renewed.id,
            authorization_status=renewed.raw_status,
            authorized_at=renewed.created_at or now,
            authorization_expires_at=renewed.expires_at,
            previous_authorization_ids=history,
            reauthorization_count=payment.reauthorization_count + 1,
        )
        payment.source.allocate(
            Decimal("0"), reference=renewed.id, status="REAUTHORIZED"
        )
        logger.info("Order %s: authorization renewed as %s", order.number, renewed.id)


def _settle_capture(order, payment, info):
    fields = {
        "capture_id": info.id,
        "capture_status": info.raw_status,
        "captured_at": info.created_at,
        "paypal_fee": info.paypal_fee,
        "net_amount": info.net,
    }
    if info.amount:
        fields["captured_amount"] = info.amount[0]
    if info.outcome == "completed":
        if info.amount is None or info.amount != (payment.amount, payment.currency):
            _set_state(
                payment,
                PaymentState.NEEDS_REVIEW,
                error="PayPal captured %s, expected %s %s"
                % (info.amount, payment.amount, payment.currency),
                **fields,
            )
            raise ApiProblem(
                502,
                "PayPal captured a different amount than the order total; an operator must review.",
                state=payment.state,
            )
        with transaction.atomic():
            _set_state(payment, PaymentState.CAPTURED, **fields)
            payment.source.debit(
                payment.amount, reference=info.id, status=info.raw_status
            )
            _event(order, "Settled", payment.amount, info.id)
            EventHandler().consume_stock_allocations(order)
            _move_order(order, STATUS_COMPLETE, "PayPal capture %s" % info.id)
        return payment
    if info.outcome == "pending":
        _set_state(
            payment,
            PaymentState.CAPTURE_PENDING,
            error="PayPal capture pending (%s)" % (info.reason or "pending"),
            **fields,
        )
        return payment
    error = "PayPal capture %s (%s)" % (
        info.raw_status,
        info.reason or "no reason given",
    )
    _set_state(payment, PaymentState.NEEDS_REVIEW, error=error, **fields)
    raise ApiProblem(
        502,
        "%s; an operator must review order %s." % (error, order.number),
        state=payment.state,
    )


# ==========================================================================
# Cancel (void)
# ==========================================================================


def cancel(number):
    order = get_any_order(number)
    payment = _payment(order)
    if payment.state in (PaymentState.CANCELLED, PaymentState.VOIDED):
        return payment

    # Nothing held at PayPal: cancel locally, once.
    if _claim(
        payment,
        [PaymentState.AWAITING_PAYMENT, PaymentState.PAYMENT_FAILED],
        PaymentState.CANCELLED,
    ):
        with transaction.atomic():
            EventHandler().cancel_stock_allocations(order)
            _move_order(order, STATUS_CANCELLED, "Cancelled before payment")
        return payment

    previous = payment.state
    claimed = _claim(
        payment,
        [
            PaymentState.AUTHORIZED,
            PaymentState.AUTHORIZATION_PENDING,
            PaymentState.VOID_UNKNOWN,
        ],
        PaymentState.VOIDING,
        stale_state=PaymentState.VOIDING,
    )
    if not claimed:
        if payment.state in (PaymentState.CAPTURED, PaymentState.CAPTURE_PENDING):
            raise ApiProblem(
                409,
                "Order %s has been fulfilled and paid; issue a refund instead."
                % order.number,
                state=payment.state,
            )
        if payment.state == PaymentState.VOIDING:
            raise ApiProblem(409, _IN_FLIGHT_MESSAGE, state=payment.state)
        raise ApiProblem(
            409,
            "Order %s cannot be cancelled in state '%s'."
            % (order.number, payment.state),
            state=payment.state,
        )

    try:
        auth = paypal_calls.void(
            authorization_id=payment.authorization_id,
            request_id=payment.request_ref("void"),
        )
    except ProviderRejected as e:
        restore = (
            previous
            if previous == PaymentState.AUTHORIZATION_PENDING
            else PaymentState.AUTHORIZED
        )
        _set_state(payment, restore, error=e.message)
        raise _from_provider(
            e, status=409, prefix="PayPal refused to release the hold:"
        ) from e
    except ProviderError as e:
        if e.outcome_unknown:
            _set_state(payment, PaymentState.VOID_UNKNOWN, error=e.message)
            raise _from_provider(
                e, prefix="Repeat the cancel request to resolve it."
            ) from e
        restore = (
            previous
            if previous in (PaymentState.AUTHORIZED, PaymentState.AUTHORIZATION_PENDING)
            else PaymentState.VOID_UNKNOWN
        )
        _set_state(payment, restore, error=e.message)
        raise _from_provider(e) from e

    if auth.outcome != "voided":
        _set_state(
            payment,
            PaymentState.NEEDS_REVIEW,
            authorization_status=auth.raw_status,
            error="Void returned status %s" % auth.raw_status,
        )
        raise ApiProblem(
            502,
            "PayPal did not confirm the hold was released (status %s); an operator must review."
            % auth.raw_status,
            state=payment.state,
        )
    with transaction.atomic():
        _set_state(
            payment,
            PaymentState.VOIDED,
            authorization_status=auth.raw_status,
            voided_at=_now(),
        )
        source = payment.source
        Transaction.objects.create(
            source=source,
            txn_type="Void",
            amount=source.amount_allocated,
            reference=auth.id,
            status=auth.raw_status,
        )
        _event(order, "Voided", payment.amount, auth.id)
        source.amount_allocated = Decimal("0")
        source.save()
        EventHandler().cancel_stock_allocations(order)
        _move_order(order, STATUS_CANCELLED, "PayPal authorization %s voided" % auth.id)
    return payment


# ==========================================================================
# Refunds
# ==========================================================================


def refund(user, number, idempotency_key, amount_raw=None, reason=""):
    order = get_user_order(user, number)
    payment = _payment(order)
    key = parse_idempotency_key(idempotency_key, required=True)
    reason = str(reason or "")[:255]

    amount = None
    if amount_raw is not None:
        amount = money.parse_amount(amount_raw, payment.currency)
        if amount is None:
            raise ApiProblem(
                422,
                "amount must be a positive number with at most %d decimals"
                % money.exponent(payment.currency),
            )

    existing = PayPalRefund.objects.filter(payment=payment, idempotency_key=key).first()
    if existing is not None:
        return _replay_refund(payment, existing, amount), False

    if payment.state != PaymentState.CAPTURED or not payment.capture_id:
        raise ApiProblem(
            409,
            "Order %s has no captured payment to refund." % order.number,
            state=payment.state,
        )

    try:
        with transaction.atomic():
            remaining = payment.captured_amount - payment.refund_reserved
            if amount is None:
                amount = remaining
            if amount <= 0:
                raise ApiProblem(
                    409, "Order %s is already fully refunded." % order.number
                )
            # The ceiling is enforced by the UPDATE itself, not by the read above.
            reserved = PayPalPayment.objects.filter(
                pk=payment.pk,
                state=PaymentState.CAPTURED,
                refund_reserved__lte=F("captured_amount") - amount,
            ).update(refund_reserved=F("refund_reserved") + amount, date_updated=_now())
            if not reserved:
                payment.refresh_from_db()
                raise ApiProblem(
                    409,
                    "Refund of %s exceeds the refundable remainder of %s %s."
                    % (
                        money.to_wire(amount, payment.currency),
                        money.to_wire(
                            payment.captured_amount - payment.refund_reserved,
                            payment.currency,
                        ),
                        payment.currency,
                    ),
                )
            record = PayPalRefund.objects.create(
                payment=payment,
                idempotency_key=key,
                amount=amount,
                currency=payment.currency,
                reason=reason,
            )
    except IntegrityError:
        # A concurrent request with the same key won; answer from its row.
        existing = PayPalRefund.objects.get(payment=payment, idempotency_key=key)
        return _replay_refund(payment, existing, amount), False
    return _send_refund(order, payment, record, resend=False), True


def _replay_refund(payment, record, amount):
    if amount is not None and amount != record.amount:
        raise ApiProblem(
            422,
            "This idempotency key was already used for a refund of %s %s."
            % (record.amount, record.currency),
            refundId=str(record.id),
        )
    if record.state == RefundState.PENDING and record.paypal_refund_id:
        try:
            info = paypal_calls.get_refund(record.paypal_refund_id)
        except ProviderError as e:
            raise _from_provider(e) from e
        return _settle_refund(payment.order, payment, record, info)
    stale = _now() - SEND_WINDOW
    claimed = (
        PayPalRefund.objects.filter(pk=record.pk)
        .filter(
            Q(state=RefundState.UNKNOWN)
            | Q(state=RefundState.SENDING, date_updated__lt=stale)
        )
        .update(state=RefundState.SENDING, date_updated=_now())
    )
    record.refresh_from_db()
    if claimed:
        return _send_refund(payment.order, payment, record, resend=True)
    if record.state == RefundState.SENDING:
        raise ApiProblem(409, _IN_FLIGHT_MESSAGE, refundId=str(record.id))
    return record


def _release(payment, amount):
    PayPalPayment.objects.filter(pk=payment.pk).update(
        refund_reserved=F("refund_reserved") - amount, date_updated=_now()
    )


def _send_refund(order, payment, record, resend):
    try:
        info = paypal_calls.refund(
            capture_id=payment.capture_id,
            request_id=record.request_ref,
            amount=money.to_wire(record.amount, record.currency),
            currency=record.currency,
            note=record.reason or None,
        )
    except ProviderRejected as e:
        with transaction.atomic():
            record.state, record.last_error = RefundState.FAILED, e.message
            record.save()
            _release(payment, record.amount)
        raise _from_provider(e, prefix="PayPal refused the refund:") from e
    except ProviderError as e:
        if e.outcome_unknown or resend:
            record.state, record.last_error = RefundState.UNKNOWN, e.message
            record.save()
            raise _from_provider(
                e,
                prefix="Repeat the request with the same idempotency key to resolve it.",
            ) from e
        with transaction.atomic():
            record.state, record.last_error = RefundState.FAILED, e.message
            record.save()
            _release(payment, record.amount)
        raise _from_provider(e) from e
    return _settle_refund(order, payment, record, info)


def _settle_refund(order, payment, record, info):
    record.paypal_refund_id = info.id
    record.paypal_status = info.raw_status
    record.refunded_at = info.created_at
    if info.outcome == "completed":
        if info.amount is None or info.amount != (record.amount, record.currency):
            record.state = RefundState.UNKNOWN
            record.last_error = "PayPal refunded %s, expected %s %s" % (
                info.amount,
                record.amount,
                record.currency,
            )
            record.save()
            raise ApiProblem(
                502,
                "PayPal refunded a different amount; an operator must review.",
                refundId=str(record.id),
            )
        with transaction.atomic():
            if record.state != RefundState.COMPLETED:
                record.state = RefundState.COMPLETED
                record.last_error = ""
                record.save()
                PayPalPayment.objects.filter(pk=payment.pk).update(
                    refunded_amount=F("refunded_amount") + record.amount,
                    date_updated=_now(),
                )
                payment.source.refund(
                    record.amount, reference=info.id, status=info.raw_status
                )
                _event(order, "Refunded", record.amount, info.id)
        return record
    if info.outcome == "pending":
        record.state = RefundState.PENDING
        record.save()
        return record
    if info.outcome == "failed":
        with transaction.atomic():
            record.state = RefundState.FAILED
            record.last_error = "PayPal refund %s (%s)" % (
                info.raw_status,
                info.reason or "no reason given",
            )
            record.save()
            _release(payment, record.amount)
        return record
    record.state = RefundState.UNKNOWN
    record.last_error = "Unexpected refund status %s" % info.raw_status
    record.save()
    return record


# ==========================================================================
# Saved cards
# ==========================================================================


def _get_active_card(user, card_id):
    try:
        card_uuid = uuid.UUID(str(card_id))
    except ValueError:
        raise ApiProblem(404, "Saved card not found") from None
    card = SavedCard.objects.filter(
        pk=card_uuid, user=user, status=SavedCardStatus.ACTIVE
    ).first()
    if card is None or not card.paypal_token_id:
        raise ApiProblem(404, "Saved card not found")
    return card


def list_cards(user):
    return SavedCard.objects.filter(user=user, status=SavedCardStatus.ACTIVE)


@sensitive_variables("card", "card_data")
def save_card(user, card_data, idempotency_key=None):
    card = parse_card(card_data)
    key = parse_idempotency_key(idempotency_key, required=False)
    ref = key or uuid.uuid4().hex  # the claim, unique per user

    resend = False
    try:
        with transaction.atomic():
            record = SavedCard.objects.create(user=user, request_ref=ref)
    except IntegrityError:
        record = SavedCard.objects.get(user=user, request_ref=ref)
        claimed = (
            SavedCard.objects.filter(pk=record.pk)
            .filter(
                Q(status=SavedCardStatus.UNKNOWN)
                | Q(
                    status=SavedCardStatus.SAVING, date_updated__lt=_now() - SEND_WINDOW
                )
            )
            .update(status=SavedCardStatus.SAVING, date_updated=_now())
        )
        record.refresh_from_db()
        if not claimed:
            if record.status == SavedCardStatus.SAVING:
                raise ApiProblem(409, _IN_FLIGHT_MESSAGE)
            if record.status == SavedCardStatus.ACTIVE:
                return record, False
            raise ApiProblem(
                409, "This idempotency key was already used (card %s)." % record.status
            )
        resend = True

    customer = PayPalCustomer.objects.filter(user=user).first()
    try:
        vaulted = paypal_calls.vault_card(
            request_id="oscar-card-%s" % record.id.hex,
            card=card,
            customer_id=customer.paypal_customer_id if customer else None,
        )
    except ProviderRejected as e:
        record.status, record.last_error = SavedCardStatus.REJECTED, e.message
        record.save()
        raise _from_provider(
            e, status=422, prefix="PayPal could not save the card:"
        ) from e
    except ProviderError as e:
        if e.outcome_unknown or resend:
            record.status, record.last_error = SavedCardStatus.UNKNOWN, e.message
            record.save()
            raise _from_provider(
                e,
                prefix="Repeat the request with the same Idempotency-Key to resolve it.",
            ) from e
        record.delete()  # never sent: nothing to remember
        raise _from_provider(e) from e

    record.paypal_token_id = vaulted.token_id
    record.paypal_customer_id = vaulted.customer_id or ""
    record.brand = (vaulted.brand or "")[:32]
    record.last_digits = (vaulted.last_digits or card.number[-4:])[-4:]
    record.expiry = vaulted.expiry or card.expiry
    record.holder_name = (vaulted.name or card.name)[:255]
    if vaulted.verification_failed:
        record.status = SavedCardStatus.REJECTED
        record.last_error = "Card verification failed"
        record.save()
        try:
            paypal_calls.delete_token(vaulted.token_id)
            record.paypal_token_id = None
            record.save()
        except ProviderError:
            logger.warning(
                "Could not delete unverified vault token for saved card %s", record.pk
            )
        raise ApiProblem(422, "The card could not be verified and was not saved.")
    record.status = SavedCardStatus.ACTIVE
    record.last_error = ""
    record.save()
    if vaulted.customer_id and customer is None:
        PayPalCustomer.objects.get_or_create(
            user=user, defaults={"paypal_customer_id": vaulted.customer_id}
        )
    return record, True


def delete_card(user, card_id):
    try:
        card_uuid = uuid.UUID(str(card_id))
    except ValueError:
        raise ApiProblem(404, "Saved card not found") from None
    record = SavedCard.objects.filter(pk=card_uuid, user=user).first()
    if record is None:
        raise ApiProblem(404, "Saved card not found")
    if record.status == SavedCardStatus.DELETED:
        return record
    previous = record.status
    claimed = (
        SavedCard.objects.filter(pk=record.pk)
        .filter(
            Q(
                status__in=[
                    SavedCardStatus.ACTIVE,
                    SavedCardStatus.REJECTED,
                    SavedCardStatus.UNKNOWN,
                ]
            )
            | Q(status=SavedCardStatus.DELETING, date_updated__lt=_now() - SEND_WINDOW)
        )
        .update(status=SavedCardStatus.DELETING, date_updated=_now())
    )
    record.refresh_from_db()
    if not claimed:
        raise ApiProblem(
            409,
            (
                _IN_FLIGHT_MESSAGE
                if record.status == SavedCardStatus.DELETING
                else "Saved card cannot be deleted in state '%s'" % record.status
            ),
        )
    # From here the card is neither listed nor usable to pay.
    if record.paypal_token_id:
        try:
            paypal_calls.delete_token(record.paypal_token_id)
        except ProviderError as e:
            if e.outcome_unknown:
                record.last_error = e.message
                record.save()  # stays DELETING: hidden and unusable; a repeat DELETE re-sends
                raise _from_provider(
                    e, prefix="Repeat the delete to confirm it."
                ) from e
            record.status, record.last_error = previous, e.message
            record.save()
            raise _from_provider(e) from e
    record.status = SavedCardStatus.DELETED
    record.last_error = ""
    record.save()
    return record


def order_queryset_for(user):
    return (
        Order.objects.filter(user=user, paypal_payment__isnull=False)
        .select_related("paypal_payment", "paypal_payment__saved_card")
        .prefetch_related("lines", "paypal_payment__refunds")
        .order_by("-date_placed")
    )


def paypal_currency():
    return money.currency()


def configured():
    return bool(settings.PAYPAL_CLIENT_ID and settings.PAYPAL_CLIENT_SECRET)
