"""Business logic for the checkout API.

This layer owns the database and the Oscar models; it calls ``paypal_gateway`` for
every PayPal interaction and never touches the SDK directly. Idempotency lives here:
each mutating flow takes a ``select_for_update`` lock on the order's ``PayPalPayment``
row and decides from its state, so a double-click can never authorize, capture or
refund twice (the gateway additionally sends a PayPal-Request-Id so PayPal dedupes a
resend across process restarts).
"""

import datetime
import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from oscar.core.loading import get_class, get_model

from . import errors, paypal_gateway
from .models import PayPalPayment, PayPalRefund

logger = logging.getLogger("api.services")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Order = get_model("order", "Order")
ShippingAddress = get_model("order", "ShippingAddress")
Country = get_model("address", "Country")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Bankcard = get_model("payment", "Bankcard")

OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")

PAYPAL_SOURCE_NAME = "PayPal"


# ---------------------------------------------------------------------------
# Lookups (scoping)
# ---------------------------------------------------------------------------


def _get_owned_order(user, order_number):
    try:
        return Order.objects.get(number=order_number, user=user)
    except Order.DoesNotExist:
        raise errors.NotFound("No such order for this account")


def _get_order(order_number):
    try:
        return Order.objects.get(number=order_number)
    except Order.DoesNotExist:
        raise errors.NotFound("No such order")


def _get_owned_bankcard(user, payment_method_id):
    try:
        return Bankcard.objects.get(pk=payment_method_id, user=user)
    except (Bankcard.DoesNotExist, ValueError, TypeError):
        raise errors.NotFound("No such saved card for this account")


def _payment_for_update(order):
    try:
        return PayPalPayment.objects.select_for_update().get(order=order)
    except PayPalPayment.DoesNotExist:
        raise errors.Conflict("This order has no PayPal payment record")


# ---------------------------------------------------------------------------
# Order status helper
# ---------------------------------------------------------------------------


def _set_order_status(order, status):
    try:
        if status in order.available_statuses():
            order.set_status(status)
        else:
            # Keep going even if the sample pipeline forbids the move; payment state
            # is authoritative and tracked on PayPalPayment.
            order.status = status
            order.save()
    except Exception:  # noqa: BLE001 -- status pipeline must never break a real payment op
        logger.warning("Could not move order %s to %s", order.number, status, exc_info=True)


# ---------------------------------------------------------------------------
# Flow 1 -- place an order
# ---------------------------------------------------------------------------


def _default_shipping_address(user):
    country = (
        Country.objects.filter(is_shipping_country=True).first() or Country.objects.first()
    )
    if country is None:
        raise errors.ApiError(
            "No country data loaded; run oscar_populate_countries", status_code=500
        )
    name = (getattr(user, "get_full_name", lambda: "")() or user.get_username() or "Customer")
    parts = name.split(" ", 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else "Customer"
    return ShippingAddress(
        first_name=first,
        last_name=last,
        line1="N/A",
        line4="N/A",
        postcode="0000",
        country=country,
    )


@transaction.atomic
def create_order(user, items, request):
    """Place an Oscar order from catalogue items and open its PayPal payment record."""
    if not items:
        raise errors.BadRequest("No items supplied")

    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(request=request, user=user)
    for item in items:
        product_id = item.get("productId")
        quantity = item.get("quantity", 1)
        if product_id is None:
            raise errors.BadRequest("Each item needs a productId")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise errors.BadRequest("quantity must be an integer")
        if quantity < 1:
            raise errors.BadRequest("quantity must be at least 1")
        try:
            product = Product.objects.get(id=product_id)
        except Product.DoesNotExist:
            raise errors.BadRequest("No product with id %s" % product_id)
        info = basket.strategy.fetch_for_product(product)
        if info.stockrecord is None or info.price.excl_tax is None:
            raise errors.BadRequest("Product %s is not purchasable" % product_id)
        basket.add_product(product, quantity)

    if basket.is_empty:
        raise errors.BadRequest("Basket is empty")

    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)
    order_number = OrderNumberGenerator().order_number(basket)
    shipping_address = _default_shipping_address(user)
    # OrderCreator points the order at this address but does not persist it, so save
    # it first (as Oscar's own checkout flow does).
    shipping_address.save()

    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        user=user,
        shipping_address=shipping_address,
        order_number=order_number,
        status=settings.OSCAR_INITIAL_ORDER_STATUS,
    )
    basket.submit()

    PayPalPayment.objects.create(
        order=order,
        currency=settings.PAYPAL_CURRENCY,
        amount=order.total_incl_tax,
        state=PayPalPayment.AWAITING_PAYMENT,
    )
    return order


# ---------------------------------------------------------------------------
# Flow 1 -- pay (authorize)
# ---------------------------------------------------------------------------


def _validate_card(card):
    if not isinstance(card, dict):
        raise errors.BadRequest("card must be an object")
    number = str(card.get("number", "")).replace(" ", "")
    expiry = str(card.get("expiry", ""))
    security_code = str(card.get("securityCode") or card.get("security_code") or "")
    if not number or not expiry or not security_code:
        raise errors.BadRequest("card requires number, expiry (YYYY-MM) and securityCode")
    return {
        "number": number,
        "expiry": expiry,
        "security_code": security_code,
        "name": card.get("name") or "",
    }


def _record_source(order, payment, label, txn_type, amount, reference, status):
    source_type, _ = SourceType.objects.get_or_create(name=PAYPAL_SOURCE_NAME)
    source, created = Source.objects.get_or_create(
        order=order,
        source_type=source_type,
        defaults={
            "currency": payment.currency,
            "reference": payment.paypal_order_id,
            "label": label or PAYPAL_SOURCE_NAME,
        },
    )
    if txn_type == "Authorise":
        source.amount_allocated = amount
    elif txn_type == "Debit":
        source.amount_debited = amount
    elif txn_type == "Refund":
        source.amount_refunded = (source.amount_refunded or Decimal("0.00")) + amount
    if label and not source.label:
        source.label = label
    source.reference = payment.paypal_order_id
    source.save()
    source.transactions.create(
        txn_type=txn_type, amount=amount, reference=reference or "", status=status or ""
    )
    return source


@transaction.atomic
def authorize_order(user, order_number, *, card=None, payment_method_id=None, request=None):
    """Authorize (hold) the order total with a one-off card or a saved card."""
    order = _get_owned_order(user, order_number)
    payment = _payment_for_update(order)

    # Idempotent: already holding funds -> return as-is, never authorize twice.
    if payment.state == PayPalPayment.AUTHORIZED and payment.authorization_id:
        return payment
    if payment.state != PayPalPayment.AWAITING_PAYMENT:
        raise errors.Conflict(
            "Order %s is not awaiting payment (state=%s)" % (order.number, payment.state)
        )

    vault_id = None
    label = None
    validated_card = None
    if payment_method_id is not None:
        bankcard = _get_owned_bankcard(user, payment_method_id)
        if not bankcard.partner_reference:
            raise errors.Conflict("That saved card can no longer be used")
        vault_id = bankcard.partner_reference
        label = str(bankcard)
    elif card is not None:
        validated_card = _validate_card(card)
        label = "Card ending %s" % validated_card["number"][-4:]
    else:
        raise errors.BadRequest("Provide either card or paymentMethodId")

    # Create the PayPal order once (durable claim), then authorize with the card.
    if not payment.paypal_order_id:
        if not payment.invoice_reference:
            payment.invoice_reference = "%s-%s" % (order.number, uuid.uuid4().hex[:12])
        payment.paypal_order_id = paypal_gateway.create_paypal_order(
            order.number, payment.invoice_reference, payment.amount, payment.currency
        )
        payment.save(update_fields=["paypal_order_id", "invoice_reference", "updated_at"])

    result = paypal_gateway.authorize_paypal_order(
        payment.paypal_order_id, order.number, card=validated_card, vault_id=vault_id
    )

    payment.authorization_id = result["authorization_id"]
    payment.authorization_status = result["status"]
    payment.authorization_expiry = result["expiry"]
    payment.state = PayPalPayment.AUTHORIZED
    payment.save()

    _record_source(
        order,
        payment,
        label,
        "Authorise",
        payment.amount,
        payment.authorization_id,
        payment.authorization_status,
    )
    _set_order_status(order, "Being processed")
    return payment


