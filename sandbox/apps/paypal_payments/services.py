"""
Order, payment and saved-card operations.

Every PayPal write follows the same shape:

1. claim - a conditional state change on ``OrderPayment`` and/or a unique
   ``PaymentOperation`` row, committed *before* PayPal is called, so a second
   request (double-click, retry, another worker) answers from the existing
   row instead of calling PayPal again;
2. call - one SDK call carrying the claim's ``PayPal-Request-Id``;
3. look up - if the outcome is unknown (timeout, 5xx, unreadable body) the
   identical request is resent once under the same ``PayPal-Request-Id``,
   which PayPal de-duplicates;
4. verify - the amount PayPal echoes must equal what was asked for;
5. settle - local state follows the status PayPal reported, member by member.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Any, TypeVar

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core import prices
from oscar.core.loading import get_class, get_model
from paypal.core import UNSET, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CaptureRequest,
    CapturedPayment,
    CardRequest,
    Customer,
    Order as PayPalOrder,
    PaymentAuthorization,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PaymentTokenResponse,
    OrderRequest,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    Refund,
    RefundRequest,
)
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CheckoutPaymentIntent,
    OrderStatus,
    RefundStatus,
)

from . import gateway, money
from .errors import ApiProblem
from .gateway import PayPalError, ProviderRejected
from .models import OrderPayment, PaymentOperation, PayPalCustomer, PayPalRefund, SavedCard

logger = logging.getLogger(__name__)

T = TypeVar("T")

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
SourceType = get_model("payment", "SourceType")
Source = get_model("payment", "Source")
Transaction = get_model("payment", "Transaction")
Selector = get_class("partner.strategy", "Selector")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
FreeShipping = get_class("shipping.methods", "Free")

# Oscar order statuses (sandbox OSCAR_ORDER_STATUS_PIPELINE).
STATUS_PROCESSING = "Being processed"
STATUS_COMPLETE = "Complete"
STATUS_CANCELLED = "Cancelled"

SOURCE_TYPE_NAME = "PayPal"

# How long a claim in "sending" is presumed to still be in flight:
# two attempts (send + one lookup) of gateway.TIMEOUT each, plus slack.
SEND_WINDOW = timedelta(seconds=gateway.TIMEOUT * 2 + 15)

# PayPal honours an authorization for three days; after that it must be
# reauthorized (possible once, from day 4 to day 29) before capture.
HONOR_PERIOD = timedelta(days=3)

MAX_ORDER_LINES = 50
MAX_LINE_QUANTITY = 100


# ---------------------------------------------------------------------------
# Status mapping: the only places a PayPal status becomes ours.
# Outcomes: done / pending / failed / unknown (+ needs_review for surprises).
# ---------------------------------------------------------------------------


def authorization_outcome(status: object) -> str:
    match status:
        case AuthorizationStatus.CREATED:
            return PaymentOperation.DONE
        case AuthorizationStatus.PENDING:
            return PaymentOperation.PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            return PaymentOperation.FAILED
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return PaymentOperation.NEEDS_REVIEW
        case _:
            return PaymentOperation.UNKNOWN


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return PaymentOperation.DONE
        case CaptureStatus.PENDING:
            return PaymentOperation.PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return PaymentOperation.FAILED
        case CaptureStatus.PARTIALLY_REFUNDED | CaptureStatus.REFUNDED:
            return PaymentOperation.NEEDS_REVIEW
        case _:
            return PaymentOperation.UNKNOWN


def refund_outcome(status: object) -> str:
    match status:
        case RefundStatus.COMPLETED:
            return PaymentOperation.DONE
        case RefundStatus.PENDING:
            return PaymentOperation.PENDING
        case RefundStatus.CANCELLED | RefundStatus.FAILED:
            return PaymentOperation.FAILED
        case _:
            return PaymentOperation.UNKNOWN


def _text(value: object) -> str:
    """An SDK scalar (or open-enum member) as plain text; '' when unset."""
    if value is None or isinstance(value, UnsetType):
        return ""
    return str(value)


def _provider_time(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


def _claim(key: str, kind: str, **fields: Any) -> tuple[PaymentOperation, bool]:
    """Insert the claim row for ``key``; if it exists, return the existing row."""
    try:
        with transaction.atomic():
            return PaymentOperation.objects.create(key=key, kind=kind, **fields), True
    except IntegrityError:
        return PaymentOperation.objects.get(key=key), False


def _is_fresh(op: PaymentOperation) -> bool:
    return op.status == PaymentOperation.SENDING and op.date_updated > timezone.now() - SEND_WINDOW


def _settle(op: PaymentOperation, status: str, **fields: Any) -> None:
    op.status = status
    for name, value in fields.items():
        setattr(op, name, value)
    op.save()


def _send_with_lookup(op: PaymentOperation, send: Callable[[str], T]) -> T:
    """
    Send once under the claim's PayPal-Request-Id. If the outcome is unknown,
    resend the identical request under the same id: PayPal returns the
    original result if the first attempt landed, or processes it once if not.
    """
    try:
        return gateway.call(lambda: send(op.request_id))
    except PayPalError as first:
        if not first.outcome_unknown:
            raise
        logger.warning("PayPal outcome unknown for %s; looking it up by request id", op.key)
    return gateway.call(lambda: send(op.request_id))


def _outcome_unknown(op: PaymentOperation, error: PayPalError, what: str) -> ApiProblem:
    _settle(op, PaymentOperation.UNKNOWN, error=error.message)
    return ApiProblem(
        504 if error.status_code == 504 else 502,
        "outcome_unknown",
        f"PayPal did not confirm the {what}; it may or may not have happened. "
        f"Reference {op.request_id} - repeating the same request is safe, or check the "
        "reconciliation report.",
        reference=str(op.request_id),
    )


def _rejection(error: PayPalError) -> ApiProblem:
    status = error.status_code
    code = {
        400: "paypal_rejected",
        404: "paypal_not_found",
        409: "paypal_conflict",
        422: "paypal_rejected",
    }.get(status, "paypal_unavailable")
    return ApiProblem(status, code, error.message, **error.details())


# ---------------------------------------------------------------------------
# Oscar payment bookkeeping (payment.Source / payment.Transaction)
# ---------------------------------------------------------------------------


def _source(payment: OrderPayment) -> Any:
    source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
    source, _ = Source.objects.get_or_create(
        order=payment.order,
        source_type=source_type,
        defaults={"currency": payment.currency, "reference": payment.paypal_order_id},
    )
    return source


def _set_order_status(order: Any, status: str) -> None:
    if order.status != status and status in order.available_statuses():
        order.set_status(status)


# ---------------------------------------------------------------------------
# Lookups with ownership
# ---------------------------------------------------------------------------


def owned_payment(user: User, order_id: str) -> OrderPayment:
    """The caller's own order payment; 404 for anything else (no existence leak)."""
    payment = (
        OrderPayment.objects.select_related("order")
        .filter(order__number=order_id, order__user=user)
        .first()
    )
    if payment is None:
        raise ApiProblem(404, "order_not_found", "No such order.")
    return payment


