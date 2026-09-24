"""
Order, payment and saved-card operations behind the payments API.

Each PayPal write goes through ``safe_write`` with its own claim reference;
reads are guarded by the same error ladder. Card numbers, expiry dates and
security codes exist only in the request being handled and in the request to
PayPal - they are never stored or logged.
"""

import hashlib
import hmac
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.apps.order.utils import OrderCreator
from oscar.apps.partner.strategy import Selector
from oscar.apps.shipping.methods import NoShippingRequired
from oscar.core.loading import get_model
from oscar.core.prices import Price
from paypal.core import ApiError, Failure, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Customer,
    Money,
    Order as PaypalOrder,
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
)
from paypal.models.enums import AuthorizationStatus, CheckoutPaymentIntent, OrderStatus

from . import money, outcomes
from .errors import (
    BadRequest,
    Conflict,
    NotFound,
    NotRenewable,
    OutcomeUnknown,
    PaymentError,
    ProviderRejected,
    paypal_issues,
    translate_failure,
)
from .models import PaypalCustomer, PaypalPayment, ProviderWrite, SavedCard
from .paypal_client import get_client
from .safe_write import Answer, WriteResult, next_attempt, parse_time, reference, safe_write

logger = logging.getLogger("apps.payments")

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")

T = TypeVar("T")

STATUS_AWAITING_PAYMENT = "Awaiting payment"
STATUS_AUTHORIZED = "Payment authorized"
STATUS_COMPLETE = "Complete"
STATUS_CANCELLED = "Cancelled"

# ReauthorizeRequest (PayPal SDK): a 3-day honor period; one reauthorization
# allowed from day 4 to day 29 of the original authorization.
HONOR_PERIOD = timedelta(days=3)
REAUTHORIZE_LIMIT = timedelta(days=29)

MAX_ORDER_LINES = 50
MAX_LINE_QUANTITY = 100

PREFER = "return=representation"


@dataclass(frozen=True)
class Outcome:
    """What a service call did: the HTTP status and the object to present."""

    status_code: int
    obj: Any


def currency() -> str:
    code = str(getattr(settings, "PAYPAL_CURRENCY", "") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ImproperlyConfigured("PAYPAL_CURRENCY must be set to a three-letter ISO-4217 currency code.")
    return code


def order_reference(order: Any) -> str:
    """This install's reference for an order, sent to PayPal as custom_id."""
    return f"{settings.PAYPAL_REFERENCE_PREFIX}-{order.number}"


def _call(fn: Callable[[], T], *, payment_write: bool = False) -> T:
    """Run a PayPal read (or an unclaimed call) through the error ladder."""
    try:
        return fn()
    except PaymentError:
        raise
    except Exception as exc:
        raise translate_failure(exc, payment_write=payment_write) from exc


def _write(fn: Callable[[], WriteResult[T]], *, payment_write: bool = True) -> WriteResult[T]:
    """Run a safe write through the error ladder."""
    try:
        return fn()
    except PaymentError:
        raise
    except Exception as exc:
        raise translate_failure(exc, payment_write=payment_write) from exc


def _set(value: object) -> Any:
    """An SDK member, with UNSET resolved to None."""
    return None if isinstance(value, UnsetType) else value


def _money_value(m: object) -> tuple[str | None, str | None]:
    if isinstance(m, Money):
        return m.value, m.currency_code
    return None, None


def _decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


# --- orders -------------------------------------------------------------------


def _parse_items(payload: dict[str, Any]) -> list[tuple[int, int]]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise BadRequest('"items" must be a non-empty list of {"productId", "quantity"}.')
    if len(items) > MAX_ORDER_LINES:
        raise BadRequest(f"An order may have at most {MAX_ORDER_LINES} lines.")
    parsed: dict[int, int] = {}
    for item in items:
        if not isinstance(item, dict):
            raise BadRequest('Each item must be an object with "productId" and "quantity".')
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            raise BadRequest('"productId" must be an integer catalogue product id.')
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_LINE_QUANTITY:
            raise BadRequest(f'"quantity" must be an integer from 1 to {MAX_LINE_QUANTITY}.')
        parsed[product_id] = parsed.get(product_id, 0) + quantity
    return list(parsed.items())


def place_order(user: Any, payload: dict[str, Any]) -> Outcome:
    """Create an Oscar order from catalogue items; it starts awaiting payment."""
    items = _parse_items(payload)
    cur = currency()
    strategy = Selector().strategy(user=user)
    with transaction.atomic():
        # A private basket (no owner) so the shopper's own session basket is untouched.
        basket = Basket.objects.create(owner=None)
        basket.strategy = strategy
        for product_id, quantity in items:
            product = Product.objects.filter(pk=product_id).first()
            if product is None or product.is_parent:
                raise BadRequest(f"Product {product_id} is not a purchasable catalogue item.")
            info = strategy.fetch_for_product(product)
            if not info.price.exists or not info.price.is_tax_known:
                raise BadRequest(f"Product {product_id} has no price.")
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise Conflict(f"Product {product_id}: {reason}")
            basket.add_product(product, quantity)
        basket.freeze()

        total_incl = basket.total_incl_tax
        if total_incl != money.quantize(total_incl, cur):
            raise BadRequest(f"Catalogue prices cannot be charged in {cur}.")
        if total_incl <= 0:
            raise BadRequest("The order total must be greater than zero.")
        total = Price(currency=cur, excl_tax=basket.total_excl_tax, incl_tax=total_incl)
        shipping_method = NoShippingRequired()
        shipping_charge = Price(currency=cur, excl_tax=Decimal("0.00"), incl_tax=Decimal("0.00"))
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            status=STATUS_AWAITING_PAYMENT,
        )
        basket.submit()
        PaypalPayment.objects.create(order=order, amount=order.total_incl_tax, currency=cur)
    logger.info("Order %s placed by user %s for %s %s", order.number, user.pk, order.total_incl_tax, cur)
    return Outcome(201, order)


