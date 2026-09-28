"""
Order, payment and saved-card flows.

Orders are Oscar orders placed through Oscar's own basket, offer, shipping
and ``OrderCreator`` machinery; money movements are mirrored into Oscar's
``payment.Source``/``Transaction`` and ``order.PaymentEvent`` models. Every
PayPal write goes through ``gateway.safe_write``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from oscar.core.prices import Price
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Money,
    Order as PayPalOrder,
    OrderAuthorizeResponse,
    OrderRequest,
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
    SearchResponse,
)
from paypal.models.enums import AuthorizationStatus, CheckoutPaymentIntent

from . import gateway
from .gateway import Answer, ProviderError, parse_time, present
from .models import InstallIdentity, OrderPayment, PayPalRefund, ProviderWrite, SavedCard

logger = logging.getLogger("apps.paypal_payments")

Order = get_model("order", "Order")
PaymentEventType = get_model("order", "PaymentEventType")
ShippingAddress = get_model("order", "ShippingAddress")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
EventHandler = get_class("order.processing", "EventHandler")
Repository = get_class("shipping.repository", "Repository")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Selector = get_class("partner.strategy", "Selector")
Applicator = get_class("offer.applicator", "Applicator")

# Oscar order statuses used by this API (see OSCAR_ORDER_STATUS_PIPELINE in settings)
AWAITING_PAYMENT = "Awaiting payment"
PAYMENT_AUTHORISED = "Payment authorised"
FULFILLED = "Fulfilled"
CANCELLED = "Cancelled"

# PayPal rules for a card authorization (reauthorize_payment docstring):
# a three-day honor period; one reauthorization allowed from day 4 to day 29.
HONOR_PERIOD = timedelta(days=3)
AUTHORIZATION_PERIOD = timedelta(days=29)

MAX_LINES = 50
MAX_QUANTITY = 99
MAX_RECONCILIATION_RANGE = timedelta(days=366)
SEARCH_CHUNK = timedelta(days=31)


class ApiProblem(Exception):
    """A request this API refuses, with the status and a message the caller can act on."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


def currency() -> str:
    return str(settings.PAYPAL_CURRENCY)


def _ref(*parts: object) -> str:
    return ":".join([InstallIdentity.prefix(), *(str(p) for p in parts)])


def _event(order: Any, name: str, amount: Decimal, reference: str) -> None:
    try:
        with transaction.atomic():
            event_type, _ = PaymentEventType.objects.get_or_create(name=name)
    except IntegrityError:
        event_type = PaymentEventType.objects.get(name=name)
    EventHandler().create_payment_event(order, event_type, amount, reference=reference)


def _source_type() -> Any:
    source_type = SourceType.objects.filter(name="PayPal").first()
    return source_type or SourceType.objects.create(name="PayPal")


def _money(value: Decimal, code: str) -> Money:
    return Money(currency_code=code, value=gateway.money_str(value, code))


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


# ======
# Orders
# ======


