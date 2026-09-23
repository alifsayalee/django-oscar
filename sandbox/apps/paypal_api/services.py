"""Orchestration tying Oscar's order machinery to the PayPal gateway.

Every money action is idempotent in effect: the :class:`OrderPayment` row is the
durable claim, and a ``select_for_update`` row lock plus a status guard mean a
double-click never authorizes or captures twice. Refunds additionally carry a
caller-supplied idempotency key.
"""
import datetime
import hashlib
import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from oscar.apps.order.exceptions import InvalidOrderStatus
from oscar.core.loading import get_class, get_model

from .errors import PayPalError, PayPalRejected
from .gateway import PayPalGateway
from .models import OrderPayment, PaymentMethod, PayPalCustomer, PaymentRefund

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
ShippingAddress = get_model("order", "ShippingAddress")
Country = get_model("address", "Country")

Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")

# Minor-unit exponent per currency; everything not listed is 2 decimal places.
_CURRENCY_EXPONENT = {"JPY": 0, "KRW": 0, "KWD": 3, "BHD": 3, "TND": 3}

# PayPal transaction search accepts at most a 31-day range per request.
_MAX_SEARCH_DAYS = 31

logger = logging.getLogger("paypal_api")


class ServiceError(Exception):
    """A caller-actionable service failure carrying an HTTP status."""

    def __init__(self, http_status, message):
        super().__init__(message)
        self.http_status = http_status
        self.message = message


def format_amount(value, currency):
    """Format a Decimal for PayPal, scaled to the currency's minor units."""
    places = _CURRENCY_EXPONENT.get(currency, 2)
    quantized = Decimal(value).quantize(Decimal(1).scaleb(-places))
    return str(quantized)


def _set_order_status(order, target):
    """Advance the Oscar order status, walking intermediate steps if needed."""
    try:
        if target == "Complete" and order.status == "Pending":
            order.set_status("Being processed")
        order.set_status(target)
    except InvalidOrderStatus:
        # The configured pipeline does not allow this move from here; the payment
        # state (OrderPayment.status) remains the source of truth regardless.
        pass


# ---------------------------------------------------------------------------
# Flow 1 -- placing and paying for an order
# ---------------------------------------------------------------------------
def place_order(user, item_specs):
    """Place an Oscar order from ``[(product, quantity), ...]``; awaiting payment.

    Reuses Oscar's basket, shipping, totals and OrderCreator rather than a
    parallel order model.
    """
    basket = Basket()
    basket.strategy = Selector().strategy(user=user)
    if user and user.is_authenticated:
        basket.owner = user
    for product, quantity in item_specs:
        basket.add_product(product, quantity=quantity)

    if basket.is_empty:
        raise ServiceError(400, "Cannot place an order with no items")

    method = Free()
    shipping_charge = method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)

    country = _default_country()
    shipping_address = ShippingAddress(
        first_name=(user.first_name or "Shopper") if user else "Shopper",
        last_name=(user.last_name or "") if user else "",
        line1="1 Market Street",
        line4="San Jose",
        state="CA",
        postcode="95131",
        country=country,
    )
    shipping_address.save()

    with transaction.atomic():
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=shipping_address,
            status=settings.OSCAR_INITIAL_ORDER_STATUS,
        )
        OrderPayment.objects.create(
            order=order,
            currency=settings.PAYPAL_CURRENCY,
            amount=order.total_incl_tax,
            status=OrderPayment.AWAITING_PAYMENT,
        )
    return order


def _default_country():
    country = Country.objects.filter(is_shipping_country=True).first()
    if country is None:
        country = Country.objects.first()
    if country is None:
        country, _ = Country.objects.get_or_create(
            iso_3166_1_a2="US",
            defaults={
                "printable_name": "United States",
                "name": "United States",
                "is_shipping_country": True,
            },
        )
    return country