def get_owned_order(user: Any, number: str) -> Any:
    order = Order.objects.filter(number=number, user=user).select_related("paypal_payment").first()
    if order is None or not hasattr(order, "paypal_payment"):
        raise NotFound("No such order.")
    return order


def get_any_order(number: str) -> Any:
    order = Order.objects.filter(number=number).select_related("paypal_payment").first()
    if order is None or not hasattr(order, "paypal_payment"):
        raise NotFound("No such order.")
    return order


def orders_for(user: Any) -> Any:
    return (
        Order.objects.filter(user=user, paypal_payment__isnull=False)
        .select_related("paypal_payment", "paypal_payment__saved_card")
        .prefetch_related("lines")
        .order_by("-date_placed")
    )


def _release_stock(order: Any) -> None:
    for line in order.lines.all():
        if line.stockrecord is not None and line.product and line.product.get_product_class().track_stock:
            line.stockrecord.cancel_allocation(line.quantity)


def _consume_stock(order: Any) -> None:
    for line in order.lines.all():
        if line.stockrecord is not None and line.product and line.product.get_product_class().track_stock:
            if line.stockrecord.is_allocation_consumption_possible(line.quantity):
                line.stockrecord.consume_allocation(line.quantity)


def _source_for(payment: PaypalPayment) -> Any:
    if payment.source_id:
        return payment.source
    source_type, _ = SourceType.objects.get_or_create(name="PayPal")
    source = Source.objects.create(
        order=payment.order,
        source_type=source_type,
        currency=payment.currency,
        reference=payment.paypal_order_id,
        label=payment.card_label,
    )
    payment.source = source
    return source


# --- card input ---------------------------------------------------------------

_EXPIRY = re.compile(r"^(\d{4})-(\d{2})$")


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


@dataclass(frozen=True)
class CardInput:
    number: str
    expiry: str
    security_code: str
    name: str | None
    address: Address | None

    def __repr__(self) -> str:  # never print card data
        return "CardInput(<redacted>)"


@sensitive_variables("raw", "number", "security_code")
def parse_card(raw: object) -> CardInput:
    if not isinstance(raw, dict):
        raise BadRequest('"card" must be an object with "number", "expiry" and "securityCode".')
    number = re.sub(r"[\s-]", "", str(raw.get("number", "")))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise BadRequest("The card number is not valid.")
    expiry = str(raw.get("expiry", "")).strip()
    match = _EXPIRY.match(expiry)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise BadRequest('"expiry" must be in YYYY-MM format.')
    today = date.today()
    if (int(match.group(1)), int(match.group(2))) < (today.year, today.month):
        raise BadRequest("The card has expired.")
    security_code = str(raw.get("securityCode", "")).strip()
    if not security_code.isdigit() or len(security_code) not in (3, 4):
        raise BadRequest('"securityCode" must be 3 or 4 digits.')
    name = raw.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 300):
        raise BadRequest('"name" must be a string of at most 300 characters.')
    return CardInput(number, expiry, security_code, name or None, _parse_address(raw.get("billingAddress")))


def _parse_address(raw: object) -> Address | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadRequest('"billingAddress" must be an object.')
    country = str(raw.get("countryCode", "")).strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise BadRequest('"billingAddress.countryCode" must be a two-letter country code.')
    fields = {
        "address_line_1": raw.get("addressLine1"),
        "address_line_2": raw.get("addressLine2"),
        "admin_area_2": raw.get("city"),
        "admin_area_1": raw.get("state"),
        "postal_code": raw.get("postalCode"),
    }
    kwargs: dict[str, Any] = {k: str(v) for k, v in fields.items() if v not in (None, "")}
    return Address(country_code=country, **kwargs)


def card_fingerprint(user: Any, card: CardInput) -> str:
    """Keyed, non-reversible identity of (shopper, card) - never the card itself."""
    message = f"{user.pk}|{card.number}|{card.expiry}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def _card_label(brand: object, last_digits: object) -> str:
    brand_text = outcomes.status_text(brand) or "Card"
    return f"{brand_text} ending {last_digits}" if isinstance(last_digits, str) else brand_text


# --- pay (authorize) --------------------------------------------------------


def _first_authorization(o: PaypalOrder) -> Any:
    units = _set(o.purchase_units) or []
    for unit in units:
        payments = _set(unit.payments)
        if payments is None:
            continue
        auths = _set(payments.authorizations) or []
        if auths:
            return auths[0]
    return None


