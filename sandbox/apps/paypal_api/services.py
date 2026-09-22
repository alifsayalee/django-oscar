"""Business logic wiring PayPal (via :mod:`gateway`) to Oscar's own models.

Orders and lines are created through Oscar's ``OrderCreator`` and priced through
its basket/strategy machinery, so no parallel order model exists. Payment state
that PayPal owns is held on :class:`~apps.paypal_api.models.PayPalPayment`, and
the money movement is also mirrored into Oscar's own payment ledger
(``payment.Source`` / ``payment.Transaction``).
"""

import datetime
import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from oscar.apps.checkout.calculators import OrderTotalCalculator
from oscar.apps.partner import strategy
from oscar.apps.shipping.methods import Free
from oscar.core.loading import get_class, get_model

from . import gateway
from .gateway import PayPalError
from .models import PayPalCustomer, PayPalPayment, PayPalRefund, SavedCard

logger = logging.getLogger("paypal_api")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
OrderCreator = get_class("order.utils", "OrderCreator")

TWO_PLACES = Decimal("0.01")


class ServiceError(Exception):
    """A validation / state error surfaced at the API boundary."""

    def __init__(self, message, http_status=400, code="invalid"):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code


def _money_str(amount):
    return str(Decimal(amount).quantize(TWO_PLACES))


def _currency():
    from django.conf import settings

    return getattr(settings, "PAYPAL_CURRENCY", "USD") or "USD"


# --------------------------------------------------------------------------
# Oscar order creation
# --------------------------------------------------------------------------

def create_order_for_items(user, items):
    """Create a real Oscar order from ``[{"productId", "quantity"}, ...]``.

    Returns ``(order, payment)`` with the payment awaiting authorization.
    """
    if not items:
        raise ServiceError("At least one line item is required")

    basket = Basket.objects.create(owner=user)
    basket.strategy = strategy.Default()

    for entry in items:
        try:
            product_id = int(entry["productId"])
            quantity = int(entry.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            raise ServiceError(
                "Each item needs an integer 'productId' and 'quantity'"
            )
        if quantity < 1:
            raise ServiceError("Quantity must be at least 1")
        try:
            product = Product.objects.get(id=product_id)
        except Product.DoesNotExist:
            raise ServiceError("Product %s does not exist" % product_id, 404, "not_found")
        try:
            basket.add_product(product, quantity=quantity)
        except ValueError as e:
            # No price/stockrecord for this product under the strategy.
            raise ServiceError(
                "Product %s cannot be ordered: %s" % (product_id, e)
            )

    if basket.is_empty:
        raise ServiceError("Basket is empty")

    method = Free()
    shipping_charge = method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)

    country = Country.objects.first()
    if country is None:
        raise ServiceError(
            "No country configured; run oscar_populate_countries", 503, "not_configured"
        )
    shipping_address = ShippingAddress(
        first_name=getattr(user, "first_name", "") or "Sandbox",
        last_name=getattr(user, "last_name", "") or "Shopper",
        line1="1 Sandbox Way",
        line4="Sandboxville",
        postcode="00000",
        country=country,
    )
    shipping_address.save()

    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=method,
        shipping_charge=shipping_charge,
        user=user,
        shipping_address=shipping_address,
    )
    basket.submit()

    payment = PayPalPayment.objects.create(
        order=order,
        user=user,
        currency=_currency(),
        amount=Decimal(order.total_incl_tax).quantize(TWO_PLACES),
        status=PayPalPayment.PENDING_PAYMENT,
    )
    return order, payment


# --------------------------------------------------------------------------
# Oscar payment ledger helpers
# --------------------------------------------------------------------------

def _get_source(payment):
    source_type, _ = SourceType.objects.get_or_create(name="PayPal")
    source = payment.order.sources.filter(source_type=source_type).first()
    if source is None:
        source = Source.objects.create(
            order=payment.order,
            source_type=source_type,
            currency=payment.currency,
            amount_allocated=Decimal("0.00"),
            label="PayPal card",
        )
    return source


# --------------------------------------------------------------------------
# Pay (authorize)
# --------------------------------------------------------------------------

def _build_card_source(user, card_payload, saved_card_id):
    """Return the ``payment_source.card`` dict for a one-off or saved card."""
    if saved_card_id is not None:
        try:
            saved = SavedCard.objects.get(id=saved_card_id, user=user)
        except SavedCard.DoesNotExist:
            raise ServiceError(
                "Saved card %s not found" % saved_card_id, 404, "not_found"
            )
        return {"vault_id": saved.vault_id}, saved

    if not card_payload:
        raise ServiceError(
            "Provide either 'card' details or a 'savedCardId' to pay with"
        )
    number = str(card_payload.get("number", "")).replace(" ", "")
    expiry = card_payload.get("expiry")
    if not number or not expiry:
        raise ServiceError("Card 'number' and 'expiry' (YYYY-MM) are required")
    card = {"number": number, "expiry": expiry}
    if card_payload.get("securityCode"):
        card["security_code"] = str(card_payload["securityCode"])
    if card_payload.get("name"):
        card["name"] = str(card_payload["name"])
    billing = card_payload.get("billingAddress") or {}
    card["billing_address"] = {
        "address_line_1": billing.get("line1", "1 Sandbox Way"),
        "admin_area_2": billing.get("city", "Sandboxville"),
        "admin_area_1": billing.get("state", "CA"),
        "postal_code": billing.get("postcode", "95131"),
        "country_code": billing.get("countryCode", "US"),
    }
    return card, None


def pay_order(user, order_id, card_payload=None, saved_card_id=None):
    with transaction.atomic():
        try:
            payment = PayPalPayment.objects.select_for_update().get(
                order_id=order_id, user=user
            )
        except PayPalPayment.DoesNotExist:
            raise ServiceError("Order %s not found" % order_id, 404, "not_found")

        # Idempotent: already authorized/captured -> return current state.
        if payment.status in (
            PayPalPayment.AUTHORIZED,
            PayPalPayment.CAPTURED,
            PayPalPayment.PARTIALLY_REFUNDED,
            PayPalPayment.REFUNDED,
        ):
            return payment
        if payment.status == PayPalPayment.CANCELLED:
            raise ServiceError(
                "Order %s was cancelled and cannot be paid" % order_id, 409, "conflict"
            )

        card, _saved = _build_card_source(user, card_payload, saved_card_id)
        # One authorization attempt per order: persist a request id on first
        # attempt and reuse it, so a retried request is de-duped by PayPal's
        # own idempotency. (The DB lock + status check above is the primary
        # guard against a double-click authorizing twice.) A random id keeps it
        # unique across installs sharing a sandbox account.
        request_id = payment.auth_request_id or ("auth-%s" % uuid.uuid4().hex)

        result = gateway.authorize_with_card(
            currency=payment.currency,
            value=_money_str(payment.amount),
            order_number=payment.order.number,
            card=card,
            request_id=request_id,
        )

        payment.auth_request_id = request_id
        payment.paypal_order_id = result["order_id"] or ""
        payment.authorization_id = result["authorization_id"] or ""
        payment.authorization_status = result["authorization_status"] or ""
        payment.authorization_expiry = _parse_dt(result["authorization_expiry"])
        payment.status = PayPalPayment.AUTHORIZED
        payment.last_error = ""
        payment.save()

        source = _get_source(payment)
        source.allocate(
            payment.amount,
            reference=payment.authorization_id,
            status=payment.authorization_status,
        )
        return payment


# --------------------------------------------------------------------------
# Fulfil (capture, with stale-auth renewal)
# --------------------------------------------------------------------------

def fulfil_order(order_id):
    with transaction.atomic():
        try:
            payment = PayPalPayment.objects.select_for_update().get(order_id=order_id)
        except PayPalPayment.DoesNotExist:
            raise ServiceError("Order %s not found" % order_id, 404, "not_found")

        if payment.status in (
            PayPalPayment.CAPTURED,
            PayPalPayment.PARTIALLY_REFUNDED,
            PayPalPayment.REFUNDED,
        ):
            return payment  # already captured -> idempotent
        if payment.status != PayPalPayment.AUTHORIZED:
            raise ServiceError(
                "Order %s is not awaiting fulfilment (status: %s)"
                % (order_id, payment.status),
                409,
                "conflict",
            )

        auth_id = payment.authorization_id
        value = _money_str(payment.amount)

        # Renew a stale authorization rather than failing the fulfilment.
        info = gateway.get_authorization(auth_id)
        if (info["status"] or "").upper() == "EXPIRED":
            try:
                renewed = gateway.reauthorize(
                    auth_id=auth_id, currency=payment.currency, value=value
                )
            except PayPalError as e:
                raise ServiceError(
                    "The authorization for order %s expired and could not be "
                    "renewed (PayPal only re-authorizes within ~30 days of the "
                    "original hold). Ask the shopper to pay again, then fulfil. "
                    "(PayPal said: %s)" % (order_id, e.message),
                    409,
                    "authorization_unrenewable",
                )
            if not renewed["authorization_id"]:
                raise ServiceError(
                    "Order %s: authorization expired and re-authorization did "
                    "not return a new hold." % order_id,
                    409,
                    "authorization_unrenewable",
                )
            auth_id = renewed["authorization_id"]
            payment.authorization_id = auth_id
            payment.authorization_status = renewed["status"] or ""
            payment.authorization_expiry = _parse_dt(renewed["expiry"])

        cap = gateway.capture(
            auth_id=auth_id,
            request_id="cap-%s" % payment.order.number,
            currency=payment.currency,
            value=value,
        )

        payment.capture_id = cap["capture_id"] or ""
        payment.capture_status = cap["status"] or ""
        payment.captured_amount = _to_decimal(cap["amount"]) or payment.amount
        payment.paypal_fee = _to_decimal(cap["fee"])
        payment.net_amount = _to_decimal(cap["net"])
        payment.status = PayPalPayment.CAPTURED
        payment.save()

        # Move the Oscar order forward and take the money in the Oscar ledger.
        _advance_order_status(payment.order, ["Being processed", "Complete"])
        source = _get_source(payment)
        source.debit(
            payment.captured_amount,
            reference=payment.capture_id,
            status=payment.capture_status,
        )
        return payment


# --------------------------------------------------------------------------
# Cancel (void before fulfilment)
# --------------------------------------------------------------------------

def cancel_order(order_id):
    with transaction.atomic():
        try:
            payment = PayPalPayment.objects.select_for_update().get(order_id=order_id)
        except PayPalPayment.DoesNotExist:
            raise ServiceError("Order %s not found" % order_id, 404, "not_found")

        if payment.status == PayPalPayment.CANCELLED:
            return payment
        if payment.status != PayPalPayment.AUTHORIZED:
            raise ServiceError(
                "Only an authorized, not-yet-fulfilled order can be cancelled "
                "(order %s status: %s)" % (order_id, payment.status),
                409,
                "conflict",
            )

        gateway.void(payment.authorization_id)
        payment.status = PayPalPayment.CANCELLED
        payment.authorization_status = "VOIDED"
        payment.save()

        _advance_order_status(payment.order, ["Cancelled"])
        source = _get_source(payment)
        source.transactions.create(
            txn_type="Void",
            amount=payment.amount,
            reference=payment.authorization_id,
            status="VOIDED",
        )
        return payment


# --------------------------------------------------------------------------
# Refund (after fulfilment)
# --------------------------------------------------------------------------

def refund_order(user, order_id, amount=None, idempotency_key=None):
    if not idempotency_key:
        raise ServiceError("An 'idempotencyKey' is required for refunds")

    with transaction.atomic():
        try:
            payment = PayPalPayment.objects.select_for_update().get(
                order_id=order_id, user=user
            )
        except PayPalPayment.DoesNotExist:
            raise ServiceError("Order %s not found" % order_id, 404, "not_found")

        # Idempotent replay under the same key.
        existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return payment, existing

        if payment.status not in (
            PayPalPayment.CAPTURED,
            PayPalPayment.PARTIALLY_REFUNDED,
        ):
            raise ServiceError(
                "Order %s has not been captured; there is nothing to refund "
                "(status: %s)" % (order_id, payment.status),
                409,
                "conflict",
            )

        remaining = payment.refundable_amount
        if amount is None:
            refund_amount = remaining
        else:
            refund_amount = _to_decimal(amount)
            if refund_amount is None:
                raise ServiceError("Invalid refund 'amount'")
        refund_amount = refund_amount.quantize(TWO_PLACES)
        if refund_amount <= 0:
            raise ServiceError("Refund amount must be positive")
        if refund_amount > remaining:
            raise ServiceError(
                "Refund amount %s exceeds the remaining refundable amount %s"
                % (refund_amount, remaining),
                409,
                "over_refund",
            )

        result = gateway.refund(
            capture_id=payment.capture_id,
            request_id=idempotency_key,
            currency=payment.currency,
            value=str(refund_amount),
        )

        try:
            refund_row = PayPalRefund.objects.create(
                payment=payment,
                refund_id=result["refund_id"] or "",
                amount=refund_amount,
                currency=payment.currency,
                status=result["status"] or "",
                idempotency_key=idempotency_key,
            )
        except IntegrityError:
            # Concurrent request with the same key won the race.
            refund_row = payment.refunds.get(idempotency_key=idempotency_key)
            return payment, refund_row

        if payment.total_refunded >= payment.captured_amount:
            payment.status = PayPalPayment.REFUNDED
        else:
            payment.status = PayPalPayment.PARTIALLY_REFUNDED
        payment.save()

        source = _get_source(payment)
        source.refund(
            refund_amount, reference=refund_row.refund_id, status=refund_row.status
        )
        return payment, refund_row


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------

def _customer_id_for(user):
    record = PayPalCustomer.objects.filter(user=user).first()
    return record.customer_id if record else None


def save_card(user, card_payload):
    if not card_payload:
        raise ServiceError("Card details are required")
    number = str(card_payload.get("number", "")).replace(" ", "")
    expiry = card_payload.get("expiry")
    if not number or not expiry:
        raise ServiceError("Card 'number' and 'expiry' (YYYY-MM) are required")
    card = {"number": number, "expiry": expiry}
    if card_payload.get("securityCode"):
        card["security_code"] = str(card_payload["securityCode"])
    if card_payload.get("name"):
        card["name"] = str(card_payload["name"])
    billing = card_payload.get("billingAddress") or {}
    card["billing_address"] = {
        "address_line_1": billing.get("line1", "1 Sandbox Way"),
        "admin_area_2": billing.get("city", "Sandboxville"),
        "admin_area_1": billing.get("state", "CA"),
        "postal_code": billing.get("postcode", "95131"),
        "country_code": billing.get("countryCode", "US"),
    }

    result = gateway.create_vault_token(
        card=card,
        customer_id=_customer_id_for(user),
        request_id=str(uuid.uuid4()),
    )

    if result["customer_id"]:
        PayPalCustomer.objects.get_or_create(
            user=user, defaults={"customer_id": result["customer_id"]}
        )

    last = result["last_digits"] or number[-4:]
    brand = result["brand"] or ""
    saved = SavedCard.objects.create(
        user=user,
        vault_id=result["token_id"],
        brand=brand,
        last_digits=last,
        expiry=result["expiry"] or expiry,
        label=("%s ending %s" % (brand or "Card", last)).strip(),
    )
    return saved


def list_cards(user):
    return list(SavedCard.objects.filter(user=user))


def delete_card(user, card_id):
    try:
        saved = SavedCard.objects.get(id=card_id, user=user)
    except SavedCard.DoesNotExist:
        raise ServiceError("Saved card %s not found" % card_id, 404, "not_found")
    gateway.delete_vault_token(saved.vault_id)
    saved.delete()
    return True


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def _paypal_dt(value):
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


def reconcile(from_str, to_str):
    start = _require_dt(from_str, "from")
    end = _require_dt(to_str, "to")
    if end <= start:
        raise ServiceError("'to' must be after 'from'")

    # PayPal caps a single search window at 31 days; walk the range in chunks
    # and paginate every chunk so the whole range is covered.
    paypal_txns = {}
    window_start = start
    max_window = datetime.timedelta(days=31)
    while window_start < end:
        window_end = min(window_start + max_window, end)
        _collect_window(window_start, window_end, paypal_txns)
        window_start = window_end

    # App-side captured orders within the range.
    app_orders = {}
    for payment in PayPalPayment.objects.select_related("order").filter(
        capture_id__gt="", updated__gte=start, updated__lte=end
    ):
        app_orders[payment.order.number] = payment

    matched, paypal_only = [], []
    seen_numbers = set()
    for number, txn in paypal_txns.items():
        entry = {"reference": number, **txn}
        if number in app_orders:
            seen_numbers.add(number)
            payment = app_orders[number]
            entry["appOrderId"] = payment.order_id
            entry["appStatus"] = payment.status
            entry["appCapturedAmount"] = (
                str(payment.captured_amount)
                if payment.captured_amount is not None
                else None
            )
            matched.append(entry)
        else:
            paypal_only.append(entry)

    app_only = [
        {
            "reference": number,
            "appOrderId": payment.order_id,
            "appStatus": payment.status,
            "appCapturedAmount": (
                str(payment.captured_amount)
                if payment.captured_amount is not None
                else None
            ),
        }
        for number, payment in app_orders.items()
        if number not in seen_numbers
    ]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "counts": {
            "matched": len(matched),
            "paypalOnly": len(paypal_only),
            "appOnly": len(app_only),
        },
        "matched": matched,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
    }


def _collect_window(window_start, window_end, out):
    page = 1
    while True:
        resp = gateway.search_transactions(
            start_date=_paypal_dt(window_start),
            end_date=_paypal_dt(window_end),
            page=page,
        )
        details = _unset_to_list(getattr(resp, "transaction_details", None))
        for detail in details:
            info = getattr(detail, "transaction_info", None)
            info = None if info is gateway.UNSET else info
            if info is None:
                continue
            reference = _unset_to_none(getattr(info, "invoice_id", None)) or _unset_to_none(
                getattr(info, "custom_field", None)
            )
            record = {
                "paypalTransactionId": _unset_to_none(getattr(info, "transaction_id", None)),
                "amount": _money_of(getattr(info, "transaction_amount", None)),
                "fee": _money_of(getattr(info, "fee_amount", None)),
                "status": _unset_to_none(getattr(info, "transaction_status", None)),
                "initiationDate": _unset_to_none(
                    getattr(info, "transaction_initiation_date", None)
                ),
            }
            key = reference or (record["paypalTransactionId"] or str(uuid.uuid4()))
            # Keep the first (or a captured) record per reference.
            out.setdefault(key, record)

        total_pages = _unset_to_none(getattr(resp, "total_pages", None)) or 1
        if page >= total_pages:
            break
        page += 1


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _advance_order_status(order, statuses):
    for status in statuses:
        try:
            if status in order.available_statuses():
                order.set_status(status)
        except Exception as e:  # pragma: no cover - status pipeline guard
            logger.warning("Could not set order %s status to %s: %s", order.number, status, e)


def _to_decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_dt(value):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is not None and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, datetime.timezone.utc)
    return dt


def _require_dt(value, field):
    if not value:
        raise ServiceError("'%s' query parameter is required (ISO-8601)" % field)
    dt = parse_datetime(value)
    if dt is None:
        raise ServiceError("'%s' is not a valid ISO-8601 date-time" % field)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, datetime.timezone.utc)
    return dt


def _unset_to_none(value):
    return None if value is gateway.UNSET else value


def _unset_to_list(value):
    if value is gateway.UNSET or value is None:
        return []
    return value


def _money_of(m):
    m = _unset_to_none(m)
    if m is None:
        return None
    return _unset_to_none(getattr(m, "value", None))