def any_payment(order_id: str) -> OrderPayment:
    payment = OrderPayment.objects.select_related("order").filter(order__number=order_id).first()
    if payment is None:
        raise ApiProblem(404, "order_not_found", "No such order.")
    return payment


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardInput:
    """Card details held only for the duration of one request - never persisted or logged."""

    number: str
    expiry: str
    security_code: str
    name: str
    billing_address: Address | UnsetType

    def __repr__(self) -> str:  # keep card data out of tracebacks and logs
        return f"CardInput(ending {self.number[-4:]})"


def _require_dict(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiProblem(400, "invalid_request", f"'{field}' must be an object.")
    return value


def _optional_str(data: dict[str, Any], key: str, max_length: int) -> str:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > max_length:
        raise ApiProblem(400, "invalid_request", f"'{key}' must be a string of at most {max_length} characters.")
    return value.strip()


def parse_card(raw: object) -> CardInput:
    data = _require_dict(raw, "card")
    number = "".join(str(data.get("number", "")).split()).replace("-", "")
    if not number.isdigit() or not 12 <= len(number) <= 19:
        raise ApiProblem(400, "invalid_card", "'card.number' must be 12-19 digits.")
    expiry = str(data.get("expiry", "")).strip()
    try:
        expiry_date = datetime.strptime(expiry, "%Y-%m")
    except ValueError:
        raise ApiProblem(400, "invalid_card", "'card.expiry' must be YYYY-MM.") from None
    now = timezone.now()
    if (expiry_date.year, expiry_date.month) < (now.year, now.month):
        raise ApiProblem(400, "invalid_card", "The card has expired.")
    security_code = str(data.get("securityCode", "")).strip()
    if not security_code.isdigit() or len(security_code) not in (3, 4):
        raise ApiProblem(400, "invalid_card", "'card.securityCode' must be 3 or 4 digits.")
    name = _optional_str(data, "name", 300)
    billing: Address | UnsetType = UNSET
    if data.get("billingAddress") is not None:
        billing = parse_address(data["billingAddress"])
    return CardInput(number, expiry_date.strftime("%Y-%m"), security_code, name, billing)


def parse_address(raw: object) -> Address:
    data = _require_dict(raw, "billingAddress")
    country = _optional_str(data, "countryCode", 2).upper()
    if len(country) != 2 or not country.isalpha():
        raise ApiProblem(400, "invalid_request", "'billingAddress.countryCode' must be a 2-letter country code.")
    fields: dict[str, str] = {}
    for key, target, length in (
        ("line1", "address_line_1", 300),
        ("line2", "address_line_2", 300),
        ("city", "admin_area_2", 120),
        ("state", "admin_area_1", 300),
        ("postalCode", "postal_code", 60),
    ):
        value = _optional_str(data, key, length)
        if value:
            fields[target] = value
    return Address(country_code=country, **fields)


def parse_idempotency_key(raw: object) -> str:
    if not isinstance(raw, str) or not 1 <= len(raw.strip()) <= 100:
        raise ApiProblem(
            400,
            "idempotency_key_required",
            "Send an idempotency key (header 'Idempotency-Key' or field 'idempotencyKey'), 1-100 characters.",
        )
    return raw.strip()


# ---------------------------------------------------------------------------
# Flow 1 - place an order
# ---------------------------------------------------------------------------


def place_order(user: User, payload: dict[str, Any]) -> OrderPayment:
    currency = money.configured_currency()
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ApiProblem(400, "invalid_request", "'items' must be a non-empty list of {productId, quantity}.")
    if len(raw_items) > MAX_ORDER_LINES:
        raise ApiProblem(400, "invalid_request", f"At most {MAX_ORDER_LINES} items per order.")
    quantities: dict[int, int] = {}
    for item in raw_items:
        item = _require_dict(item, "items[]")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            if not (isinstance(product_id, str) and product_id.isdigit()):
                raise ApiProblem(400, "invalid_request", "'productId' must be a catalogue product id.")
            product_id = int(product_id)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_LINE_QUANTITY:
            raise ApiProblem(400, "invalid_request", f"'quantity' must be an integer from 1 to {MAX_LINE_QUANTITY}.")
        quantities[product_id] = quantities.get(product_id, 0) + quantity

    shipping_address_data = payload.get("shippingAddress")
    strategy = Selector().strategy(user=user)

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in quantities.items():
            product = Product.objects.filter(pk=product_id, is_public=True).first()
            if product is None:
                raise ApiProblem(422, "unknown_product", f"Product {product_id} is not in the catalogue.")
            if product.is_parent:
                raise ApiProblem(
                    422, "choose_variant", f"Product {product_id} has variants; order one of its child products."
                )
            info = strategy.fetch_for_product(product)
            if not info.price.exists:
                raise ApiProblem(422, "unavailable_product", f"Product {product_id} has no price.")
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ApiProblem(422, "unavailable_product", f"Product {product_id}: {reason}")
            basket.add_product(product, quantity)

        shipping_method = FreeShipping()
        shipping_charge = shipping_method.calculate(basket)
        basket_total = OrderTotalCalculator().calculate(basket, shipping_charge)
        if basket_total.incl_tax is None:
            raise ApiProblem(422, "tax_unknown", "The order total could not be determined.")
        total_amount = Decimal(basket_total.incl_tax)
        if total_amount <= 0 or not money.is_exact(total_amount, currency):
            raise ApiProblem(
                422, "amount_not_payable", f"The order total {total_amount} cannot be charged in {currency}."
            )
        # Amounts come from catalogue prices; the charge currency is configuration.
        total = prices.Price(
            currency=currency, excl_tax=basket_total.excl_tax, incl_tax=basket_total.incl_tax
        )
        shipping_address = None
        if shipping_address_data is not None:
            shipping_address = _shipping_address(shipping_address_data)

        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=shipping_address,
        )
        basket.submit()
        return OrderPayment.objects.create(
            order=order, currency=currency, amount=total_amount, state=OrderPayment.AWAITING_PAYMENT
        )


