"""Business logic for the PayPal payment + saved-card flows.

Views stay thin; every rule the task states lives here:

* orders start awaiting payment and reuse Oscar's ``Order``/``Line``;
* ``/pay`` authorizes (holds) the exact order total; ``/fulfil`` captures (takes) it,
  renewing a stale authorization first; ``/cancel`` voids (releases) it before capture;
  ``/refunds`` refunds after capture, never beyond what was captured;
* every money move is idempotent in effect;
* a saved card belongs only to the shopper who saved it, and one shopper never sees or
  acts on another's cards or orders.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from oscar.core.loading import get_class, get_model

from .gateway import PayPalError, format_amount, gateway
from .models import PayPalCustomer, PayPalPayment, PayPalRefund, SavedCard

logger = logging.getLogger("apps.api")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
Order = get_model("order", "Order")
ShippingAddress = get_model("order", "ShippingAddress")
Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")

# Human-readable Oscar order statuses mirrored for the dashboard (set directly; the
# sandbox defines no status pipeline, so we do not use set_status()).
ORDER_AWAITING_PAYMENT = "Awaiting payment"
ORDER_AUTHORIZED = "Authorized"
ORDER_FULFILLED = "Fulfilled"
ORDER_CANCELLED = "Cancelled"
ORDER_REFUNDED = "Refunded"
ORDER_PARTIALLY_REFUNDED = "Partially refunded"

# Authorization statuses (from AuthorizationStatus enum) — enumerate, never rely on else.
AUTH_OK = {"CREATED", "PENDING"}
AUTH_EXPIRED = "EXPIRED"
AUTH_DEAD = {"VOIDED", "DENIED"}


class ValidationProblem(PayPalError):
    """A 4xx caused by the caller's request rather than by PayPal."""

    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_request"):
        super().__init__(status, code, message)


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------
def _default_country():
    return (Country.objects.filter(is_shipping_country=True).first()
            or Country.objects.first())


def _parse_decimal(value, field: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationProblem(f"'{field}' must be a decimal amount.")
    return d


def _auth_expiry(authorization) -> datetime | None:
    exp = getattr(authorization, "expiration_time", None)
    if not exp or not isinstance(exp, str):
        return None
    try:
        return datetime.fromisoformat(exp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _breakdown_amount(money):
    if money is None:
        return None
    try:
        return Decimal(str(money.value))
    except (InvalidOperation, AttributeError, TypeError):
        return None


def _card_from_request(card: dict) -> dict:
    """Build a PayPal CardRequest dict from caller-supplied one-off card details.

    The PAN never touches our database or logs — it is passed straight to PayPal.
    """
    if not isinstance(card, dict):
        raise ValidationProblem("'card' must be an object with card details.")
    number = card.get("number")
    expiry = card.get("expiry")
    if not number or not expiry:
        raise ValidationProblem("Card 'number' and 'expiry' (YYYY-MM) are required.")
    out = {"number": str(number).replace(" ", ""), "expiry": str(expiry)}
    if card.get("security_code"):
        out["security_code"] = str(card["security_code"])
    if card.get("name"):
        out["name"] = str(card["name"])
    billing = card.get("billing_address")
    if isinstance(billing, dict):
        out["billing_address"] = billing
    return out


def _resolve_payment_source(user, body: dict) -> dict:
    """Either a one-off card, or the caller's own saved card (by paymentMethodId)."""
    saved_id = body.get("paymentMethodId") or body.get("savedCardId")
    if saved_id is not None:
        try:
            card = SavedCard.objects.get(pk=saved_id, user=user)
        except (SavedCard.DoesNotExist, ValueError, TypeError):
            # Scoped to the caller: another shopper's card is simply not found.
            raise ValidationProblem("Saved card not found.", status=404, code="not_found")
        return {"card": {"vault_id": card.vault_token_id}}
    if body.get("card"):
        return {"card": _card_from_request(body["card"])}
    raise ValidationProblem("Provide either 'card' details or a 'paymentMethodId'.")


# ---------------------------------------------------------------------------------------
# Flow 1 — orders & payments
# ---------------------------------------------------------------------------------------
def place_order(user, items) -> Order:
    """Create an Oscar order (awaiting payment) from catalogue item ids + quantities."""
    if not items or not isinstance(items, list):
        raise ValidationProblem("'items' must be a non-empty list of {productId, quantity}.")

    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(user=user)
    for entry in items:
        if not isinstance(entry, dict):
            raise ValidationProblem("Each item must be an object {productId, quantity}.")
        pid = entry.get("productId") or entry.get("product_id")
        qty = entry.get("quantity", 1)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise ValidationProblem("'quantity' must be an integer.")
        if qty < 1:
            raise ValidationProblem("'quantity' must be at least 1.")
        try:
            product = Product.objects.get(pk=pid)
        except (Product.DoesNotExist, ValueError, TypeError):
            raise ValidationProblem(f"Product {pid!r} does not exist.", status=404, code="not_found")
        info = basket.strategy.fetch_for_product(product)
        if not info.availability.is_available_to_buy:
            raise ValidationProblem(
                f"Product {pid} is not available to buy.", status=409, code="unavailable")
        basket.add_product(product, quantity=qty)

    if basket.is_empty:
        raise ValidationProblem("The basket is empty.")

    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)

    country = _default_country()
    if country is None:
        raise PayPalError(500, "no_country", "No country is configured for shipping.")
    shipping_address = ShippingAddress(
        first_name=(user.get_full_name() or user.get_username() or "Customer")[:255],
        last_name="", line1="N/A", line4="N/A", postcode="00000", country=country)
    shipping_address.save()

    with transaction.atomic():
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user,
            shipping_address=shipping_address, status=ORDER_AWAITING_PAYMENT)
        basket.submit()
        currency = _configured_currency()
        PayPalPayment.objects.create(
            order=order, state=PayPalPayment.PENDING,
            currency=currency, amount=order.total_incl_tax,
            reference=f"{order.number}-{uuid.uuid4().hex[:12]}")
    return order