@sensitive_variables("payload", "card", "body", "payment_source")
def pay_order(user: Any, number: str, payload: dict[str, Any]) -> Outcome:
    """Authorize (hold) the order total on a card or a saved card."""
    order = get_owned_order(user, number)
    payment: PaypalPayment = order.paypal_payment
    if payment.state in (
        PaypalPayment.AUTHORIZED,
        PaypalPayment.CAPTURED,
        PaypalPayment.CAPTURE_PENDING,
        PaypalPayment.PARTIALLY_REFUNDED,
        PaypalPayment.REFUNDED,
    ):
        return Outcome(200, order)  # already paid: a double-click changes nothing
    if payment.state in (PaypalPayment.VOIDED, PaypalPayment.CANCELLED) or order.status == STATUS_CANCELLED:
        raise Conflict("This order has been cancelled.")

    has_card, has_saved = "card" in payload, "paymentMethodId" in payload
    if has_card == has_saved:
        raise BadRequest('Send either "card" (card details) or "paymentMethodId" (a saved card).')

    saved_card: SavedCard | None = None
    if has_saved:
        card_pk = _uuid_or_none(payload["paymentMethodId"])
        saved_card = (
            SavedCard.objects.filter(pk=card_pk, user=user, deleted_at__isnull=True).first()
            if card_pk is not None
            else None
        )
        if saved_card is None:
            raise NotFound("No such saved card.")
        payment_source = PaymentSource(card=CardRequest(vault_id=saved_card.vault_token_id))
    else:
        card = parse_card(payload["card"])
        card_kwargs: dict[str, Any] = {}
        if card.name:
            card_kwargs["name"] = card.name
        if card.address is not None:
            card_kwargs["billing_address"] = card.address
        payment_source = PaymentSource(
            card=CardRequest(
                number=card.number, expiry=card.expiry, security_code=card.security_code, **card_kwargs
            )
        )

    cur = payment.currency
    amount = payment.amount
    ref = next_attempt(reference(f"o{order.pk}", "authorize"))
    attempt = ref.rsplit(":", 1)[1]
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=order.number,
                custom_id=order_reference(order),
                invoice_id=f"{order_reference(order)}-{attempt}",
                description=f"Order {order.number}",
                amount=AmountWithBreakdown(currency_code=cur, value=money.format_amount(amount, cur)),
            )
        ],
        payment_source=payment_source,
    )
    client = get_client()

    def send(key: str) -> PaypalOrder:
        return client.orders.create_order(body, pay_pal_request_id=key, prefer=PREFER)

    def read(o: PaypalOrder) -> Answer:
        auth = _first_authorization(o)
        auth_status = _set(auth.status) if auth is not None else None
        outcome = outcomes.order_outcome(_set(o.status), auth_status)
        if _set(o.id) is None:
            outcome = outcomes.UNKNOWN
        value, code = _money_value(_set(auth.amount) if auth is not None else None)
        detail = ""
        if _set(o.status) == OrderStatus.PAYER_ACTION_REQUIRED:
            detail = (
                "PayPal requires the shopper to approve this card payment in a browser "
                "(e.g. 3-D Secure); this API does not support that step."
            )
        return Answer(
            provider_id=(_set(auth.id) if auth is not None else None) or _set(o.id) or "",
            status=outcomes.status_text(auth_status) or outcomes.status_text(_set(o.status)),
            outcome=outcome,
            provider_time=parse_time(_set(auth.create_time) if auth is not None else _set(o.create_time)),
            amount=value,
            currency=code,
            detail=detail,
        )

    def apply(record: ProviderWrite, o: PaypalOrder, answer: Answer) -> None:
        fresh = PaypalPayment.objects.select_for_update().get(pk=payment.pk)
        auth = _first_authorization(o)
        fresh.paypal_order_id = _set(o.id) or ""
        fresh.saved_card = saved_card
        source_response = _set(o.payment_source)
        card_response = _set(source_response.card) if source_response is not None else None
        if card_response is not None:
            fresh.card_label = _card_label(_set(card_response.brand), _set(card_response.last_digits))
        if auth is not None:
            fresh.authorization_id = _set(auth.id) or ""
            fresh.authorization_status = outcomes.status_text(_set(auth.status))
            fresh.authorization_created_at = parse_time(_set(auth.create_time))
            fresh.authorization_expires_at = parse_time(_set(auth.expiration_time))
            fresh.original_authorization_id = fresh.authorization_id
            fresh.original_authorization_created_at = fresh.authorization_created_at
        fresh.detail = answer.detail
        if answer.outcome == outcomes.DONE:
            fresh.state = PaypalPayment.AUTHORIZED
            source = _source_for(fresh)
            source.label = fresh.card_label
            source.reference = fresh.paypal_order_id
            source.allocate(fresh.amount, reference=fresh.authorization_id, status=answer.status)
            order.set_status(STATUS_AUTHORIZED)
        elif answer.outcome == outcomes.PENDING:
            fresh.state = PaypalPayment.AUTHORIZATION_PENDING
        elif answer.outcome == outcomes.FAILED:
            fresh.state = PaypalPayment.AWAITING_PAYMENT  # the shopper may pay again
        else:
            fresh.state = PaypalPayment.NEEDS_REVIEW
        fresh.save()

    result = _write(
        lambda: safe_write(
            ref,
            kind=ProviderWrite.AUTHORIZE,
            send=send,
            read=read,
            apply=apply,
            sent=(amount, cur),
            order=order,
            user=user,
        )
    )
    order.refresh_from_db()
    record = result.record
    logger.info("Authorization for order %s: %s (%s)", order.number, record.outcome, record.provider_status)
    if record.outcome == ProviderWrite.DONE:
        return Outcome(200, order)
    if record.outcome == ProviderWrite.FAILED:
        if record.detail.startswith("PayPal requires the shopper"):
            raise ProviderRejected(record.detail, status_code=402, code="payer_action_required")
        raise ProviderRejected(
            "The card was not authorized. Pay again with another card.",
            status_code=402,
            code="payment_declined",
        )
    return Outcome(202, order)  # pending, unknown, needs review: never reported as paid


def _uuid_or_none(value: object) -> str | None:
    text = str(value)
    return text if re.fullmatch(r"[0-9a-fA-F]{8}-?([0-9a-fA-F]{4}-?){3}[0-9a-fA-F]{12}", text) else None