def _shipping_address(raw: object) -> Any:
    data = _require_dict(raw, "shippingAddress")
    country_code = _optional_str(data, "countryCode", 2).upper()
    country = Country.objects.filter(iso_3166_1_a2=country_code).first()
    if country is None:
        raise ApiProblem(422, "invalid_address", "'shippingAddress.countryCode' is not a known country.")
    line1 = _optional_str(data, "line1", 255)
    if not line1:
        raise ApiProblem(400, "invalid_address", "'shippingAddress.line1' is required.")
    return ShippingAddress.objects.create(
        first_name=_optional_str(data, "firstName", 255),
        last_name=_optional_str(data, "lastName", 255),
        line1=line1,
        line2=_optional_str(data, "line2", 255),
        line4=_optional_str(data, "city", 255),
        state=_optional_str(data, "state", 255),
        postcode=_optional_str(data, "postalCode", 64),
        country=country,
    )


# ---------------------------------------------------------------------------
# Flow 1 - authorize (pay)
# ---------------------------------------------------------------------------


def authorize(user: User, order_id: str, payload: dict[str, Any]) -> tuple[OrderPayment, bool]:
    """
    Put a hold on the order total. Returns (payment, created) - ``created`` is
    False when the order was already paid and this call changed nothing.
    """
    payment = owned_payment(user, order_id)

    has_card, has_saved = payload.get("card") is not None, payload.get("paymentMethodId") is not None
    if has_card == has_saved:
        raise ApiProblem(400, "invalid_request", "Send exactly one of 'card' or 'paymentMethodId'.")
    card: CardInput | None = None
    saved: SavedCard | None = None
    if has_card:
        card = parse_card(payload["card"])
    else:
        saved = _usable_saved_card(user, payload["paymentMethodId"])

    # Built before claiming: a configuration error must not strand a claim.
    client = gateway.get_client()

    # An attempt whose outcome was never confirmed is resumed under the SAME
    # PayPal-Request-Id while PayPal still de-duplicates it, so it can only
    # ever produce one authorization. Past that window it stays "unknown"
    # for an operator (see the reconciliation report).
    resumed = _resumable_authorization(payment)
    if resumed is not None:
        changed = OrderPayment.objects.filter(pk=payment.pk, state=OrderPayment.UNKNOWN).update(
            state=OrderPayment.AUTHORIZING, saved_card=saved, last_error="", date_updated=timezone.now()
        )
        if not changed:
            resumed = None
    else:
        # Claim: one conditional write moves the payment into "authorizing".
        changed = OrderPayment.objects.filter(
            pk=payment.pk, state__in=OrderPayment.PAYABLE_STATES
        ).update(
            state=OrderPayment.AUTHORIZING,
            attempt=F("attempt") + 1,
            saved_card=saved,
            last_error="",
            date_updated=timezone.now(),
        )
    payment.refresh_from_db()
    if not changed:
        if payment.state == OrderPayment.AUTHORIZING:
            raise ApiProblem(409, "payment_in_progress", "A payment for this order is already being processed.")
        if payment.state in (OrderPayment.AUTHORIZED, OrderPayment.AUTHORIZATION_PENDING, OrderPayment.CAPTURING,
                             OrderPayment.CAPTURE_PENDING) + OrderPayment.CAPTURED_STATES:
            return payment, False  # already paid: a repeat changes nothing
        raise ApiProblem(409, "not_payable", f"The order cannot be paid in state '{payment.state}'.")

    currency = payment.currency
    if resumed is not None:
        op = resumed
        _settle(op, PaymentOperation.SENDING, error="")
    else:
        op, _ = _claim(
            f"authorize:{payment.pk}:{payment.attempt}",
            PaymentOperation.AUTHORIZE,
            order_payment=payment,
            user=user,
            amount=payment.amount,
        )
    # Unique across every transaction on the merchant account (PayPal can be
    # set to reject a reused invoice id), and identical when this attempt is
    # resent under the same PayPal-Request-Id.
    invoice_id = f"{payment.order.number}-{payment.attempt}-{str(op.request_id)[:8]}"
    if card is not None:
        card_request = CardRequest(
            name=card.name or UNSET,
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            billing_address=card.billing_address,
        )
    else:
        assert saved is not None
        card_request = CardRequest(vault_id=saved.token_id)
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=payment.order.number,
                custom_id=payment.order.number,
                invoice_id=invoice_id,
                amount=AmountWithBreakdown(currency_code=currency, value=money.to_str(payment.amount, currency)),
            )
        ],
        payment_source=PaymentSource(card=card_request),
    )
    try:
        result = _send_with_lookup(
            op,
            lambda request_id: client.orders.create_order(
                body, pay_pal_request_id=request_id, prefer="return=representation"
            ),
        )
    except PayPalError as e:
        if e.outcome_unknown:
            problem = _outcome_unknown(op, e, "payment authorization")
            _finish_authorizing(payment, OrderPayment.UNKNOWN, last_error=problem.message)
            raise problem from e
        _settle(op, PaymentOperation.FAILED, error=e.message)
        # Definitely did not happen: the shopper may pay again.
        _finish_authorizing(payment, OrderPayment.FAILED, last_error=e.message)
        raise _rejection(e) from e

    try:
        _settle_authorization(payment, op, result, saved)
    except ApiProblem:
        raise
    except Exception:
        # PayPal answered but we could not record it: never leave the claim
        # looking like nothing happened.
        _finish_authorizing(payment, OrderPayment.UNKNOWN, last_error="Recording PayPal's answer failed.")
        raise
    payment.refresh_from_db()
    return payment, True


# PayPal keeps a create-order PayPal-Request-Id for 6 hours (create_order docstring).
ORDER_REQUEST_ID_RETENTION = timedelta(hours=6)


def _resumable_authorization(payment: OrderPayment) -> PaymentOperation | None:
    if payment.state != OrderPayment.UNKNOWN:
        return None
    op = (
        PaymentOperation.objects.filter(order_payment=payment, kind=PaymentOperation.AUTHORIZE)
        .order_by("-date_created")
        .first()
    )
    if op is None or op.status != PaymentOperation.UNKNOWN:
        return None
    if op.date_created <= timezone.now() - ORDER_REQUEST_ID_RETENTION + SEND_WINDOW:
        return None
    return op


def _usable_saved_card(user: User, raw_id: object) -> SavedCard:
    try:
        public_id = uuid.UUID(str(raw_id))
    except ValueError:
        raise ApiProblem(404, "payment_method_not_found", "No such saved card.") from None
    card = SavedCard.objects.filter(public_id=public_id, user=user, state=SavedCard.ACTIVE).first()
    if card is None:
        raise ApiProblem(404, "payment_method_not_found", "No such saved card.")
    return card