def _configured_currency() -> str:
    from django.conf import settings
    return getattr(settings, "PAYPAL_CURRENCY", None) or getattr(
        settings, "OSCAR_DEFAULT_CURRENCY", "USD")


def get_owned_order(user, order_number: str) -> Order:
    """Fetch the caller's own order; another shopper's order is not found."""
    try:
        return Order.objects.get(number=order_number, user=user)
    except Order.DoesNotExist:
        raise ValidationProblem("Order not found.", status=404, code="not_found")


def get_any_order(order_number: str) -> Order:
    """Operator lookup: any order by number."""
    try:
        return Order.objects.get(number=order_number)
    except Order.DoesNotExist:
        raise ValidationProblem("Order not found.", status=404, code="not_found")


def pay_order(user, order, body: dict) -> PayPalPayment:
    """Authorize (hold) the exact order total. Idempotent: a double-click never authorizes twice."""
    payment_source = _resolve_payment_source(user, body)

    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(order=order)
        # Idempotent: already authorized (or beyond) — return the existing hold.
        if payment.state != PayPalPayment.PENDING:
            if payment.state in (PayPalPayment.AUTHORIZED,):
                return payment
            raise ValidationProblem(
                f"Order is already {payment.get_state_display().lower()}.",
                status=409, code="invalid_state")

        request_id = payment.authorize_request_id or f"auth-{payment.reference}"
        amount = format_amount(payment.amount, payment.currency)
        result = gateway.create_authorized_order(
            amount=amount, currency=payment.currency, custom_id=order.number,
            invoice_id=payment.reference, payment_source=payment_source,
            request_id=request_id)

        authorization = _extract_authorization(result)
        status = str(getattr(authorization, "status", "") or "")
        payment.paypal_order_id = result.id or ""
        payment.authorize_request_id = request_id
        payment.authorization_id = authorization.id or ""
        payment.authorization_status = status
        payment.authorization_expires_at = _auth_expiry(authorization)

        if status in AUTH_OK:
            payment.state = PayPalPayment.AUTHORIZED
            _set_order_status(order, ORDER_AUTHORIZED)
        elif status in AUTH_DEAD:
            payment.state = PayPalPayment.FAILED
            payment.save()
            raise ValidationProblem(
                "The card was declined by PayPal.", status=402, code="card_declined")
        else:
            # Unknown/intermediate status — do not claim success.
            payment.save()
            raise PayPalError(
                502, "authorization_unknown",
                f"PayPal returned an unexpected authorization status ({status}).")
        payment.save()
    return payment