def place_order(request: Any, user: Any, payload: dict[str, Any]) -> Any:
    items = payload.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_LINES:
        raise ApiProblem(400, "invalid_items", "items must be a list of 1-%d {productId, quantity} objects." % MAX_LINES)
    wanted: dict[int, int] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ApiProblem(400, "invalid_items", "Each item must be an object with productId and quantity.")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise ApiProblem(400, "invalid_items", "productId must be an integer catalogue id.")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise ApiProblem(400, "invalid_items", "quantity must be an integer from 1 to %d." % MAX_QUANTITY)
        wanted[product_id] = wanted.get(product_id, 0) + quantity

    products = {p.pk: p for p in Product.objects.filter(pk__in=wanted, is_public=True)}
    missing = sorted(set(wanted) - set(products))
    if missing:
        raise ApiProblem(400, "unknown_products", "No such catalogue items: %s." % missing)

    with transaction.atomic():
        basket = Basket(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        basket.save()
        for product_id, quantity in wanted.items():
            product = products[product_id]
            if product.is_parent:
                raise ApiProblem(400, "choose_variant", "Product %s has variants; order one of them." % product_id)
            info = basket.strategy.fetch_for_product(product)
            if not info.availability.is_available_to_buy:
                raise ApiProblem(409, "unavailable", "Product %s is not available to buy." % product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ApiProblem(409, "unavailable", "Product %s: %s" % (product_id, reason))
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, request)

        shipping_address = _shipping_address(payload.get("shippingAddress"))
        method = Repository().get_default_shipping_method(
            basket=basket, shipping_addr=shipping_address, user=user, request=request
        )
        charge = method.calculate(basket)
        basket_total = OrderTotalCalculator(request).calculate(basket, charge)
        # Amounts come from catalogue prices; the currency is configuration.
        total = Price(
            currency=currency(),
            excl_tax=basket_total.excl_tax,
            incl_tax=basket_total.incl_tax if basket_total.is_tax_known else basket_total.excl_tax,
        )
        if total.incl_tax <= 0:
            raise ApiProblem(400, "zero_total", "The order total must be greater than zero.")
        number = OrderNumberGenerator().order_number(basket)
        try:
            order = OrderCreator().place_order(
                basket=basket,
                total=total,
                shipping_method=method,
                shipping_charge=charge,
                user=user,
                shipping_address=shipping_address,
                order_number=number,
                status=AWAITING_PAYMENT,
                request=request,
            )
        except ValueError as e:
            raise ApiProblem(409, "order_rejected", str(e)) from e
        basket.submit()
        OrderPayment.objects.create(order=order, currency=order.currency)
    return order


def _shipping_address(data: Any) -> Any:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_address", "shippingAddress must be an object.")
    country = Country.objects.filter(iso_3166_1_a2=str(data.get("countryCode", "")).upper()).first()
    if country is None or not data.get("line1"):
        raise ApiProblem(400, "invalid_address", "shippingAddress needs line1 and a known countryCode.")
    address = ShippingAddress(
        first_name=str(data.get("firstName", ""))[:255],
        last_name=str(data.get("lastName", ""))[:255],
        line1=str(data["line1"])[:255],
        line2=str(data.get("line2", ""))[:255],
        line4=str(data.get("city", ""))[:255],
        state=str(data.get("state", ""))[:255],
        postcode=str(data.get("postcode", ""))[:64],
        country=country,
    )
    address.save()
    return address


def owned_order(user: Any, number: str) -> Any:
    order = Order.objects.filter(number=number, user=user).first()
    if order is None:
        raise ApiProblem(404, "order_not_found", "No such order.")
    return order


def any_order(number: str) -> Any:
    order = Order.objects.filter(number=number).first()
    if order is None:
        raise ApiProblem(404, "order_not_found", "No such order.")
    return order


def payment_for(order: Any) -> OrderPayment:
    payment, _ = OrderPayment.objects.get_or_create(order=order, defaults={"currency": order.currency})
    return payment


# ===
# Pay
# ===


@dataclass
class CardInput:
    number: str
    expiry: str
    security_code: str
    name: str = ""
    billing_address: dict[str, str] = field(default_factory=dict)

    @property
    def last_digits(self) -> str:
        return self.number[-4:]

    def __repr__(self) -> str:  # never let the number or code reach a log line
        return "CardInput(****%s, %s)" % (self.last_digits, self.expiry)


def parse_card(data: Any) -> CardInput:
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_card", "card must be an object.")
    number = re.sub(r"[\s-]", "", str(data.get("number", "")))
    expiry = str(data.get("expiry", ""))
    security_code = str(data.get("securityCode", ""))
    if not re.fullmatch(r"\d{12,19}", number) or not _luhn(number):
        raise ApiProblem(400, "invalid_card", "card.number is not a valid card number.")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", expiry):
        raise ApiProblem(400, "invalid_card", "card.expiry must be YYYY-MM.")
    if not re.fullmatch(r"\d{3,4}", security_code):
        raise ApiProblem(400, "invalid_card", "card.securityCode must be 3 or 4 digits.")
    address = data.get("billingAddress") or {}
    if not isinstance(address, dict) or (address and not re.fullmatch(r"[A-Za-z]{2}", str(address.get("countryCode", "")))):
        raise ApiProblem(400, "invalid_card", "card.billingAddress needs a 2-letter countryCode.")
    return CardInput(
        number=number,
        expiry=expiry,
        security_code=security_code,
        name=str(data.get("name", ""))[:300],
        billing_address={k: str(v)[:300] for k, v in address.items()},
    )


def _luhn(number: str) -> bool:
    digits = [int(d) for d in number][::-1]
    total = sum(digits[0::2]) + sum(sum(divmod(2 * d, 10)) for d in digits[1::2])
    return total % 10 == 0


def _paypal_address(data: dict[str, str]) -> Address | None:
    if not data:
        return None
    optional = {
        "address_line_1": data.get("line1"),
        "address_line_2": data.get("line2"),
        "admin_area_2": data.get("city"),
        "admin_area_1": data.get("state"),
        "postal_code": data.get("postcode"),
    }
    return Address(country_code=data["countryCode"].upper(), **{k: v for k, v in optional.items() if v})


def _card_request(card: CardInput) -> CardRequest:
    fields: dict[str, Any] = {"number": card.number, "expiry": card.expiry, "security_code": card.security_code}
    if card.name:
        fields["name"] = card.name
    address = _paypal_address(card.billing_address)
    if address is not None:
        fields["billing_address"] = address
    return CardRequest(**fields)


def read_pay(result: PayPalOrder | OrderAuthorizeResponse) -> Answer:
    """create_order / authorize_order: the authorization if there is one, else the order envelope."""
    units = present(result.purchase_units) or []
    unit = units[0] if units else None
    payments = present(unit.payments) if unit else None
    authorizations = (present(payments.authorizations) if payments else None) or []
    if isinstance(result, PayPalOrder):
        order_source = present(result.payment_source)
        card = present(order_source.card) if order_source else None
    else:
        authorize_source = present(result.payment_source)
        card = present(authorize_source.card) if authorize_source else None
    data: dict[str, Any] = {"paypal_order_id": present(result.id)}
    if card is not None:
        brand = present(card.brand)
        data.update(card_brand=str(brand) if brand else "", card_last_digits=present(card.last_digits) or "")
    if authorizations:
        authorization = authorizations[-1]
        amount = present(authorization.amount)
        data["expiration_time"] = present(authorization.expiration_time)
        return Answer(
            present(authorization.id),
            ("authorization", present(authorization.status)),
            parse_time(authorization.create_time),
            amount.value if amount else None,
            amount.currency_code if amount else None,
            data,
        )
    unit_amount = present(unit.amount) if unit else None
    return Answer(
        present(result.id),
        ("order", present(result.status)),
        parse_time(result.create_time),
        unit_amount.value if unit_amount else None,
        unit_amount.currency_code if unit_amount else None,
        data,
    )


def read_authorization(result: PaymentAuthorization) -> Answer:
    amount = present(result.amount)
    return Answer(
        present(result.id),
        present(result.status),
        parse_time(result.create_time),
        amount.value if amount else None,
        amount.currency_code if amount else None,
        {"expiration_time": present(result.expiration_time)},
    )


def read_void(result: PaymentAuthorization) -> Answer:
    return Answer(present(result.id), present(result.status), parse_time(result.update_time) or parse_time(result.create_time))


def read_capture(result: CapturedPayment) -> Answer:
    amount = present(result.amount)
    breakdown = present(result.seller_receivable_breakdown)
    data: dict[str, Any] = {}
    if breakdown is not None:
        fee, net = present(breakdown.paypal_fee), present(breakdown.net_amount)
        data = {
            "gross": breakdown.gross_amount.value,
            "fee": fee.value if fee else None,
            "net": net.value if net else None,
        }
    return Answer(
        present(result.id),
        present(result.status),
        parse_time(result.create_time),
        amount.value if amount else None,
        amount.currency_code if amount else None,
        data,
    )


def read_refund(result: Refund) -> Answer:
    amount = present(result.amount)
    return Answer(
        present(result.id),
        present(result.status),
        parse_time(result.create_time),
        amount.value if amount else None,
        amount.currency_code if amount else None,
    )


def _refresh_pay(record: ProviderWrite) -> Answer:
    client = gateway.get_client()
    if record.provider_status.startswith("authorization:"):
        a = read_authorization(client.payments.get_authorized_payment(record.provider_id))
        return a._replace(status=("authorization", a.status))
    return read_pay(client.orders.get_order(record.provider_id))


PAID_STATES = (
    OrderPayment.AUTHORIZED,
    OrderPayment.CAPTURE_PENDING,
    OrderPayment.CAPTURED,
    OrderPayment.PARTIALLY_REFUNDED,
    OrderPayment.REFUNDED,
)


def pay_order(user: Any, number: str, payload: dict[str, Any]) -> tuple[str, OrderPayment, ProviderWrite | None]:
    order = owned_order(user, number)
    has_card, has_saved = "card" in payload, "paymentMethodId" in payload
    if has_card == has_saved:
        raise ApiProblem(400, "invalid_payment_source", "Send either card or paymentMethodId, not both.")
    card = parse_card(payload["card"]) if has_card else None
    saved = _usable_card(user, payload["paymentMethodId"]) if has_saved else None

    payment = payment_for(order)
    if payment.state in PAID_STATES:
        return ProviderWrite.DONE, payment, None
    if order.status != AWAITING_PAYMENT:
        raise ApiProblem(409, "not_payable", "Order %s is %s and cannot be paid." % (number, order.status))

    attempt = _current_attempt(order, payment)
    create_ref = _ref("order", order.number, "a%d" % attempt, "create")
    authorize_ref = _ref("order", order.number, "a%d" % attempt, "authorize")
    code = order.currency
    amount = gateway.quantize(order.total_incl_tax, code)
    invoice_id = "%s-%s-%d" % (InstallIdentity.prefix(), order.number, attempt)
    if card is not None:
        payment_source = PaymentSource(card=_card_request(card))
    else:
        assert saved is not None
        payment_source = PaymentSource(card=CardRequest(vault_id=saved.vault_token_id))
    client = gateway.get_client()

    def create(request_id: str) -> PayPalOrder:
        body = OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[
                PurchaseUnitRequest(
                    amount=AmountWithBreakdown(currency_code=code, value=gateway.money_str(amount, code)),
                    invoice_id=invoice_id,
                    custom_id=request_id,
                    description="Order %s" % order.number,
                )
            ],
            payment_source=payment_source,
        )
        return client.orders.create_order(body, pay_pal_request_id=request_id, prefer=gateway.REPRESENTATION)

    try:
        write = gateway.safe_write(
            ref=create_ref,
            operation=ProviderWrite.AUTHORIZE,
            order=order,
            send=create,
            read=read_pay,
            outcome_of=gateway.pay_outcome,
            dedup_window=gateway.ORDERS_DEDUP_WINDOW,
            sent=(amount, code),
            refresh=_refresh_pay,
            claim_data={"attempt": attempt, "saved_card": str(saved.pk) if saved else None},
        )
        if write.outcome == ProviderWrite.PENDING and write.provider_status.startswith("order:"):
            paypal_order_id = write.provider_id
            write = gateway.safe_write(
                ref=authorize_ref,
                operation=ProviderWrite.ORDER_AUTHORIZE,
                order=order,
                send=lambda rid: client.orders.authorize_order(
                    paypal_order_id, pay_pal_request_id=rid, prefer=gateway.REPRESENTATION
                ),
                read=read_pay,
                outcome_of=gateway.pay_outcome,
                dedup_window=gateway.ORDERS_DEDUP_WINDOW,
                sent=(amount, code),
                refresh=_refresh_pay,
                claim_data={"attempt": attempt, "paypal_order_id": paypal_order_id},
            )
    except ProviderError as e:
        OrderPayment.objects.filter(pk=payment.pk).update(last_error=e.message[:1000])
        raise
    payment = _apply_pay(payment.pk, write, saved)
    return write.outcome, payment, write


