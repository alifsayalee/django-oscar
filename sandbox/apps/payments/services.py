"""
Order, payment, refund and saved-card flows.

Every flow that calls PayPal follows the same shape:

1. **Claim** — one conditional UPDATE moves the local row into an in-flight
   state. Exactly one request wins; the others answer from the row. The claim
   is committed before PayPal is called (views run outside ATOMIC_REQUESTS).
2. **Call** — with a PayPal-Request-Id derived from the operation, so a retry
   after an unknown outcome is collapsed by PayPal into the original.
3. **Settle** — from what PayPal *said* (its status enums, mapped member by
   member), after checking the echoed amount against what was asked.

Every exit leaves the row saying what is known, including "unknown".
"""

import hashlib
import logging
import re
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Case, F, Q, Value, When
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core import prices
from oscar.core.loading import get_class, get_model
from paypal.core import Success, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Customer,
    Money,
    Order as PayPalOrder,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    Refund,
    RefundRequest,
    OrderRequest,
    TransactionInformation,
)
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CardVerificationStatus,
    CheckoutPaymentIntent,
    OrderStatus,
    RefundStatus,
)

from . import gateway
from .gateway import ProviderError
from .models import PayPalCustomer, PayPalPayment, PayPalRefund, PayPalTransaction, SavedCard
from .money import parse_amount, parse_provider_time, quantize, to_provider_time, to_wire

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
Selector = get_class("partner.strategy", "Selector")
Applicator = get_class("offer.applicator", "Applicator")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
NoShippingRequired = get_class("shipping.methods", "NoShippingRequired")
Price = prices.Price

# Oscar order statuses (sandbox OSCAR_ORDER_STATUS_PIPELINE)
STATUS_PENDING = settings.OSCAR_INITIAL_ORDER_STATUS  # awaiting payment
STATUS_PROCESSING = "Being processed"  # funds held
STATUS_COMPLETE = "Complete"  # fulfilled, funds taken
STATUS_CANCELLED = "Cancelled"

# An in-flight claim older than this is treated as abandoned (the worker died
# mid-call). Comfortably above one PayPal timeout.
SEND_WINDOW = timedelta(minutes=2)

# PayPal honours a card authorization for 3 days; after that it must be
# reauthorized (once, day 4 to 29) before capture. (reauthorize_payment docstring)
HONOR_PERIOD = timedelta(days=3)

# Reporting API: at most 31 days per query (search_transactions docstring)
SEARCH_CHUNK = timedelta(days=31)
SEARCH_MAX_SPAN = timedelta(days=3 * 366)
SEARCH_PAGE_SIZE = 500  # the API's maximum (501 is rejected)
SEARCH_MAX_PAGES = 100  # per 31-day chunk; beyond it the report says truncated
SEARCH_CONCURRENCY = 4

MAX_LINES = 50
MAX_QUANTITY = 1000

SOURCE_TYPE_NAME = "PayPal"


# ---------------------------------------------------------------------------
# Errors a flow reports to the view layer
# ---------------------------------------------------------------------------


class ServiceError(Exception):
    status_code = 400
    code = "invalid_request"

    def __init__(self, message: str, *, code: str | None = None, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.extra = extra or {}


class InvalidRequest(ServiceError):
    status_code = 422
    code = "invalid_request"


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class Conflict(ServiceError):
    status_code = 409
    code = "conflict"


class InProgress(ServiceError):
    """Another request holds the claim and may still be talking to PayPal."""

    status_code = 202
    code = "in_progress"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _set(value: Any) -> Any:
    """``None`` for an SDK ``UNSET`` member, the value otherwise."""
    return None if isinstance(value, UnsetType) else value


def _money(value: Any) -> tuple[Decimal, str] | None:
    money = _set(value)
    if money is None:
        return None
    return Decimal(money.value), money.currency_code


def _request_id(payment: PayPalPayment, *parts: str) -> str:
    return "-".join([payment.reference, *parts])


def _source_for(payment: PayPalPayment) -> Any:
    return Source.objects.filter(order=payment.order, source_type__name=SOURCE_TYPE_NAME).first()


def _record_transaction(payment: PayPalPayment, kind: str, paypal_id: str, amount: Decimal,
                        currency: str, status: str, provider_time: datetime | None) -> None:
    PayPalTransaction.objects.update_or_create(
        paypal_id=paypal_id,
        defaults={
            "payment": payment,
            "kind": kind,
            "amount": amount,
            "currency": currency,
            "status": status,
            "provider_time": provider_time,
        },
    )


def _move(payment: PayPalPayment, to_state: str, *, from_states: list[str] | None = None, **fields: Any) -> bool:
    """Conditionally move ``payment`` to ``to_state``; returns whether this call changed it."""
    qs = PayPalPayment.objects.filter(pk=payment.pk)
    if from_states is not None:
        qs = qs.filter(state__in=from_states)
    now = timezone.now()
    changed = qs.update(state=to_state, state_changed_at=now, **fields) == 1
    if changed:
        payment.state = to_state
        payment.state_changed_at = now
        for name, value in fields.items():
            if not hasattr(value, "resolve_expression"):
                setattr(payment, name, value)
    return changed


def _set_order_status(order: Any, status: str) -> None:
    order.refresh_from_db()
    if order.status != status and status in order.available_statuses():
        order.set_status(status)


# ---------------------------------------------------------------------------
# Card input
# ---------------------------------------------------------------------------


@dataclass
class CardInput:
    number: str
    expiry: str
    security_code: str
    name: str = ""
    billing_address: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # never let card data reach a log line
        return "CardInput(ending %s)" % self.number[-4:]

    @property
    def last_digits(self) -> str:
        return self.number[-4:]

    def paypal_address(self) -> Address | None:
        if not self.billing_address:
            return None
        return Address(**self.billing_address)


_ADDRESS_FIELDS = {
    "addressLine1": "address_line_1",
    "addressLine2": "address_line_2",
    "adminArea2": "admin_area_2",
    "city": "admin_area_2",
    "adminArea1": "admin_area_1",
    "state": "admin_area_1",
    "postalCode": "postal_code",
    "countryCode": "country_code",
}


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, digit in enumerate(reversed(number)):
        d = int(digit)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Card details must never appear in an error report's frame locals
@sensitive_variables()
def parse_card(data: Any) -> CardInput:
    if not isinstance(data, dict):
        raise InvalidRequest("card must be an object with number, expiry and securityCode")
    number = re.sub(r"[\s-]", "", str(data.get("number", "")))
    if not re.fullmatch(r"\d{12,19}", number) or not _luhn_ok(number):
        raise InvalidRequest("card.number is not a valid card number", code="invalid_card")
    raw_expiry = str(data.get("expiry", "")).strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})", raw_expiry) or re.fullmatch(r"(\d{2})/(\d{2}|\d{4})", raw_expiry)
    if not m:
        raise InvalidRequest("card.expiry must be YYYY-MM (or MM/YY)", code="invalid_card")
    if "-" in raw_expiry:
        year, month = int(m.group(1)), int(m.group(2))
    else:
        month, year = int(m.group(1)), int(m.group(2))
        year = year + 2000 if year < 100 else year
    today = timezone.now().date()
    if not 1 <= month <= 12 or (year, month) < (today.year, today.month):
        raise InvalidRequest("card.expiry must be a current or future month", code="invalid_card")
    cvc = str(data.get("securityCode", data.get("cvc", ""))).strip()
    if not re.fullmatch(r"\d{3,4}", cvc):
        raise InvalidRequest("card.securityCode must be 3 or 4 digits", code="invalid_card")
    name = str(data.get("name", "")).strip()[:300]
    address: dict[str, str] = {}
    raw_address = data.get("billingAddress")
    if raw_address is not None:
        if not isinstance(raw_address, dict):
            raise InvalidRequest("card.billingAddress must be an object")
        for key, value in raw_address.items():
            target = _ADDRESS_FIELDS.get(key)
            if target is None:
                raise InvalidRequest("card.billingAddress.%s is not a recognised field" % key)
            if value not in (None, ""):
                address[target] = str(value).strip()[:300]
        country = address.get("country_code", "").upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise InvalidRequest("card.billingAddress.countryCode must be a 2-letter ISO code")
        address["country_code"] = country
    return CardInput(
        number=number,
        expiry="%04d-%02d" % (year, month),
        security_code=cvc,
        name=name,
        billing_address=address,
    )