def _extract_authorization(order_result):
    units = getattr(order_result, "purchase_units", None) or []
    if not units:
        _raise_no_authorization(order_result)
    payments = getattr(units[0], "payments", None)
    auths = getattr(payments, "authorizations", None) if payments else None
    if not auths:
        _raise_no_authorization(order_result)
    return auths[0]


def _raise_no_authorization(order_result):
    status = str(getattr(order_result, "status", "") or "")
    if status == "PAYER_ACTION_REQUIRED":
        # A browser approval / 3-D Secure challenge — out of scope by the task.
        raise PayPalError(
            402, "approval_required",
            "This card requires shopper approval in a browser, which this API cannot complete.")
    raise PayPalError(502, "no_authorization",
                      "PayPal did not return an authorization for the order.")


def fulfil_order(order) -> PayPalPayment:
    """Operator marks fulfilled — money is taken now. Renews a stale hold first."""
    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(order=order)
        if payment.state == PayPalPayment.CAPTURED or payment.capture_id:
            return payment  # idempotent
        if payment.state != PayPalPayment.AUTHORIZED:
            raise ValidationProblem(
                f"Order cannot be fulfilled while {payment.get_state_display().lower()}.",
                status=409, code="invalid_state")

        auth_id = _ensure_capturable_authorization(payment)

        request_id = payment.capture_request_id or f"cap-{payment.reference}"
        capture = gateway.capture(auth_id, request_id=request_id)
        status = str(getattr(capture, "status", "") or "")
        payment.capture_request_id = request_id
        payment.capture_id = capture.id or ""
        payment.capture_status = status
        if status in ("COMPLETED", "PENDING"):
            payment.captured_amount = _breakdown_amount(getattr(capture, "amount", None)) or payment.amount
            breakdown = getattr(capture, "seller_receivable_breakdown", None)
            if breakdown is not None:
                payment.paypal_fee = _breakdown_amount(getattr(breakdown, "paypal_fee", None))
                payment.net_amount = _breakdown_amount(getattr(breakdown, "net_amount", None))
                gross = _breakdown_amount(getattr(breakdown, "gross_amount", None))
                if gross is not None:
                    payment.captured_amount = gross
            payment.state = PayPalPayment.CAPTURED
            _set_order_status(order, ORDER_FULFILLED)
        else:
            payment.save()
            raise PayPalError(502, "capture_failed",
                              f"PayPal reported capture status {status}.")
        payment.save()
    return payment


def _ensure_capturable_authorization(payment: PayPalPayment) -> str:
    """Return an authorization id ready to capture, renewing an expired one.

    If it has expired and cannot be renewed, raise an error the operator can act on.
    """
    auth_id = payment.authorization_id
    if not auth_id:
        raise PayPalError(409, "no_authorization",
                          "This order has no authorization to capture.")
    authorization = gateway.get_authorization(auth_id)
    status = str(getattr(authorization, "status", "") or "")

    if status == AUTH_EXPIRED:
        amount = format_amount(payment.amount, payment.currency)
        try:
            renewed = gateway.reauthorize(
                auth_id, amount=amount, currency=payment.currency,
                request_id=f"reauth-{payment.reference}")
        except PayPalError as e:
            raise PayPalError(
                409, "authorization_expired",
                "The authorization for this order has expired and could not be renewed; "
                "ask the shopper to pay again.", detail=e.detail)
        # reauthorize yields a NEW authorization id.
        payment.authorization_id = renewed.id or auth_id
        payment.authorization_status = str(getattr(renewed, "status", "") or "")
        payment.authorization_expires_at = _auth_expiry(renewed)
        payment.save(update_fields=[
            "authorization_id", "authorization_status", "authorization_expires_at", "updated_at"])
        return payment.authorization_id
    if status in AUTH_DEAD:
        raise PayPalError(
            409, "authorization_dead",
            "The authorization for this order is no longer valid; ask the shopper to pay again.")
    return auth_id