# ---------------------------------------------------------------------------
# Flow 1 -- fulfil (capture)
# ---------------------------------------------------------------------------


def _renew_authorization(payment):
    """Reauthorize a stale hold. Raises a plain-language Conflict if it cannot be renewed."""
    try:
        result = paypal_gateway.reauthorize(
            payment.authorization_id, payment.amount, payment.currency
        )
    except errors.ProviderUnavailable:
        raise
    except errors.ApiError:
        raise errors.Conflict(
            "The authorization for order %s has expired and can no longer be renewed. "
            "Ask the shopper to pay for the order again." % payment.order.number
        )
    payment.authorization_id = result["authorization_id"]
    payment.authorization_status = result["status"]
    payment.authorization_expiry = result["expiry"]
    payment.save(
        update_fields=[
            "authorization_id",
            "authorization_status",
            "authorization_expiry",
            "updated_at",
        ]
    )
    return payment.authorization_id


@transaction.atomic
def fulfil_order(order_number):
    """Operator marks the order fulfilled; this is when the money is actually taken."""
    order = _get_order(order_number)
    payment = _payment_for_update(order)

    # Idempotent: already captured -> return the existing capture.
    if payment.state in (
        PayPalPayment.CAPTURED,
        PayPalPayment.PARTIALLY_REFUNDED,
        PayPalPayment.REFUNDED,
    ):
        return payment
    if payment.state != PayPalPayment.AUTHORIZED:
        raise errors.Conflict(
            "Order %s cannot be fulfilled from state %s" % (order.number, payment.state)
        )

    auth_id = payment.authorization_id
    if payment.is_authorization_stale():
        auth_id = _renew_authorization(payment)

    try:
        result = paypal_gateway.capture(auth_id, order.number, payment.amount, payment.currency)
    except paypal_gateway.AuthorizationUnusable:
        # Stale in a way our expiry check missed -> renew and capture once more.
        auth_id = _renew_authorization(payment)
        result = paypal_gateway.capture(auth_id, order.number, payment.amount, payment.currency)

    payment.capture_id = result["capture_id"]
    payment.capture_status = result["status"]
    payment.gross_amount = result["gross_amount"]
    payment.paypal_fee = result["paypal_fee"]
    payment.net_amount = result["net_amount"]
    payment.captured_at = result["captured_at"]
    payment.state = PayPalPayment.CAPTURED
    payment.save()

    _record_source(
        order,
        payment,
        None,
        "Debit",
        payment.amount,
        payment.capture_id,
        payment.capture_status,
    )
    _set_order_status(order, "Complete")
    return payment


# ---------------------------------------------------------------------------
# Flow 1 -- cancel (void)
# ---------------------------------------------------------------------------


@transaction.atomic
def cancel_order(order_number):
    """Operator cancels before fulfilment; the held funds are released."""
    order = _get_order(order_number)
    payment = _payment_for_update(order)

    if payment.state == PayPalPayment.VOIDED:
        return payment
    if payment.state in (
        PayPalPayment.CAPTURED,
        PayPalPayment.PARTIALLY_REFUNDED,
        PayPalPayment.REFUNDED,
    ):
        raise errors.Conflict(
            "Order %s has already been fulfilled; issue a refund instead of cancelling"
            % order.number
        )

    if payment.state == PayPalPayment.AUTHORIZED and payment.authorization_id:
        status = paypal_gateway.void(payment.authorization_id)
        payment.authorization_status = status
        _record_source(
            order, payment, None, "Void", Decimal("0.00"), payment.authorization_id, status
        )

    payment.state = PayPalPayment.VOIDED
    payment.save()
    _set_order_status(order, "Cancelled")
    return payment