# --- fulfil (capture) ---------------------------------------------------------


def _read_authorization(a: PaymentAuthorization, outcome_fn: Callable[[object], str]) -> Answer:
    status = _set(a.status)
    value, code = _money_value(_set(a.amount))
    return Answer(
        provider_id=_set(a.id) or "",
        status=outcomes.status_text(status),
        outcome=outcome_fn(status) if _set(a.id) is not None else outcomes.UNKNOWN,
        provider_time=parse_time(_set(a.update_time)) or parse_time(_set(a.create_time)),
        amount=value,
        currency=code,
    )


def _ensure_fresh_authorization(order: Any, payment: PaypalPayment, operator: Any) -> Outcome | None:
    """
    Make sure the hold can be captured. Returns None when capture may proceed
    (possibly on a renewed authorization), or an Outcome to answer with now.
    """
    client = get_client()
    auth = _call(lambda: client.payments.get_authorized_payment(payment.authorization_id))
    status = _set(auth.status)
    payment.authorization_status = outcomes.status_text(status)
    payment.authorization_expires_at = parse_time(_set(auth.expiration_time)) or payment.authorization_expires_at
    created = parse_time(_set(auth.create_time)) or payment.authorization_created_at
    payment.authorization_created_at = created
    payment.save()

    held = f"{payment.currency} {payment.amount}"
    if status in (AuthorizationStatus.VOIDED, AuthorizationStatus.DENIED):
        raise NotRenewable(
            f"Authorization {payment.authorization_id} is {payment.authorization_status}; there is no hold "
            f"to capture. Cancel order {order.number} and ask the shopper to place and pay for a new order."
        )
    if status in (AuthorizationStatus.CAPTURED, AuthorizationStatus.PARTIALLY_CAPTURED):
        return None  # a capture already exists; the capture claim resolves to it

    now = timezone.now()
    expires = payment.authorization_expires_at
    if expires is not None and now >= expires:
        raise NotRenewable(
            f"Authorization {payment.authorization_id} for {held} expired on {expires:%Y-%m-%d %H:%M} UTC "
            f"and cannot be renewed. Cancel order {order.number} and ask the shopper to pay again."
        )
    if created is None or now < created + HONOR_PERIOD:
        return None  # still inside the honor period

    original_created = payment.original_authorization_created_at or created
    if payment.reauthorized_at is not None:
        raise NotRenewable(
            f"Authorization {payment.authorization_id} for {held} is past its 3-day honor period and was "
            f"already renewed on {payment.reauthorized_at:%Y-%m-%d}; PayPal allows one renewal. Cancel "
            f"order {order.number} to release the hold and ask the shopper to pay again."
        )
    if now >= original_created + REAUTHORIZE_LIMIT:
        raise NotRenewable(
            f"Authorization {payment.authorization_id} for {held} was created on "
            f"{original_created:%Y-%m-%d} and is too old to renew (PayPal allows renewal up to day 29). "
            f"Cancel order {order.number} and ask the shopper to pay again."
        )
    return _renew_authorization(order, payment, operator)


def _renew_authorization(order: Any, payment: PaypalPayment, operator: Any) -> Outcome | None:
    client = get_client()
    cur = payment.currency
    old_id = payment.authorization_id
    ref = next_attempt(reference(f"o{order.pk}", "reauthorize"))
    body = ReauthorizeRequest(amount=Money(currency_code=cur, value=money.format_amount(payment.amount, cur)))

    def send(key: str) -> PaymentAuthorization:
        return client.payments.reauthorize_payment(old_id, pay_pal_request_id=key, prefer=PREFER, body=body)

    def apply(record: ProviderWrite, a: PaymentAuthorization, answer: Answer) -> None:
        fresh = PaypalPayment.objects.select_for_update().get(pk=payment.pk)
        if answer.outcome == outcomes.DONE:
            fresh.authorization_id = answer.provider_id
            fresh.authorization_status = answer.status
            fresh.authorization_created_at = parse_time(_set(a.create_time)) or timezone.now()
            fresh.authorization_expires_at = parse_time(_set(a.expiration_time))
            fresh.reauthorized_at = timezone.now()
            _source_for(fresh).transactions.create(
                txn_type="Reauthorise", amount=fresh.amount, reference=answer.provider_id, status=answer.status
            )
        fresh.detail = f"reauthorization {answer.outcome}"
        fresh.save()

    try:
        result = safe_write(
            ref,
            kind=ProviderWrite.REAUTHORIZE,
            send=send,
            read=lambda a: _read_authorization(a, outcomes.authorization_outcome),
            apply=apply,
            sent=(payment.amount, cur),
            order=order,
            user=operator,
        )
    except ApiError as exc:
        if exc.status_code in (400, 404, 409, 422):
            raise NotRenewable(
                f"PayPal would not renew stale authorization {old_id} for order {order.number}"
                f" ({exc.error.message if hasattr(exc.error, 'message') else 'refused'}). Cancel the order to "
                "release the hold and ask the shopper to pay again.",
                issues=paypal_issues(exc.error),
            ) from exc
        raise translate_failure(exc, payment_write=True) from exc
    except PaymentError:
        raise
    except Exception as exc:
        raise translate_failure(exc, payment_write=True) from exc

    payment.refresh_from_db()
    outcome = result.record.outcome
    if outcome == ProviderWrite.DONE:
        logger.info("Renewed authorization %s -> %s for order %s", old_id, payment.authorization_id, order.number)
        return None
    if outcome == ProviderWrite.FAILED:
        raise NotRenewable(
            f"PayPal refused to renew stale authorization {old_id} for order {order.number} "
            f"(status {result.record.provider_status}). Cancel the order and ask the shopper to pay again."
        )
    if outcome == ProviderWrite.PENDING:
        payment.detail = "Authorization renewal is pending at PayPal; fulfil again once it completes."
        payment.save(update_fields=["detail", "updated_at"])
        return Outcome(202, order)
    raise OutcomeUnknown(ref)