def _finish_authorizing(payment: OrderPayment, state: str, **fields: Any) -> None:
    OrderPayment.objects.filter(pk=payment.pk, state=OrderPayment.AUTHORIZING).update(
        state=state, date_updated=timezone.now(), **fields
    )


def _settle_authorization(
    payment: OrderPayment, op: PaymentOperation, result: PayPalOrder, saved: SavedCard | None
) -> None:
    order_status = result.status
    authorization = None
    if not isinstance(result.purchase_units, UnsetType) and result.purchase_units:
        payments = result.purchase_units[0].payments
        if not isinstance(payments, UnsetType) and not isinstance(payments.authorizations, UnsetType):
            authorization = payments.authorizations[0] if payments.authorizations else None

    card_brand, card_last = (saved.brand, saved.last_digits) if saved else ("", "")
    if not isinstance(result.payment_source, UnsetType) and not isinstance(result.payment_source.card, UnsetType):
        card_brand = _text(result.payment_source.card.brand) or card_brand
        card_last = _text(result.payment_source.card.last_digits) or card_last

    fields: dict[str, Any] = {
        "paypal_order_id": _text(result.id),
        "card_brand": card_brand,
        "card_last_digits": card_last,
    }
    message = ""
    match order_status:
        case OrderStatus.COMPLETED if authorization is not None:
            outcome = authorization_outcome(authorization.status)
        case OrderStatus.PAYER_ACTION_REQUIRED:
            outcome = PaymentOperation.FAILED
            message = (
                "PayPal requires the shopper to approve this card payment in a browser "
                "(e.g. 3-D Secure); this integration does not support that approval step."
            )
        case OrderStatus.VOIDED:
            outcome = PaymentOperation.FAILED
            message = "PayPal voided the order."
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED:
            outcome = PaymentOperation.PENDING
            message = f"PayPal has not authorized the payment yet (order status {order_status})."
        case _:
            outcome = PaymentOperation.UNKNOWN
            message = f"Unrecognised PayPal order status {_text(order_status) or '(none)'}."

    if authorization is not None:
        echoed = money.from_money(authorization.amount)
        fields.update(
            authorization_id=_text(authorization.id),
            authorization_status=_text(authorization.status),
            authorized_amount=echoed[0] if echoed else None,
            authorization_time=_provider_time(authorization.create_time),
            authorization_expires=_provider_time(authorization.expiration_time),
            reauthorized=False,
        )
        if outcome == PaymentOperation.DONE and echoed != (payment.amount, payment.currency):
            outcome = PaymentOperation.NEEDS_REVIEW
            message = (
                f"PayPal authorized {echoed} but the order total is {payment.amount} {payment.currency}; "
                "an operator must review this payment."
            )
        if outcome == PaymentOperation.DONE and not fields["authorization_id"]:
            outcome = PaymentOperation.UNKNOWN
            message = "PayPal's response carried no authorization id."
        if outcome == PaymentOperation.FAILED and not message:
            message = f"PayPal did not authorize the payment (status {_text(authorization.status)})."
    elif outcome == PaymentOperation.DONE:
        outcome = PaymentOperation.UNKNOWN

    state = {
        PaymentOperation.DONE: OrderPayment.AUTHORIZED,
        PaymentOperation.PENDING: OrderPayment.AUTHORIZATION_PENDING,
        PaymentOperation.FAILED: OrderPayment.FAILED,
        PaymentOperation.NEEDS_REVIEW: OrderPayment.NEEDS_REVIEW,
    }.get(outcome, OrderPayment.UNKNOWN)

    with transaction.atomic():
        _settle(
            op,
            outcome,
            provider_id=fields.get("authorization_id") or fields["paypal_order_id"],
            provider_status=_text(authorization.status if authorization is not None else order_status),
            provider_time=fields.get("authorization_time"),
            error=message,
        )
        _finish_authorizing(payment, state, last_error=message, **fields)
        if state == OrderPayment.AUTHORIZED:
            payment.refresh_from_db()
            _source(payment).allocate(payment.amount, reference=payment.authorization_id, status="CREATED")
            _set_order_status(payment.order, STATUS_PROCESSING)

    if state == OrderPayment.FAILED:
        raise ApiProblem(402, "payment_declined", message, orderId=payment.order.number)


# ---------------------------------------------------------------------------
# Flow 1 - fulfil (capture)
# ---------------------------------------------------------------------------


def fulfil(order_id: str) -> tuple[OrderPayment, bool]:
    payment = any_payment(order_id)
    changed = OrderPayment.objects.filter(pk=payment.pk, state=OrderPayment.AUTHORIZED).update(
        state=OrderPayment.CAPTURING, last_error="", date_updated=timezone.now()
    )
    payment.refresh_from_db()
    if not changed:
        if payment.state in OrderPayment.CAPTURED_STATES + (OrderPayment.CAPTURE_PENDING,):
            return payment, False
        if payment.state == OrderPayment.CAPTURING:
            raise ApiProblem(409, "fulfilment_in_progress", "This order is already being fulfilled.")
        if payment.state in OrderPayment.PAYABLE_STATES:
            raise ApiProblem(409, "not_paid", "The order has no authorized payment to capture.")
        raise ApiProblem(409, "not_fulfillable", f"The order cannot be fulfilled in state '{payment.state}'.")

    try:
        renewal_refused = _ensure_fresh_authorization(payment)
        _capture(payment, renewal_refused)
    except BaseException:
        # Whatever happened, never leave the claim held: return to "authorized"
        # unless a step already moved the payment on.
        OrderPayment.objects.filter(pk=payment.pk, state=OrderPayment.CAPTURING).update(
            state=OrderPayment.AUTHORIZED, date_updated=timezone.now()
        )
        raise
    payment.refresh_from_db()
    return payment, True


def _release_capturing(payment: OrderPayment, state: str, message: str) -> None:
    OrderPayment.objects.filter(pk=payment.pk, state=OrderPayment.CAPTURING).update(
        state=state, last_error=message, date_updated=timezone.now()
    )