def _usable_card(user: Any, card_id: Any) -> SavedCard:
    pk = _uuid_or_none(card_id)
    card = SavedCard.objects.filter(pk=pk, user=user, removed_at__isnull=True).first() if pk else None
    if card is None:
        raise ApiProblem(404, "payment_method_not_found", "No such saved card.")
    return card


def _uuid_or_none(value: Any) -> str | None:
    text = str(value)
    return text if re.fullmatch(r"[0-9a-fA-F-]{32,36}", text) else None


def _current_attempt(order: Any, payment: OrderPayment) -> int:
    """The attempt to pay under; a new one only after the last one definitively failed or expired."""
    attempt = payment.attempt
    refs = [_ref("order", order.number, "a%d" % attempt, step) for step in ("create", "authorize")]
    writes = list(ProviderWrite.objects.filter(ref__in=refs))
    if any(w.outcome == ProviderWrite.NEEDS_REVIEW for w in writes):
        raise ApiProblem(409, "needs_review", "The last payment attempt needs review by an operator before paying again.")
    finished = payment.state == OrderPayment.AUTHORIZATION_EXPIRED or (
        writes and all(w.outcome == ProviderWrite.FAILED for w in writes)
    )
    if finished:
        OrderPayment.objects.filter(pk=payment.pk, attempt=attempt).update(
            attempt=attempt + 1, state=OrderPayment.AWAITING_PAYMENT
        )
        payment.refresh_from_db()
    return payment.attempt


def _apply_pay(payment_pk: int, write: ProviderWrite, saved: SavedCard | None) -> OrderPayment:
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().select_related("order").get(pk=payment_pk)
        if write.outcome == ProviderWrite.DONE:
            if payment.authorization_id == write.provider_id or payment.state in PAID_STATES:
                return payment
            order = payment.order
            amount = write.amount or Decimal("0")
            data = write.data
            payment.paypal_order_id = data.get("paypal_order_id") or ""
            payment.authorization_id = write.provider_id
            payment.authorization_status = write.provider_status.split(":")[-1]
            payment.authorized_amount = amount
            payment.authorization_created_at = write.provider_time
            payment.original_authorization_at = write.provider_time
            payment.authorization_expires_at = parse_time(data.get("expiration_time"))
            payment.reauthorized = False
            payment.card_brand = data.get("card_brand", "") or (saved.brand if saved else "")
            payment.card_last_digits = data.get("card_last_digits", "") or (saved.last_digits if saved else "")
            payment.saved_card = saved
            payment.state = OrderPayment.AUTHORIZED
            payment.last_error = ""
            source = Source.objects.create(
                order=order,
                source_type=_source_type(),
                currency=payment.currency,
                reference=payment.paypal_order_id,
                label=("%s ending %s" % (payment.card_brand, payment.card_last_digits)).strip(),
            )
            source.allocate(amount, reference=write.provider_id, status=payment.authorization_status)
            payment.source = source
            _event(order, "Authorised", amount, write.provider_id)
            order.set_status(PAYMENT_AUTHORISED)
        elif write.outcome in (ProviderWrite.PENDING, ProviderWrite.SENDING):
            payment.state = OrderPayment.AUTHORIZATION_PENDING
        elif write.outcome == ProviderWrite.FAILED:
            payment.state = OrderPayment.AWAITING_PAYMENT
            payment.last_error = _pay_failure_message(write)
        payment.save()
        return payment


def _pay_failure_message(write: ProviderWrite) -> str:
    if write.provider_status == "order:PAYER_ACTION_REQUIRED":
        return (
            "PayPal requires the shopper to approve this card payment in a browser (3-D Secure). "
            "That approval step is not supported by this API."
        )
    status = write.provider_status.split(":")[-1]
    return write.detail or ("PayPal did not authorize the payment (status %s)." % (status or "unknown"))


# ======
# Fulfil
# ======


class OperatorActionRequired(ApiProblem):
    pass


def fulfil_order(number: str) -> tuple[str, OrderPayment, ProviderWrite | None]:
    order = any_order(number)
    payment = payment_for(order)
    if payment.state in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED):
        return ProviderWrite.DONE, payment, None
    if payment.state not in (OrderPayment.AUTHORIZED, OrderPayment.CAPTURE_PENDING) or not payment.authorization_id:
        raise ApiProblem(
            409, "not_fulfillable",
            "Order %s cannot be fulfilled: its payment is %s (order status %s)."
            % (number, payment.get_state_display().lower(), order.status),
        )
    code = payment.currency
    amount = payment.authorized_amount or gateway.quantize(order.total_incl_tax, code)
    authorization_id = payment.authorization_id
    capture_ref = _ref("order", order.number, "a%d" % payment.attempt, "capture", authorization_id)
    if not ProviderWrite.objects.filter(ref=capture_ref).exists():
        authorization_id = _ensure_fresh_authorization(order, payment, amount)
        capture_ref = _ref("order", order.number, "a%d" % payment.attempt, "capture", authorization_id)

    client = gateway.get_client()
    invoice_id = "%s-%s-%d" % (InstallIdentity.prefix(), order.number, payment.attempt)
    write = gateway.safe_write(
        ref=capture_ref,
        operation=ProviderWrite.CAPTURE,
        order=order,
        send=lambda rid: client.payments.capture_authorized_payment(
            authorization_id,
            pay_pal_request_id=rid,
            prefer=gateway.REPRESENTATION,
            body=CaptureRequest(amount=_money(amount, code), final_capture=True, invoice_id=invoice_id),
        ),
        read=read_capture,
        outcome_of=gateway.capture_outcome,
        dedup_window=gateway.PAYMENTS_DEDUP_WINDOW,
        sent=(amount, code),
        refresh=lambda record: read_capture(client.payments.get_captured_payment(record.provider_id)),
    )
    if write.outcome == ProviderWrite.DONE and not write.data.get("fee"):
        # The fee breakdown can lag the capture itself: read it back once.
        try:
            got = read_capture(gateway.call(lambda: client.payments.get_captured_payment(write.provider_id), retries=1))
            write.data = {**write.data, **got.data}
            write.save(update_fields=["data"])
        except ProviderError:
            logger.warning("Capture %s has no fee breakdown yet", write.provider_id)
    return write.outcome, _apply_capture(payment.pk, write), write


def _ensure_fresh_authorization(order: Any, payment: OrderPayment, amount: Decimal) -> str:
    """
    Return an authorization id that can be captured now, renewing a stale one.

    PayPal honors a card authorization for three days; after that it must be
    reauthorized - once, between day 4 and day 29 of the original
    authorization. Anything else can no longer be renewed.
    """
    client = gateway.get_client()
    authorization_id = payment.authorization_id
    current = gateway.call(lambda: client.payments.get_authorized_payment(authorization_id), retries=2)
    status = present(current.status)
    now = timezone.now()
    created = parse_time(current.create_time) or payment.authorization_created_at
    original = payment.original_authorization_at or created
    expires = parse_time(current.expiration_time) or payment.authorization_expires_at

    if status in (AuthorizationStatus.CAPTURED, AuthorizationStatus.PARTIALLY_CAPTURED):
        return authorization_id  # capture below resends under its own PayPal-Request-Id
    if status in (AuthorizationStatus.VOIDED, AuthorizationStatus.DENIED):
        _authorization_not_renewable(order, payment, "PayPal reports the authorization as %s" % status)
    if status != AuthorizationStatus.CREATED:
        raise ApiProblem(
            409, "authorization_not_capturable",
            "Authorization %s is %s at PayPal; try fulfilment again once PayPal has finished processing it."
            % (authorization_id, status or "in an unknown state"),
        )
    if expires is not None and now >= expires:
        _authorization_not_renewable(order, payment, "it expired at %s" % expires.isoformat())
    if created is None or now <= created + HONOR_PERIOD:
        return authorization_id
    if original is not None and now > original + AUTHORIZATION_PERIOD:
        _authorization_not_renewable(order, payment, "it is more than 29 days old (authorized %s)" % original.isoformat())
    if payment.reauthorized:
        _authorization_not_renewable(order, payment, "its honor period has lapsed and PayPal allows only one reauthorization, already used")

    code = payment.currency
    try:
        write = gateway.safe_write(
            ref=_ref("order", order.number, "a%d" % payment.attempt, "reauthorize", authorization_id),
            operation=ProviderWrite.REAUTHORIZE,
            order=order,
            send=lambda rid: client.payments.reauthorize_payment(
                authorization_id,
                pay_pal_request_id=rid,
                prefer=gateway.REPRESENTATION,
                body=ReauthorizeRequest(amount=_money(amount, code)),
            ),
            read=read_authorization,
            outcome_of=gateway.authorization_outcome,
            dedup_window=gateway.PAYMENTS_DEDUP_WINDOW,
            sent=(amount, code),
            refresh=lambda record: read_authorization(client.payments.get_authorized_payment(record.provider_id)),
        )
    except ProviderError as e:
        if e.status_code in (400, 409, 422) and not e.outcome_unknown:
            _authorization_not_renewable(order, payment, "PayPal refused to reauthorize it (%s)" % (e.issue or e.message))
        raise
    if write.outcome == ProviderWrite.FAILED:
        _authorization_not_renewable(order, payment, "PayPal's reauthorization came back %s" % (write.provider_status or "failed"))
    if write.outcome != ProviderWrite.DONE:
        raise ApiProblem(
            504 if write.outcome == ProviderWrite.UNKNOWN else 202, "reauthorization_" + write.outcome,
            "Renewing the stale authorization is %s at PayPal; repeat the fulfilment to continue." % write.outcome,
        )
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        if locked.authorization_id == authorization_id:
            locked.authorization_id = write.provider_id
            locked.authorization_status = write.provider_status
            locked.authorization_created_at = write.provider_time or now
            locked.authorization_expires_at = parse_time(write.data.get("expiration_time")) or locked.authorization_expires_at
            locked.reauthorized = True
            locked.save()
            if locked.source_id:
                Transaction.objects.create(
                    source=locked.source, txn_type="Reauthorise", amount=amount,
                    reference=write.provider_id, status=write.provider_status,
                )
        payment.refresh_from_db()
    return payment.authorization_id


def _authorization_not_renewable(order: Any, payment: OrderPayment, reason: str) -> None:
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        locked.state = OrderPayment.AUTHORIZATION_EXPIRED
        locked.last_error = "Authorization %s cannot be renewed: %s." % (locked.authorization_id, reason)
        locked.save()
        order.set_status(AWAITING_PAYMENT)
    raise OperatorActionRequired(
        409,
        "authorization_not_renewable",
        "Cannot fulfil order %s: the card authorization %s can no longer be renewed because %s. "
        "No money has been taken. The order is back to 'Awaiting payment': ask the shopper to pay again "
        "(POST /api/orders/%s/pay), then fulfil it." % (order.number, payment.authorization_id, reason, order.number),
    )


def _apply_capture(payment_pk: int, write: ProviderWrite) -> OrderPayment:
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().select_related("order").get(pk=payment_pk)
        if write.outcome == ProviderWrite.DONE:
            if payment.capture_id == write.provider_id and payment.state != OrderPayment.CAPTURE_PENDING:
                return payment
            order = payment.order
            amount = write.amount or Decimal("0")
            payment.capture_id = write.provider_id
            payment.capture_status = write.provider_status
            payment.captured_amount = amount
            payment.paypal_fee = _dec(write.data.get("fee"))
            payment.net_amount = _dec(write.data.get("net"))
            payment.captured_at = write.provider_time
            payment.state = OrderPayment.CAPTURED
            if payment.source is not None:
                payment.source.debit(amount, reference=write.provider_id, status=write.provider_status)
            _event(order, "Settled", amount, write.provider_id)
            lines = list(order.lines.all())
            EventHandler().consume_stock_allocations(order, lines, [line.quantity for line in lines])
            order.set_status(FULFILLED)
        elif write.outcome in (ProviderWrite.PENDING, ProviderWrite.SENDING):
            payment.state = OrderPayment.CAPTURE_PENDING
            payment.capture_id = write.provider_id or payment.capture_id
            payment.capture_status = write.provider_status
        elif write.outcome == ProviderWrite.FAILED:
            payment.last_error = write.detail or "PayPal did not capture the payment (%s)." % write.provider_status
        payment.save()
        return payment


# ======
# Cancel
# ======