# ---------------------------------------------------------------------------
# Placing an order
# ---------------------------------------------------------------------------


def _parse_items(items: Any) -> list[tuple[int, int]]:
    if not isinstance(items, list) or not items:
        raise InvalidRequest("items must be a non-empty list of {itemId, quantity}")
    if len(items) > MAX_LINES:
        raise InvalidRequest("an order may contain at most %d items" % MAX_LINES)
    wanted: dict[int, int] = {}
    for entry in items:
        if not isinstance(entry, dict):
            raise InvalidRequest("each item must be an object with itemId and quantity")
        item_id, quantity = entry.get("itemId"), entry.get("quantity", 1)
        if isinstance(item_id, str) and item_id.isdigit():
            item_id = int(item_id)
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            raise InvalidRequest("itemId must be a catalogue item id")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise InvalidRequest("quantity must be an integer between 1 and %d" % MAX_QUANTITY)
        wanted[item_id] = wanted.get(item_id, 0) + quantity
    return list(wanted.items())


def place_order(user: Any, items: Any, request: Any = None) -> Any:
    """
    Create an Oscar order (via a basket and Oscar's ``OrderCreator``) awaiting payment.

    Amounts are the catalogue prices, after any offers Oscar applies; the
    currency is ``PAYPAL_CURRENCY``.
    """
    lines = _parse_items(items)
    currency = gateway.currency()
    products = {
        p.pk: p
        for p in Product.objects.filter(pk__in=[pk for pk, _ in lines]).exclude(structure=Product.PARENT)
    }
    missing = [pk for pk, _ in lines if pk not in products or not products[pk].is_public]
    if missing:
        raise InvalidRequest(
            "unknown catalogue item(s): %s" % ", ".join(map(str, missing)), code="unknown_item"
        )

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for pk, quantity in lines:
            product = products[pk]
            info = basket.strategy.fetch_for_product(product)
            if not info.price.exists or info.stockrecord is None:
                raise InvalidRequest("item %s has no price" % pk, code="unavailable_item")
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise InvalidRequest("item %s: %s" % (pk, reason), code="unavailable_item")
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, request)
        basket.freeze()

        shipping_method = NoShippingRequired()
        shipping_charge = shipping_method.calculate(basket)
        basket_total = OrderTotalCalculator().calculate(basket, shipping_charge)
        if basket_total.incl_tax is None:
            raise InvalidRequest("the order total could not be determined")
        total = Price(
            currency=currency,
            excl_tax=quantize(basket_total.excl_tax, currency),
            incl_tax=quantize(basket_total.incl_tax, currency),
        )
        if total.incl_tax <= 0:
            raise InvalidRequest("the order total must be greater than zero")

        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            order_number=OrderNumberGenerator().order_number(basket),
            status=STATUS_PENDING,
            request=request,
        )
        basket.submit()
        PayPalPayment.objects.create(
            order=order,
            state=PayPalPayment.AWAITING_PAYMENT,
            state_changed_at=timezone.now(),
            currency=currency,
            amount=total.incl_tax,
        )
    logger.info("Order %s placed by user %s for %s %s", order.number, user.pk, total.incl_tax, currency)
    return order


def get_order_for(user: Any, number: str, *, staff: bool = False) -> Any:
    """The order, if ``user`` may act on it; NotFound otherwise (never reveals others' orders)."""
    qs = Order.objects.select_related("paypal_payment")
    if not staff:
        qs = qs.filter(user=user)
    try:
        order = qs.get(number=number)
    except Order.DoesNotExist:
        raise NotFound("order not found") from None
    if not hasattr(order, "paypal_payment"):
        raise NotFound("order not found")
    return order


# ---------------------------------------------------------------------------
# Authorize (pay)
# ---------------------------------------------------------------------------


def _claim_for_pay(payment: PayPalPayment, saved_card: SavedCard | None) -> str:
    """
    Claim ``payment`` for an authorization and return the state it was claimed
    from. Raises when another request holds it or it is not payable.

    Resuming ``awaiting_payment`` or an ``authorization_unknown`` / abandoned
    attempt keeps ``attempt`` (so PayPal sees the same PayPal-Request-Id and
    returns the original order if the earlier call landed). Retrying after a
    definitive decline or expiry starts a new attempt.
    """
    now = timezone.now()
    open_order = PayPalPayment.objects.filter(pk=payment.pk).exclude(
        order__status__in=[STATUS_CANCELLED, STATUS_COMPLETE]
    )
    claim = {
        "saved_card": saved_card,
        "state": PayPalPayment.AUTHORIZING,
        "state_changed_at": now,
        "last_error": "",
    }
    candidates: list[tuple[str, Any, dict[str, Any]]] = [
        (PayPalPayment.AWAITING_PAYMENT, Q(state=PayPalPayment.AWAITING_PAYMENT), {}),
        (
            PayPalPayment.AUTHORIZATION_UNKNOWN,
            Q(state=PayPalPayment.AUTHORIZATION_UNKNOWN)
            | Q(state=PayPalPayment.AUTHORIZING, state_changed_at__lt=now - SEND_WINDOW),
            {},
        ),
        (
            PayPalPayment.AUTHORIZATION_FAILED,
            Q(state__in=[PayPalPayment.AUTHORIZATION_FAILED, PayPalPayment.AUTHORIZATION_EXPIRED]),
            {"attempt": F("attempt") + 1},
        ),
    ]
    for prior, condition, extra in candidates:
        if open_order.filter(condition).update(**claim, **extra):
            payment.refresh_from_db()
            return prior
    payment.refresh_from_db()
    order = payment.order
    if payment.state == PayPalPayment.AUTHORIZING:
        raise InProgress("a payment for this order is already being processed")
    if order.status in (STATUS_CANCELLED, STATUS_COMPLETE):
        raise Conflict("order %s is %s and cannot be paid" % (order.number, order.status.lower()))
    raise Conflict(
        "order %s is not awaiting payment (payment state: %s)" % (order.number, payment.state),
        code="already_paid",
    )


def _saved_card_for(user: Any, payment_method_id: Any) -> SavedCard:
    try:
        public_id = uuid.UUID(str(payment_method_id))
    except ValueError:
        raise NotFound("saved card not found", code="payment_method_not_found") from None
    card = SavedCard.objects.filter(public_id=public_id, user=user, status=SavedCard.ACTIVE).first()
    if card is None:
        raise NotFound("saved card not found", code="payment_method_not_found")
    return card