def _ensure_fresh_authorization(payment: OrderPayment) -> str | None:
    """
    Make sure the authorization can still be captured; renew a stale one.
    Returns PayPal's reason when it refused to renew a stale authorization.
    """
    client = gateway.get_client()
    try:
        current: PaymentAuthorization = gateway.call(
            lambda: client.payments.get_authorized_payment(payment.authorization_id)
        )
    except PayPalError as e:
        raise _rejection(e) if isinstance(e, ProviderRejected) else ApiProblem(
            e.status_code, "paypal_unavailable", e.message, **e.details()
        ) from e

    status = current.status
    now = timezone.now()
    expires = _provider_time(current.expiration_time) or payment.authorization_expires
    if status in (AuthorizationStatus.VOIDED, AuthorizationStatus.DENIED):
        message = (
            f"PayPal reports authorization {payment.authorization_id} as {_text(status)}; no funds are held. "
            "Ask the shopper to pay again, or cancel the order."
        )
        _release_capturing(payment, OrderPayment.FAILED, message)
        raise ApiProblem(409, "authorization_not_capturable", message)
    if expires is not None and expires <= now:
        message = (
            f"Authorization {payment.authorization_id} expired on {expires.isoformat()} and can no longer "
            "be renewed or captured (PayPal holds authorizations for at most 29 days). "
            "Ask the shopper to pay again (POST /api/orders/{id}/pay), or cancel the order.".replace(
                "{id}", payment.order.number
            )
        )
        _release_capturing(payment, OrderPayment.EXPIRED, message)
        raise ApiProblem(409, "authorization_expired", message)

    honor_start = _provider_time(current.create_time) or payment.authorization_time
    if honor_start is None or now - honor_start <= HONOR_PERIOD:
        return None
    return _reauthorize(payment)


def _reauthorize(payment: OrderPayment) -> str | None:
    """Renew a stale authorization. Returns PayPal's refusal reason, or None when renewed."""
    op, created = _claim(
        f"reauthorize:{payment.pk}:{payment.authorization_id}",
        PaymentOperation.REAUTHORIZE,
        order_payment=payment,
        amount=payment.amount,
    )
    if not created:
        if op.status == PaymentOperation.FAILED:
            return op.error or "renewal refused earlier"
        if op.status == PaymentOperation.DONE:
            return None
    body = ReauthorizeRequest(amount=money.money(payment.amount, payment.currency))
    client = gateway.get_client()
    try:
        result: PaymentAuthorization = _send_with_lookup(
            op,
            lambda request_id: client.payments.reauthorize_payment(
                payment.authorization_id,
                pay_pal_request_id=request_id,
                prefer="return=representation",
                body=body,
            ),
        )
    except ProviderRejected as e:
        refusal = f"{e.issue}: {e.description}" if e.issue else e.message
        _settle(op, PaymentOperation.FAILED, error=refusal)
        logger.warning("PayPal refused to reauthorize %s: %s", payment.authorization_id, refusal)
        return refusal
    except PayPalError as e:
        if e.outcome_unknown:
            raise _outcome_unknown(op, e, "reauthorization") from e
        _settle(op, PaymentOperation.FAILED, error=e.message)
        raise ApiProblem(e.status_code, "paypal_unavailable", e.message, **e.details()) from e

    outcome = authorization_outcome(result.status)
    echoed = money.from_money(result.amount)
    new_id = _text(result.id)
    if outcome == PaymentOperation.DONE and (not new_id or echoed != (payment.amount, payment.currency)):
        outcome = PaymentOperation.NEEDS_REVIEW
    _settle(
        op,
        outcome,
        provider_id=new_id,
        provider_status=_text(result.status),
        provider_time=_provider_time(result.create_time),
    )
    if outcome == PaymentOperation.DONE:
        OrderPayment.objects.filter(pk=payment.pk).update(
            authorization_id=new_id,
            authorization_status=_text(result.status),
            authorization_time=_provider_time(result.create_time) or timezone.now(),
            authorization_expires=_provider_time(result.expiration_time) or payment.authorization_expires,
            reauthorized=True,
        )
        payment.refresh_from_db()
        return None
    if outcome == PaymentOperation.FAILED:
        return f"reauthorization {_text(result.status)}"
    message = (
        f"PayPal's renewal of the stale authorization is {_text(result.status) or 'unconfirmed'}; "
        "retry fulfilment later."
    )
    _release_capturing(payment, OrderPayment.AUTHORIZED, message)
    raise ApiProblem(409, "reauthorization_pending", message)


def _capture(payment: OrderPayment, renewal_refused: str | None) -> None:
    op, created = _claim(
        f"capture:{payment.pk}:{payment.authorization_id}",
        PaymentOperation.CAPTURE,
        order_payment=payment,
        amount=payment.amount,
    )
    if not created and op.status == PaymentOperation.FAILED:
        # A previous capture of this authorization was definitely refused:
        # this is a new attempt, so it gets a new request id.
        op.request_id = str(uuid.uuid4())
        _settle(op, PaymentOperation.SENDING, error="")
    body = CaptureRequest(amount=money.money(payment.amount, payment.currency), final_capture=True)
    client = gateway.get_client()
    try:
        result: CapturedPayment = _send_with_lookup(
            op,
            lambda request_id: client.payments.capture_authorized_payment(
                payment.authorization_id,
                pay_pal_request_id=request_id,
                prefer="return=representation",
                body=body,
            ),
        )
    except PayPalError as e:
        if e.outcome_unknown:
            problem = _outcome_unknown(op, e, "capture")
            _release_capturing(payment, OrderPayment.AUTHORIZED, problem.message)
            raise problem from e
        _settle(op, PaymentOperation.FAILED, error=e.message)
        if renewal_refused:
            message = (
                f"Authorization {payment.authorization_id} is past PayPal's 3-day honor period and PayPal "
                f"refused to renew it ({renewal_refused}); the capture was then refused too "
                f"({e.issue + ': ' if e.issue else ''}{e.description or e.message}). The held funds cannot be "
                "collected. Cancel the order to release the hold, or ask the shopper to pay again."
            )
            _release_capturing(payment, OrderPayment.AUTHORIZED, message)
            raise ApiProblem(409, "authorization_not_renewable", message, **e.details()) from e
        _release_capturing(payment, OrderPayment.AUTHORIZED, e.message)
        raise _rejection(e) from e

    outcome = capture_outcome(result.status)
    echoed = money.from_money(result.amount)
    fee = net = None
    if not isinstance(result.seller_receivable_breakdown, UnsetType):
        breakdown = result.seller_receivable_breakdown
        fee_money = money.from_money(breakdown.paypal_fee)
        net_money = money.from_money(breakdown.net_amount)
        fee = fee_money[0] if fee_money else None
        net = net_money[0] if net_money else None
    capture_id = _text(result.id)
    message = ""
    if outcome == PaymentOperation.DONE and (not capture_id or echoed != (payment.amount, payment.currency)):
        outcome = PaymentOperation.NEEDS_REVIEW
        message = f"PayPal captured {echoed}, expected {payment.amount} {payment.currency}; review required."

    capture_time = _provider_time(result.create_time)
    state = {
        PaymentOperation.DONE: OrderPayment.CAPTURED,
        PaymentOperation.PENDING: OrderPayment.CAPTURE_PENDING,
        PaymentOperation.NEEDS_REVIEW: OrderPayment.NEEDS_REVIEW,
        PaymentOperation.FAILED: OrderPayment.AUTHORIZED,
    }.get(outcome, OrderPayment.UNKNOWN)
    if outcome == PaymentOperation.FAILED:
        message = f"PayPal did not capture the payment (status {_text(result.status)})."

    with transaction.atomic():
        _settle(
            op,
            outcome,
            provider_id=capture_id,
            provider_status=_text(result.status),
            provider_time=capture_time,
            error=message,
        )
        fields: dict[str, Any] = {"last_error": message}
        if capture_id:
            fields.update(
                capture_id=capture_id,
                capture_status=_text(result.status),
                captured_amount=echoed[0] if echoed else None,
                paypal_fee=fee,
                net_amount=net,
                capture_time=capture_time,
            )
        _release_capturing(payment, state, message)
        OrderPayment.objects.filter(pk=payment.pk).update(**fields)
        if state == OrderPayment.CAPTURED:
            payment.refresh_from_db()
            _source(payment).debit(payment.captured_amount, reference=capture_id, status=_text(result.status))
            _set_order_status(payment.order, STATUS_COMPLETE)

    if outcome == PaymentOperation.FAILED:
        raise ApiProblem(409, "capture_declined", message)