def cancel_order(order) -> PayPalPayment:
    """Cancel before fulfilment: release the held funds (void). Idempotent."""
    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(order=order)
        if payment.state == PayPalPayment.VOIDED:
            return payment  # idempotent
        if payment.state == PayPalPayment.PENDING:
            # Nothing held yet — just close the order.
            payment.state = PayPalPayment.VOIDED
            payment.save()
            _set_order_status(order, ORDER_CANCELLED)
            return payment
        if payment.state != PayPalPayment.AUTHORIZED:
            raise ValidationProblem(
                f"Order cannot be cancelled while {payment.get_state_display().lower()}; "
                "use a refund instead.", status=409, code="invalid_state")
        result = gateway.void(payment.authorization_id)
        payment.authorization_status = str(getattr(result, "status", "") or "VOIDED")
        payment.state = PayPalPayment.VOIDED
        payment.save()
        _set_order_status(order, ORDER_CANCELLED)
    return payment


def refund_order(order, *, amount, idempotency_key: str) -> PayPalRefund:
    """Refund a captured payment, full or partial. Never beyond what was captured."""
    if not idempotency_key:
        raise ValidationProblem("An 'idempotencyKey' is required for refunds.")

    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(order=order)
        # Idempotent by caller key: repeating the same key must not refund twice.
        existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing

        if payment.state not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED):
            raise ValidationProblem(
                "Only a captured (fulfilled) order can be refunded.",
                status=409, code="invalid_state")

        refundable = payment.refundable_amount
        if amount is None:
            refund_amount = refundable
        else:
            refund_amount = _parse_decimal(amount, "amount")
        if refund_amount <= Decimal("0"):
            raise ValidationProblem("Refund amount must be greater than zero.")
        if refund_amount > refundable:
            raise ValidationProblem(
                f"Refund of {refund_amount} exceeds the {refundable} still refundable "
                f"on this order.", status=422, code="exceeds_refundable")

        value = format_amount(refund_amount, payment.currency)
        result = gateway.refund(
            payment.capture_id, amount=value, currency=payment.currency,
            request_id=idempotency_key)
        status = str(getattr(result, "status", "") or "")
        refund_value = _breakdown_amount(getattr(result, "amount", None)) or refund_amount

        try:
            refund = PayPalRefund.objects.create(
                payment=payment, refund_id=result.id or "", amount=refund_value,
                currency=payment.currency, status=status or "PENDING",
                idempotency_key=idempotency_key)
        except IntegrityError:
            # Concurrent request with the same key won the race — return its result.
            return payment.refunds.get(idempotency_key=idempotency_key)

        # Recompute state from the ledger (refunds sum vs captured).
        if payment.total_refunded >= (payment.captured_amount or Decimal("0")):
            payment.state = PayPalPayment.REFUNDED
            _set_order_status(order, ORDER_REFUNDED)
        else:
            payment.state = PayPalPayment.PARTIALLY_REFUNDED
            _set_order_status(order, ORDER_PARTIALLY_REFUNDED)
        payment.save(update_fields=["state", "updated_at"])
    return refund


def _set_order_status(order, status: str) -> None:
    order.status = status
    order.save(update_fields=["status"])


# ---------------------------------------------------------------------------------------
# Flow 2 — saved cards (vault)
# ---------------------------------------------------------------------------------------
def save_card(user, body: dict) -> SavedCard:
    """Vault a card for the signed-in shopper; store only safe descriptors."""
    card = _card_from_request(body.get("card") or body)

    customer_id = None
    cust = PayPalCustomer.objects.filter(user=user).first()
    if cust is not None:
        customer_id = cust.paypal_customer_id

    result = gateway.create_payment_token(
        card=card, customer_id=customer_id,
        request_id=f"vault-{user.pk}-{_now_stamp()}")

    token_id = getattr(result, "id", None)
    if not token_id:
        raise PayPalError(502, "vault_failed", "PayPal did not return a saved-card id.")

    returned_customer = getattr(getattr(result, "customer", None), "id", None)
    if returned_customer and customer_id is None:
        PayPalCustomer.objects.get_or_create(
            user=user, defaults={"paypal_customer_id": returned_customer})
        customer_id = returned_customer

    src_card = getattr(getattr(result, "payment_source", None), "card", None)
    saved = SavedCard.objects.create(
        user=user, vault_token_id=token_id,
        paypal_customer_id=customer_id or "",
        brand=str(getattr(src_card, "brand", "") or ""),
        last_digits=str(getattr(src_card, "last_digits", "") or ""),
        expiry=str(getattr(src_card, "expiry", "") or ""),
        cardholder_name=str(getattr(src_card, "name", "") or card.get("name", "") or ""),
    )
    return saved