def _record_capture(payment: PaypalPayment, order: Any, c: CapturedPayment, outcome: str) -> None:
    """Store what PayPal reported for a capture; debit the Oscar source once when done."""
    value, _ = _money_value(_set(c.amount))
    payment.capture_id = _set(c.id) or payment.capture_id
    payment.capture_status = outcomes.status_text(_set(c.status))
    payment.captured_amount = _decimal(value)
    payment.captured_at = parse_time(_set(c.create_time))
    breakdown = _set(c.seller_receivable_breakdown)
    if breakdown is not None:
        payment.paypal_fee = _decimal(_money_value(_set(breakdown.paypal_fee))[0])
        payment.net_amount = _decimal(_money_value(_set(breakdown.net_amount))[0])
    if outcome == outcomes.DONE:
        if payment.state not in (PaypalPayment.CAPTURED, PaypalPayment.PARTIALLY_REFUNDED, PaypalPayment.REFUNDED):
            payment.state = PaypalPayment.CAPTURED
            _source_for(payment).debit(
                payment.captured_amount, reference=payment.capture_id, status=payment.capture_status
            )
            _consume_stock(order)
            order.set_status(STATUS_COMPLETE)
        payment.detail = ""
    elif outcome == outcomes.PENDING:
        payment.state = PaypalPayment.CAPTURE_PENDING
        payment.detail = "PayPal has not completed the capture yet; fulfil again to refresh."
    elif outcome == outcomes.FAILED:
        payment.state = PaypalPayment.AUTHORIZED
        payment.detail = f"Capture {payment.capture_status or 'failed'}."
    else:
        payment.state = PaypalPayment.NEEDS_REVIEW
        payment.detail = "PayPal reported a capture state this site does not recognise."
    payment.save()


def fulfil_order(operator: Any, number: str) -> Outcome:
    """Operator: mark the order fulfilled and take the held money."""
    order = get_any_order(number)
    payment: PaypalPayment = order.paypal_payment
    client = get_client()

    if payment.state in (PaypalPayment.CAPTURED, PaypalPayment.PARTIALLY_REFUNDED, PaypalPayment.REFUNDED):
        return Outcome(200, order)
    if payment.state == PaypalPayment.CAPTURE_PENDING and payment.capture_id:
        c = _call(lambda: client.payments.get_captured_payment(payment.capture_id))
        with transaction.atomic():
            fresh = PaypalPayment.objects.select_for_update().get(pk=payment.pk)
            _record_capture(fresh, order, c, outcomes.capture_outcome(_set(c.status)))
        order.refresh_from_db()
        return Outcome(200 if order.paypal_payment.state == PaypalPayment.CAPTURED else 202, order)
    if payment.state != PaypalPayment.AUTHORIZED or not payment.authorization_id:
        raise Conflict(
            f"Order {order.number} has no authorized payment to capture (payment state: {payment.state})."
        )

    early = _ensure_fresh_authorization(order, payment, operator)
    if early is not None:
        return early
    payment.refresh_from_db()

    cur = payment.currency
    auth_id = payment.authorization_id
    ref = next_attempt(reference(f"o{order.pk}", "capture"))
    body = CaptureRequest(
        amount=Money(currency_code=cur, value=money.format_amount(payment.amount, cur)),
        final_capture=True,
    )

    def send(key: str) -> CapturedPayment:
        return client.payments.capture_authorized_payment(auth_id, pay_pal_request_id=key, prefer=PREFER, body=body)

    def read(c: CapturedPayment) -> Answer:
        status = _set(c.status)
        value, code = _money_value(_set(c.amount))
        return Answer(
            provider_id=_set(c.id) or "",
            status=outcomes.status_text(status),
            outcome=outcomes.capture_outcome(status) if _set(c.id) is not None else outcomes.UNKNOWN,
            provider_time=parse_time(_set(c.create_time)),
            amount=value,
            currency=code,
        )

    def apply(record: ProviderWrite, c: CapturedPayment, answer: Answer) -> None:
        fresh = PaypalPayment.objects.select_for_update().get(pk=payment.pk)
        _record_capture(fresh, order, c, answer.outcome)

    try:
        result = safe_write(
            ref,
            kind=ProviderWrite.CAPTURE,
            send=send,
            read=read,
            apply=apply,
            sent=(payment.amount, cur),
            order=order,
            user=operator,
        )
    except ApiError as exc:
        if exc.status_code in (400, 409, 422):
            raise Conflict(
                f"PayPal refused to capture authorization {auth_id} for order {order.number}: "
                f"{exc.error.message if hasattr(exc.error, 'message') else 'refused'}. If the authorization "
                "has expired, cancel the order and ask the shopper to pay again.",
                code="capture_refused",
                issues=paypal_issues(exc.error),
            ) from exc
        raise translate_failure(exc, payment_write=True) from exc
    except PaymentError:
        raise
    except Exception as exc:
        raise translate_failure(exc, payment_write=True) from exc

    order.refresh_from_db()
    outcome = result.record.outcome
    logger.info("Capture for order %s: %s (%s)", order.number, outcome, result.record.provider_status)
    if outcome == ProviderWrite.DONE:
        return Outcome(200, order)
    if outcome == ProviderWrite.FAILED:
        raise Conflict(
            f"PayPal did not capture the payment for order {order.number} "
            f"(status {result.record.provider_status}).",
            code="capture_failed",
        )
    return Outcome(202, order)