def authorize_payment(order, *, card=None, payment_method=None):
    """Authorize (hold) the order total. Idempotent: repeat calls return the
    existing authorization without charging again."""
    gateway = PayPalGateway()
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().get(order=order)
        if payment.status in (
            OrderPayment.AUTHORIZED,
            OrderPayment.CAPTURED,
            OrderPayment.PARTIALLY_REFUNDED,
            OrderPayment.REFUNDED,
        ):
            return payment  # already authorized or beyond -- no second hold
        if payment.status == OrderPayment.CANCELLED:
            raise ServiceError(409, "Order has been cancelled and cannot be paid")

        vault_id = payment_method.paypal_vault_id if payment_method is not None else None
        result = gateway.authorize_order(
            amount_value=format_amount(payment.amount, payment.currency),
            currency=payment.currency,
            card=card,
            vault_id=vault_id,
            custom_id=order.number,
            request_id=f"auth-{order.number}",
        )
        auth = result["authorization"]
        # Verify PayPal held exactly the order total, to the cent.
        held = auth.get("amount_value")
        if held is not None and Decimal(held) != payment.amount:
            raise ServiceError(
                502,
                "PayPal held a different amount than the order total "
                f"({held} vs {payment.amount})",
            )
        payment.paypal_order_id = result["paypal_order_id"] or ""
        payment.authorization_id = auth["id"]
        payment.auth_status = auth.get("status") or ""
        payment.auth_expiry = auth.get("expiry") or ""
        payment.status = OrderPayment.AUTHORIZED
        payment.save()
        return payment


def capture_payment(order):
    """Capture (take) the money at fulfilment. Renews a stale authorization
    rather than failing outright. Idempotent."""
    gateway = PayPalGateway()
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().get(order=order)
        if payment.status in (
            OrderPayment.CAPTURED,
            OrderPayment.PARTIALLY_REFUNDED,
            OrderPayment.REFUNDED,
        ):
            return payment  # already captured -- do not charge twice
        if payment.status != OrderPayment.AUTHORIZED:
            raise ServiceError(409, "Order is not authorized and cannot be fulfilled")

        auth_id = _ensure_capturable_authorization(gateway, order, payment)

        try:
            capture = gateway.capture(auth_id, request_id=f"cap-{order.number}")
        except PayPalRejected as exc:
            # A borderline-stale authorization may still be refused at capture;
            # renew once and retry before giving up.
            renewed = _reauthorize(gateway, order, payment)
            if renewed is None:
                raise ServiceError(
                    409,
                    "Authorization has expired and could not be renewed: "
                    + exc.message,
                )
            capture = gateway.capture(renewed, request_id=f"cap-{order.number}-r")

        payment.capture_id = capture["capture_id"]
        payment.capture_status = capture.get("status") or ""
        payment.captured_value = _to_decimal(capture.get("amount_value"))
        payment.paypal_fee = _to_decimal(capture.get("fee"))
        payment.net_value = _to_decimal(capture.get("net"))
        payment.paypal_event_time = _parse_time(capture.get("update_time"))
        payment.status = OrderPayment.CAPTURED
        payment.save()

    _set_order_status(order, "Complete")
    return payment


def _ensure_capturable_authorization(gateway, order, payment):
    """Return an authorization id ready to capture, renewing a stale one."""
    auth = gateway.get_authorization(payment.authorization_id)
    status = (auth.get("status") or "").upper()
    payment.auth_status = auth.get("status") or payment.auth_status
    if status == "VOIDED":
        raise ServiceError(409, "Authorization was voided; the order cannot be fulfilled")
    if _authorization_is_stale(auth):
        renewed = _reauthorize(gateway, order, payment)
        if renewed is None:
            raise ServiceError(
                409,
                "Authorization has expired and could not be renewed; re-collect payment",
            )
        return renewed
    return payment.authorization_id


def _authorization_is_stale(auth):
    expiry = _parse_time(auth.get("expiry"))
    if expiry is None:
        return False
    # Treat as stale a little before the deadline to avoid a capture racing expiry.
    return expiry <= timezone.now() + datetime.timedelta(minutes=1)