def list_cards(user):
    return list(SavedCard.objects.filter(user=user))


def delete_card(user, payment_method_id) -> None:
    """Remove a saved card. Afterwards it must not appear or be usable to pay."""
    try:
        card = SavedCard.objects.get(pk=payment_method_id, user=user)
    except (SavedCard.DoesNotExist, ValueError, TypeError):
        raise ValidationProblem("Saved card not found.", status=404, code="not_found")
    gateway.delete_payment_token(card.vault_token_id)
    card.delete()


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


# ---------------------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------------------
def _parse_iso(value: str, field: str) -> datetime:
    if not value:
        raise ValidationProblem(f"'{field}' is required (ISO-8601 date-time).")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationProblem(f"'{field}' must be an ISO-8601 date-time.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_paypal(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S-0000")


def reconcile(from_str: str, to_str: str) -> dict:
    """List PayPal's own transactions for a range and line them up against app orders.

    Covers the WHOLE range (chunked into <=31-day windows, every page of each window),
    not just the first page. A range with no PayPal data yet is a legitimate empty result.
    """
    start = _parse_iso(from_str, "from")
    end = _parse_iso(to_str, "to")
    if end <= start:
        raise ValidationProblem("'to' must be after 'from'.")

    paypal_txns: list[dict] = []
    window_start = start
    max_window = timedelta(days=31)
    while window_start < end:
        window_end = min(window_start + max_window, end)
        paypal_txns.extend(_search_window(window_start, window_end))
        window_start = window_end

    # Index app payments in range by BOTH their order number (sent as custom_id) and their
    # unique reference (sent as invoice_id), so a PayPal txn carrying either lines up.
    payments_in_range = list(
        PayPalPayment.objects.select_related("order")
        .filter(created_at__gte=start, created_at__lte=end)
        .exclude(paypal_order_id=""))
    by_key: dict[str, PayPalPayment] = {}
    for p in payments_in_range:
        by_key[str(p.order.number)] = p
        by_key[p.reference] = p

    matched, paypal_only = [], []
    seen_orders: set[str] = set()
    for txn in paypal_txns:
        ref = txn.get("reference")
        p = by_key.get(ref) if ref else None
        if p is not None:
            seen_orders.add(str(p.order.number))
            matched.append({
                "orderId": p.order.number,
                "appState": p.state,
                "appAmount": str(p.amount),
                "paypalTransactionId": txn.get("transaction_id"),
                "paypalReference": ref,
                "paypalAmount": txn.get("amount"),
                "paypalStatus": txn.get("status"),
                "initiatedAt": txn.get("date"),
            })
        else:
            paypal_only.append(txn)

    app_only = [
        {"orderId": p.order.number, "appState": p.state, "appAmount": str(p.amount),
         "authorizationId": p.authorization_id, "captureId": p.capture_id}
        for p in payments_in_range if str(p.order.number) not in seen_orders
    ]

    return {
        "from": _fmt_paypal(start),
        "to": _fmt_paypal(end),
        "counts": {
            "paypalTransactions": len(paypal_txns),
            "matched": len(matched),
            "paypalOnly": len(paypal_only),
            "appOnly": len(app_only),
        },
        "matched": matched,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
    }


def _search_window(start: datetime, end: datetime) -> list[dict]:
    """Every page of one <=31-day window; the gateway returns plain, UNSET-free dicts."""
    out: list[dict] = []
    page = 1
    while True:
        result = gateway.search_transactions_page(
            start_date=_fmt_paypal(start), end_date=_fmt_paypal(end),
            page=page, page_size=100)
        out.extend(result["transactions"])
        if page >= result["total_pages"]:
            break
        page += 1
    return out