# --- cancel (void) ------------------------------------------------------------


def cancel_order(operator: Any, number: str) -> Outcome:
    """Operator: cancel before fulfilment, releasing the shopper's held funds."""
    order = get_any_order(number)
    payment: PaypalPayment = order.paypal_payment
    if payment.state in (PaypalPayment.VOIDED, PaypalPayment.CANCELLED):
        return Outcome(200, order)
    if payment.state in (
        PaypalPayment.CAPTURED,
        PaypalPayment.CAPTURE_PENDING,
        PaypalPayment.PARTIALLY_REFUNDED,
        PaypalPayment.REFUNDED,
    ):
        raise Conflict(
            f"Order {order.number} has been fulfilled and its payment captured; refund it instead.",
            code="already_captured",
        )

    unsettled = ProviderWrite.objects.filter(
        order=order,
        kind=ProviderWrite.AUTHORIZE,
        outcome__in=[ProviderWrite.SENDING, ProviderWrite.UNKNOWN, ProviderWrite.NEEDS_REVIEW],
    ).exists()
    if not payment.authorization_id:
        if unsettled:
            raise Conflict(
                f"A payment attempt for order {order.number} has not been settled with PayPal yet; "
                "cancel again once it has.",
                code="payment_unsettled",
            )
        with transaction.atomic():
            fresh = PaypalPayment.objects.select_for_update().get(pk=payment.pk)
            fresh.state = PaypalPayment.CANCELLED
            fresh.save()
            _release_stock(order)
            order.set_status(STATUS_CANCELLED)
        return Outcome(200, order)

    client = get_client()
    auth_id = payment.authorization_id
    ref = next_attempt(reference(f"o{order.pk}", "void"))

    def send(key: str) -> PaymentAuthorization:
        return client.payments.void_payment(auth_id, pay_pal_request_id=key, prefer=PREFER)

    def apply(record: ProviderWrite, a: PaymentAuthorization, answer: Answer) -> None:
        fresh = PaypalPayment.objects.select_for_update().get(pk=payment.pk)
        fresh.authorization_status = answer.status or fresh.authorization_status
        if answer.outcome == outcomes.DONE:
            fresh.state = PaypalPayment.VOIDED
            fresh.detail = ""
            _source_for(fresh).transactions.create(
                txn_type="Void", amount=fresh.amount, reference=auth_id, status=answer.status
            )
            _release_stock(order)
            order.set_status(STATUS_CANCELLED)
        elif answer.outcome == outcomes.PENDING:
            fresh.detail = "PayPal has not released the hold yet; cancel again to refresh."
        elif answer.outcome == outcomes.UNKNOWN:
            fresh.state = PaypalPayment.NEEDS_REVIEW
            fresh.detail = f"PayPal answered the void with status {answer.status or 'none'}."
        fresh.save()

    result = _write(
        lambda: safe_write(
            ref,
            kind=ProviderWrite.VOID,
            send=send,
            read=lambda a: _read_authorization(a, outcomes.void_outcome),
            apply=apply,
            order=order,
            user=operator,
        )
    )
    order.refresh_from_db()
    outcome = result.record.outcome
    logger.info("Void for order %s: %s (%s)", order.number, outcome, result.record.provider_status)
    if outcome == ProviderWrite.DONE:
        return Outcome(200, order)
    if outcome == ProviderWrite.FAILED:
        raise Conflict(
            f"The hold for order {order.number} could not be released (status "
            f"{result.record.provider_status}); the payment was already captured - refund it instead.",
            code="already_captured",
        )
    return Outcome(202, order)


# --- refunds ----------------------------------------------------------------

REFUND_RESERVING = [
    ProviderWrite.SENDING,
    ProviderWrite.DONE,
    ProviderWrite.PENDING,
    ProviderWrite.UNKNOWN,
    ProviderWrite.NEEDS_REVIEW,
]


def refund_writes(order: Any) -> Any:
    return ProviderWrite.objects.filter(order=order, kind=ProviderWrite.REFUND)


def refunded_amount(order: Any) -> Decimal:
    total = refund_writes(order).filter(outcome=ProviderWrite.DONE).aggregate(s=Sum("amount"))["s"]
    return total or Decimal("0")


def reserved_refund_amount(order: Any) -> Decimal:
    total = refund_writes(order).filter(outcome__in=REFUND_RESERVING).aggregate(s=Sum("amount"))["s"]
    return total or Decimal("0")


def refundable_amount(payment: PaypalPayment) -> Decimal:
    if payment.captured_amount is None or payment.state not in (
        PaypalPayment.CAPTURED,
        PaypalPayment.PARTIALLY_REFUNDED,
    ):
        return Decimal("0")
    return max(payment.captured_amount - reserved_refund_amount(payment.order), Decimal("0"))


def _apply_refund(payment_pk: int, order: Any, record: ProviderWrite, outcome: str) -> None:
    """Mirror a refund's outcome onto the payment; called inside a transaction."""
    fresh = PaypalPayment.objects.select_for_update().get(pk=payment_pk)
    if outcome == outcomes.DONE:
        _source_for(fresh).refund(record.amount, reference=record.provider_id, status=record.provider_status)
        total = refunded_amount(order)
        captured = fresh.captured_amount or Decimal("0")
        fresh.state = PaypalPayment.REFUNDED if total >= captured else PaypalPayment.PARTIALLY_REFUNDED
    fresh.save()