# Card details must never appear in an error report's frame locals
@sensitive_variables()
def pay(order: Any, user: Any, payload: dict[str, Any]) -> PayPalPayment:
    """Authorize the order total on a one-off card or one of the shopper's saved cards."""
    card: CardInput | None = None
    saved_card: SavedCard | None = None
    method_id = payload.get("paymentMethodId")
    if method_id and payload.get("card"):
        raise InvalidRequest("send either card or paymentMethodId, not both")
    if method_id:
        saved_card = _saved_card_for(user, method_id)
    elif "card" in payload:
        card = parse_card(payload["card"])
    else:
        raise InvalidRequest("send card details or a paymentMethodId")

    payment: PayPalPayment = order.paypal_payment
    prior = _claim_for_pay(payment, saved_card)
    if saved_card is not None:
        # The card may have been removed between the lookup and the claim
        if not SavedCard.objects.filter(pk=saved_card.pk, status=SavedCard.ACTIVE).exists():
            _move(payment, prior, saved_card=None)
            raise NotFound("saved card not found", code="payment_method_not_found")
        card_request = CardRequest(vault_id=saved_card.vault_token_id)
    else:
        assert card is not None
        card_request = _card_request(card)
    currency = payment.currency
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=order.number,
                invoice_id=_request_id(payment, str(payment.attempt)),
                custom_id=order.number,
                amount=AmountWithBreakdown(currency_code=currency, value=to_wire(payment.amount, currency)),
            )
        ],
        payment_source=PaymentSource(card=card_request),
    )
    request_id = _request_id(payment, "auth", str(payment.attempt))
    try:
        result = gateway.call(
            "authorization",
            lambda: gateway.get_client().orders.create_order(
                body, pay_pal_request_id=request_id, prefer="return=representation"
            ),
        )
    except ProviderError as e:
        if e.outcome_unknown:
            _move(payment, PayPalPayment.AUTHORIZATION_UNKNOWN, last_error=e.message)
            e.message += " Retrying the payment is safe: it will not authorize twice."
        elif e.provider_status is None:
            # Never sent: nothing happened this time, so the row goes back to
            # what it was — including "unknown" if an earlier attempt may have landed.
            _move(payment, prior, last_error=e.message)
        else:
            _move(payment, PayPalPayment.AUTHORIZATION_FAILED, last_error=e.message)
        raise
    return _settle_authorization(payment, result, saved_card=saved_card, card=card)


# Card details must never appear in an error report's frame locals
@sensitive_variables()
def _card_request(card: CardInput) -> CardRequest:
    fields: dict[str, Any] = {
        "number": card.number,
        "expiry": card.expiry,
        "security_code": card.security_code,
    }
    if card.name:
        fields["name"] = card.name
    address = card.paypal_address()
    if address is not None:
        fields["billing_address"] = address
    return CardRequest(**fields)


def _first_authorization(result: PayPalOrder) -> Any:
    units = _set(result.purchase_units) or []
    if not units:
        return None
    payments = _set(units[0].payments)
    auths = (_set(payments.authorizations) if payments is not None else None) or []
    return auths[0] if auths else None


def _settle_authorization(payment: PayPalPayment, result: PayPalOrder, *, saved_card: SavedCard | None,
                          card: CardInput | None) -> PayPalPayment:
    order = payment.order
    paypal_order_id = _set(result.id)
    if not paypal_order_id:
        _move(payment, PayPalPayment.AUTHORIZATION_UNKNOWN,
              last_error="PayPal accepted the payment but returned no order id.")
        raise ProviderError(502, "PayPal's answer carried no order id; retrying the payment is safe.",
                            outcome_unknown=True)
    authorization = _first_authorization(result)

    brand, last_digits = "", ""
    paypal_source = _set(result.payment_source)
    paypal_card = _set(paypal_source.card) if paypal_source is not None else None
    if paypal_card is not None:
        brand = str(_set(paypal_card.brand) or "")
        last_digits = str(_set(paypal_card.last_digits) or "")
    if not last_digits:
        if saved_card is not None:
            brand, last_digits = saved_card.brand, saved_card.last_digits
        elif card is not None:
            last_digits = card.last_digits

    common: dict[str, Any] = {
        "paypal_order_id": paypal_order_id,
        "card_brand": brand,
        "card_last_digits": last_digits,
    }
    order_status = _set(result.status)
    if order_status == OrderStatus.PAYER_ACTION_REQUIRED:
        # A 3-D Secure (or similar) challenge the shopper would have to complete
        # in a browser. This integration deliberately has no approval round-trip.
        message = ("PayPal requires the shopper to approve this card payment in a browser "
                   "(PAYER_ACTION_REQUIRED); browser approval is not supported. Use another card.")
        _move(payment, PayPalPayment.AUTHORIZATION_FAILED, last_error=message, **common)
        logger.warning("Order %s: PayPal asked for payer action on %s", order.number, paypal_order_id)
        raise Conflict(message, code="payer_action_required")
    if order_status == OrderStatus.VOIDED:
        _move(payment, PayPalPayment.AUTHORIZATION_FAILED, last_error="PayPal voided the payment.", **common)
        raise Conflict("PayPal voided the payment; try another card.", code="authorization_failed")
    if order_status != OrderStatus.COMPLETED or authorization is None:
        # CREATED / SAVED / APPROVED should not happen for a single-step card
        # payment, and a status newer than this SDK is unknown: neither is "paid".
        message = "PayPal answered with order status %s and no authorization." % (order_status or "none")
        _move(payment, PayPalPayment.NEEDS_REVIEW, last_error=message, **common)
        raise Conflict(message + " An operator must review PayPal order %s." % paypal_order_id,
                       code="needs_review")

    auth_id = _set(authorization.id)
    auth_status = _set(authorization.status)
    echoed = _money(authorization.amount)
    created_at = parse_provider_time(_set(authorization.create_time) or "")
    common.update({
        "authorization_id": auth_id or "",
        "authorization_status": str(auth_status or ""),
        "authorization_created_at": created_at,
        "authorization_expires_at": parse_provider_time(_set(authorization.expiration_time) or ""),
        "reauthorized": False,
    })
    if auth_id and echoed:
        _record_transaction(payment, PayPalTransaction.AUTHORIZATION, auth_id, echoed[0], echoed[1],
                            str(auth_status or ""), created_at)

    match auth_status:
        case AuthorizationStatus.CREATED:
            outcome = PayPalPayment.AUTHORIZED
        case AuthorizationStatus.PENDING:
            outcome = PayPalPayment.AUTHORIZATION_PENDING
        case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
            outcome = PayPalPayment.AUTHORIZATION_FAILED
        case _:  # CAPTURED / PARTIALLY_CAPTURED make no sense here; unknown values neither
            outcome = PayPalPayment.NEEDS_REVIEW

    if outcome == PayPalPayment.AUTHORIZATION_FAILED:
        details = _set(authorization.status_details)
        reason = _set(details.reason) if details is not None else None
        message = "The card was declined (authorization %s%s)." % (auth_status, ", %s" % reason if reason else "")
        _move(payment, outcome, last_error=message, **common)
        raise Conflict(message + " Try another card.", code="card_declined")
    if outcome == PayPalPayment.NEEDS_REVIEW:
        message = "PayPal returned authorization status %s." % auth_status
        _move(payment, outcome, last_error=message, **common)
        raise Conflict(message + " An operator must review the payment.", code="needs_review")

    expected = (quantize(payment.amount, payment.currency), payment.currency)
    if not auth_id or echoed is None or (quantize(echoed[0], echoed[1]), echoed[1]) != expected:
        message = "PayPal held %s instead of %s %s." % (
            "%s %s" % echoed if echoed else "an unknown amount", expected[0], expected[1])
        _move(payment, PayPalPayment.NEEDS_REVIEW, last_error=message, **common)
        logger.error("Order %s: authorization amount mismatch: %s", order.number, message)
        raise Conflict(message + " An operator must review the payment.", code="needs_review")

    with transaction.atomic():
        _move(payment, outcome, last_error="", **common)
        if outcome == PayPalPayment.AUTHORIZED:
            source_type, _ = SourceType.objects.get_or_create(name=SOURCE_TYPE_NAME)
            source = _source_for(payment) or Source(order=order, source_type=source_type,
                                                    currency=payment.currency)
            source.reference = auth_id
            source.label = ("%s ending %s" % (brand or "Card", last_digits)).strip()
            source.amount_allocated = Decimal("0")
            source.save()
            source.allocate(payment.amount, reference=auth_id, status=str(auth_status))
            _set_order_status(order, STATUS_PROCESSING)
    logger.info("Order %s: authorization %s is %s", order.number, auth_id, auth_status)
    if outcome == PayPalPayment.AUTHORIZATION_PENDING:
        raise InProgress("PayPal is reviewing the authorization; it is not held yet.",
                         code="authorization_pending")
    return payment


