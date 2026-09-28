"""
Orders, payments and saved cards: the domain operations behind the API.

Every PayPal write goes through ``safe_write`` with a reference derived from
the order (or user) and the step, so a double click, a retried request or two
workers racing never authorize, capture, void, refund or vault twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.apps.order.exceptions import InvalidOrderStatus
from oscar.core import prices
from oscar.core.loading import get_class, get_model
from paypal.core import UNSET, ApiResult, Failure, Success, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CaptureRequest,
    CapturedPayment,
    CardRequest,
    CardStoredCredential,
    Customer,
    Money,
    Order as PayPalOrder,
    PaymentAuthorization,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PaymentTokenResponse,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    Refund,
    RefundRequest,
)
from paypal.models import OrderRequest as PayPalOrderRequest
from paypal.models.enums import (
    AuthorizationStatus,
    CaptureStatus,
    CheckoutPaymentIntent,
    OrderStatus,
    PaymentInitiator,
    RefundStatus,
    StoredPaymentSourcePaymentType,
    StoredPaymentSourceUsageType,
)

from .gateway import PaymentError, call_read, get_client, get_config, money_str, quantize
from .models import PayPalCustomer, PayPalPayment, PayPalRefund, ProviderWrite, SavedCard
from .safe_write import (
    DONE,
    FAILED,
    NEEDS_REVIEW,
    PENDING,
    SENDING,
    UNKNOWN,
    Answer,
    WriteResult,
    deterministic_ref,
    install_prefix,
    key_hash,
    provider_time,
    safe_write,
)

logger = logging.getLogger("apps.paypal_payments")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Order = get_model("order", "Order")
ShippingAddress = get_model("order", "ShippingAddress")
Country = get_model("address", "Country")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
Selector = get_class("partner.strategy", "Selector")
Applicator = get_class("offer.applicator", "Applicator")
Repository = get_class("shipping.repository", "Repository")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")

# Oscar order statuses used by this flow (sandbox OSCAR_ORDER_STATUS_PIPELINE).
STATUS_PAID = "Being processed"
STATUS_FULFILLED = "Complete"
STATUS_CANCELLED = "Cancelled"

# PayPal honours an authorization for three days, after which it has to be
# reauthorized (payments.reauthorize_payment docstring).
HONOR_PERIOD = timedelta(days=3)

MAX_LINES = 50
MAX_QUANTITY = 100


# ---------------------------------------------------------------------------
# Status mappers: PayPal's status -> done / pending / failed / needs_review / unknown
# ---------------------------------------------------------------------------


def authorize_outcome(status: tuple[object, object]) -> str:
    """The authorize step (create_order, intent AUTHORIZE): the authorization's
    status decides; the order's only when PayPal made no authorization."""
    authorization_status, order_status = status
    if authorization_status is not None:
        match authorization_status:
            case AuthorizationStatus.CREATED:
                return DONE  # the hold is in place
            case AuthorizationStatus.PENDING:
                return PENDING
            case AuthorizationStatus.DENIED | AuthorizationStatus.VOIDED:
                return FAILED
            case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
                return NEEDS_REVIEW  # money taken where only a hold was asked for
            case _:
                return UNKNOWN
    match order_status:
        case OrderStatus.PAYER_ACTION_REQUIRED | OrderStatus.VOIDED:
            return FAILED
        case OrderStatus.CREATED | OrderStatus.SAVED | OrderStatus.APPROVED:
            return PENDING
        case _:
            return UNKNOWN


def reauthorize_outcome(status: object) -> str:
    match status:
        case AuthorizationStatus.CREATED:
            return DONE
        case AuthorizationStatus.PENDING:
            return PENDING
        case (
            AuthorizationStatus.DENIED
            | AuthorizationStatus.VOIDED
            | AuthorizationStatus.CAPTURED
            | AuthorizationStatus.PARTIALLY_CAPTURED
        ):
            return FAILED
        case _:
            return UNKNOWN


def capture_outcome(status: object) -> str:
    match status:
        case CaptureStatus.COMPLETED:
            return DONE
        case CaptureStatus.PENDING:
            return PENDING
        case CaptureStatus.DECLINED | CaptureStatus.FAILED:
            return FAILED
        case CaptureStatus.REFUNDED | CaptureStatus.PARTIALLY_REFUNDED:
            return FAILED  # taken, then (partly) given back: not in effect as asked
        case _:
            return UNKNOWN


def void_outcome(status: object) -> str:
    """The cancel step's own mapper: VOIDED is its done."""
    match status:
        case AuthorizationStatus.VOIDED:
            return DONE
        case AuthorizationStatus.CAPTURED | AuthorizationStatus.PARTIALLY_CAPTURED:
            return FAILED  # too late: the money was taken
        case _:
            return UNKNOWN


def refund_outcome(status: object) -> str:
    """The refund step's own mapper: COMPLETED is its done."""
    match status:
        case RefundStatus.COMPLETED:
            return DONE
        case RefundStatus.PENDING:
            return PENDING
        case RefundStatus.FAILED | RefundStatus.CANCELLED:
            return FAILED
        case _:
            return UNKNOWN


def vault_outcome(status: object) -> str:
    """create_payment_token returns no status: done only when PayPal returned
    both the token id and the card it vaulted."""
    return DONE if status is True else UNKNOWN


def delete_outcome(status: object) -> str:
    """delete_payment_token returns no body; its HTTP status is the outcome."""
    if not isinstance(status, int):
        return UNKNOWN
    if 200 <= status < 300 or status == 404:
        return DONE  # deleted now, or already gone
    if 400 <= status < 500:
        return FAILED
    return UNKNOWN


# ---------------------------------------------------------------------------
# Small readers over SDK models (UNSET never leaves this module)
# ---------------------------------------------------------------------------


def _s(value: object) -> str:
    return value if isinstance(value, str) else ""


def _label(value: object) -> str:
    return str(value) if isinstance(value, str) else ""


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _money(amount: Decimal, currency: str) -> Money:
    return Money(currency_code=currency, value=money_str(amount, currency))