def refund_order(user: Any, number: str, payload: dict[str, Any], header_key: str | None) -> Outcome:
    """Refund the captured payment in full or in part, under a caller idempotency key."""
    order = get_owned_order(user, number)
    payment: PaypalPayment = order.paypal_payment
    cur = payment.currency

    key = payload.get("idempotencyKey", header_key)
    if not isinstance(key, str) or not 1 <= len(key.strip()) <= 255:
        raise BadRequest('An idempotency key is required ("idempotencyKey" or the Idempotency-Key header).')
    key_hash = hashlib.sha256(key.strip().encode()).hexdigest()[:32]
    ref = reference(f"o{order.pk}", "refund", key_hash)

    requested: Decimal | None = None
    if payload.get("amount") is not None:
        try:
            requested = money.parse_amount(payload["amount"], cur)
        except ValueError as exc:
            raise BadRequest(str(exc)) from None

    # Looked up only to answer a repeat precisely; the unique claim below is
    # what actually stops a second refund under this key.
    existing = ProviderWrite.objects.filter(reference=ref).first()
    if existing is not None:
        if requested is not None and existing.amount != requested:
            raise Conflict(
                "This idempotency key was already used for a refund of a different amount.",
                status_code=422,
                code="idempotency_key_reused",
            )
        if existing.outcome not in (ProviderWrite.SENDING, ProviderWrite.UNKNOWN):
            return _answer_refund(order, payment, _refresh_pending_refund(order, payment, existing))
        amount = existing.amount or Decimal("0")  # settle it under the same key and amount
    else:
        if payment.state not in (PaypalPayment.CAPTURED, PaypalPayment.PARTIALLY_REFUNDED) or not payment.capture_id:
            if payment.state == PaypalPayment.REFUNDED:
                raise Conflict("This order has already been refunded in full.", code="nothing_refundable")
            raise Conflict(
                "Only a fulfilled (captured) order can be refunded; an unfulfilled order is cancelled instead.",
                code="not_captured",
            )
        amount = requested if requested is not None else refundable_amount(payment)
        if amount <= 0:
            raise Conflict("Nothing is left to refund on this order.", code="nothing_refundable")

    def reserve() -> None:
        # Under a row lock, in the claim's transaction: the captured amount
        # can never be exceeded by refunds in flight or done.
        locked = PaypalPayment.objects.select_for_update().get(pk=payment.pk)
        if ProviderWrite.objects.filter(reference=ref).exists():
            return  # a repeat of this key: let the unique claim answer it

        remaining = (locked.captured_amount or Decimal("0")) - reserved_refund_amount(order)
        if amount > remaining:
            raise Conflict(
                f"Refund of {cur} {amount} exceeds the refundable amount ({cur} {max(remaining, Decimal(0))}).",
                status_code=422,
                code="refund_exceeds_captured",
            )

    client = get_client()
    capture_id = payment.capture_id
    body = RefundRequest(
        amount=Money(currency_code=cur, value=money.format_amount(amount, cur)),
        custom_id=order_reference(order),
    )

    def send(k: str) -> Refund:
        return client.payments.refund_captured_payment(capture_id, pay_pal_request_id=k, prefer=PREFER, body=body)

    def read(r: Refund) -> Answer:
        status = _set(r.status)
        value, code = _money_value(_set(r.amount))
        return Answer(
            provider_id=_set(r.id) or "",
            status=outcomes.status_text(status),
            outcome=outcomes.refund_outcome(status) if _set(r.id) is not None else outcomes.UNKNOWN,
            provider_time=parse_time(_set(r.create_time)),
            amount=value,
            currency=code,
        )

    def apply(record: ProviderWrite, r: Refund, answer: Answer) -> None:
        _apply_refund(payment.pk, order, record, answer.outcome)

    try:
        result = safe_write(
            ref,
            kind=ProviderWrite.REFUND,
            send=send,
            read=read,
            apply=apply,
            sent=(amount, cur),
            order=order,
            user=user,
            reserve=reserve,
        )
    except PaymentError:
        raise
    except Exception as exc:
        raise translate_failure(exc, payment_write=False) from exc

    record = result.record
    if result.result is None:
        # Another request holds this key: answer from what it recorded.
        if requested is not None and record.amount != requested:
            raise Conflict(
                "This idempotency key was already used for a refund of a different amount.",
                status_code=422,
                code="idempotency_key_reused",
            )
        record = _refresh_pending_refund(order, payment, record)
    logger.info("Refund %s for order %s: %s", record.public_id, order.number, record.outcome)
    return _answer_refund(order, payment, record)


def _refresh_pending_refund(order: Any, payment: PaypalPayment, record: ProviderWrite) -> ProviderWrite:
    """A pending refund is re-read from PayPal when the same request repeats."""
    if record.outcome != ProviderWrite.PENDING or not record.provider_id:
        return record
    client = get_client()
    r = _call(lambda: client.payments.get_refund(record.provider_id))
    outcome = outcomes.refund_outcome(_set(r.status))
    if outcome == outcomes.PENDING:
        return record
    with transaction.atomic():
        fresh = ProviderWrite.objects.select_for_update().get(pk=record.pk)
        if fresh.outcome != ProviderWrite.PENDING:
            return fresh
        fresh.outcome = outcome
        fresh.provider_status = outcomes.status_text(_set(r.status))
        fresh.save()
        _apply_refund(payment.pk, order, fresh, outcome)
    return fresh