# ---------------------------------------------------------------------------
# Flow 1 -- refund
# ---------------------------------------------------------------------------


@transaction.atomic
def refund_order(user, order_number, amount, idempotency_key):
    """Refund a captured payment, in full or in part, idempotent per key."""
    if not idempotency_key:
        raise errors.BadRequest("An idempotencyKey is required for refunds")
    order = _get_owned_order(user, order_number)
    payment = _payment_for_update(order)

    if payment.state not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED):
        raise errors.Conflict(
            "Order %s is not in a refundable state (state=%s)" % (order.number, payment.state)
        )

    # Repeat under the same key: return the same refund, never refund twice.
    existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
    if existing is not None and existing.refund_id:
        return existing

    # Resolve amount: default to the whole remaining refundable balance.
    refundable = payment.amount_refundable
    if amount is None:
        amount = refundable
    else:
        try:
            amount = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError):
            raise errors.BadRequest("amount must be a number")
    if amount <= Decimal("0.00"):
        raise errors.BadRequest("amount must be greater than zero")
    if amount > refundable:
        raise errors.Unprocessable(
            "Refund of %s exceeds the %s still refundable on order %s"
            % (amount, refundable, order.number)
        )

    # Durable claim: one row per (payment, key). A racing duplicate hits the unique
    # constraint and returns the winner's row rather than refunding again.
    row = existing
    if row is None:
        try:
            row = PayPalRefund.objects.create(
                payment=payment,
                idempotency_key=idempotency_key,
                amount=amount,
                currency=payment.currency,
                status=PayPalRefund.PENDING,
            )
        except IntegrityError:
            row = payment.refunds.get(idempotency_key=idempotency_key)
            if row.refund_id:
                return row

    result = paypal_gateway.refund(
        payment.capture_id, amount, payment.currency, idempotency_key
    )
    row.refund_id = result["refund_id"]
    row.status = result["status"]
    row.amount = result["amount"]
    row.save()

    _record_source(order, payment, None, "Refund", row.amount, row.refund_id, row.status)

    if payment.amount_refunded >= (payment.gross_amount or payment.amount):
        payment.state = PayPalPayment.REFUNDED
    else:
        payment.state = PayPalPayment.PARTIALLY_REFUNDED
    payment.save(update_fields=["state", "updated_at"])
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_orders(user):
    payments = (
        PayPalPayment.objects.filter(order__user=user)
        .select_related("order")
        .prefetch_related("refunds")
        .order_by("-created_at")
    )
    return [serialize_payment(p) for p in payments]


def serialize_payment(payment):
    order = payment.order
    return {
        "orderId": order.number,
        "state": payment.state,
        "orderStatus": order.status,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "paypalOrderId": payment.paypal_order_id or None,
        "authorizationId": payment.authorization_id or None,
        "authorizationStatus": payment.authorization_status or None,
        "authorizationExpiry": (
            payment.authorization_expiry.isoformat() if payment.authorization_expiry else None
        ),
        "captureId": payment.capture_id or None,
        "captureStatus": payment.capture_status or None,
        "grossAmount": str(payment.gross_amount) if payment.gross_amount is not None else None,
        "paypalFee": str(payment.paypal_fee) if payment.paypal_fee is not None else None,
        "netAmount": str(payment.net_amount) if payment.net_amount is not None else None,
        "amountRefunded": str(payment.amount_refunded),
        "amountRefundable": str(payment.amount_refundable),
        "refunds": [
            {
                "refundId": r.refund_id or None,
                "amount": str(r.amount),
                "status": r.status,
                "idempotencyKey": r.idempotency_key,
            }
            for r in payment.refunds.all()
        ],
    }


# ---------------------------------------------------------------------------
# Saved cards (Flow 2)
# ---------------------------------------------------------------------------


def _brand_to_card_type(brand):
    return (brand or "Card").replace("_", " ").title()


def _expiry_to_date(expiry):
    # PayPal returns "YYYY-MM"; store the first of that month (Oscar keeps only the month).
    try:
        year, month = expiry.split("-")
        return datetime.date(int(year), int(month), 1)
    except (ValueError, AttributeError):
        # Fall back to a far-future date so the record is still storable.
        return datetime.date(2099, 12, 1)


@transaction.atomic
def save_card(user, card):
    """Vault a card at PayPal and store a safe, obfuscated record for the shopper."""
    validated = _validate_card(card)
    vaulted = paypal_gateway.vault_card(user.id, validated)

    # Bankcard obfuscates the number on save; we pass an already-masked value so the
    # full PAN never reaches the database at all.
    masked = "XXXX-XXXX-XXXX-%s" % vaulted["last_digits"]
    bankcard = Bankcard(
        user=user,
        number=masked,
        expiry_date=_expiry_to_date(vaulted["expiry"]),
        name=vaulted["name"],
        partner_reference=vaulted["token"],
    )
    bankcard.card_type = _brand_to_card_type(vaulted["brand"])
    bankcard.save()
    return bankcard


def list_cards(user):
    return [serialize_card(bc) for bc in Bankcard.objects.filter(user=user).order_by("-id")]


def serialize_card(bankcard):
    return {
        "paymentMethodId": bankcard.id,
        "brand": bankcard.card_type,
        "maskedNumber": bankcard.number,
        "expiry": bankcard.expiry_month("%Y-%m"),
        "name": bankcard.name,
    }


@transaction.atomic
def delete_card(user, payment_method_id):
    bankcard = _get_owned_bankcard(user, payment_method_id)
    token = bankcard.partner_reference
    # Remove from PayPal's vault first so it can no longer be used to pay; then drop
    # the local record. A vault 404 is treated as already-gone.
    if token:
        paypal_gateway.delete_vault_token(token)
    bankcard.delete()


# ---------------------------------------------------------------------------
# Reconciliation (operator)
# ---------------------------------------------------------------------------


def _to_paypal_time(dt):
    dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S-0000")


def reconcile(from_dt, to_dt):
    """Line up PayPal's own transaction records against this app's orders for a range."""
    data = paypal_gateway.search_transactions(_to_paypal_time(from_dt), _to_paypal_time(to_dt))
    paypal_txns = data["transactions"]

    # App-side records captured/refunded in the window, filtered on PayPal's own clock.
    payments = (
        PayPalPayment.objects.filter(captured_at__gte=from_dt, captured_at__lt=to_dt)
        .exclude(capture_id="")
        .select_related("order")
        .prefetch_related("refunds")
    )

    app_records = []
    app_ids = set()
    app_order_numbers = set()
    app_invoice_refs = set()
    for payment in payments:
        app_order_numbers.add(payment.order.number)
        if payment.invoice_reference:
            app_invoice_refs.add(payment.invoice_reference)
        app_ids.add(payment.capture_id)
        app_records.append(
            {
                "orderId": payment.order.number,
                "type": "capture",
                "paypalId": payment.capture_id,
                "amount": str(payment.gross_amount or payment.amount),
                "currency": payment.currency,
            }
        )
        for r in payment.refunds.all():
            if not r.refund_id:
                continue
            app_ids.add(r.refund_id)
            app_records.append(
                {
                    "orderId": payment.order.number,
                    "type": "refund",
                    "paypalId": r.refund_id,
                    "amount": str(r.amount),
                    "currency": r.currency,
                }
            )

    paypal_ids = {t["transaction_id"] for t in paypal_txns if t["transaction_id"]}

    matched = []
    paypal_only = []
    for txn in paypal_txns:
        tid = txn["transaction_id"]
        invoice = txn["invoice_id"]
        custom = txn["custom_field"]
        if (
            (tid and tid in app_ids)
            or (invoice and invoice in app_invoice_refs)
            or (custom and custom in app_order_numbers)
        ):
            matched.append(txn)
        else:
            paypal_only.append(txn)

    app_only = [rec for rec in app_records if rec["paypalId"] not in paypal_ids]

    return {
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "paypalTransactionCount": len(paypal_txns),
        "appRecordCount": len(app_records),
        "matched": matched,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
        "truncated": data["truncated"],
    }