def _first_authorization(order: PayPalOrder) -> Any:
    units = order.purchase_units
    if isinstance(units, UnsetType) or not units:
        return None
    payments = units[0].payments
    if isinstance(payments, UnsetType):
        return None
    auths = payments.authorizations
    if isinstance(auths, UnsetType) or not auths:
        return None
    return auths[0]


def _amount_of(obj: Any) -> tuple[object, object]:
    amount = getattr(obj, "amount", UNSET)
    if amount is None or isinstance(amount, UnsetType):
        return None, None
    return amount.value, amount.currency_code


# ---------------------------------------------------------------------------
# Placing orders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderItem:
    product_id: int
    quantity: int


def place_order(user: Any, items: list[OrderItem], shipping: dict[str, str] | None, request: Any) -> Any:
    """Build a basket from catalogue items and place an Oscar order from it,
    exactly as Oscar's checkout does, in a state awaiting payment."""
    config = get_config()
    if not items:
        raise PaymentError(400, "An order needs at least one item.", code="invalid_request")
    if len(items) > MAX_LINES:
        raise PaymentError(400, f"An order can have at most {MAX_LINES} lines.", code="invalid_request")

    with transaction.atomic():
        # A SAVED basket is editable but never picked up as the shopper's open
        # basket by Oscar's BasketMiddleware; it is submitted with the order.
        basket = Basket.objects.create(owner=user, status=Basket.SAVED)
        basket.strategy = Selector().strategy(request=request, user=user)
        for item in items:
            if item.quantity < 1 or item.quantity > MAX_QUANTITY:
                raise PaymentError(400, f"Quantity must be between 1 and {MAX_QUANTITY}.", code="invalid_request")
            product = Product.objects.filter(pk=item.product_id, is_public=True).first()
            if product is None:
                raise PaymentError(404, f"Catalogue item {item.product_id} does not exist.", code="unknown_item")
            if product.is_parent:
                raise PaymentError(
                    422, f"Catalogue item {item.product_id} has variants; order one of them.", code="not_purchasable"
                )
            info = basket.strategy.fetch_for_product(product)
            if not info.availability.is_available_to_buy:
                raise PaymentError(409, f"Catalogue item {item.product_id} is not available.", code="unavailable")
            existing = basket.line_quantity(product, info.stockrecord)
            allowed, message = info.availability.is_purchase_permitted(existing + item.quantity)
            if not allowed:
                raise PaymentError(409, f"Catalogue item {item.product_id}: {message}", code="unavailable")
            basket.add_product(product, item.quantity)
        basket.reset_offer_applications()
        Applicator().apply(basket, user, request)

        shipping_address = None
        if basket.is_shipping_required():
            if not shipping:
                raise PaymentError(400, "shippingAddress is required for these items.", code="invalid_request")
            shipping_address = _shipping_address(shipping)
            shipping_address.save()
        method = Repository().get_default_shipping_method(
            basket=basket, shipping_addr=shipping_address, user=user, request=request
        )
        shipping_charge = method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        if total.incl_tax is None:
            raise PaymentError(409, "The order total could not be calculated.", code="no_total")
        # Amounts come from the catalogue; the currency from configuration.
        total = prices.Price(
            currency=config.currency,
            excl_tax=quantize(total.excl_tax, config.currency),
            incl_tax=quantize(total.incl_tax, config.currency),
        )
        if total.incl_tax <= 0:
            raise PaymentError(409, "The order total must be greater than zero.", code="no_total")

        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=shipping_address,
            order_number=OrderNumberGenerator().order_number(basket),
            request=request,
        )
        basket.submit()
        PayPalPayment.objects.create(order=order, currency=config.currency, amount=total.incl_tax)
    return order


def _shipping_address(data: dict[str, str]) -> Any:
    code = (data.get("countryCode") or "").upper()
    country = Country.objects.filter(iso_3166_1_a2=code).first()
    if country is None:
        raise PaymentError(400, "shippingAddress.countryCode is not a known country.", code="invalid_request")
    if not country.is_shipping_country:
        raise PaymentError(422, "We do not ship to that country.", code="invalid_request")
    required = ("firstName", "lastName", "line1", "city", "postcode")
    missing = [f for f in required if not (data.get(f) or "").strip()]
    if missing:
        raise PaymentError(400, f"shippingAddress is missing {', '.join(missing)}.", code="invalid_request")
    return ShippingAddress(
        first_name=data["firstName"][:255],
        last_name=data["lastName"][:255],
        line1=data["line1"][:255],
        line2=(data.get("line2") or "")[:255],
        line4=data["city"][:255],
        state=(data.get("state") or "")[:255],
        postcode=data["postcode"][:64],
        country=country,
        phone_number=data.get("phoneNumber") or "",
    )


# ---------------------------------------------------------------------------
# Card input (validated here, forwarded to PayPal, never stored or logged)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class CardInput:
    number: str
    expiry: str  # YYYY-MM
    security_code: str
    name: str
    billing_address: dict[str, str] | None

    def __repr__(self) -> str:  # keep card data out of tracebacks and logs
        return "CardInput(<redacted>)"


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@sensitive_variables("data", "number", "security_code")
def parse_card(data: object) -> CardInput:
    if not isinstance(data, dict):
        raise PaymentError(400, "card must be an object.", code="invalid_card")
    number = "".join(ch for ch in str(data.get("number", "")) if ch not in " -")
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise PaymentError(400, "card.number is not a valid card number.", code="invalid_card")
    expiry = _parse_expiry(str(data.get("expiry", "")))
    security_code = str(data.get("securityCode", ""))
    if not security_code.isdigit() or not 3 <= len(security_code) <= 4:
        raise PaymentError(400, "card.securityCode must be 3 or 4 digits.", code="invalid_card")
    name = str(data.get("name", "")).strip()[:300]
    address = data.get("billingAddress")
    billing: dict[str, str] | None = None
    if address is not None:
        if not isinstance(address, dict):
            raise PaymentError(400, "card.billingAddress must be an object.", code="invalid_card")
        billing = {k: str(v) for k, v in address.items() if v is not None}
        code = billing.get("countryCode", "")
        if len(code) != 2 or not code.isalpha():
            raise PaymentError(400, "card.billingAddress.countryCode must be a two-letter code.", code="invalid_card")
    return CardInput(number, expiry, security_code, name, billing)