def _answer_refund(order: Any, payment: PaypalPayment, record: ProviderWrite) -> Outcome:
    if record.outcome == ProviderWrite.FAILED:
        raise ProviderRejected(
            f"PayPal did not refund this request (status {record.provider_status or 'refused'}). "
            "Use a new idempotency key to try again.",
            status_code=402,
            code="refund_failed",
        )
    order.refresh_from_db()
    return Outcome(201 if record.outcome == ProviderWrite.DONE else 202, (order, record))


# --- saved cards --------------------------------------------------------------


def saved_cards_for(user: Any) -> Any:
    return SavedCard.objects.filter(user=user, deleted_at__isnull=True)


@sensitive_variables("payload", "card", "body")
def save_card(user: Any, payload: dict[str, Any]) -> Outcome:
    """Vault a card at PayPal for the signed-in shopper."""
    card = parse_card(payload.get("card", payload))
    fingerprint = card_fingerprint(user, card)
    active = saved_cards_for(user).filter(fingerprint=fingerprint).first()
    if active is not None:
        return Outcome(200, active)  # this card is already saved for this shopper

    generation = SavedCard.objects.filter(user=user, fingerprint=fingerprint, deleted_at__isnull=False).count()
    ref = next_attempt(reference(f"u{user.pk}", "vault", fingerprint[:24], generation))
    customer = PaypalCustomer.objects.filter(user=user).first()

    card_kwargs: dict[str, Any] = {}
    if card.name:
        card_kwargs["name"] = card.name
    if card.address is not None:
        card_kwargs["billing_address"] = card.address
    request_kwargs: dict[str, Any] = {}
    if customer is not None:
        request_kwargs["customer"] = Customer(id=customer.paypal_customer_id)
    body = PaymentTokenRequest(
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                number=card.number, expiry=card.expiry, security_code=card.security_code, **card_kwargs
            )
        ),
        **request_kwargs,
    )
    client = get_client()

    def send(key: str) -> PaymentTokenResponse:
        return client.vault.create_payment_token(body, pay_pal_request_id=key)

    def read(t: PaymentTokenResponse) -> Answer:
        source = _set(t.payment_source)
        card_entity = _set(source.card) if source is not None else None
        token_id = _set(t.id)
        # A payment token has no status: it exists once PayPal returns its id and card.
        done = token_id is not None and card_entity is not None
        return Answer(
            provider_id=token_id or "",
            status="CREATED" if done else "",
            outcome=outcomes.DONE if done else outcomes.UNKNOWN,
            provider_time=None,
        )

    def apply(record: ProviderWrite, t: PaymentTokenResponse, answer: Answer) -> None:
        if answer.outcome != outcomes.DONE:
            return
        customer_response = _set(t.customer)
        customer_id = (_set(customer_response.id) if customer_response is not None else None) or ""
        if customer_id:
            PaypalCustomer.objects.get_or_create(user=user, defaults={"paypal_customer_id": customer_id})
        source = _set(t.payment_source)
        entity = _set(source.card) if source is not None else None
        SavedCard.objects.get_or_create(
            vault_token_id=answer.provider_id,
            defaults={
                "user": user,
                "paypal_customer_id": customer_id,
                "brand": outcomes.status_text(_set(entity.brand)) if entity is not None else "",
                "last_digits": (_set(entity.last_digits) or "") if entity is not None else "",
                "expiry": (_set(entity.expiry) or "") if entity is not None else "",
                "fingerprint": fingerprint,
            },
        )

    result = _write(
        lambda: safe_write(ref, kind=ProviderWrite.VAULT, send=send, read=read, apply=apply, user=user),
        payment_write=False,
    )
    record = result.record
    if record.outcome == ProviderWrite.DONE:
        saved = SavedCard.objects.filter(vault_token_id=record.provider_id, user=user).first()
        if saved is not None:
            return Outcome(201 if result.result is not None else 200, saved)
    if record.outcome == ProviderWrite.FAILED:
        raise ProviderRejected("PayPal did not save this card.", status_code=422, code="card_not_saved")
    raise OutcomeUnknown(ref)


def delete_saved_card(user: Any, card_id: str) -> Outcome:
    """
    Remove a saved card. It stops appearing and stops being usable at once;
    the PayPal vault token is then deleted (and retried on a repeated DELETE
    if PayPal could not be reached).
    """
    card_pk = _uuid_or_none(card_id)
    card = SavedCard.objects.filter(pk=card_pk, user=user).first() if card_pk is not None else None
    if card is None:
        raise NotFound("No such saved card.")
    if card.provider_deleted_at is not None:
        return Outcome(204, None)
    if card.deleted_at is None:
        card.deleted_at = timezone.now()
        card.save(update_fields=["deleted_at"])

    client = get_client()
    try:
        outcome = client.vault.with_raw_response.delete_payment_token(card.vault_token_id)
        status_code = outcome.response.status_code
        gone = not isinstance(outcome, Failure) or status_code == 404
    except Exception as exc:
        logger.warning("Deleting vault token for saved card %s failed: %s", card.pk, type(exc).__name__)
        gone, status_code = False, 0
    if gone:
        card.provider_deleted_at = timezone.now()
        card.save(update_fields=["provider_deleted_at"])
        logger.info("Saved card %s deleted from the PayPal vault", card.pk)
        return Outcome(204, None)
    logger.warning("PayPal did not confirm deleting saved card %s (HTTP %s)", card.pk, status_code)
    return Outcome(202, card)