def _reauthorize(gateway, order, payment):
    """Try to renew the authorization; return the new id or ``None`` if refused."""
    try:
        reauth = gateway.reauthorize(
            payment.authorization_id,
            amount_value=format_amount(payment.amount, payment.currency),
            currency=payment.currency,
            request_id=f"reauth-{order.number}",
        )
    except PayPalRejected:
        return None
    payment.authorization_id = reauth["id"]
    payment.auth_status = reauth.get("status") or ""
    payment.auth_expiry = reauth.get("expiry") or ""
    payment.save()
    return reauth["id"]


def cancel_payment(order):
    """Cancel before fulfilment: release the held funds. Idempotent."""
    gateway = PayPalGateway()
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().get(order=order)
        if payment.status == OrderPayment.CANCELLED:
            return payment
        if payment.status != OrderPayment.AUTHORIZED:
            raise ServiceError(
                409,
                "Only an authorized (not yet fulfilled) order can be cancelled",
            )
        auth = gateway.get_authorization(payment.authorization_id)
        if (auth.get("status") or "").upper() != "VOIDED":
            gateway.void(payment.authorization_id)
        payment.auth_status = "VOIDED"
        payment.status = OrderPayment.CANCELLED
        payment.save()

    _set_order_status(order, "Cancelled")
    return payment


def refund_payment(order, *, amount=None, idempotency_key):
    """Refund a captured payment (full or partial). The idempotency key makes a
    repeat request return the same refund; distinct keys make distinct refunds."""
    gateway = PayPalGateway()
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().get(order=order)
        if payment.status not in (
            OrderPayment.CAPTURED,
            OrderPayment.PARTIALLY_REFUNDED,
        ):
            raise ServiceError(409, "Order has no captured payment to refund")

        available = payment.amount_available_for_refund
        requested = amount if amount is not None else available
        refund_row, created = PaymentRefund.objects.get_or_create(
            payment=payment,
            idempotency_key=idempotency_key,
            defaults={"amount": requested},
        )
        if refund_row.paypal_refund_id:
            return refund_row  # already refunded under this key

        if created:
            if requested <= 0:
                refund_row.delete()
                raise ServiceError(400, "Refund amount must be positive")
            if requested > available:
                refund_row.delete()
                raise ServiceError(
                    409,
                    "Refund amount exceeds the amount available for refund "
                    f"({requested} > {available})",
                )

        # The caller's idempotency_key governs OUR persistence (unique per
        # payment). PayPal's PayPal-Request-Id must be unique account-wide *and*
        # stable across retries of this same logical refund, so we derive it from
        # the (capture id, caller key) pair -- both stable, PayPal-unique values.
        # (A DB pk would be wrong here: an atomic request that rolls back after a
        # successful PayPal refund reuses the pk, colliding at PayPal.)
        paypal_request_id = "rf-" + hashlib.sha256(
            f"{payment.capture_id}:{idempotency_key}".encode("utf-8")
        ).hexdigest()[:40]
        result = gateway.refund(
            payment.capture_id,
            amount_value=format_amount(refund_row.amount, payment.currency),
            currency=payment.currency,
            request_id=paypal_request_id,
        )
        refund_row.paypal_refund_id = result["refund_id"]
        refund_row.status = result.get("status") or ""
        refund_row.save()

        if payment.amount_available_for_refund <= 0:
            payment.status = OrderPayment.REFUNDED
        else:
            payment.status = OrderPayment.PARTIALLY_REFUNDED
        payment.save()
        return refund_row


# ---------------------------------------------------------------------------
# Flow 2 -- saved cards
# ---------------------------------------------------------------------------
def save_card(user, card):
    """Vault a card for the shopper and record safe display data."""
    gateway = PayPalGateway()
    customer = PayPalCustomer.objects.filter(user=user).first()
    result = gateway.vault_card(
        card=card,
        customer_id=customer.paypal_customer_id if customer else None,
        merchant_customer_id=None if customer else f"oscar-user-{user.pk}",
        request_id=f"vault-{user.pk}-{timezone.now().timestamp()}",
    )
    paypal_customer_id = result.get("customer_id") or (
        customer.paypal_customer_id if customer else ""
    )
    if paypal_customer_id and customer is None:
        PayPalCustomer.objects.get_or_create(
            user=user, defaults={"paypal_customer_id": paypal_customer_id}
        )
    method = PaymentMethod.objects.create(
        user=user,
        paypal_vault_id=result["vault_id"],
        paypal_customer_id=paypal_customer_id or "",
        brand=result.get("brand") or "",
        last_digits=result.get("last_digits") or "",
        expiry=result.get("expiry") or "",
        cardholder_name=result.get("name") or (card.get("name") or ""),
    )
    return method