def _parse_expiry(raw: str) -> str:
    raw = raw.strip()
    year = month = None
    if len(raw) == 7 and raw[4] == "-":  # YYYY-MM
        year, month = raw[:4], raw[5:]
    elif "/" in raw:  # MM/YY or MM/YYYY
        month, year = raw.split("/", 1)
        if len(year) == 2:
            year = "20" + year
    if not (year and month and year.isdigit() and month.isdigit() and len(year) == 4 and 1 <= int(month) <= 12):
        raise PaymentError(400, "card.expiry must be YYYY-MM (or MM/YY).", code="invalid_card")
    today = timezone.now().date()
    if (int(year), int(month)) < (today.year, today.month):
        raise PaymentError(400, "The card has expired.", code="invalid_card")
    return f"{year}-{int(month):02d}"


def _paypal_address(billing: dict[str, str] | None) -> Address | UnsetType:
    if not billing:
        return UNSET
    fields: dict[str, str] = {"country_code": billing["countryCode"].upper()}
    for ours, theirs in (
        ("line1", "address_line_1"),
        ("line2", "address_line_2"),
        ("city", "admin_area_2"),
        ("state", "admin_area_1"),
        ("postalCode", "postal_code"),
    ):
        if billing.get(ours):
            fields[theirs] = billing[ours]
    return Address.model_validate(fields)


# ---------------------------------------------------------------------------
# Serialisation helpers shared with the views
# ---------------------------------------------------------------------------


def get_source(order: Any, payment: PayPalPayment) -> Any:
    source_type, _ = SourceType.objects.get_or_create(name="PayPal")
    source, _ = Source.objects.get_or_create(
        order=order,
        source_type=source_type,
        defaults={"currency": payment.currency, "reference": payment.paypal_order_id},
    )
    return source


def _set_order_status(order: Any, status: str) -> None:
    try:
        order.set_status(status)
    except InvalidOrderStatus:
        logger.warning("Order %s: cannot move from %r to %r", order.number, order.status, status)


def _move_allocations(order: Any, consume: bool) -> None:
    """Consume stock allocations on fulfilment, release them on cancel."""
    for line in order.lines.select_related("stockrecord"):
        if line.stockrecord is None:
            continue
        if consume:
            line.stockrecord.consume_allocation(line.quantity)
        else:
            line.stockrecord.cancel_allocation(line.quantity)


def _locked_payment(order: Any) -> PayPalPayment:
    return PayPalPayment.objects.select_for_update().get(order=order)


def payment_for(order: Any) -> PayPalPayment:
    try:
        return PayPalPayment.objects.get(order=order)
    except PayPalPayment.DoesNotExist:
        raise PaymentError(409, "This order was not placed through the payments API.", code="no_payment") from None


# ---------------------------------------------------------------------------
# Flow 1: authorize
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What an operation reports: the write's outcome and the order it concerns."""

    outcome: str
    order: Any
    message: str = ""
    extra: dict[str, Any] | None = None


@sensitive_variables("card")
def authorize(order: Any, user: Any, card: CardInput | None, saved_card_id: str | None) -> Outcome:
    payment = payment_for(order)
    if payment.status in (PayPalPayment.VOIDED, PayPalPayment.CANCELLED):
        raise PaymentError(409, "This order was cancelled.", code="order_cancelled")
    if payment.status == PayPalPayment.NEEDS_REVIEW:
        raise PaymentError(409, "This order's payment is under review.", code="needs_review")
    if payment.status not in (PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZATION_PENDING):
        return Outcome(DONE, order, "This order is already paid.")  # repeat of a paid order: no new hold

    saved: SavedCard | None = None
    if saved_card_id is not None:
        saved = SavedCard.objects.filter(public_id=saved_card_id, user=user, deleted_at__isnull=True).first()
        if saved is None:
            raise PaymentError(404, "Saved card not found.", code="unknown_payment_method")
    elif card is None:
        raise PaymentError(400, "Provide card details or a paymentMethodId.", code="invalid_request")

    config = get_config()
    amount = quantize(payment.amount, payment.currency)
    attempt = payment.pay_attempt
    ref = deterministic_ref(order.number, "auth", attempt)
    body = _authorize_body(order, payment, amount, card, saved)
    client = get_client()

    def send(key: str) -> PayPalOrder:
        return client.orders.create_order(body, pay_pal_request_id=key, prefer="return=representation")

    def find(record: ProviderWrite) -> PayPalOrder:
        return client.orders.get_order(record.provider_id)

    def read(result: PayPalOrder) -> Answer:
        auth = _first_authorization(result)
        value, currency = _amount_of(auth) if auth is not None else (None, None)
        auth_status = auth.status if auth is not None and not isinstance(auth.status, UnsetType) else None
        order_status = result.status if not isinstance(result.status, UnsetType) else None
        return Answer(
            provider_id=_s(result.id),
            status=(auth_status, order_status),
            provider_time=provider_time(auth.create_time) if auth is not None else None,
            amount=value,
            currency=currency,
            status_label=_label(auth_status) or _label(order_status),
        )

    def apply(record: ProviderWrite, result: PayPalOrder) -> None:
        p = _locked_payment(order)
        auth = _first_authorization(result)
        p.paypal_order_id = _s(result.id) or p.paypal_order_id
        p.saved_card = saved
        card_info = result.payment_source.card if not isinstance(result.payment_source, UnsetType) else UNSET
        if not isinstance(card_info, UnsetType):
            p.card_brand = _label(card_info.brand)
            p.card_last_digits = _s(card_info.last_digits)
        if auth is not None:
            p.authorization_id = _s(auth.id)
            p.authorization_status = _label(auth.status)
            p.authorized_at = provider_time(auth.create_time) or timezone.now()
            p.authorization_expires_at = provider_time(auth.expiration_time)
        if record.outcome == DONE:
            p.status = PayPalPayment.AUTHORIZED
            p.last_error = ""
            source = get_source(order, p)
            source.reference = p.paypal_order_id
            source.label = f"{p.card_brand} ending {p.card_last_digits}".strip()
            source.save()
            source.allocate(amount, reference=p.authorization_id, status=p.authorization_status)
            _set_order_status(order, STATUS_PAID)
        elif record.outcome == PENDING:
            p.status = PayPalPayment.AUTHORIZATION_PENDING
        elif record.outcome == FAILED:
            p.status = PayPalPayment.AWAITING_PAYMENT
            p.last_error = _authorize_failure_message(record)
            if p.pay_attempt == attempt:
                p.pay_attempt = attempt + 1  # the next try is a new write
        elif record.outcome == NEEDS_REVIEW:
            p.status = PayPalPayment.NEEDS_REVIEW
        p.save()

    try:
        result = safe_write(
            ref, "authorize", send=send, find=find, read=read, outcome_of=authorize_outcome,
            sent=(amount, config.currency), apply=apply,
        )
    except PaymentError as exc:
        if exc.code == "provider_rejected":
            # PayPal refused this attempt outright (e.g. the card was declined).
            PayPalPayment.objects.filter(pk=payment.pk, pay_attempt=attempt).update(
                pay_attempt=attempt + 1, last_error=exc.message[:500]
            )
            exc.status_code = 402
            exc.code = "payment_declined"
        raise
    order.refresh_from_db()
    message = ""
    if result.outcome == FAILED:
        message = _authorize_failure_message(result.record)
    return Outcome(result.outcome, order, message)