def cancel_order(number: str) -> tuple[str, OrderPayment, ProviderWrite | None]:
    order = any_order(number)
    payment = payment_for(order)
    if payment.state in (OrderPayment.VOIDED, OrderPayment.CANCELLED):
        return ProviderWrite.DONE, payment, None
    if payment.capture_id or payment.state in (OrderPayment.CAPTURED, OrderPayment.CAPTURE_PENDING,
                                               OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED):
        raise ApiProblem(409, "already_fulfilled", "Order %s has been captured; refund it instead." % number)
    attempt_refs = [_ref("order", order.number, "a%d" % payment.attempt, s) for s in ("create", "authorize")]
    if ProviderWrite.objects.filter(ref__in=attempt_refs, outcome__in=(
            ProviderWrite.SENDING, ProviderWrite.UNKNOWN, ProviderWrite.PENDING, ProviderWrite.NEEDS_REVIEW)).exists():
        raise ApiProblem(
            409, "payment_unresolved",
            "A payment for order %s is still unresolved at PayPal; repeat the pay request to settle it, then cancel." % number,
        )

    if payment.state != OrderPayment.AUTHORIZED:
        # Nothing is held at PayPal: cancel the order only.
        with transaction.atomic():
            locked = OrderPayment.objects.select_for_update().get(pk=payment.pk)
            locked.state = OrderPayment.CANCELLED
            locked.save()
            _cancel_order_status(order)
        return ProviderWrite.DONE, locked, None

    client = gateway.get_client()
    authorization_id = payment.authorization_id
    write = gateway.safe_write(
        ref=_ref("order", order.number, "a%d" % payment.attempt, "void", authorization_id),
        operation=ProviderWrite.VOID,
        order=order,
        send=lambda rid: client.payments.void_payment(
            authorization_id, pay_pal_request_id=rid, prefer=gateway.REPRESENTATION
        ),
        read=read_void,
        outcome_of=gateway.void_outcome,
        dedup_window=gateway.PAYMENTS_DEDUP_WINDOW,
        refresh=lambda record: read_void(client.payments.get_authorized_payment(record.provider_id)),
    )
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        if write.outcome == ProviderWrite.DONE and locked.state == OrderPayment.AUTHORIZED:
            locked.state = OrderPayment.VOIDED
            locked.authorization_status = write.provider_status
            if locked.source is not None:
                Transaction.objects.create(
                    source=locked.source, txn_type="Void", amount=locked.source.amount_allocated,
                    reference=write.provider_id, status=write.provider_status,
                )
                Source.objects.filter(pk=locked.source_id).update(amount_allocated=Decimal("0.00"))
            _event(order, "Voided", locked.authorized_amount or Decimal("0"), write.provider_id)
            _cancel_order_status(order)
        elif write.outcome == ProviderWrite.FAILED:
            locked.last_error = write.detail or "PayPal did not release the hold (%s)." % write.provider_status
        locked.save()
    return write.outcome, locked, write


def _cancel_order_status(order: Any) -> None:
    lines = list(order.lines.all())
    EventHandler().cancel_stock_allocations(order, lines, [line.quantity for line in lines])
    order.set_status(CANCELLED)


# =======
# Refunds
# =======


def refund_order(user: Any, number: str, key: str, payload: dict[str, Any]) -> tuple[str, PayPalRefund, ProviderWrite | None]:
    order = owned_order(user, number)
    if not key or len(key) > 128:
        raise ApiProblem(400, "idempotency_key_required", "Send an Idempotency-Key header (1-128 characters).")
    payment = payment_for(order)
    if not payment.capture_id or payment.state not in (
        OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED, OrderPayment.REFUNDED
    ):
        raise ApiProblem(409, "not_refundable", "Order %s has not been fulfilled, so there is nothing to refund; cancel it instead." % number)
    code = payment.currency
    requested: Decimal | None = None
    if payload.get("amount") is not None:
        try:
            requested = Decimal(str(payload["amount"]))
        except InvalidOperation:
            raise ApiProblem(400, "invalid_amount", "amount must be a decimal string such as \"5.00\".") from None
        if not requested.is_finite() or requested <= 0 or gateway.quantize(requested, code) != requested:
            raise ApiProblem(400, "invalid_amount", "amount must be positive with at most the currency's decimal places.")

    refund = _reserve_refund(payment.pk, key, requested)
    if refund.outcome not in (PayPalRefund.RESERVED, ProviderWrite.SENDING, ProviderWrite.UNKNOWN, ProviderWrite.PENDING):
        return refund.outcome, refund, refund.write  # a repeat of a settled refund: answer from it

    client = gateway.get_client()
    capture_id = payment.capture_id
    amount = refund.amount
    try:
        write = gateway.safe_write(
            ref=_ref("order", order.number, "refund", key),
            operation=ProviderWrite.REFUND,
            order=order,
            send=lambda rid: client.payments.refund_captured_payment(
                capture_id,
                pay_pal_request_id=rid,
                prefer=gateway.REPRESENTATION,
                body=RefundRequest(amount=_money(amount, code), custom_id=rid),
            ),
            read=read_refund,
            outcome_of=gateway.refund_outcome,
            dedup_window=gateway.PAYMENTS_DEDUP_WINDOW,
            sent=(amount, code),
            refresh=lambda record: read_refund(client.payments.get_refund(record.provider_id)),
            claim_data={"idempotency_key": key},
        )
    except ProviderError:
        _sync_refund(refund.pk, _ref("order", order.number, "refund", key))
        raise
    return write.outcome, _sync_refund(refund.pk, write.ref), write