# ---------------------------------------------------------------------------
# Fulfil (capture)
# ---------------------------------------------------------------------------

# PayPal issues that mean the hold itself is no longer capturable
_AUTHORIZATION_GONE_ISSUES = {
    "AUTHORIZATION_EXPIRED",
    "AUTHORIZATION_VOIDED",
    "AUTHORIZATION_DENIED",
    "PREVIOUSLY_VOIDED",
    "REAUTHORIZATION_EXPIRED",
}


def fulfil(order: Any) -> PayPalPayment:
    """Operator marks the order fulfilled: take the held money, renewing a stale hold first."""
    payment: PayPalPayment = order.paypal_payment
    if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        return payment
    if payment.state == PayPalPayment.CAPTURE_PENDING:
        return _refresh_pending_capture(payment)

    now = timezone.now()
    base = PayPalPayment.objects.filter(pk=payment.pk)
    prior = None
    for state, condition in (
        (PayPalPayment.AUTHORIZED, Q(state=PayPalPayment.AUTHORIZED)),
        (PayPalPayment.CAPTURE_UNKNOWN, Q(state=PayPalPayment.CAPTURE_UNKNOWN)
         | Q(state=PayPalPayment.CAPTURING, state_changed_at__lt=now - SEND_WINDOW)),
    ):
        if base.filter(condition).update(state=PayPalPayment.CAPTURING, state_changed_at=now):
            prior = state
            break
    payment.refresh_from_db()
    if prior is None:
        if payment.state == PayPalPayment.CAPTURING:
            raise InProgress("this order is already being fulfilled")
        raise Conflict(_not_capturable_message(payment), code="not_capturable")

    try:
        return _capture(payment)
    except ProviderError as e:
        if payment.state == PayPalPayment.CAPTURING:
            if e.outcome_unknown:
                _move(payment, PayPalPayment.CAPTURE_UNKNOWN, last_error=e.message)
                e.message += " Retrying the fulfilment is safe: it will not capture twice."
            else:
                _move(payment, prior, last_error=e.message)
        raise
    except ServiceError:
        if payment.state == PayPalPayment.CAPTURING:
            _move(payment, prior)
        raise


def _not_capturable_message(payment: PayPalPayment) -> str:
    match payment.state:
        case PayPalPayment.AWAITING_PAYMENT | PayPalPayment.AUTHORIZATION_FAILED:
            return "The order has not been paid yet; there is nothing to capture."
        case PayPalPayment.AUTHORIZATION_PENDING:
            return "PayPal is still reviewing the authorization; fulfil again once it is approved."
        case PayPalPayment.AUTHORIZATION_EXPIRED:
            return ("The authorization is no longer valid and could not be renewed (%s). Ask the shopper "
                    "to pay again, or cancel the order." % (payment.last_error or "expired"))
        case PayPalPayment.AUTHORIZING | PayPalPayment.AUTHORIZATION_UNKNOWN:
            return "The payment's outcome is not known yet; the shopper must retry the payment first."
        case PayPalPayment.VOIDED | PayPalPayment.CANCELLED | PayPalPayment.VOIDING | PayPalPayment.VOID_UNKNOWN:
            return "The order was cancelled; nothing can be captured."
        case PayPalPayment.CAPTURE_FAILED:
            return "PayPal declined the capture (%s). Contact the shopper." % payment.last_error
        case _:
            return "The payment is in state %s and cannot be captured." % payment.state


def _expired(payment: PayPalPayment, reason: str) -> Conflict:
    """Mark the hold unusable and build the operator-facing explanation."""
    number = payment.order.number
    _move(payment, PayPalPayment.AUTHORIZATION_EXPIRED, last_error=reason)
    logger.warning("Order %s: %s", number, reason)
    return Conflict(
        "The payment hold for order %s can no longer be captured or renewed: %s. Ask the shopper to pay "
        "again (POST /api/orders/%s/pay), or cancel the order." % (number, reason, number),
        code="authorization_expired",
    )


def _capture(payment: PayPalPayment) -> PayPalPayment:
    auth = gateway.call(
        "authorization lookup",
        lambda: gateway.get_client().payments.get_authorized_payment(payment.authorization_id))
    status = _set(auth.status)
    payment.authorization_status = str(status or "")
    payment.authorization_created_at = (
        parse_provider_time(_set(auth.create_time) or "") or payment.authorization_created_at)
    payment.authorization_expires_at = (
        parse_provider_time(_set(auth.expiration_time) or "") or payment.authorization_expires_at)
    payment.save(update_fields=["authorization_status", "authorization_created_at", "authorization_expires_at"])

    now = timezone.now()
    match status:
        case AuthorizationStatus.CREATED | AuthorizationStatus.PARTIALLY_CAPTURED | AuthorizationStatus.CAPTURED:
            pass  # capturable, or captured by an earlier attempt (the same request id replays it)
        case AuthorizationStatus.PENDING:
            _move(payment, PayPalPayment.AUTHORIZATION_PENDING)
            raise Conflict("PayPal is still reviewing the authorization; fulfil again once it is approved.",
                           code="authorization_pending")
        case AuthorizationStatus.DENIED:
            raise _expired(payment, "PayPal denied the authorization")
        case AuthorizationStatus.VOIDED:
            expires = payment.authorization_expires_at
            raise _expired(payment, "the authorization expired on %s" % expires.isoformat()
                           if expires and expires <= now else "the authorization was voided at PayPal")
        case _:
            _move(payment, PayPalPayment.NEEDS_REVIEW, last_error="authorization status %s" % status)
            raise Conflict("PayPal reports authorization status %s; an operator must review it." % status,
                           code="needs_review")

    if status == AuthorizationStatus.CREATED:
        expires = payment.authorization_expires_at
        if expires and expires <= now:
            raise _expired(payment, "the authorization expired on %s (PayPal's 29-day limit)" % expires.isoformat())
        honor_ends = (payment.authorization_created_at or now) + HONOR_PERIOD
        if now >= honor_ends and not payment.reauthorized:
            _reauthorize(payment, required=False)

    try:
        captured = _send_capture(payment)
    except ProviderError as e:
        if not e.rejected or e.issue not in _AUTHORIZATION_GONE_ISSUES:
            raise
        if e.issue == "AUTHORIZATION_EXPIRED" and not payment.reauthorized:
            # A stale hold: renew it once, then capture on the renewed one
            _reauthorize(payment, required=True)
            captured = _send_capture(payment)
        else:
            raise _expired(payment, "PayPal refused the capture (%s)" % e.issue) from e
    return _settle_capture(payment, captured)


def _reauthorize(payment: PayPalPayment, *, required: bool) -> None:
    """
    Renew a hold past its honor period. With ``required`` False a refusal is
    logged and the capture is still attempted on the original authorization;
    with ``required`` True a refusal ends the fulfilment with an operator message.
    """
    old_id = payment.authorization_id
    body = ReauthorizeRequest(
        amount=Money(currency_code=payment.currency, value=to_wire(payment.amount, payment.currency)))
    try:
        renewed = gateway.call(
            "reauthorization",
            lambda: gateway.get_client().payments.reauthorize_payment(
                old_id, pay_pal_request_id=_request_id(payment, "reauth", old_id),
                prefer="return=representation", body=body),
        )
    except ProviderError as e:
        if not e.rejected:
            raise
        logger.warning("Order %s: reauthorization of %s refused (%s)", payment.order.number, old_id, e.issue)
        if required:
            raise _expired(payment, "the authorization expired and PayPal refused to renew it (%s)"
                           % (e.issue or e.provider_status)) from e
        return
    new_id = _set(renewed.id)
    new_status = _set(renewed.status)
    if not new_id or new_status != AuthorizationStatus.CREATED:
        if required:
            raise _expired(payment, "PayPal's renewed authorization is %s" % (new_status or "missing"))
        return
    created_at = parse_provider_time(_set(renewed.create_time) or "")
    echoed = _money(renewed.amount) or (payment.amount, payment.currency)
    _record_transaction(payment, PayPalTransaction.AUTHORIZATION, new_id, echoed[0], echoed[1],
                        str(new_status), created_at)
    payment.authorization_id = new_id
    payment.authorization_status = str(new_status)
    payment.authorization_created_at = created_at
    payment.authorization_expires_at = (
        parse_provider_time(_set(renewed.expiration_time) or "") or payment.authorization_expires_at)
    payment.reauthorized = True
    payment.save(update_fields=["authorization_id", "authorization_status", "authorization_created_at",
                                "authorization_expires_at", "reauthorized"])
    Source.objects.filter(order=payment.order, source_type__name=SOURCE_TYPE_NAME).update(reference=new_id)
    logger.info("Order %s: authorization %s renewed as %s", payment.order.number, old_id, new_id)