def _authorize_failure_message(record: ProviderWrite) -> str:
    if record.provider_status == OrderStatus.PAYER_ACTION_REQUIRED:
        return ("PayPal requires the cardholder to approve this payment in a browser, "
                "which this API does not support. Try a different card.")
    return "The card was declined. No money was taken; try another card."


@sensitive_variables("card")
def _authorize_body(
    order: Any, payment: PayPalPayment, amount: Decimal, card: CardInput | None, saved: SavedCard | None
) -> PayPalOrderRequest:
    prefix = install_prefix()
    if saved is not None:
        card_request = CardRequest(
            vault_id=saved.token_id,
            stored_credential=CardStoredCredential(
                payment_initiator=PaymentInitiator.CUSTOMER,
                payment_type=StoredPaymentSourcePaymentType.UNSCHEDULED,
                usage=StoredPaymentSourceUsageType.SUBSEQUENT,
            ),
        )
    else:
        assert card is not None
        card_request = CardRequest(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            name=card.name or UNSET,
            billing_address=_paypal_address(card.billing_address),
        )
    return PayPalOrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                amount=AmountWithBreakdown(currency_code=payment.currency, value=money_str(amount, payment.currency)),
                # custom_id identifies the order; invoice_id the attempt (PayPal
                # rejects a reused invoice id).
                custom_id=f"{prefix}-{order.number}",
                invoice_id=f"{prefix}-{order.number}-{payment.pay_attempt}",
                description=f"Order {order.number}"[:127],
            )
        ],
        payment_source=PaymentSource(card=card_request),
    )


# ---------------------------------------------------------------------------
# Flow 1: fulfil (renew a stale hold, then capture)
# ---------------------------------------------------------------------------


def fulfil(order: Any) -> Outcome:
    payment = payment_for(order)
    if payment.status in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        return Outcome(DONE, order, "This order is already fulfilled.")
    if payment.status == PayPalPayment.CAPTURE_PENDING:
        return _capture(order, payment)  # refreshes the pending capture from PayPal
    if payment.status in (PayPalPayment.VOIDED, PayPalPayment.CANCELLED):
        raise PaymentError(409, "This order was cancelled; there is nothing to capture.", code="order_cancelled")
    if payment.status == PayPalPayment.NEEDS_REVIEW:
        raise PaymentError(409, "This order's payment is under review.", code="needs_review")
    if payment.status != PayPalPayment.AUTHORIZED:
        raise PaymentError(409, "This order has not been paid yet.", code="not_paid")

    if payment.authorized_at is not None and timezone.now() - payment.authorized_at > HONOR_PERIOD:
        renewed = _renew_authorization(order, payment, reason="the three-day honor period has passed")
        if renewed is not None:
            return renewed
        payment.refresh_from_db()
    try:
        return _capture(order, payment)
    except PaymentError as exc:
        if exc.code != "provider_rejected" or not _authorization_needs_renewal(payment):
            raise
    # PayPal refused the capture because the hold went stale: renew it once and capture again.
    renewed = _renew_authorization(order, payment, reason="PayPal refused to capture the stale authorization")
    if renewed is not None:
        return renewed
    payment.refresh_from_db()
    return _capture(order, payment)


def _authorization_needs_renewal(payment: PayPalPayment) -> bool:
    auth = call_read(get_client().payments.get_authorized_payment, payment.authorization_id)
    status = auth.status if not isinstance(auth.status, UnsetType) else None
    expires = provider_time(auth.expiration_time)
    still_valid = status in (AuthorizationStatus.CREATED, AuthorizationStatus.PENDING)
    return not still_valid or (expires is not None and expires <= timezone.now())


def _renew_authorization(order: Any, payment: PayPalPayment, reason: str) -> Outcome | None:
    """Reauthorize the hold. Returns None when the order can go on to capture,
    an Outcome when the renewal is still pending, and raises an
    operator-actionable error when the hold can no longer be renewed."""
    old_id = payment.authorization_id
    amount = quantize(payment.amount, payment.currency)
    ref = deterministic_ref(order.number, "reauth", old_id)
    client = get_client()

    def send(key: str) -> PaymentAuthorization:
        return client.payments.reauthorize_payment(
            old_id, pay_pal_request_id=key, prefer="return=representation",
            body=ReauthorizeRequest(amount=_money(amount, payment.currency)),
        )

    def find(record: ProviderWrite) -> PaymentAuthorization:
        return client.payments.get_authorized_payment(record.provider_id)

    def read(result: PaymentAuthorization) -> Answer:
        value, currency = _amount_of(result)
        return Answer(_s(result.id), result.status, provider_time(result.create_time), value, currency,
                      _label(result.status))

    def apply(record: ProviderWrite, result: PaymentAuthorization) -> None:
        if record.outcome not in (DONE, PENDING):
            return
        p = _locked_payment(order)
        p.authorization_id = _s(result.id) or p.authorization_id
        p.authorization_status = _label(result.status)
        p.authorized_at = provider_time(result.create_time) or timezone.now()
        p.authorization_expires_at = provider_time(result.expiration_time) or p.authorization_expires_at
        p.save()

    try:
        result = safe_write(ref, "reauthorize", send=send, find=find, read=read,
                            outcome_of=reauthorize_outcome, sent=(amount, payment.currency), apply=apply)
    except PaymentError as exc:
        if exc.code != "provider_rejected":
            raise
        return _renewal_refused(order, payment, reason, exc)
    if result.outcome == DONE:
        logger.info("Order %s: authorization %s renewed (%s)", order.number, old_id, reason)
        return None
    if result.outcome in (PENDING, SENDING):
        return Outcome(PENDING, order, "PayPal is still renewing the authorization; fulfil again shortly.")
    return _renewal_refused(order, payment, reason, None)


def _renewal_refused(order: Any, payment: PayPalPayment, reason: str, exc: PaymentError | None) -> Outcome | None:
    """PayPal will not renew the hold. If the original authorization is still
    capturable, capture it; otherwise tell the operator what to do."""
    if not _authorization_needs_renewal(payment):
        return None  # still valid: capturing it is the best outcome
    expires = payment.authorization_expires_at
    when = f" (it expired {expires.isoformat()})" if expires else ""
    message = (
        f"The payment authorization for order {order.number} can no longer be captured{when} and PayPal "
        f"refused to renew it ({reason}). No money has been taken. Cancel this order and ask the shopper "
        f"to place and pay for it again."
    )
    PayPalPayment.objects.filter(pk=payment.pk).update(last_error=message)
    raise PaymentError(409, message, code="authorization_expired", details=exc.details if exc else None)


def _capture(order: Any, payment: PayPalPayment) -> Outcome:
    authorization_id = payment.authorization_id
    amount = quantize(payment.amount, payment.currency)
    ref = deterministic_ref(order.number, "capture", authorization_id)
    client = get_client()

    def send(key: str) -> CapturedPayment:
        return client.payments.capture_authorized_payment(
            authorization_id, pay_pal_request_id=key, prefer="return=representation",
            body=CaptureRequest(amount=_money(amount, payment.currency), final_capture=True),
        )

    def find(record: ProviderWrite) -> CapturedPayment:
        return client.payments.get_captured_payment(record.provider_id)

    def read(result: CapturedPayment) -> Answer:
        value, currency = _amount_of(result)
        return Answer(_s(result.id), result.status, provider_time(result.create_time), value, currency,
                      _label(result.status))

    def apply(record: ProviderWrite, result: CapturedPayment) -> None:
        p = _locked_payment(order)
        was_captured = p.status == PayPalPayment.CAPTURED
        p.capture_id = _s(result.id) or p.capture_id
        p.capture_status = _label(result.status)
        p.captured_at = provider_time(result.create_time) or p.captured_at
        breakdown = result.seller_receivable_breakdown
        if not isinstance(breakdown, UnsetType):
            p.captured_amount = _decimal(breakdown.gross_amount.value)
            if not isinstance(breakdown.paypal_fee, UnsetType):
                p.paypal_fee = _decimal(breakdown.paypal_fee.value)
            if not isinstance(breakdown.net_amount, UnsetType):
                p.net_amount = _decimal(breakdown.net_amount.value)
        if p.captured_amount is None:
            value, _currency = _amount_of(result)
            p.captured_amount = _decimal(value)
        if record.outcome == DONE and not was_captured:
            p.status = PayPalPayment.CAPTURED
            p.last_error = ""
            get_source(order, p).debit(p.captured_amount or amount, reference=p.capture_id, status=p.capture_status)
            _move_allocations(order, consume=True)
            _set_order_status(order, STATUS_FULFILLED)
        elif record.outcome == PENDING:
            p.status = PayPalPayment.CAPTURE_PENDING
        elif record.outcome == FAILED:
            p.last_error = f"PayPal reported the capture as {p.capture_status}."
        elif record.outcome == NEEDS_REVIEW:
            p.status = PayPalPayment.NEEDS_REVIEW
        p.save()

    result = safe_write(ref, "capture", send=send, find=find, read=read, outcome_of=capture_outcome,
                        sent=(amount, payment.currency), apply=apply)
    order.refresh_from_db()
    if result.outcome == FAILED:
        return Outcome(FAILED, order, "PayPal declined the capture. No money was taken.")
    return Outcome(result.outcome, order)


# ---------------------------------------------------------------------------
# Flow 1: cancel (release the hold)
# ---------------------------------------------------------------------------


def cancel(order: Any) -> Outcome:
    payment = payment_for(order)
    if payment.status in (PayPalPayment.VOIDED, PayPalPayment.CANCELLED):
        return Outcome(DONE, order, "This order is already cancelled.")
    if payment.status in (
        PayPalPayment.CAPTURED, PayPalPayment.CAPTURE_PENDING,
        PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED,
    ):
        raise PaymentError(409, "This order has been fulfilled and the money taken; refund it instead.",
                           code="already_captured")
    if payment.status == PayPalPayment.AWAITING_PAYMENT:
        return _cancel_unpaid(order, payment)
    if payment.status != PayPalPayment.AUTHORIZED:
        raise PaymentError(409, "The payment for this order is still being settled; try again shortly.",
                           code="payment_in_progress")
    return _void(order, payment)