def _reserve_refund(payment_pk: int, key: str, requested: Decimal | None) -> PayPalRefund:
    """Hold part of the refundable balance for this key, serialised against every other refund of the capture."""
    with transaction.atomic():
        # Write first so this transaction owns the row (and SQLite's write lock) before it reads totals.
        OrderPayment.objects.filter(pk=payment_pk).update(updated_at=timezone.now())
        payment = OrderPayment.objects.select_for_update().get(pk=payment_pk)
        existing = PayPalRefund.objects.filter(payment=payment, idempotency_key=key).first()
        if existing is not None:
            if requested is not None and requested != existing.amount:
                raise ApiProblem(
                    409, "idempotency_key_reused",
                    "Idempotency-Key %r was already used for a refund of %s %s." % (key, existing.amount, existing.currency),
                )
            released = existing.outcome == ProviderWrite.FAILED and existing.write_id is None
            if not released:
                return existing
        others = payment.refunds.exclude(outcome__in=PayPalRefund.RELEASED_OUTCOMES)
        if existing is not None:
            others = others.exclude(pk=existing.pk)
        held = sum((r.amount for r in others), Decimal("0.00"))
        remaining = (payment.captured_amount or Decimal("0.00")) - held
        amount = requested if requested is not None else (existing.amount if existing else remaining)
        if remaining <= 0:
            raise ApiProblem(409, "fully_refunded", "Order %s has nothing left to refund." % payment.order.number)
        if amount > remaining:
            raise ApiProblem(
                422, "refund_exceeds_captured",
                "A refund of %s would exceed the %s %s still refundable." % (amount, remaining, payment.currency),
                refundable=str(remaining),
            )
        if existing is not None:
            existing.outcome, existing.amount = PayPalRefund.RESERVED, amount
            existing.save()
            return existing
        return PayPalRefund.objects.create(
            payment=payment, idempotency_key=key, amount=amount, currency=payment.currency
        )


def _sync_refund(refund_pk: int, ref: str) -> PayPalRefund:
    write = ProviderWrite.objects.filter(ref=ref).first()
    with transaction.atomic():
        refund = PayPalRefund.objects.select_for_update().select_related("payment__order").get(pk=refund_pk)
        if write is None:  # released: PayPal never acted, the reservation is freed
            refund.outcome = ProviderWrite.FAILED
            refund.write = None
            refund.save()
            return refund
        refund.write = write
        refund.outcome = write.outcome
        refund.refund_id = write.provider_id
        refund.status = write.provider_status
        refund.provider_time = write.provider_time
        if write.outcome == ProviderWrite.DONE and not refund.applied:
            payment = OrderPayment.objects.select_for_update().get(pk=refund.payment_id)
            if payment.source is not None:
                payment.source.refund(refund.amount, reference=write.provider_id, status=write.provider_status)
            payment.refunded_amount = F("refunded_amount") + refund.amount
            payment.save(update_fields=["refunded_amount", "updated_at"])
            payment.refresh_from_db()
            payment.state = (
                OrderPayment.REFUNDED
                if payment.refunded_amount >= (payment.captured_amount or Decimal("0"))
                else OrderPayment.PARTIALLY_REFUNDED
            )
            payment.save(update_fields=["state", "updated_at"])
            _event(payment.order, "Refunded", refund.amount, write.provider_id)
            refund.applied = True
        refund.save()
        return refund


# ===========
# Saved cards
# ===========


def read_token(result: PaymentTokenResponse) -> Answer:
    source = present(result.payment_source)
    card = present(source.card) if source else None
    customer = present(result.customer)
    token_id = present(result.id)
    data: dict[str, Any] = {"customer_id": present(customer.id) if customer else None}
    if card is not None:
        brand = present(card.brand)
        data.update(brand=str(brand) if brand else "", last_digits=present(card.last_digits) or "",
                    expiry=present(card.expiry) or "")
    created = (result.model_extra or {}).get("create_time")
    return Answer(
        token_id,
        gateway.VAULTED if token_id and card is not None else None,
        parse_time(created if isinstance(created, str) else None),
        data=data,
    )


def save_card(user: Any, key: str, payload: dict[str, Any]) -> tuple[str, SavedCard | None, ProviderWrite]:
    if not key or len(key) > 128:
        raise ApiProblem(400, "idempotency_key_required", "Send an Idempotency-Key header (1-128 characters).")
    card = parse_card(payload.get("card", payload))
    ref = _ref("user", user.pk, "card", key)
    fingerprint = {"last_digits": card.last_digits, "expiry": card.expiry}
    existing = ProviderWrite.objects.filter(ref=ref).first()
    if existing is not None and existing.data.get("request") not in (None, fingerprint):
        raise ApiProblem(409, "idempotency_key_reused", "Idempotency-Key %r was already used to save a different card." % key)

    client = gateway.get_client()
    fields: dict[str, Any] = {"number": card.number, "expiry": card.expiry, "security_code": card.security_code}
    if card.name:
        fields["name"] = card.name
    address = _paypal_address(card.billing_address)
    if address is not None:
        fields["billing_address"] = address
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(**fields))
    )
    write = gateway.safe_write(
        ref=ref,
        operation=ProviderWrite.VAULT,
        send=lambda rid: client.vault.create_payment_token(body, pay_pal_request_id=rid),
        read=read_token,
        outcome_of=gateway.token_outcome,
        dedup_window=gateway.VAULT_DEDUP_WINDOW,
        claim_data={"request": fingerprint},
    )
    saved = None
    if write.outcome == ProviderWrite.DONE:
        saved = SavedCard.objects.filter(write=write).first()
        if saved is None:
            try:
                with transaction.atomic():
                    saved = SavedCard.objects.create(
                        user=user,
                        write=write,
                        vault_token_id=write.provider_id,
                        paypal_customer_id=write.data.get("customer_id") or "",
                        brand=write.data.get("brand", ""),
                        last_digits=write.data.get("last_digits", ""),
                        expiry=write.data.get("expiry", ""),
                    )
            except IntegrityError:
                saved = SavedCard.objects.get(write=write)
    return write.outcome, saved, write


def list_cards(user: Any) -> list[SavedCard]:
    return list(SavedCard.objects.filter(user=user, removed_at__isnull=True))