def _send_capture(payment: PayPalPayment) -> CapturedPayment:
    auth_id = payment.authorization_id
    body = CaptureRequest(
        amount=Money(currency_code=payment.currency, value=to_wire(payment.amount, payment.currency)),
        final_capture=True,
    )
    return gateway.call(
        "capture",
        lambda: gateway.get_client().payments.capture_authorized_payment(
            auth_id, pay_pal_request_id=_request_id(payment, "capture", auth_id),
            prefer="return=representation", body=body),
    )


def _settle_capture(payment: PayPalPayment, captured: CapturedPayment) -> PayPalPayment:
    order = payment.order
    capture_id = _set(captured.id)
    if not capture_id:
        _move(payment, PayPalPayment.CAPTURE_UNKNOWN, last_error="PayPal returned no capture id.")
        raise ProviderError(502, "PayPal's capture answer carried no id; retrying the fulfilment is safe.",
                            outcome_unknown=True)
    status = _set(captured.status)
    echoed = _money(captured.amount)
    fee = net = None
    breakdown = _set(captured.seller_receivable_breakdown)
    if breakdown is not None:
        fee = _money(breakdown.paypal_fee)
        net = _money(breakdown.net_amount)
    captured_at = parse_provider_time(_set(captured.create_time) or "")
    fields: dict[str, Any] = {
        "capture_id": capture_id,
        "capture_status": str(status or ""),
        "captured_amount": echoed[0] if echoed else None,
        "paypal_fee": fee[0] if fee else None,
        "net_amount": net[0] if net else None,
        "captured_at": captured_at,
        "last_error": "",
    }
    if echoed:
        _record_transaction(payment, PayPalTransaction.CAPTURE, capture_id, echoed[0], echoed[1],
                            str(status or ""), captured_at)

    match status:
        case CaptureStatus.COMPLETED | CaptureStatus.PARTIALLY_REFUNDED | CaptureStatus.REFUNDED:
            outcome = PayPalPayment.CAPTURED  # refunds are tracked by this app's own refund rows
        case CaptureStatus.PENDING:
            outcome = PayPalPayment.CAPTURE_PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            outcome = PayPalPayment.CAPTURE_FAILED
        case _:
            outcome = PayPalPayment.NEEDS_REVIEW

    expected = (quantize(payment.amount, payment.currency), payment.currency)
    if echoed is None or (quantize(echoed[0], echoed[1]), echoed[1]) != expected:
        fields["last_error"] = "PayPal captured %s instead of %s %s." % (
            "%s %s" % echoed if echoed else "an unknown amount", *expected)
        outcome = PayPalPayment.NEEDS_REVIEW

    with transaction.atomic():
        _move(payment, outcome, **fields)
        if outcome == PayPalPayment.CAPTURED:
            source = _source_for(payment)
            if source is not None and not source.amount_debited:
                source.debit(fields["captured_amount"], reference=capture_id, status=str(status))
            _set_order_status(order, STATUS_COMPLETE)
    logger.info("Order %s: capture %s is %s (fee %s, net %s)", order.number, capture_id, status,
                fields["paypal_fee"], fields["net_amount"])
    if outcome == PayPalPayment.CAPTURE_FAILED:
        raise Conflict("PayPal declined the capture (status %s). Contact the shopper." % status,
                       code="capture_declined")
    if outcome == PayPalPayment.NEEDS_REVIEW:
        raise Conflict("The capture needs review: %s" % (fields["last_error"] or "status %s" % status),
                       code="needs_review")
    if outcome == PayPalPayment.CAPTURE_PENDING:
        raise InProgress("PayPal accepted the capture but it is still pending; fulfil again later to refresh it.",
                         code="capture_pending")
    return payment


def _refresh_pending_capture(payment: PayPalPayment) -> PayPalPayment:
    captured = gateway.call(
        "capture lookup", lambda: gateway.get_client().payments.get_captured_payment(payment.capture_id))
    return _settle_capture(payment, captured)


# ---------------------------------------------------------------------------
# Cancel (release the hold)
# ---------------------------------------------------------------------------


def cancel(order: Any) -> PayPalPayment:
    """Operator cancels before fulfilment: release any hold and cancel the Oscar order."""
    payment: PayPalPayment = order.paypal_payment
    if payment.state in (PayPalPayment.VOIDED, PayPalPayment.CANCELLED):
        return payment

    # Nothing is held at PayPal: cancel locally
    if _move(payment, PayPalPayment.CANCELLED, from_states=[
            PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZATION_FAILED,
            PayPalPayment.AUTHORIZATION_EXPIRED]):
        with transaction.atomic():
            _set_order_status(order, STATUS_CANCELLED)
        return payment

    now = timezone.now()
    base = PayPalPayment.objects.filter(pk=payment.pk)
    prior = None
    for state, condition in (
        (PayPalPayment.AUTHORIZED, Q(state=PayPalPayment.AUTHORIZED)),
        (PayPalPayment.AUTHORIZATION_PENDING, Q(state=PayPalPayment.AUTHORIZATION_PENDING)),
        (PayPalPayment.VOID_UNKNOWN, Q(state=PayPalPayment.VOID_UNKNOWN)
         | Q(state=PayPalPayment.VOIDING, state_changed_at__lt=now - SEND_WINDOW)),
    ):
        if base.filter(condition).update(state=PayPalPayment.VOIDING, state_changed_at=now):
            prior = state
            break
    payment.refresh_from_db()
    if prior is None:
        match payment.state:
            case PayPalPayment.VOIDING:
                raise InProgress("this order is already being cancelled")
            case PayPalPayment.VOIDED | PayPalPayment.CANCELLED:
                return payment
            case PayPalPayment.AUTHORIZING | PayPalPayment.AUTHORIZATION_UNKNOWN:
                raise Conflict("The payment's outcome is not known yet, so a hold may exist; the shopper must "
                               "retry the payment before the order can be cancelled.", code="payment_unknown")
            case PayPalPayment.NEEDS_REVIEW:
                raise Conflict("The payment needs review (%s); resolve it at PayPal first." % payment.last_error,
                               code="needs_review")
            case _:
                raise Conflict("The order has already been fulfilled (payment %s); use refunds instead."
                               % payment.state, code="already_captured")

    auth_id = payment.authorization_id
    try:
        voided = gateway.call(
            "release of the hold",
            lambda: gateway.get_client().payments.void_payment(
                auth_id, pay_pal_request_id=_request_id(payment, "void", auth_id),
                prefer="return=representation"),
        )
        status = _set(voided.status)
    except ProviderError as e:
        if e.rejected and e.issue in ("AUTHORIZATION_VOIDED", "PREVIOUSLY_VOIDED", "AUTHORIZATION_EXPIRED"):
            status = AuthorizationStatus.VOIDED  # the hold is already gone
        elif e.outcome_unknown:
            _move(payment, PayPalPayment.VOID_UNKNOWN, last_error=e.message)
            e.message += " Retrying the cancellation is safe."
            raise
        else:
            _move(payment, prior, last_error=e.message)
            raise

    if status != AuthorizationStatus.VOIDED:
        _move(payment, PayPalPayment.NEEDS_REVIEW, last_error="release answered status %s" % status)
        raise Conflict("PayPal answered the release with status %s; an operator must review it." % status,
                       code="needs_review")
    with transaction.atomic():
        _move(payment, PayPalPayment.VOIDED, authorization_status=str(status), last_error="")
        source = _source_for(payment)
        if source is not None and source.amount_allocated:
            Transaction.objects.create(source=source, txn_type="Void", amount=source.amount_allocated,
                                       reference=auth_id, status=str(status))
            source.amount_allocated = Decimal("0")
            source.save(update_fields=["amount_allocated"])
        _set_order_status(order, STATUS_CANCELLED)
    logger.info("Order %s: hold %s released", order.number, auth_id)
    return payment


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

REFUNDABLE = [PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED]


def refund(order: Any, user: Any, raw_amount: Any, idempotency_key: Any) -> tuple[PayPalRefund, bool]:
    """
    Refund all or part of the captured payment. Returns (refund, created).

    The same idempotency key never refunds twice; distinct keys may refund the
    same capture repeatedly until the captured amount is exhausted.
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 255:
        raise InvalidRequest("an idempotency key (Idempotency-Key header or idempotencyKey, 1-255 characters) "
                             "is required", code="idempotency_key_required")
    key = idempotency_key.strip()
    payment: PayPalPayment = order.paypal_payment
    try:
        amount = parse_amount(raw_amount, payment.currency) if raw_amount not in (None, "") else None
    except ValueError as e:
        raise InvalidRequest(str(e), code="invalid_amount") from None

    existing = PayPalRefund.objects.filter(payment=payment, idempotency_key=key).first()
    if existing is not None:
        return _replay_refund(payment, existing, amount), False

    if payment.state not in REFUNDABLE or not payment.capture_id or payment.captured_amount is None:
        raise Conflict("Only a fulfilled (captured) order can be refunded; this payment is %s." % payment.state,
                       code="not_refundable")
    if amount is None:
        amount = payment.captured_amount - payment.refund_reserved
        if amount <= 0:
            raise Conflict("The captured amount has already been refunded in full.", code="refund_exceeds_capture")

    row: PayPalRefund | None
    try:
        with transaction.atomic():
            row = PayPalRefund.objects.create(
                payment=payment, idempotency_key=key, amount=amount, currency=payment.currency,
                status=PayPalRefund.SENDING, status_changed_at=timezone.now(), requested_by=user,
            )
            if not _reserve(payment, amount):
                transaction.set_rollback(True)
                row = None
    except IntegrityError:
        # A concurrent request with the same key won; answer from its row
        existing = PayPalRefund.objects.get(payment=payment, idempotency_key=key)
        return _replay_refund(payment, existing, amount), False
    if row is None:
        payment.refresh_from_db()
        raise Conflict("A refund of %s %s exceeds the refundable balance of %s %s." % (
            amount, payment.currency,
            quantize((payment.captured_amount or Decimal(0)) - payment.refund_reserved, payment.currency),
            payment.currency), code="refund_exceeds_capture")
    return _send_refund(payment, row), True


def _reserve(payment: PayPalPayment, amount: Decimal) -> bool:
    """Hold ``amount`` against the refundable balance, atomically; False if it does not fit."""
    return PayPalPayment.objects.filter(
        pk=payment.pk, state__in=REFUNDABLE, captured_amount__gte=F("refund_reserved") + amount,
    ).update(refund_reserved=F("refund_reserved") + amount) == 1


def _replay_refund(payment: PayPalPayment, row: PayPalRefund, amount: Decimal | None) -> PayPalRefund:
    if amount is not None and quantize(amount, row.currency) != quantize(row.amount, row.currency):
        raise Conflict("This idempotency key was already used for a refund of %s %s." % (
            quantize(row.amount, row.currency), row.currency), code="idempotency_key_reused")
    if row.status == PayPalRefund.PENDING and row.paypal_refund_id:
        return _refresh_pending_refund(payment, row)
    now = timezone.now()
    base = PayPalRefund.objects.filter(pk=row.pk)
    # Resume an unknown or abandoned send under the same PayPal-Request-Id
    if base.filter(Q(status=PayPalRefund.UNKNOWN)
                   | Q(status=PayPalRefund.SENDING, status_changed_at__lt=now - SEND_WINDOW)).update(
            status=PayPalRefund.SENDING, status_changed_at=now):
        row.refresh_from_db()
        return _send_refund(payment, row)
    # Retry a definitively failed refund: re-reserve, new attempt
    if row.status == PayPalRefund.FAILED:
        with transaction.atomic():
            claimed = base.filter(status=PayPalRefund.FAILED).update(
                status=PayPalRefund.SENDING, status_changed_at=now, attempt=F("attempt") + 1) == 1
            if claimed and not _reserve(payment, row.amount):
                transaction.set_rollback(True)
                raise Conflict("A refund of %s %s exceeds the refundable balance." % (row.amount, row.currency),
                               code="refund_exceeds_capture")
        row.refresh_from_db()
        if claimed:
            return _send_refund(payment, row)
    if row.status == PayPalRefund.SENDING:
        raise InProgress("a refund with this idempotency key is already being processed")
    return row


def _send_refund(payment: PayPalPayment, row: PayPalRefund) -> PayPalRefund:
    key_digest = hashlib.sha256(row.idempotency_key.encode()).hexdigest()[:24]
    body = RefundRequest(
        amount=Money(currency_code=row.currency, value=to_wire(row.amount, row.currency)),
        custom_id=str(row.public_id),
    )
    try:
        result = gateway.call(
            "refund",
            lambda: gateway.get_client().payments.refund_captured_payment(
                payment.capture_id,
                pay_pal_request_id=_request_id(payment, "refund", key_digest, str(row.attempt)),
                prefer="return=representation", body=body),
        )
    except ProviderError as e:
        if e.outcome_unknown:
            _refund_move(row, PayPalRefund.UNKNOWN, last_error=e.message)
            e.message += " Repeating the request with the same idempotency key is safe."
        else:
            _fail_refund(payment, row, e.message)
        raise
    return _settle_refund(payment, row, result)


def _refund_move(row: PayPalRefund, status: str, **fields: Any) -> None:
    now = timezone.now()
    PayPalRefund.objects.filter(pk=row.pk).update(status=status, status_changed_at=now, **fields)
    row.status, row.status_changed_at = status, now
    for name, value in fields.items():
        setattr(row, name, value)


def _fail_refund(payment: PayPalPayment, row: PayPalRefund, message: str) -> None:
    """Mark the refund failed and give its amount back to the refundable balance (once)."""
    with transaction.atomic():
        if PayPalRefund.objects.filter(pk=row.pk).exclude(status=PayPalRefund.FAILED).update(
                status=PayPalRefund.FAILED, status_changed_at=timezone.now(), last_error=message):
            PayPalPayment.objects.filter(pk=payment.pk).update(refund_reserved=F("refund_reserved") - row.amount)
    row.status, row.last_error = PayPalRefund.FAILED, message


def _settle_refund(payment: PayPalPayment, row: PayPalRefund, result: Refund) -> PayPalRefund:
    refund_id = _set(result.id)
    if not refund_id:
        _refund_move(row, PayPalRefund.UNKNOWN, last_error="PayPal returned no refund id")
        raise ProviderError(502, "PayPal's refund answer carried no id; repeat the request with the same "
                                 "idempotency key.", outcome_unknown=True)
    status = _set(result.status)
    echoed = _money(result.amount)
    provider_time = parse_provider_time(_set(result.create_time) or "")
    fields = {"paypal_refund_id": refund_id, "paypal_status": str(status or ""), "provider_time": provider_time}
    if echoed:
        _record_transaction(payment, PayPalTransaction.REFUND, refund_id, echoed[0], echoed[1],
                            str(status or ""), provider_time)
    if echoed is None or (quantize(echoed[0], echoed[1]), echoed[1]) != (
            quantize(row.amount, row.currency), row.currency):
        message = "PayPal refunded %s instead of %s %s" % (
            "%s %s" % echoed if echoed else "an unknown amount", row.amount, row.currency)
        _refund_move(row, PayPalRefund.UNKNOWN, last_error=message, **fields)
        _move(payment, PayPalPayment.NEEDS_REVIEW, last_error=message)
        raise Conflict("The refund needs review: %s." % message, code="needs_review")

    match status:
        case RefundStatus.COMPLETED:
            with transaction.atomic():
                if PayPalRefund.objects.filter(pk=row.pk).exclude(status=PayPalRefund.COMPLETED).update(
                        status=PayPalRefund.COMPLETED, status_changed_at=timezone.now(), last_error="", **fields):
                    PayPalPayment.objects.filter(pk=payment.pk).update(
                        refunded_amount=F("refunded_amount") + row.amount)
                    # Evaluated against the row as it is now, so concurrent refunds settle correctly
                    PayPalPayment.objects.filter(pk=payment.pk, state__in=REFUNDABLE).update(
                        state=Case(When(refunded_amount__gte=F("captured_amount"),
                                        then=Value(PayPalPayment.REFUNDED)),
                                   default=Value(PayPalPayment.PARTIALLY_REFUNDED)),
                        state_changed_at=timezone.now())
                    source = _source_for(payment)
                    if source is not None:
                        Source.objects.filter(pk=source.pk).update(amount_refunded=F("amount_refunded") + row.amount)
                        Transaction.objects.create(source=source, txn_type=Transaction.REFUND, amount=row.amount,
                                                   reference=refund_id, status=str(status))
            row.refresh_from_db()
        case RefundStatus.PENDING:
            _refund_move(row, PayPalRefund.PENDING, **fields)
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            PayPalRefund.objects.filter(pk=row.pk).update(**fields)
            _fail_refund(payment, row, "PayPal reports the refund as %s" % status)
            raise Conflict("PayPal reports the refund as %s." % status, code="refund_failed")
        case _:
            _refund_move(row, PayPalRefund.UNKNOWN, last_error="refund status %s" % status, **fields)
    logger.info("Order %s: refund %s of %s %s is %s", payment.order.number, refund_id,
                to_wire(row.amount, row.currency),
                row.currency, status)
    return row


def _refresh_pending_refund(payment: PayPalPayment, row: PayPalRefund) -> PayPalRefund:
    result = gateway.call("refund lookup", lambda: gateway.get_client().payments.get_refund(row.paypal_refund_id))
    return _settle_refund(payment, row, result)


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------


# Card details must never appear in an error report's frame locals
@sensitive_variables()
def save_card(user: Any, payload: dict[str, Any], request_key: str) -> tuple[SavedCard, bool]:
    """Vault a card at PayPal for ``user``. Returns (card, created)."""
    card = parse_card(payload.get("card"))
    key = (request_key or uuid.uuid4().hex).strip()[:100]
    now = timezone.now()
    try:
        with transaction.atomic():
            row = SavedCard.objects.create(user=user, request_key=key, status=SavedCard.SAVING,
                                           last_digits=card.last_digits, cardholder_name=card.name)
    except IntegrityError:
        row = SavedCard.objects.get(user=user, request_key=key)
        if row.status == SavedCard.ACTIVE:
            return row, False
        if row.status in (SavedCard.DELETING, SavedCard.DELETED):
            raise Conflict("this Idempotency-Key belongs to a card that was removed", code="idempotency_key_reused")
        claimed = SavedCard.objects.filter(pk=row.pk).filter(
            Q(status__in=[SavedCard.FAILED, SavedCard.UNKNOWN])
            | Q(status=SavedCard.SAVING, date_updated__lt=now - SEND_WINDOW)
        ).update(status=SavedCard.SAVING, date_updated=now)
        if not claimed:
            raise InProgress("this card is already being saved")
        row.refresh_from_db()

    customer = PayPalCustomer.objects.filter(user=user).first()
    token_card: dict[str, Any] = {"number": card.number, "expiry": card.expiry, "security_code": card.security_code}
    if card.name:
        token_card["name"] = card.name
    address = card.paypal_address()
    if address is not None:
        token_card["billing_address"] = address
    body = PaymentTokenRequest(
        customer=Customer(id=customer.paypal_customer_id) if customer is not None
        else Customer(merchant_customer_id="oscar-user-%s" % user.pk),
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(**token_card)),
    )
    try:
        token = gateway.call(
            "card save",
            lambda: gateway.get_client().vault.create_payment_token(
                body, pay_pal_request_id="card-%s" % row.public_id.hex),
        )
    except ProviderError as e:
        SavedCard.objects.filter(pk=row.pk).update(
            status=SavedCard.UNKNOWN if e.outcome_unknown else SavedCard.FAILED, last_error=e.message)
        if e.outcome_unknown:
            e.message += " Repeating the request with the same Idempotency-Key is safe."
        raise

    token_id = _set(token.id)
    token_customer = _set(token.customer)
    customer_id = _set(token_customer.id) if token_customer is not None else None
    if not token_id:
        SavedCard.objects.filter(pk=row.pk).update(status=SavedCard.UNKNOWN, last_error="no token id")
        raise ProviderError(502, "PayPal's answer carried no token id; repeat with the same Idempotency-Key.",
                            outcome_unknown=True)
    if customer_id and customer is None:
        PayPalCustomer.objects.get_or_create(user=user, defaults={"paypal_customer_id": customer_id})
    source = _set(token.payment_source)
    entity = _set(source.card) if source is not None else None
    row.vault_token_id = token_id
    row.status = SavedCard.ACTIVE
    row.last_error = ""
    row.expiry = card.expiry
    if entity is not None:
        row.brand = str(_set(entity.brand) or "")
        row.last_digits = str(_set(entity.last_digits) or card.last_digits)
        row.expiry = str(_set(entity.expiry) or card.expiry)
        row.cardholder_name = str(_set(entity.name) or card.name)
        if _set(entity.verification_status) == CardVerificationStatus.FAILED:
            row.status = SavedCard.FAILED
            row.last_error = "PayPal could not verify the card"
    row.save()
    if row.status != SavedCard.ACTIVE:
        raise Conflict("PayPal could not verify the card; it was not saved.", code="card_not_verified")
    logger.info("User %s saved card %s (%s ending %s)", user.pk, row.public_id, row.brand, row.last_digits)
    return row, True


def list_cards(user: Any) -> list[SavedCard]:
    return list(SavedCard.objects.filter(user=user, status=SavedCard.ACTIVE))


def delete_card(user: Any, payment_method_id: str) -> None:
    try:
        public_id = uuid.UUID(str(payment_method_id))
    except ValueError:
        raise NotFound("saved card not found", code="payment_method_not_found") from None
    row = SavedCard.objects.filter(public_id=public_id, user=user,
                                   status__in=[SavedCard.ACTIVE, SavedCard.DELETING]).first()
    if row is None:
        raise NotFound("saved card not found", code="payment_method_not_found")
    # Hidden and unusable from here on, whatever PayPal answers
    SavedCard.objects.filter(pk=row.pk).update(status=SavedCard.DELETING)
    result = gateway.call(
        "card removal",
        lambda: gateway.get_client().vault.with_raw_response.delete_payment_token(row.vault_token_id))
    status = result.response.status_code
    if isinstance(result, Success) or status == 404:
        SavedCard.objects.filter(pk=row.pk).update(status=SavedCard.DELETED, last_error="")
        logger.info("User %s removed card %s", user.pk, row.public_id)
        return
    SavedCard.objects.filter(pk=row.pk).update(last_error="PayPal answered HTTP %s" % status)
    raise ProviderError(
        503 if status == 429 else 502,
        "The card can no longer be used here, but PayPal did not confirm removing it (HTTP %s); "
        "repeat the request to retry." % status,
        outcome_unknown=False, provider_status=status)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def parse_range_bound(raw: str | None, name: str) -> datetime:
    if not raw:
        raise InvalidRequest("'%s' is required (ISO-8601 date-time)" % name)
    text = raw.strip().replace(" ", "+")  # an unencoded '+' in a query string arrives as a space
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidRequest("'%s' must be an ISO-8601 date-time" % name) from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


def _fetch_provider_transactions(start: datetime, end: datetime) -> tuple[
        dict[str, TransactionInformation], datetime | None, bool]:
    """
    Every PayPal transaction initiated in [start, end): 31-day chunks, every
    page of each. Page 1 of a chunk tells how many pages it has; the rest are
    fetched concurrently (bounded). A page cap bounds the walk and is reported
    in the result, never hidden.
    """
    client = gateway.get_client()
    query_end = min(end, timezone.now())
    chunks: list[tuple[datetime, datetime]] = []
    chunk_start = start
    while chunk_start < query_end:
        chunk_end = min(chunk_start + SEARCH_CHUNK, query_end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end

    def fetch(chunk: tuple[datetime, datetime], page: int) -> Any:
        return gateway.call(
            "transaction search",
            lambda: client.transaction_search.search_transactions(
                to_provider_time(chunk[0]), to_provider_time(chunk[1]),
                balance_affecting_records_only="N", page_size=SEARCH_PAGE_SIZE, page=page),
        )

    pages: list[Any] = []
    truncated = False
    with ThreadPoolExecutor(max_workers=SEARCH_CONCURRENCY) as pool:
        firsts = list(pool.map(lambda chunk: fetch(chunk, 1), chunks))
        rest: list[Future[Any]] = []
        for chunk, first in zip(chunks, firsts):
            pages.append(first)
            total_pages = _set(first.total_pages) or 1
            if total_pages > SEARCH_MAX_PAGES:
                truncated = True
                logger.warning("Reconciliation of %s..%s has %d pages; reading the first %d",
                               chunk[0], chunk[1], total_pages, SEARCH_MAX_PAGES)
            rest.extend(pool.submit(fetch, chunk, page)
                        for page in range(2, min(total_pages, SEARCH_MAX_PAGES) + 1))
        pages.extend(future.result() for future in rest)

    found: dict[str, TransactionInformation] = {}
    refreshed_at: datetime | None = None
    for result in pages:
        refreshed = parse_provider_time(_set(result.last_refreshed_datetime) or "")
        if refreshed and (refreshed_at is None or refreshed < refreshed_at):
            refreshed_at = refreshed
        for detail in _set(result.transaction_details) or []:
            info = _set(detail.transaction_info)
            txn_id = _set(info.transaction_id) if info is not None else None
            if not txn_id:
                continue
            # The query is second-granular; narrow back to the caller's instants
            initiated = parse_provider_time(_set(info.transaction_initiation_date) or "")
            if initiated is None or not start <= initiated < end:
                continue
            # One id can be listed more than once (e.g. pending, then settled): keep the newest
            seen = found.get(txn_id)
            if seen is None or (_set(info.transaction_updated_date) or "") >= (
                    _set(seen.transaction_updated_date) or ""):
                found[txn_id] = info
    return found, refreshed_at, truncated


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    """
    Line PayPal's own record of transactions in [start, end) up against this app's.

    Both sides are compared on PayPal's event time. One order legitimately owns
    several PayPal transactions (authorization, capture, refunds), so matching is
    by PayPal transaction id, never "first hit per order".
    """
    if end <= start:
        raise InvalidRequest("'to' must be after 'from'")
    if end - start > SEARCH_MAX_SPAN:
        raise InvalidRequest("the range may span at most three years")
    provider, refreshed_at, truncated = _fetch_provider_transactions(start, end)

    known = {
        t.paypal_id: t
        for t in PayPalTransaction.objects.filter(paypal_id__in=list(provider)).select_related("payment__order")
    }
    matched: list[dict[str, Any]] = []
    paypal_only: list[dict[str, Any]] = []
    for txn_id, info in sorted(provider.items(), key=lambda kv: _set(kv[1].transaction_initiation_date) or ""):
        entry = _provider_entry(info)
        local = known.get(txn_id)
        if local is None:
            paypal_only.append(entry)
            continue
        amount_matches = entry["amount"] is not None and abs(Decimal(entry["amount"])) == abs(local.amount)
        matched.append({**entry, "orderId": local.payment.order.number, "kind": local.kind,
                        "appAmount": str(local.amount), "appStatus": local.status,
                        "amountMatches": amount_matches})

    app_only: list[dict[str, Any]] = []
    not_yet_reported: list[dict[str, Any]] = []
    for t in PayPalTransaction.objects.filter(provider_time__gte=start, provider_time__lt=end).select_related(
            "payment__order"):
        if t.paypal_id in provider:
            continue
        entry = {"paypalId": t.paypal_id, "orderId": t.payment.order.number, "kind": t.kind,
                 "amount": str(t.amount), "currency": t.currency, "status": t.status,
                 "time": t.provider_time.isoformat() if t.provider_time else None}
        # PayPal's reporting lags; newer than its last refresh is not (yet) a discrepancy
        if refreshed_at is None or (t.provider_time is not None and t.provider_time > refreshed_at):
            not_yet_reported.append(entry)
        else:
            app_only.append(entry)

    # Writes whose PayPal outcome this app does not know: never folded into either side
    unsettled = [
        {"orderId": p.order.number, "state": p.state, "since": p.state_changed_at.isoformat()}
        for p in PayPalPayment.objects.select_related("order").filter(
            state__in=[PayPalPayment.AUTHORIZING, PayPalPayment.AUTHORIZATION_UNKNOWN, PayPalPayment.CAPTURING,
                       PayPalPayment.CAPTURE_UNKNOWN, PayPalPayment.VOIDING, PayPalPayment.VOID_UNKNOWN,
                       PayPalPayment.NEEDS_REVIEW],
            state_changed_at__gte=start, state_changed_at__lt=end)
    ] + [
        {"orderId": r.payment.order.number, "refundId": str(r.public_id), "state": "refund_" + r.status,
         "since": r.status_changed_at.isoformat()}
        for r in PayPalRefund.objects.select_related("payment__order").filter(
            status__in=[PayPalRefund.SENDING, PayPalRefund.UNKNOWN],
            status_changed_at__gte=start, status_changed_at__lt=end)
    ]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalDataRefreshedAt": refreshed_at.isoformat() if refreshed_at else None,
        "truncated": truncated,
        "summary": {
            "paypalTransactions": len(provider),
            "matched": len(matched),
            "amountMismatches": sum(1 for m in matched if not m["amountMatches"]),
            "paypalOnly": len(paypal_only),
            "appOnly": len(app_only),
            "notYetReportedByPayPal": len(not_yet_reported),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
        "notYetReportedByPayPal": not_yet_reported,
        "unsettled": unsettled,
    }


def _provider_entry(info: TransactionInformation) -> dict[str, Any]:
    amount = _money(info.transaction_amount)
    fee = _money(info.fee_amount)
    return {
        "paypalId": _set(info.transaction_id),
        "referenceId": _set(info.paypal_reference_id),
        "eventCode": _set(info.transaction_event_code),
        "status": _set(info.transaction_status),
        "time": _set(info.transaction_initiation_date),
        "amount": str(amount[0]) if amount else None,
        "currency": amount[1] if amount else None,
        "fee": str(fee[0]) if fee else None,
        "invoiceId": _set(info.invoice_id),
    }