def _cancel_unpaid(order: Any, payment: PayPalPayment) -> Outcome:
    # A pay request whose outcome is not yet known may still have placed a hold.
    in_doubt = ProviderWrite.objects.filter(
        ref=deterministic_ref(order.number, "auth", payment.pay_attempt),
        outcome__in=[SENDING, UNKNOWN, PENDING],
    ).exists()
    if in_doubt:
        raise PaymentError(409, "A payment for this order is still being settled; try again shortly.",
                           code="payment_in_progress")
    with transaction.atomic():
        updated = PayPalPayment.objects.filter(pk=payment.pk, status=PayPalPayment.AWAITING_PAYMENT).update(
            status=PayPalPayment.CANCELLED
        )
        if updated:
            _move_allocations(order, consume=False)
            _set_order_status(order, STATUS_CANCELLED)
    order.refresh_from_db()
    return Outcome(DONE, order, "The order was cancelled before any payment was taken.")


def _void(order: Any, payment: PayPalPayment) -> Outcome:
    authorization_id = payment.authorization_id
    ref = deterministic_ref(order.number, "void", authorization_id)
    client = get_client()

    def send(key: str) -> PaymentAuthorization:
        return client.payments.void_payment(authorization_id, pay_pal_request_id=key, prefer="return=representation")

    def find(record: ProviderWrite) -> PaymentAuthorization:
        return client.payments.get_authorized_payment(authorization_id)

    def read(result: PaymentAuthorization) -> Answer:
        return Answer(_s(result.id) or authorization_id, result.status,
                      provider_time(result.update_time), status_label=_label(result.status))

    def apply(record: ProviderWrite, result: PaymentAuthorization) -> None:
        p = _locked_payment(order)
        p.authorization_status = _label(result.status) or p.authorization_status
        if record.outcome == DONE and p.status != PayPalPayment.VOIDED:
            p.status = PayPalPayment.VOIDED
            p.last_error = ""
            source = get_source(order, p)
            Transaction.objects.create(source=source, txn_type="Void", amount=p.amount,
                                       reference=authorization_id, status=p.authorization_status)
            _move_allocations(order, consume=False)
            _set_order_status(order, STATUS_CANCELLED)
        p.save()

    result = safe_write(ref, "void", send=send, find=find, read=read, outcome_of=void_outcome, apply=apply)
    order.refresh_from_db()
    if result.outcome == FAILED:
        raise PaymentError(409, "PayPal reports this authorization was already captured; refund it instead.",
                           code="already_captured")
    return Outcome(result.outcome, order)


# ---------------------------------------------------------------------------
# Flow 1: refunds
# ---------------------------------------------------------------------------


def parse_amount(raw: object, currency: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise PaymentError(400, "amount must be a decimal string such as \"5.00\".", code="invalid_amount") from None
    if not value.is_finite() or value <= 0 or quantize(value, currency) != value:
        raise PaymentError(400, f"amount must be a positive amount in {currency}.", code="invalid_amount")
    return quantize(value, currency)


def refund(order: Any, amount_raw: object | None, idempotency_key: str, note: str = "") -> tuple[Outcome, PayPalRefund]:
    payment = payment_for(order)
    currency = payment.currency
    hashed = key_hash(idempotency_key)
    existing = PayPalRefund.objects.filter(payment=payment, key_hash=hashed).first()
    if existing is not None:
        if amount_raw is not None and parse_amount(amount_raw, currency) != existing.amount:
            raise PaymentError(422, "This Idempotency-Key was already used for a refund of a different amount.",
                               code="idempotency_key_reused")
        refund_row = existing
        if existing.status in (PayPalRefund.COMPLETED, PayPalRefund.FAILED, PayPalRefund.NEEDS_REVIEW):
            outcome = {PayPalRefund.COMPLETED: DONE, PayPalRefund.FAILED: FAILED}.get(existing.status, NEEDS_REVIEW)
            return Outcome(outcome, order), existing
    else:
        if payment.status not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED):
            if payment.status == PayPalPayment.REFUNDED:
                raise PaymentError(409, "This order has been refunded in full.", code="nothing_to_refund")
            raise PaymentError(409, "Only a fulfilled (captured) order can be refunded.", code="not_captured")
        refund_row = _reserve_refund(payment, amount_raw, hashed, currency)

    ref = refund_row.ref
    amount = refund_row.amount
    capture_id = payment.capture_id
    client = get_client()
    body = RefundRequest(
        amount=_money(amount, currency),
        custom_id=f"{install_prefix()}-{order.number}",
        note_to_payer=note[:255] if note else UNSET,
    )

    def send(key: str) -> Refund:
        return client.payments.refund_captured_payment(
            capture_id, pay_pal_request_id=key, prefer="return=representation", body=body
        )

    def find(record: ProviderWrite) -> Refund:
        return client.payments.get_refund(record.provider_id)

    def read(result: Refund) -> Answer:
        value, cur = _amount_of(result)
        return Answer(_s(result.id), result.status, provider_time(result.create_time), value, cur,
                      _label(result.status))

    def apply(record: ProviderWrite, result: Refund) -> None:
        _apply_refund(order, refund_row.pk, record, _s(result.id), _label(result.status),
                      provider_time(result.create_time))

    try:
        result = safe_write(ref, "refund", send=send, find=find, read=read, outcome_of=refund_outcome,
                            sent=(amount, currency), apply=apply)
    except PaymentError as exc:
        if exc.code == "provider_rejected":
            # PayPal refused it outright: nothing was refunded, give the amount back.
            _release_refund(refund_row.pk, PayPalRefund.FAILED)
        elif exc.code == "amount_mismatch":
            PayPalRefund.objects.filter(pk=refund_row.pk).update(status=PayPalRefund.NEEDS_REVIEW)
        elif exc.outcome_unknown:
            PayPalRefund.objects.filter(pk=refund_row.pk).update(status=PayPalRefund.UNKNOWN)
        raise
    refund_row.refresh_from_db()
    order.refresh_from_db()
    return Outcome(result.outcome, order), refund_row


def _reserve_refund(payment: PayPalPayment, amount_raw: object | None, hashed: str, currency: str) -> PayPalRefund:
    captured = payment.captured_amount or Decimal("0")
    if amount_raw is None:
        amount = captured - payment.refund_reserved  # the rest of the capture
        if amount <= 0:
            raise PaymentError(409, "Nothing is left to refund on this order.", code="nothing_to_refund")
    else:
        amount = parse_amount(amount_raw, currency)
    with transaction.atomic():
        # Conditional UPDATE: two concurrent refunds can never reserve more than was captured.
        reserved = PayPalPayment.objects.filter(
            pk=payment.pk,
            status__in=[PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED],
            captured_amount__isnull=False,
            refund_reserved__lte=F("captured_amount") - amount,
        ).update(refund_reserved=F("refund_reserved") + amount)
        if not reserved:
            payment.refresh_from_db()
            remaining = (payment.captured_amount or Decimal("0")) - payment.refund_reserved
            raise PaymentError(
                409, f"A refund of {amount} {currency} exceeds what is left to refund ({remaining} {currency}).",
                code="refund_exceeds_capture",
            )
        n = PayPalRefund.objects.filter(payment=payment).count() + 1
        try:
            with transaction.atomic():
                return PayPalRefund.objects.create(
                    payment=payment,
                    key_hash=hashed,
                    ref=deterministic_ref(payment.order.number, "refund", hashed[:20]),
                    amount=amount,
                )
        except IntegrityError:
            # The same key raced us: the other request owns the refund; undo our reservation.
            PayPalPayment.objects.filter(pk=payment.pk).update(refund_reserved=F("refund_reserved") - amount)
            logger.info("Refund %s for order %s raced on the same key", n, payment.order.number)
    return PayPalRefund.objects.get(payment=payment, key_hash=hashed)


def _release_refund(refund_pk: int, status: str) -> None:
    with transaction.atomic():
        row = PayPalRefund.objects.select_for_update().get(pk=refund_pk)
        if row.reservation_held:
            PayPalPayment.objects.filter(pk=row.payment_id).update(refund_reserved=F("refund_reserved") - row.amount)
            row.reservation_held = False
        row.status = status
        row.save()


def _apply_refund(order: Any, refund_pk: int, record: ProviderWrite, refund_id: str, status_label: str,
                  refunded_at: datetime | None) -> None:
    row = PayPalRefund.objects.select_for_update().get(pk=refund_pk)
    row.paypal_refund_id = refund_id or row.paypal_refund_id
    row.paypal_status = status_label
    row.refunded_at = refunded_at or row.refunded_at
    if record.outcome == DONE and row.status != PayPalRefund.COMPLETED:
        row.status = PayPalRefund.COMPLETED
        p = _locked_payment(order)
        p.amount_refunded += row.amount
        p.status = PayPalPayment.REFUNDED if p.amount_refunded >= (p.captured_amount or 0) else PayPalPayment.PARTIALLY_REFUNDED
        p.save()
        get_source(order, p).refund(row.amount, reference=row.paypal_refund_id, status=status_label)
    elif record.outcome == PENDING:
        row.status = PayPalRefund.PENDING
    elif record.outcome == FAILED:
        if row.reservation_held:
            PayPalPayment.objects.filter(pk=row.payment_id).update(refund_reserved=F("refund_reserved") - row.amount)
            row.reservation_held = False
        row.status = PayPalRefund.FAILED
    elif record.outcome == UNKNOWN:
        row.status = PayPalRefund.UNKNOWN
    row.save()


# ---------------------------------------------------------------------------
# Flow 2: saved cards
# ---------------------------------------------------------------------------


@sensitive_variables("card")
def save_card(user: Any, card: CardInput, idempotency_key: str) -> tuple[str, SavedCard | None]:
    ref = deterministic_ref(f"u{user.pk}", "card", key_hash(idempotency_key)[:20])
    customer = PayPalCustomer.objects.filter(user=user).first()
    request_body = PaymentTokenRequest(
        customer=Customer(id=customer.vault_customer_id) if customer else UNSET,
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                number=card.number,
                expiry=card.expiry,
                security_code=card.security_code,
                name=card.name or UNSET,
                billing_address=_paypal_address(card.billing_address),
            )
        ),
    )
    client = get_client()

    def send(key: str) -> PaymentTokenResponse:
        return client.vault.create_payment_token(request_body, pay_pal_request_id=key)

    def read(result: PaymentTokenResponse) -> Answer:
        has_card = not isinstance(result.payment_source, UnsetType) and not isinstance(
            result.payment_source.card, UnsetType
        )
        token_id = _s(result.id)
        return Answer(token_id, bool(token_id) and has_card, timezone.now(), status_label="VAULTED" if has_card else "")

    def apply(record: ProviderWrite, result: PaymentTokenResponse) -> None:
        if record.outcome != DONE:
            return
        source = result.payment_source  # present: vault_outcome checked it
        assert not isinstance(source, UnsetType)
        details = source.card
        assert not isinstance(details, UnsetType)
        SavedCard.objects.get_or_create(
            token_id=record.provider_id,
            defaults={
                "user": user,
                "brand": _label(details.brand),
                "last_digits": _s(details.last_digits),
                "expiry": _s(details.expiry),
                "cardholder_name": _s(details.name)[:128],
            },
        )
        if not isinstance(result.customer, UnsetType) and _s(result.customer.id):
            PayPalCustomer.objects.get_or_create(user=user, defaults={"vault_customer_id": _s(result.customer.id)})

    result = safe_write(ref, "vault_card", send=send, read=read, outcome_of=vault_outcome, apply=apply)
    saved = None
    if result.record.provider_id:
        saved = SavedCard.objects.filter(token_id=result.record.provider_id, user=user).first()
    return result.outcome, saved