def delete_card(user, payment_method):
    """Remove a saved card.

    The local row is the authority for whether a card is usable to pay and
    whether it is listed, so we always remove it -- guaranteeing the card can no
    longer be seen or used. Deleting the PayPal vault token is idempotent and
    best-effort: a 404 means it is already gone, and a transient failure is
    retried a few times before we give up and log the residual token.
    """
    gateway = PayPalGateway()
    vault_id = payment_method.paypal_vault_id
    last_error: PayPalError | None = None
    for attempt in range(3):
        try:
            gateway.delete_card(vault_id)
            last_error = None
            break
        except PayPalRejected as exc:
            if exc.status_code == 404:
                last_error = None  # already gone at PayPal
            else:
                last_error = exc
            break  # a definite rejection will not improve on retry
        except PayPalError as exc:
            last_error = exc  # transient -- retry

    payment_method.delete()
    if last_error is not None:
        logger.warning(
            "Deleted saved card %s locally but PayPal vault token %s could not be "
            "removed: %s",
            payment_method.pk,
            vault_id,
            last_error.message,
        )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def _search_dt(value):
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


def reconcile(from_dt, to_dt):
    """List PayPal's transactions for a window and line them up against the
    app's orders. Covers the whole range by chunking into <=31-day windows and
    paging each chunk fully."""
    gateway = PayPalGateway()
    provider_txns = []
    truncated = False

    chunk_start = from_dt
    while chunk_start < to_dt:
        chunk_end = min(chunk_start + datetime.timedelta(days=_MAX_SEARCH_DAYS), to_dt)
        page = gateway.search_transactions(
            _search_dt(chunk_start),
            _search_dt(chunk_end),
            currency=settings.PAYPAL_CURRENCY,
        )
        truncated = truncated or page["truncated"]
        for txn in page["transactions"]:
            initiated = _parse_time(txn.get("initiation_date"))
            # Narrow whole-day/inclusive provider results back to the window.
            if initiated is not None and not (from_dt <= initiated < to_dt):
                continue
            provider_txns.append(txn)
        chunk_start = chunk_end

    # App side: payments whose money moved in the window (provider clock), plus
    # any still-unsettled ones created in the window.
    payments = (
        OrderPayment.objects.select_related("order")
        .exclude(status=OrderPayment.AWAITING_PAYMENT)
        .filter(created__lt=to_dt)
    )

    by_order_number = {}
    for payment in payments:
        by_order_number[payment.order.number] = payment

    matched = []
    provider_only = []
    seen_order_numbers = set()
    for txn in provider_txns:
        custom = txn.get("custom_field")
        payment = by_order_number.get(custom) if custom else None
        if payment is not None:
            seen_order_numbers.add(payment.order.number)
            matched.append(
                {
                    "transaction": txn,
                    "orderId": payment.order_id,
                    "orderNumber": payment.order.number,
                    "appStatus": payment.status,
                }
            )
        else:
            provider_only.append(txn)

    app_only = [
        {
            "orderId": payment.order_id,
            "orderNumber": payment.order.number,
            "appStatus": payment.status,
            "authorizationId": payment.authorization_id,
            "captureId": payment.capture_id,
        }
        for number, payment in by_order_number.items()
        if number not in seen_order_numbers
        and payment.status not in (OrderPayment.AWAITING_PAYMENT, OrderPayment.FAILED)
    ]

    return {
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "currency": settings.PAYPAL_CURRENCY,
        "providerTransactionCount": len(provider_txns),
        "matched": matched,
        "providerOnly": provider_only,
        "appOnly": app_only,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _to_decimal(value):
    if value is None:
        return None
    return Decimal(str(value))


def _parse_time(value):
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed
