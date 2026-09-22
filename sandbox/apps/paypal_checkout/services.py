"""Order + payment orchestration for the PayPal checkout API.

Orders are placed through Oscar's own :class:`OrderCreator` (reusing
``order.Order``/``order.Line``) and money movement is mirrored onto Oscar's
``payment.Source``/``payment.Transaction``. PayPal-owned state lives on this
app's models. Every payment mutation locks the :class:`PayPalPayment` row and is
idempotent in effect, so a double-click never authorizes or captures twice.
"""
import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from oscar.apps.checkout.calculators import OrderTotalCalculator
from oscar.apps.order.utils import OrderCreator
from oscar.apps.partner.strategy import Selector
from oscar.apps.shipping.methods import Free
from oscar.core.loading import get_model

from . import exceptions as exc
from . import gateway
from .models import PayPalCustomer, PayPalPayment, PayPalRefund, SavedCard

log = logging.getLogger("paypal_checkout")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")

# PayPal authorization statuses from which a capture can proceed directly.
_CAPTURABLE = {"CREATED", "PENDING"}
# Statuses that mean the hold is gone and cannot be renewed.
_DEAD_AUTH = {"VOIDED", "CAPTURED", "DENIED"}


def _source_type():
    st, _ = SourceType.objects.get_or_create(name="PayPal")
    return st


def _short_id():
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------------------- #
# Flow 1 -- place an order
# --------------------------------------------------------------------------- #
@transaction.atomic
def place_order(*, user, items):
    """Create an Oscar order from catalogue item ids/quantities for ``user``.

    ``items`` is a list of ``{"product_id": int, "quantity": int}``. The order
    starts awaiting payment. Returns the Oscar order.
    """
    if not items:
        raise exc.BadRequest("At least one item is required.")

    basket = Basket()
    basket.owner = user
    basket.save()
    basket.strategy = Selector().strategy(user=user)

    for entry in items:
        product_id = entry.get("product_id", entry.get("id"))
        quantity = entry.get("quantity", 1)
        if product_id is None:
            raise exc.BadRequest("Each item needs a product_id.")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise exc.BadRequest("Quantity must be an integer.")
        if quantity < 1:
            raise exc.BadRequest("Quantity must be at least 1.")
        try:
            product = Product.objects.get(id=product_id)
        except Product.DoesNotExist:
            raise exc.BadRequest(f"Product {product_id} does not exist.")
        try:
            basket.add_product(product, quantity=quantity)
        except ValueError as e:
            raise exc.BadRequest(f"Product {product_id} cannot be purchased: {e}")

    if basket.is_empty:
        raise exc.BadRequest("The order has no purchasable items.")

    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket=basket, shipping_charge=shipping_charge)

    creator = OrderCreator()
    order = creator.place_order(
        basket=basket,
        total=total,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        user=user,
    )

    PayPalPayment.objects.create(
        order=order,
        status=PayPalPayment.PENDING,
        currency=settings.PAYPAL_CURRENCY,
        order_total=order.total_incl_tax,
        request_id_base=_short_id(),
    )
    return order


# --------------------------------------------------------------------------- #
# Flow 1 -- pay (authorize)
# --------------------------------------------------------------------------- #
def _build_card_source(*, user, card, saved_card_id):
    """Return a PayPal card source dict from either one-off details or a saved card."""
    if saved_card_id is not None:
        try:
            saved = SavedCard.objects.get(id=saved_card_id, user=user)
        except SavedCard.DoesNotExist:
            raise exc.NotFound("Saved card not found.")
        return {"vault_id": saved.paypal_vault_id}, saved

    if not card:
        raise exc.BadRequest("Provide card details or a saved payment_method_id.")
    number = str(card.get("number", "")).replace(" ", "")
    expiry = card.get("expiry")
    if not number or not expiry:
        raise exc.BadRequest("Card number and expiry are required.")
    source = {"number": number, "expiry": expiry}
    if card.get("security_code"):
        source["security_code"] = str(card["security_code"])
    if card.get("name"):
        source["name"] = card["name"]
    billing = _billing_address(card.get("billing_address"))
    if billing:
        source["billing_address"] = billing
    return source, None


def _billing_address(raw):
    if not raw:
        return None
    fields = {
        "address_line_1": raw.get("address_line_1") or raw.get("line1"),
        "address_line_2": raw.get("address_line_2") or raw.get("line2"),
        "admin_area_2": raw.get("admin_area_2") or raw.get("city"),
        "admin_area_1": raw.get("admin_area_1") or raw.get("state"),
        "postal_code": raw.get("postal_code") or raw.get("postcode"),
        "country_code": raw.get("country_code") or raw.get("country"),
    }
    out = {k: v for k, v in fields.items() if v}
    return out or None


def pay_order(*, user, order, card=None, saved_card_id=None):
    """Authorize (hold) the order total against a card or saved card.

    Idempotent: if the order is already authorized (or further along) the current
    state is returned without contacting PayPal a second time.
    """
    card_source, _saved = _build_card_source(user=user, card=card, saved_card_id=saved_card_id)

    with transaction.atomic():
        payment = _lock_payment(order)
        if payment.status in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURED,
                              PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
            return payment  # already authorized/captured -- no double charge
        if payment.status == PayPalPayment.CANCELLED:
            raise exc.Conflict("This order was cancelled and can no longer be paid.")

        base = payment.request_id_base or _short_id()
        # Ensure a PayPal order exists (idempotent via stored id + request id).
        if not payment.paypal_order_id:
            created = gateway.create_order(
                currency=payment.currency,
                value=payment.order_total,
                # invoice_id must be globally unique on the merchant account, so
                # it carries a per-payment suffix; custom_id stays the bare order
                # number, which is what reconciliation matches on (custom_field).
                invoice_id=f"{order.number}-{base}",
                custom_id=str(order.number),
                request_id=f"{base}-order",
            )
            payment.paypal_order_id = created["id"]
            payment.request_id_base = base
            payment.save(update_fields=["paypal_order_id", "request_id_base", "updated_at"])

        result = gateway.authorize_order(
            paypal_order_id=payment.paypal_order_id,
            card_source=card_source,
            request_id=f"{base}-auth",
        )

        authorized = result["amount"]
        if authorized is None:
            raise exc.ProviderUnreadable("PayPal did not report the authorized amount.")
        if authorized != payment.order_total:
            log.error("Authorized amount %s != order total %s for order %s",
                      authorized, payment.order_total, order.number)
            raise exc.Conflict(
                "The authorized amount did not match the order total; authorization not accepted."
            )

        payment.authorization_id = result["authorization_id"] or ""
        payment.authorized_amount = authorized
        payment.status = PayPalPayment.AUTHORIZED
        payment.authorization_expires_at = _parse_dt(result.get("expires_at"))
        payment.save()

        _record_source(order, payment).allocate(
            authorized, reference=payment.authorization_id, status=result["status"] or ""
        )
    return payment


# --------------------------------------------------------------------------- #
# Flow 1 -- fulfil (capture)
# --------------------------------------------------------------------------- #
def fulfil_order(*, order):
    """Operator action: capture the held funds and mark the order fulfilled.

    A stale authorization is renewed (reauthorized) before capture; one that can
    no longer be renewed raises a clear, operator-actionable error.
    """
    with transaction.atomic():
        payment = _lock_payment(order)
        if payment.status in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED,
                              PayPalPayment.REFUNDED):
            return payment  # already captured -- idempotent
        if payment.status != PayPalPayment.AUTHORIZED:
            raise exc.Conflict(
                f"Order cannot be fulfilled from state '{payment.status}'. "
                "It must be authorized first."
            )

        base = payment.request_id_base or _short_id()
        auth_id = payment.authorization_id
        status = gateway.get_authorization_status(auth_id)

        if status not in _CAPTURABLE:
            auth_id = _renew_authorization(payment, current_status=status, base=base)

        result = gateway.capture_authorization(
            authorization_id=auth_id, request_id=f"{base}-cap"
        )
        if (result["status"] or "").upper() not in ("COMPLETED", "PENDING"):
            raise exc.Conflict(
                f"PayPal reported capture status '{result['status']}'; funds were not taken."
            )

        payment.capture_id = result["capture_id"]
        payment.captured_amount = result["captured"] or payment.authorized_amount
        payment.paypal_fee = result["fee"] or Decimal("0.00")
        payment.net_amount = result["net"] or Decimal("0.00")
        payment.status = PayPalPayment.CAPTURED
        payment.save()

        _record_source(order, payment).debit(
            payment.captured_amount, reference=payment.capture_id, status=result["status"] or ""
        )
        _advance_status(order, "Being processed")
    return payment


def _renew_authorization(payment, *, current_status, base):
    """Renew a stale authorization, or raise an operator-actionable error."""
    if current_status in _DEAD_AUTH:
        raise exc.Conflict(
            f"The payment authorization is '{current_status}' and cannot be renewed. "
            "Ask the shopper to pay again to place a fresh authorization."
        )
    # EXPIRED (or PENDING that never cleared) -- attempt reauthorization.
    try:
        renewed = gateway.reauthorize(
            authorization_id=payment.authorization_id,
            currency=payment.currency,
            value=payment.authorized_amount or payment.order_total,
            request_id=f"{base}-reauth",
        )
    except exc.ProviderRejected as e:
        raise exc.Conflict(
            "The payment authorization has expired and PayPal declined to renew it. "
            "Ask the shopper to pay again to place a fresh authorization."
        ) from e
    new_id = renewed.get("authorization_id") or payment.authorization_id
    if new_id != payment.authorization_id:
        payment.authorization_id = new_id
        payment.save(update_fields=["authorization_id", "updated_at"])
    return new_id


# --------------------------------------------------------------------------- #
# Flow 1 -- cancel (void)
# --------------------------------------------------------------------------- #
def cancel_order(*, order):
    """Operator action: void the authorization before fulfilment; no money moves."""
    with transaction.atomic():
        payment = _lock_payment(order)
        if payment.status == PayPalPayment.CANCELLED:
            return payment  # idempotent
        if payment.status in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED,
                              PayPalPayment.REFUNDED):
            raise exc.Conflict(
                "This order has already been fulfilled; use a refund to return funds."
            )

        base = payment.request_id_base or _short_id()
        if payment.status == PayPalPayment.AUTHORIZED and payment.authorization_id:
            gateway.void_authorization(
                authorization_id=payment.authorization_id, request_id=f"{base}-void"
            )
        payment.status = PayPalPayment.CANCELLED
        payment.save(update_fields=["status", "updated_at"])
        _advance_status(order, "Cancelled")
    return payment


# --------------------------------------------------------------------------- #
# Flow 1 -- refund
# --------------------------------------------------------------------------- #
def create_refund(*, order, amount, idempotency_key):
    """Refund the captured payment, in full or in part; idempotent per key."""
    if not idempotency_key:
        raise exc.BadRequest("An idempotency key is required for refunds.")

    with transaction.atomic():
        payment = _lock_payment(order)

        # Idempotent replay: same key -> return the existing refund.
        existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return payment, existing

        if payment.status not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED):
            raise exc.Conflict("Only a captured order can be refunded.")

        available = payment.amount_available_for_refund
        if amount is None:
            refund_amount = available
        else:
            refund_amount = _to_amount(amount)
            if refund_amount <= 0:
                raise exc.BadRequest("Refund amount must be positive.")
        if refund_amount > available:
            raise exc.Conflict(
                f"Refund of {refund_amount} exceeds the {available} still available to refund."
            )
        if refund_amount == 0:
            raise exc.Conflict("Nothing remains to refund on this order.")

        base = payment.request_id_base or _short_id()
        # A stable, key-derived request id gives PayPal-side idempotency too.
        request_id = f"{base}-refund-{idempotency_key}"[:100]
        full = refund_amount == available and payment.amount_refunded == 0

        result = gateway.refund_capture(
            capture_id=payment.capture_id,
            currency=payment.currency,
            value=None if full else refund_amount,
            invoice_id=f"{order.number}-{base}-R{payment.refunds.count() + 1}",
            custom_id=str(order.number),
            request_id=request_id,
        )
        actual = result["amount"] if result["amount"] is not None else refund_amount

        refund = PayPalRefund.objects.create(
            payment=payment,
            idempotency_key=idempotency_key,
            paypal_refund_id=result["refund_id"],
            amount=actual,
            currency=payment.currency,
            status=result["status"] or "",
        )
        payment.amount_refunded = payment.amount_refunded + actual
        payment.status = (
            PayPalPayment.REFUNDED
            if payment.amount_refunded >= payment.captured_amount
            else PayPalPayment.PARTIALLY_REFUNDED
        )
        payment.save(update_fields=["amount_refunded", "status", "updated_at"])

        _record_source(order, payment).refund(
            actual, reference=refund.paypal_refund_id, status=refund.status
        )
    return payment, refund


# --------------------------------------------------------------------------- #
# Flow 2 -- saved cards
# --------------------------------------------------------------------------- #
def save_card(*, user, card):
    """Vault a card for ``user`` and record a safe description of it."""
    source, _ = _build_card_source(user=user, card=card, saved_card_id=None)
    if "vault_id" in source:
        raise exc.BadRequest("Provide new card details to save, not an existing saved card.")

    customer = PayPalCustomer.objects.filter(user=user).first()
    customer_id = customer.paypal_customer_id if customer else None

    result = gateway.create_payment_token(
        card=source, customer_id=customer_id, request_id=_short_id()
    )

    if result["customer_id"] and not customer:
        PayPalCustomer.objects.get_or_create(
            user=user, defaults={"paypal_customer_id": result["customer_id"]}
        )

    saved = SavedCard.objects.create(
        user=user,
        paypal_vault_id=result["vault_id"],
        brand=result["brand"] or "",
        last_digits=result["last_digits"] or "",
        expiry=result["expiry"] or "",
        cardholder_name=result["name"] or (card.get("name") or ""),
    )
    return saved


def list_cards(*, user):
    return list(SavedCard.objects.filter(user=user))


def delete_card(*, user, card_id):
    try:
        saved = SavedCard.objects.get(id=card_id, user=user)
    except SavedCard.DoesNotExist:
        raise exc.NotFound("Saved card not found.")
    gateway.delete_payment_token(saved.paypal_vault_id)
    saved.delete()


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def reconcile(*, start_date, end_date):
    """Line PayPal's transactions for a range up against this app's orders."""
    transactions = gateway.search_transactions(start_date=start_date, end_date=end_date)

    # Index app payments by order number for matching.
    payments = {
        p.order.number: p
        for p in PayPalPayment.objects.select_related("order").all()
    }
    matched, paypal_only = [], []
    seen_numbers = set()

    for tx in transactions:
        # custom_field carries the bare order number; invoice_id is suffixed for
        # uniqueness (and for refunds), so fall back to its leading segment.
        base_number = tx.get("custom_field")
        if not base_number and tx.get("invoice_id"):
            base_number = tx["invoice_id"].split("-")[0]
        payment = payments.get(base_number) if base_number else None
        row = {
            "transaction_id": tx["transaction_id"],
            "amount": _dec_str(tx["amount"]),
            "currency": tx["currency"],
            "status": tx["status"],
            "invoice_id": tx["invoice_id"],
            "custom_field": tx["custom_field"],
            "initiated_at": tx["initiated_at"],
            "order_number": base_number if payment else None,
            "matched": payment is not None,
        }
        if payment is not None:
            seen_numbers.add(payment.order.number)
            matched.append(row)
        else:
            paypal_only.append(row)

    # App payments that PayPal's report does not (yet) show for this range.
    app_only = [
        {
            "order_number": number,
            "status": p.status,
            "paypal_order_id": p.paypal_order_id,
            "capture_id": p.capture_id,
            "captured_amount": _dec_str(p.captured_amount),
            "currency": p.currency,
        }
        for number, p in payments.items()
        if p.capture_id and number not in seen_numbers
    ]

    return {
        "matched": matched,
        "paypal_only": paypal_only,
        "app_only": app_only,
        "counts": {
            "paypal_transactions": len(transactions),
            "matched": len(matched),
            "paypal_only": len(paypal_only),
            "app_only": len(app_only),
        },
    }


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _lock_payment(order):
    try:
        return PayPalPayment.objects.select_for_update().get(order=order)
    except PayPalPayment.DoesNotExist:
        raise exc.NotFound("No payment exists for this order.")


def _record_source(order, payment):
    """Return (creating if needed) the Oscar payment Source for this order."""
    st = _source_type()
    source = order.sources.filter(source_type=st).first()
    if source is None:
        source = Source.objects.create(
            order=order,
            source_type=st,
            currency=payment.currency,
            reference=payment.paypal_order_id,
        )
    return source


def _advance_status(order, new_status):
    try:
        if new_status in order.available_statuses():
            order.set_status(new_status)
    except Exception as e:  # noqa: BLE001 - status pipeline is a sandbox convenience
        log.warning("Could not set order %s status to %s: %s", order.number, new_status, e)


def _parse_dt(value):
    if not value:
        return None
    parsed = timezone.datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed


def _to_amount(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        raise exc.BadRequest("Invalid refund amount.")


def _dec_str(value):
    if value is None:
        return None
    return str(value)