# ---------------------------------------------------------------------------
# Flow 1 - cancel (void)
# ---------------------------------------------------------------------------


def cancel(order_id: str) -> tuple[OrderPayment, bool]:
    payment = any_payment(order_id)

    # Nothing held at PayPal: cancel locally.
    with transaction.atomic():
        changed = OrderPayment.objects.filter(
            pk=payment.pk, state__in=OrderPayment.PAYABLE_STATES
        ).update(state=OrderPayment.CANCELLED, date_updated=timezone.now())
        if changed:
            payment.refresh_from_db()
            _set_order_status(payment.order, STATUS_CANCELLED)
            return payment, True

    changed = OrderPayment.objects.filter(
        pk=payment.pk, state__in=(OrderPayment.AUTHORIZED, OrderPayment.AUTHORIZATION_PENDING)
    ).update(state=OrderPayment.VOIDING, last_error="", date_updated=timezone.now())
    payment.refresh_from_db()
    if not changed:
        if payment.state in (OrderPayment.VOIDED, OrderPayment.CANCELLED):
            return payment, False
        if payment.state in OrderPayment.CAPTURED_STATES + (OrderPayment.CAPTURE_PENDING,):
            raise ApiProblem(
                409, "already_fulfilled", "The payment was already captured; issue a refund instead."
            )
        if payment.state in (OrderPayment.VOIDING, OrderPayment.CAPTURING, OrderPayment.AUTHORIZING):
            raise ApiProblem(409, "operation_in_progress", f"The order is currently {payment.state}.")
        raise ApiProblem(409, "not_cancellable", f"The order cannot be cancelled in state '{payment.state}'.")

    def release(state: str, message: str = "") -> None:
        OrderPayment.objects.filter(pk=payment.pk, state=OrderPayment.VOIDING).update(
            state=state, last_error=message, date_updated=timezone.now()
        )

    try:
        return _void(payment, release), True
    except BaseException:
        release(OrderPayment.AUTHORIZED)
        raise


def _void(payment: OrderPayment, release: Callable[..., None]) -> OrderPayment:
    op, created = _claim(
        f"void:{payment.pk}:{payment.authorization_id}",
        PaymentOperation.VOID,
        order_payment=payment,
        amount=payment.amount,
    )
    if not created and op.status == PaymentOperation.FAILED:
        op.request_id = str(uuid.uuid4())
        _settle(op, PaymentOperation.SENDING, error="")
    client = gateway.get_client()
    provider_status = ""
    try:
        result: PaymentAuthorization = _send_with_lookup(
            op,
            lambda request_id: client.payments.void_payment(
                payment.authorization_id, pay_pal_request_id=request_id, prefer="return=representation"
            ),
        )
        provider_status = _text(result.status)
        outcome = (
            PaymentOperation.DONE if result.status == AuthorizationStatus.VOIDED else PaymentOperation.UNKNOWN
        )
    except PayPalError as e:
        if e.outcome_unknown:
            problem = _outcome_unknown(op, e, "cancellation")
            release(OrderPayment.AUTHORIZED, problem.message)
            raise problem from e
        if isinstance(e, ProviderRejected) and e.issue == "PREVIOUSLY_VOIDED":
            outcome, provider_status = PaymentOperation.DONE, "VOIDED"  # an earlier void landed
        else:
            _settle(op, PaymentOperation.FAILED, error=e.message)
            release(OrderPayment.AUTHORIZED, e.message)
            raise _rejection(e) from e

    if outcome != PaymentOperation.DONE:
        message = f"PayPal answered the void with status {provider_status or '(none)'}; review required."
        _settle(op, PaymentOperation.UNKNOWN, provider_id=payment.authorization_id,
                provider_status=provider_status, error=message)
        release(OrderPayment.NEEDS_REVIEW, message)
        raise ApiProblem(502, "void_unconfirmed", message)

    with transaction.atomic():
        _settle(
            op,
            PaymentOperation.DONE,
            provider_id=payment.authorization_id,
            provider_status=provider_status,
            provider_time=timezone.now(),
        )
        release(OrderPayment.VOIDED)
        OrderPayment.objects.filter(pk=payment.pk).update(authorization_status=provider_status)
        payment.refresh_from_db()
        source = _source(payment)
        Transaction.objects.create(
            source=source, txn_type="Void", amount=payment.amount,
            reference=payment.authorization_id, status=provider_status,
        )
        _set_order_status(payment.order, STATUS_CANCELLED)
    return payment


# ---------------------------------------------------------------------------
# Flow 1 - refunds
# ---------------------------------------------------------------------------


def refund(
    user: User, order_id: str, raw_amount: object, idempotency_key: str
) -> tuple[PayPalRefund, bool]:
    """Returns (refund, created). ``created`` is False when the key was seen before."""
    payment = owned_payment(user, order_id)
    currency = payment.currency
    amount: Decimal | None = None
    if raw_amount is not None:
        amount = money.parse_amount(raw_amount)
        if amount is None or amount <= 0 or not money.is_exact(amount, currency):
            raise ApiProblem(
                400, "invalid_amount", f"'amount' must be a positive {currency} amount, e.g. \"5.00\"."
            )
    fingerprint = money.to_str(amount, currency) if amount is not None else "full"

    op, created = _claim(
        f"refund:{payment.pk}:{idempotency_key}",
        PaymentOperation.REFUND,
        order_payment=payment,
        user=user,
        fingerprint=fingerprint,
    )
    if not created:
        return _replay_refund(payment, op, fingerprint), False

    try:
        if payment.state not in OrderPayment.CAPTURED_STATES or not payment.capture_id:
            raise ApiProblem(409, "not_refundable", "Only a fulfilled (captured) order can be refunded.")
        reserved = _reserve_refund(payment, amount)
    except ApiProblem as problem:
        _settle(op, PaymentOperation.FAILED, error=problem.message)
        raise
    _settle(op, PaymentOperation.SENDING, amount=reserved)
    record = PayPalRefund.objects.create(
        order_payment=payment,
        operation=op,
        idempotency_key=idempotency_key,
        amount=reserved,
        currency=currency,
    )
    _send_refund(payment, op, record)
    record.refresh_from_db()
    return record, True


def _reserve_refund(payment: OrderPayment, amount: Decimal | None) -> Decimal:
    """Atomically reserve ``amount`` (or everything left) against the captured amount."""
    for _ in range(10):
        current = OrderPayment.objects.get(pk=payment.pk)
        captured = current.captured_amount or Decimal(0)
        remaining = captured - current.refund_reserved
        wanted = remaining if amount is None else amount
        if remaining <= 0:
            raise ApiProblem(409, "fully_refunded", "The payment has already been fully refunded.")
        if wanted > remaining:
            raise ApiProblem(
                422,
                "exceeds_refundable",
                f"At most {money.to_str(remaining, current.currency)} {current.currency} can still be refunded.",
                refundable=money.to_str(remaining, current.currency),
            )
        # Compare-and-set on the reserved total: a concurrent refund makes
        # this update match nothing, and we re-read and re-check.
        if OrderPayment.objects.filter(pk=payment.pk, refund_reserved=current.refund_reserved).update(
            refund_reserved=current.refund_reserved + wanted
        ):
            return wanted
    raise ApiProblem(409, "refund_in_progress", "Another refund is being recorded; try again.")


def _release_reservation(payment: OrderPayment, amount: Decimal) -> None:
    OrderPayment.objects.filter(pk=payment.pk).update(refund_reserved=F("refund_reserved") - amount)


def _replay_refund(payment: OrderPayment, op: PaymentOperation, fingerprint: str) -> PayPalRefund:
    if op.fingerprint != fingerprint:
        raise ApiProblem(
            422,
            "idempotency_key_reused",
            "This idempotency key was already used for a different refund request.",
        )
    record = PayPalRefund.objects.filter(operation=op).first()
    if record is None:
        # The earlier request failed before anything was sent (e.g. nothing refundable).
        raise ApiProblem(409, "refund_rejected", op.error or "The earlier refund request was rejected.")
    if op.status == PaymentOperation.SENDING and _is_fresh(op):
        raise ApiProblem(409, "refund_in_progress", "This refund is still being processed.")
    if op.status in (PaymentOperation.SENDING, PaymentOperation.UNKNOWN):
        # Outcome never confirmed: take the claim over and look it up by
        # resending under the same PayPal-Request-Id (never a new one).
        taken = PaymentOperation.objects.filter(
            pk=op.pk, status=op.status, date_updated=op.date_updated
        ).update(status=PaymentOperation.SENDING, date_updated=timezone.now())
        if not taken:
            raise ApiProblem(409, "refund_in_progress", "This refund is still being processed.")
        op.refresh_from_db()
        _send_refund(payment, op, record)
        record.refresh_from_db()
    return record


def _send_refund(payment: OrderPayment, op: PaymentOperation, record: PayPalRefund) -> None:
    body = RefundRequest(amount=money.money(record.amount, record.currency))
    client = gateway.get_client()
    try:
        result: Refund = _send_with_lookup(
            op,
            lambda request_id: client.payments.refund_captured_payment(
                payment.capture_id, pay_pal_request_id=request_id, prefer="return=representation", body=body
            ),
        )
    except PayPalError as e:
        if e.outcome_unknown:
            problem = _outcome_unknown(op, e, "refund")
            PayPalRefund.objects.filter(pk=record.pk).update(status=PaymentOperation.UNKNOWN)
            problem.extra["refundId"] = str(record.public_id)
            raise problem from e
        with transaction.atomic():
            _settle(op, PaymentOperation.FAILED, error=e.message)
            PayPalRefund.objects.filter(pk=record.pk).update(status=PaymentOperation.FAILED)
            _release_reservation(payment, record.amount)
        problem = _rejection(e)
        problem.extra["refundId"] = str(record.public_id)
        raise problem from e

    outcome = refund_outcome(result.status)
    echoed = money.from_money(result.amount)
    refund_id = _text(result.id)
    if outcome == PaymentOperation.DONE and (not refund_id or echoed != (record.amount, record.currency)):
        outcome = PaymentOperation.NEEDS_REVIEW
    provider_time = _provider_time(result.create_time)
    with transaction.atomic():
        _settle(
            op,
            outcome,
            provider_id=refund_id,
            provider_status=_text(result.status),
            provider_time=provider_time,
        )
        PayPalRefund.objects.filter(pk=record.pk).update(
            status=outcome,
            refund_id=refund_id,
            provider_status=_text(result.status),
            provider_time=provider_time,
        )
        if outcome == PaymentOperation.FAILED:
            _release_reservation(payment, record.amount)
        elif outcome == PaymentOperation.DONE:
            OrderPayment.objects.filter(pk=payment.pk).update(
                refunded_amount=F("refunded_amount") + record.amount
            )
            payment.refresh_from_db()
            fully = payment.captured_amount is not None and payment.refunded_amount >= payment.captured_amount
            OrderPayment.objects.filter(pk=payment.pk, state__in=OrderPayment.CAPTURED_STATES).update(
                state=OrderPayment.REFUNDED if fully else OrderPayment.PARTIALLY_REFUNDED
            )
            _source(payment).refund(record.amount, reference=refund_id, status=_text(result.status))


# ---------------------------------------------------------------------------
# Flow 2 - saved cards
# ---------------------------------------------------------------------------


def save_card(user: User, payload: dict[str, Any], idempotency_key: str | None) -> tuple[SavedCard, bool]:
    card = parse_card(payload.get("card"))
    key = f"vault:{user.pk}:{idempotency_key or uuid.uuid4()}"
    op, created = _claim(key, PaymentOperation.VAULT, user=user)
    if not created:
        if op.status == PaymentOperation.DONE:
            saved = SavedCard.objects.filter(token_id=op.provider_id, user=user).first()
            if saved is not None:
                return saved, False
        if _is_fresh(op):
            raise ApiProblem(409, "save_in_progress", "This card is already being saved.")
        if op.status == PaymentOperation.FAILED:
            op.request_id = str(uuid.uuid4())  # definitely not saved: a new attempt
        _settle(op, PaymentOperation.SENDING, error="")

    customer = PayPalCustomer.objects.filter(user=user).first()
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                name=card.name or UNSET,
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                billing_address=card.billing_address,
            )
        ),
        customer=Customer(id=customer.customer_id) if customer else UNSET,
    )
    client = gateway.get_client()
    try:
        result: PaymentTokenResponse = _send_with_lookup(
            op, lambda request_id: client.vault.create_payment_token(body, pay_pal_request_id=request_id)
        )
    except PayPalError as e:
        if e.outcome_unknown:
            raise _outcome_unknown(op, e, "card save") from e
        _settle(op, PaymentOperation.FAILED, error=e.message)
        raise _rejection(e) from e

    token_id = _text(result.id)
    customer_id = ""
    if not isinstance(result.customer, UnsetType):
        customer_id = _text(result.customer.id)
    if not token_id:
        _settle(op, PaymentOperation.UNKNOWN, error="PayPal returned no payment token id.")
        raise ApiProblem(502, "outcome_unknown", "PayPal did not return a saved-card id; the outcome is unknown.")
    brand = last_digits = expiry = name = ""
    if not isinstance(result.payment_source, UnsetType) and not isinstance(result.payment_source.card, UnsetType):
        entity = result.payment_source.card
        brand, last_digits = _text(entity.brand), _text(entity.last_digits)
        expiry, name = _text(entity.expiry), _text(entity.name)

    with transaction.atomic():
        if customer is None and customer_id:
            try:
                with transaction.atomic():
                    PayPalCustomer.objects.create(user=user, customer_id=customer_id)
            except IntegrityError:
                pass  # a concurrent first save recorded the customer first
        saved, _ = SavedCard.objects.get_or_create(
            token_id=token_id,
            defaults={
                "user": user,
                "customer_id": customer_id or (customer.customer_id if customer else ""),
                "brand": brand,
                "last_digits": last_digits or card.number[-4:],
                "expiry": expiry or card.expiry,
                "cardholder_name": name or card.name,
            },
        )
        _settle(op, PaymentOperation.DONE, provider_id=token_id, provider_time=timezone.now())
    return saved, True


def delete_card(user: User, public_id: uuid.UUID) -> None:
    card = SavedCard.objects.filter(
        public_id=public_id, user=user, state__in=(SavedCard.ACTIVE, SavedCard.DELETING)
    ).first()
    if card is None:
        raise ApiProblem(404, "payment_method_not_found", "No such saved card.")
    # Unusable for payment from this point on, whatever PayPal answers.
    SavedCard.objects.filter(pk=card.pk, state=SavedCard.ACTIVE).update(state=SavedCard.DELETING)
    client = gateway.get_client()
    try:
        gateway.call(lambda: client.vault.delete_payment_token(card.token_id))
    except ProviderRejected as e:
        if e.paypal_status != 404:  # 404: already gone at PayPal - the goal is met
            raise _rejection(e) from e
    except PayPalError as e:
        raise ApiProblem(
            e.status_code,
            "paypal_unavailable",
            "The card is disabled here but PayPal did not confirm deletion; repeat the request.",
            **e.details(),
        ) from e
    SavedCard.objects.filter(pk=card.pk).update(state=SavedCard.DELETED, date_deleted=timezone.now())


# ---------------------------------------------------------------------------
# Representations
# ---------------------------------------------------------------------------


def _amount(value: Decimal | None, currency: str) -> str | None:
    return money.to_str(value, currency) if value is not None else None


def payment_dict(payment: OrderPayment) -> dict[str, Any]:
    currency = payment.currency
    refunds = [refund_dict(r) for r in payment.refunds.all()]
    captured = payment.captured_amount
    refundable = (captured - payment.refund_reserved) if captured is not None else None
    if payment.state not in OrderPayment.CAPTURED_STATES:
        refundable = None
    return {
        "state": payment.state,
        "currency": currency,
        "amount": money.to_str(payment.amount, currency),
        "paymentMethod": (
            {"brand": payment.card_brand, "lastDigits": payment.card_last_digits,
             "paymentMethodId": str(payment.saved_card.public_id) if payment.saved_card else None}
            if payment.card_last_digits else None
        ),
        "authorization": {
            "id": payment.authorization_id,
            "status": payment.authorization_status,
            "amount": _amount(payment.authorized_amount, currency),
            "createdAt": payment.authorization_time.isoformat() if payment.authorization_time else None,
            "expiresAt": payment.authorization_expires.isoformat() if payment.authorization_expires else None,
            "reauthorized": payment.reauthorized,
            "paypalOrderId": payment.paypal_order_id,
        } if payment.authorization_id else None,
        "capture": {
            "id": payment.capture_id,
            "status": payment.capture_status,
            "amount": _amount(payment.captured_amount, currency),
            "paypalFee": _amount(payment.paypal_fee, currency),
            "netAmount": _amount(payment.net_amount, currency),
            "capturedAt": payment.capture_time.isoformat() if payment.capture_time else None,
        } if payment.capture_id else None,
        "refundedAmount": money.to_str(payment.refunded_amount, currency),
        "refundableAmount": _amount(refundable, currency),
        "refunds": refunds,
        "lastError": payment.last_error or None,
    }


def refund_dict(record: PayPalRefund) -> dict[str, Any]:
    return {
        "refundId": str(record.public_id),
        "status": record.status,
        "paypalStatus": record.provider_status or None,
        "paypalRefundId": record.refund_id or None,
        "amount": money.to_str(record.amount, record.currency),
        "currency": record.currency,
        "idempotencyKey": record.idempotency_key,
        "createdAt": record.date_created.isoformat(),
    }


def order_dict(order: Any, payment: OrderPayment | None) -> dict[str, Any]:
    return {
        "orderId": order.number,
        "status": order.status,
        "placedAt": order.date_placed.isoformat() if order.date_placed else None,
        "currency": order.currency,
        "total": str(order.total_incl_tax),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": str(line.unit_price_incl_tax) if line.unit_price_incl_tax is not None else None,
                "linePrice": str(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": payment_dict(payment) if payment is not None else None,
    }


def card_dict(card: SavedCard) -> dict[str, Any]:
    return {
        "paymentMethodId": str(card.public_id),
        "brand": card.brand,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "cardholderName": card.cardholder_name,
        "state": card.state,
        "createdAt": card.date_created.isoformat(),
    }