def delete_card(user: Any, public_id: str) -> tuple[str, SavedCard]:
    card = SavedCard.objects.filter(public_id=public_id, user=user, provider_deleted=False).first()
    if card is None:
        raise PaymentError(404, "Saved card not found.", code="unknown_payment_method")
    # Hidden and unusable from this moment, whatever PayPal answers below.
    SavedCard.objects.filter(pk=card.pk, deleted_at__isnull=True).update(deleted_at=timezone.now())
    ref = deterministic_ref("card", card.pk, "delete")
    client = get_client()
    token_id = card.token_id

    def send(key: str) -> ApiResult[None, Any]:
        # Returns None on success: the raw peer is the only way to see the status.
        return client.vault.with_raw_response.delete_payment_token(token_id)

    def read(result: ApiResult[None, Any]) -> Answer:
        status = result.response.status_code if isinstance(result, (Success, Failure)) else None
        return Answer(token_id, status, timezone.now(), status_label=str(status))

    def apply(record: ProviderWrite, result: ApiResult[None, Any]) -> None:
        if record.outcome == DONE:
            SavedCard.objects.filter(pk=card.pk).update(provider_deleted=True)

    try:
        result = safe_write(ref, "delete_card", send=send, read=read, outcome_of=delete_outcome, apply=apply)
        outcome = result.outcome
    except PaymentError as exc:
        # The card is already removed for the shopper; PayPal's copy is retried
        # on a repeated DELETE and stays visible to operators until then.
        logger.warning("PayPal token for saved card %s not deleted yet: %s", card.pk, exc.message)
        outcome = UNKNOWN
    card.refresh_from_db()
    return outcome, card


def list_cards(user: Any) -> list[SavedCard]:
    return list(SavedCard.objects.filter(user=user, deleted_at__isnull=True))


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

MAX_WINDOW = timedelta(days=31)  # search_transactions' maximum range
MAX_PAGES = 500


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    """PayPal's transactions for [start, end) lined up against this app's
    captures and refunds, both filtered on PayPal's own clock."""
    client = get_client()
    prefix = install_prefix()
    provider: list[dict[str, Any]] = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + MAX_WINDOW, end)
        page = 1
        while True:
            response = call_read(
                client.transaction_search.search_transactions,
                _rfc3339(window_start), _rfc3339(window_end),
                page=page, fields="transaction_info",
            )
            details = response.transaction_details if not isinstance(response.transaction_details, UnsetType) else []
            for detail in details:
                info = detail.transaction_info
                if isinstance(info, UnsetType):
                    continue
                provider.append(_transaction_row(info))
            total_pages = response.total_pages if isinstance(response.total_pages, int) else 1
            if page >= total_pages or page >= MAX_PAGES:
                break
            page += 1
        window_start = window_end

    # PayPal's end date is inclusive; keep exactly [start, end).
    provider = [t for t in provider if t["initiatedAt"] is None or start <= t["_at"] < end]

    local: dict[str, dict[str, Any]] = {}
    for p in PayPalPayment.objects.filter(captured_at__gte=start, captured_at__lt=end).exclude(capture_id="").select_related("order"):
        local[p.capture_id] = {"kind": "capture", "orderId": p.order.number, "paypalId": p.capture_id,
                               "amount": str(p.captured_amount), "currency": p.currency, "at": p.captured_at.isoformat(),
                               "status": p.capture_status}
    for r in PayPalRefund.objects.filter(refunded_at__gte=start, refunded_at__lt=end).exclude(paypal_refund_id="").select_related("payment__order"):
        local[r.paypal_refund_id] = {"kind": "refund", "orderId": r.payment.order.number, "paypalId": r.paypal_refund_id,
                                     "refundId": str(r.public_id), "amount": str(r.amount), "currency": r.payment.currency,
                                     "at": r.refunded_at.isoformat() if r.refunded_at else None, "status": r.paypal_status}

    matched, paypal_only = [], []
    seen: set[str] = set()
    for t in provider:
        row = {k: v for k, v in t.items() if not k.startswith("_")}
        ours = local.get(t["transactionId"])
        if ours is not None:
            seen.add(t["transactionId"])
            matched.append({"paypal": row, "app": ours,
                            "amountsAgree": _amounts_agree(t["amount"], ours["amount"])})
        else:
            row["carriesOurReference"] = any(
                isinstance(v, str) and v.startswith(prefix + "-") for v in (t["invoiceId"], t["customField"])
            )
            paypal_only.append(row)
    app_only = [v for k, v in local.items() if k not in seen]
    unsettled = [
        {"reference": w.ref, "operation": w.operation, "outcome": w.outcome, "claimedAt": w.claimed_at.isoformat(),
         "paypalId": w.provider_id or None}
        for w in ProviderWrite.objects.filter(
            claimed_at__gte=start, claimed_at__lt=end, outcome__in=[SENDING, UNKNOWN, PENDING]
        ).order_by("claimed_at")
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "summary": {"paypalTransactions": len(provider), "matched": len(matched), "paypalOnly": len(paypal_only),
                    "appOnly": len(app_only), "unsettled": len(unsettled)},
        "matched": matched,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
        "unsettled": unsettled,
        "note": "PayPal can take up to three hours to list a transaction; very recent activity may show as appOnly.",
    }


def _rfc3339(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _amounts_agree(paypal_amount: str | None, ours: str | None) -> bool:
    a, b = _decimal(paypal_amount), _decimal(ours)
    return a is not None and b is not None and abs(a) == abs(b)


def _transaction_row(info: Any) -> dict[str, Any]:
    at = provider_time(info.transaction_initiation_date)

    def money(m: Any) -> str | None:
        return None if isinstance(m, UnsetType) else _s(m.value) or None

    currency = None if isinstance(info.transaction_amount, UnsetType) else _s(info.transaction_amount.currency_code)
    return {
        "transactionId": _s(info.transaction_id),
        "referenceId": _s(info.paypal_reference_id) or None,
        "eventCode": _s(info.transaction_event_code) or None,
        "status": _s(info.transaction_status) or None,
        "initiatedAt": at.isoformat() if at else None,
        "amount": money(info.transaction_amount),
        "fee": money(info.fee_amount),
        "currency": currency,
        "invoiceId": _s(info.invoice_id) or None,
        "customField": _s(info.custom_field) or None,
        "_at": at,
    }