def delete_card(user: Any, card_id: str) -> tuple[SavedCard, str]:
    pk = _uuid_or_none(card_id)
    card = SavedCard.objects.filter(pk=pk, user=user).first() if pk else None
    if card is None or (card.removed_at is not None and card.provider_deleted_at is not None):
        raise ApiProblem(404, "payment_method_not_found", "No such saved card.")
    # Removing it locally first is what makes it unusable, whatever PayPal answers.
    SavedCard.objects.filter(pk=card.pk, removed_at__isnull=True).update(removed_at=timezone.now())
    card.refresh_from_db()
    return card, _delete_vault_token(card)


def _delete_vault_token(card: SavedCard) -> str:
    client = gateway.get_client()
    try:
        result = client.vault.with_raw_response.delete_payment_token(card.vault_token_id)
    except Exception as e:  # noqa: BLE001 - any failure leaves the provider deletion pending, never the card usable
        logger.warning("Deleting vault token for saved card %s failed: %s", card.pk, type(e).__name__)
        return "pending"
    status = result.response.status_code
    if 200 <= status < 300 or status == 404:
        SavedCard.objects.filter(pk=card.pk).update(provider_deleted_at=timezone.now())
        return "done"
    logger.warning("PayPal answered %s deleting the vault token for saved card %s", status, card.pk)
    return "pending"


# ==============
# Reconciliation
# ==============


def parse_instant(value: str | None, name: str) -> datetime:
    if not value:
        raise ApiProblem(400, "invalid_range", "%s is required (ISO-8601 date-time)." % name)
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "+"))
    except ValueError:
        raise ApiProblem(400, "invalid_range", "%s must be an ISO-8601 date-time." % name) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    if end <= start:
        raise ApiProblem(400, "invalid_range", "to must be later than from.")
    if end - start > MAX_RECONCILIATION_RANGE:
        raise ApiProblem(400, "invalid_range", "The range may cover at most %d days." % MAX_RECONCILIATION_RANGE.days)

    client = gateway.get_client()
    records: list[dict[str, Any]] = []
    last_refreshed: datetime | None = None
    pages = 0
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + SEARCH_CHUNK, end)
        page, total_pages = 1, 1
        while page <= total_pages:
            response: SearchResponse = gateway.call(
                lambda: client.transaction_search.search_transactions(
                    _rfc3339(chunk_start), _rfc3339(chunk_end),
                    balance_affecting_records_only="N", page_size=100, page=page,
                ),
                retries=2,
            )
            pages += 1
            total_pages = present(response.total_pages) or 1
            refreshed = parse_time(response.last_refreshed_datetime)
            if refreshed is not None:
                last_refreshed = refreshed if last_refreshed is None else min(last_refreshed, refreshed)
            for detail in present(response.transaction_details) or []:
                info = present(detail.transaction_info)
                if info is not None:
                    records.append(_transaction_record(info))
            page += 1
        chunk_start = chunk_end

    # Our side, on PayPal's clock: the provider time stored when each write completed.
    local = list(
        ProviderWrite.objects.filter(provider_time__gte=start, provider_time__lt=end)
        .exclude(operation=ProviderWrite.VAULT).exclude(provider_id="").select_related("order")
    )
    unsettled = list(
        ProviderWrite.objects.filter(created_at__gte=start, created_at__lt=end, provider_time__isnull=True)
        .exclude(operation=ProviderWrite.VAULT).select_related("order")
    )
    by_id: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_id.setdefault(record["transactionId"], []).append(record)
    request_ids = {w.request_id: w for w in local}
    matched, local_only, not_yet_reported = [], [], []
    for write in local:
        found = by_id.pop(write.provider_id, [])
        if found:
            matched.append({"local": _write_summary(write), "paypal": found})
        elif last_refreshed is not None and write.provider_time and write.provider_time > last_refreshed:
            not_yet_reported.append(_write_summary(write))
        else:
            local_only.append(_write_summary(write))
    provider_only = []
    for found in by_id.values():
        for record in found:
            owner = request_ids.get(record.get("customField") or "")
            if owner is not None:
                matched.append({"local": _write_summary(owner), "paypal": [record]})
            else:
                provider_only.append(record)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalLastRefreshedAt": last_refreshed.isoformat() if last_refreshed else None,
        "pagesFetched": pages,
        "paypalTransactionCount": len(records),
        "summary": {
            "matched": len(matched),
            "paypalOnly": len(provider_only),
            "localOnly": len(local_only),
            "notYetReportedByPayPal": len(not_yet_reported),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "paypalOnly": provider_only,
        "localOnly": local_only,
        "notYetReportedByPayPal": not_yet_reported,
        "unsettled": [_write_summary(w) for w in unsettled],
    }


def _rfc3339(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _transaction_record(info: Any) -> dict[str, Any]:
    amount, fee = present(info.transaction_amount), present(info.fee_amount)
    return {
        "transactionId": present(info.transaction_id) or "",
        "referenceId": present(info.paypal_reference_id),
        "eventCode": present(info.transaction_event_code),
        "status": present(info.transaction_status),
        "initiatedAt": present(info.transaction_initiation_date),
        "amount": amount.value if amount else None,
        "fee": fee.value if fee else None,
        "currency": amount.currency_code if amount else None,
        "invoiceId": present(info.invoice_id),
        "customField": present(info.custom_field),
    }


def _write_summary(write: ProviderWrite) -> dict[str, Any]:
    return {
        "orderId": str(write.order.number) if write.order else None,
        "operation": write.operation,
        "outcome": write.outcome,
        "paypalId": write.provider_id or None,
        "paypalStatus": write.provider_status or None,
        "amount": str(write.amount) if write.amount is not None else None,
        "currency": write.currency or None,
        "paypalTime": write.provider_time.isoformat() if write.provider_time else None,
        "requestedAt": write.created_at.isoformat(),
    }
