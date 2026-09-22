"""Domain orchestration for the PayPal flows.

Reuses Oscar's ``Order``/``Line`` (via ``OrderCreator``) and mirrors money into
``payment.Source``/``Transaction``; PayPal-owned state lives in the sidecars.
Every money operation is DB-atomic, idempotent in effect, and scoped to the
caller. Amounts come from the catalogue (``order.total_incl_tax``); the currency
comes from ``settings.PAYPAL_CURRENCY``.
"""
from __future__ import annotations

import uuid
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from oscar.core.loading import get_class, get_model

from . import money
from .exceptions import (
    PaymentConflict,
    PaymentError,
    PaymentRejected,
    ReauthorizationNeeded,
)
from .gateway import PayPalGateway
from .models import PayPalPayment, PayPalRefund, SavedCard

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Order = get_model("order", "Order")

Default = get_class("partner.strategy", "Default")
Free = get_class("shipping.methods", "Free")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")

PAYPAL_SOURCE_NAME = "PayPal"


def paypal_currency() -> str:
    return getattr(settings, "PAYPAL_CURRENCY", "USD")


# ---------------------------------------------------------------------------
# Flow 1 — place an order
# ---------------------------------------------------------------------------
def place_order(user, items):
    """Create an Oscar order from catalogue item ids + quantities.

    ``items`` is a list of ``{"productId": int, "quantity": int}``. Returns the
    created ``PayPalPayment`` (awaiting payment).
    """
    if not items:
        raise PaymentError("At least one order item is required")

    basket = Basket.objects.create(owner=user if user.is_authenticated else None)
    basket.strategy = Default()

    for entry in items:
        product_id = entry.get("productId") or entry.get("product_id")
        quantity = entry.get("quantity", 1)
        if product_id is None:
            raise PaymentError("Each item needs a productId")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise PaymentError("quantity must be an integer")
        if quantity < 1:
            raise PaymentError("quantity must be at least 1")
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise PaymentError(f"Unknown product {product_id}", code="unknown_product")

        info = basket.strategy.fetch_for_product(product)
        permitted, reason = info.availability.is_purchase_permitted(quantity)
        if not permitted:
            raise PaymentError(
                reason or f"Product {product_id} is not purchasable in quantity {quantity}",
                code="not_purchasable",
            )
        if not getattr(info.price, "exists", False) or info.price.excl_tax is None:
            raise PaymentError(f"Product {product_id} has no price", code="no_price")
        basket.add_product(product, quantity=quantity)

    if basket.is_empty:
        raise PaymentError("The order is empty")

    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)
    shipping_address = _build_shipping_address(user)

    order = OrderCreator().place_order(
        user=user if user.is_authenticated else None,
        basket=basket,
        shipping_address=shipping_address,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        total=total,
        status=settings.OSCAR_INITIAL_ORDER_STATUS,
    )
    basket.set_as_submitted()

    payment = PayPalPayment.objects.create(
        order=order,
        status=PayPalPayment.AWAITING_PAYMENT,
        currency=paypal_currency(),
        amount=order.total_incl_tax,
    )
    return payment


def _build_shipping_address(user):
    country = (
        Country.objects.filter(is_shipping_country=True).first()
        or Country.objects.first()
    )
    if country is None:
        # Countries are seeded by oscar_populate_countries; without one Oscar
        # cannot store a shipping address.
        raise PaymentError(
            "No Country rows exist; run oscar_populate_countries",
            code="no_country",
        )
    first_name = (getattr(user, "first_name", "") or "Sandbox").strip() or "Sandbox"
    last_name = (getattr(user, "last_name", "") or "Shopper").strip() or "Shopper"
    return ShippingAddress.objects.create(
        first_name=first_name,
        last_name=last_name,
        line1="1 Test Street",
        line4="Testville",
        postcode="00000",
        country=country,
    )


# ---------------------------------------------------------------------------
# Flow 1 — pay (authorize)
# ---------------------------------------------------------------------------
def pay(payment_id, *, user, card=None, saved_card_id=None, gateway=None):
    """Authorize the order total (place a hold). Idempotent: a repeat call once
    authorized returns the existing state without re-authorizing."""
    gateway = gateway or PayPalGateway()
    if card is None and saved_card_id is None:
        raise PaymentError("Provide either card details or a savedCardId")

    # 1) Claim the attempt under a row lock and resolve the saved card. The
    #    PayPal-Request-Id is deterministic per attempt: two concurrent
    #    duplicates of the same click send the same id and PayPal dedupes them
    #    to a single authorization; a genuine re-attempt after a failure uses
    #    the next attempt number (bumped only in the failure path below).
    with transaction.atomic():
        payment = _locked_payment_for_shopper(payment_id, user)
        if payment.status != PayPalPayment.AWAITING_PAYMENT:
            return payment  # already authorized (or beyond) — no second hold
        saved_card = None
        vault_id = None
        if saved_card_id is not None:
            saved_card = _get_owned_saved_card(saved_card_id, user)
            vault_id = saved_card.vault_id
        attempt = payment.pay_attempts
        order_number = payment.order.number
        amount = payment.amount
        currency = payment.currency

    # 2) Call PayPal OUTSIDE the DB transaction (never hold a write lock across
    #    a network call). A failed attempt is recorded so a retry re-attempts.
    try:
        result = gateway.authorize(
            amount=amount,
            currency=currency,
            order_number=order_number,
            request_id=f"auth-{order_number}-{attempt}",
            card=card,
            vault_id=vault_id,
        )
    except PaymentError:
        with transaction.atomic():
            payment = _locked_payment(payment_id)
            if payment.status == PayPalPayment.AWAITING_PAYMENT:
                payment.pay_attempts = attempt + 1
                payment.save(update_fields=["pay_attempts", "updated"])
        raise

    # 3) Persist the hold. If a concurrent duplicate already recorded it (same
    #    PayPal authorization via the deduped request id), return that.
    with transaction.atomic():
        payment = _locked_payment(payment_id)
        if payment.status != PayPalPayment.AWAITING_PAYMENT:
            return payment
        payment.paypal_order_id = result["paypal_order_id"]
        payment.authorization_id = result["authorization_id"]
        payment.authorization_status = result["authorization_status"]
        payment.status = PayPalPayment.AUTHORIZED
        payment.saved_card = saved_card
        payment.source = _record_source_allocation(payment)
        payment.save()
    return payment


# ---------------------------------------------------------------------------
# Flow 1 — fulfil (capture)
# ---------------------------------------------------------------------------
def fulfil(payment_id, *, gateway=None):
    """Operator marks the order fulfilled — capture the held funds now.

    A stale authorization is renewed (reauthorize) before capture rather than
    failing outright; one that can no longer be renewed raises a conflict the
    operator can act on.
    """
    gateway = gateway or PayPalGateway()
    with transaction.atomic():
        payment = _locked_payment(payment_id)

        if payment.status in (
            PayPalPayment.CAPTURED,
            PayPalPayment.PARTIALLY_REFUNDED,
            PayPalPayment.REFUNDED,
        ):
            return payment  # already fulfilled — no second capture
        if payment.status != PayPalPayment.AUTHORIZED:
            raise PaymentConflict(
                f"Order cannot be fulfilled from state {payment.status}; "
                "it must be authorized first"
            )

        auth_id = payment.authorization_id
        try:
            result = gateway.capture(auth_id, request_id=f"capture-{auth_id}")
        except ReauthorizationNeeded:
            result = _renew_and_capture(gateway, payment)

        payment.capture_id = result["capture_id"]
        payment.capture_status = result["status"]
        payment.captured_amount = result.get("gross_amount") or payment.amount
        payment.paypal_fee = result.get("paypal_fee")
        payment.net_amount = result.get("net_amount")
        payment.status = PayPalPayment.CAPTURED
        _record_source_debit(payment)
        _advance_order_status(payment.order, "Being processed")
        payment.save()
    return payment


def _renew_and_capture(gateway, payment):
    """Reauthorize a stale hold, then capture. Raise a conflict if unrenewable."""
    try:
        reauth = gateway.reauthorize(
            payment.authorization_id,
            request_id=f"reauth-{payment.authorization_id}-{uuid.uuid4().hex[:8]}",
        )
    except PaymentRejected as exc:
        raise PaymentConflict(
            "The authorization has expired and can no longer be renewed; "
            "a new payment is required for this order.",
            detail=exc.detail,
        ) from exc
    new_auth = reauth["authorization_id"]
    payment.authorization_id = new_auth
    payment.authorization_status = reauth["status"]
    return gateway.capture(new_auth, request_id=f"capture-{new_auth}")


# ---------------------------------------------------------------------------
# Flow 1 — cancel (void before fulfilment)
# ---------------------------------------------------------------------------
def cancel(payment_id, *, gateway=None):
    """Operator cancels before fulfilment — release the held funds."""
    gateway = gateway or PayPalGateway()
    with transaction.atomic():
        payment = _locked_payment(payment_id)

        if payment.status == PayPalPayment.CANCELLED:
            return payment  # idempotent — do not re-fire
        if payment.status in (
            PayPalPayment.CAPTURED,
            PayPalPayment.PARTIALLY_REFUNDED,
            PayPalPayment.REFUNDED,
        ):
            raise PaymentConflict(
                "Order has already been fulfilled; use a refund to return funds"
            )

        if payment.status == PayPalPayment.AUTHORIZED and payment.authorization_id:
            status = gateway.void(
                payment.authorization_id, request_id=f"void-{payment.authorization_id}"
            )
            payment.authorization_status = status or "VOIDED"

        payment.status = PayPalPayment.CANCELLED
        _advance_order_status(payment.order, "Cancelled")
        payment.save()
    return payment


# ---------------------------------------------------------------------------
# Flow 1 — refund (after fulfilment)
# ---------------------------------------------------------------------------
def refund(payment_id, *, user, amount=None, idempotency_key, gateway=None):
    """Refund a captured payment, in full or in part. The idempotency key makes
    a repeat a no-op; two distinct keys are two legitimate partial refunds."""
    gateway = gateway or PayPalGateway()
    if not idempotency_key:
        raise PaymentError("An idempotencyKey is required for refunds")

    with transaction.atomic():
        payment = _locked_payment_for_shopper(payment_id, user)

        if payment.status not in (
            PayPalPayment.CAPTURED,
            PayPalPayment.PARTIALLY_REFUNDED,
        ):
            raise PaymentConflict(
                f"Order in state {payment.status} cannot be refunded; "
                "it must be captured first"
            )

        # A repeat under the same key returns the original refund, never a
        # second one.
        existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return payment, existing

        refundable = payment.refundable_amount
        if amount is None:
            refund_amount = refundable
        else:
            refund_amount = _parse_money(amount, payment.currency)
            if refund_amount <= 0:
                raise PaymentError("Refund amount must be positive")
        # Never refundable beyond what was captured.
        if refund_amount > refundable:
            raise PaymentError(
                f"Refund of {refund_amount} exceeds the refundable balance "
                f"{refundable} {payment.currency}",
                code="refund_exceeds_balance",
            )

        # Claim the idempotency slot BEFORE calling PayPal, so a racing repeat
        # cannot produce a second provider refund.
        try:
            with transaction.atomic():
                refund_row = PayPalRefund.objects.create(
                    payment=payment,
                    amount=refund_amount,
                    currency=payment.currency,
                    idempotency_key=idempotency_key,
                    status="PENDING",
                )
        except IntegrityError:
            existing = payment.refunds.get(idempotency_key=idempotency_key)
            return payment, existing

        result = gateway.refund(
            payment.capture_id,
            amount=refund_amount,
            currency=payment.currency,
            request_id=idempotency_key,
            order_number=payment.order.number,
        )
        refund_row.refund_id = result["refund_id"]
        refund_row.status = result["status"]
        refund_row.save()

        payment.refunded_amount = (payment.refunded_amount or Decimal("0")) + refund_amount
        if payment.refunded_amount >= payment.captured_amount:
            payment.status = PayPalPayment.REFUNDED
        else:
            payment.status = PayPalPayment.PARTIALLY_REFUNDED
        _record_source_refund(payment, refund_amount, refund_row.refund_id, refund_row.status)
        payment.save()
    return payment, refund_row


# ---------------------------------------------------------------------------
# Flow 1 — my orders
# ---------------------------------------------------------------------------
def my_orders(user):
    payments = (
        PayPalPayment.objects.filter(order__user=user)
        .select_related("order")
        .order_by("-created")
    )
    return [p.describe() for p in payments]


# ---------------------------------------------------------------------------
# Flow 2 — saved cards
# ---------------------------------------------------------------------------
def save_card(user, *, card, gateway=None):
    gateway = gateway or PayPalGateway()
    result = gateway.vault_card(card=card, request_id=f"vault-{uuid.uuid4().hex}")
    label = f"{result['brand']} ending {result['last4']}".strip()
    saved = SavedCard.objects.create(
        user=user,
        vault_id=result["vault_id"],
        brand=result["brand"],
        last4=result["last4"],
        expiry=result["expiry"],
        label=label,
    )
    return saved


def list_saved_cards(user):
    return [c.describe() for c in SavedCard.objects.filter(user=user)]


def delete_saved_card(payment_method_id, *, user, gateway=None):
    gateway = gateway or PayPalGateway()
    card = _get_owned_saved_card(payment_method_id, user)
    try:
        gateway.delete_vault_card(card.vault_id)
    except PaymentRejected as exc:
        # 404 = already gone at PayPal; that is fine, proceed to remove locally.
        if exc.provider_status not in (404, None):
            raise
    card.delete()


# ---------------------------------------------------------------------------
# Reconciliation (operator)
# ---------------------------------------------------------------------------
def reconciliation(from_dt: str, to_dt: str, *, gateway=None):
    gateway = gateway or PayPalGateway()
    start = _parse_iso(from_dt, "from")
    end = _parse_iso(to_dt, "to")
    if end < start:
        raise PaymentError("'to' must not be before 'from'")

    search = gateway.search_transactions(
        _paypal_time(start), _paypal_time(end)
    )
    txns = search["transactions"]

    # App side, filtered on the same (PayPal-event) clock intent.
    payments = list(
        PayPalPayment.objects.filter(created__gte=start, created__lte=end).select_related("order")
    )
    by_number = {p.order.number: p for p in payments}

    matched: dict = {}
    paypal_only = []
    for t in txns:
        key = t.get("invoiceId") or t.get("customField")
        if key and key in by_number:
            matched.setdefault(key, []).append(t)
        else:
            paypal_only.append(t)

    matched_report = []
    for number, p in by_number.items():
        matched_report.append(
            {
                "orderNumber": number,
                "appStatus": p.status,
                "appAmount": str(p.amount),
                "currency": p.currency,
                "paypalTransactions": matched.get(number, []),
                "matched": bool(matched.get(number)),
            }
        )

    # App payments in range with no PayPal transaction found (a real gap, OR the
    # expected-empty case when PayPal's reporting still lags — documented below).
    app_only = [
        {"orderNumber": number, "appStatus": by_number[number].status}
        for number in by_number
        if not matched.get(number)
    ]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalTransactionCount": len(txns),
        "pagesScanned": search["pages"],
        "truncated": search["truncated"],
        "matched": matched_report,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
        "note": (
            "PayPal transaction reporting lags live activity; an empty or partial "
            "result over a very recent range is expected, not a missing capability."
        ),
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _locked_payment(payment_id):
    try:
        return (
            PayPalPayment.objects.select_for_update()
            .select_related("order")
            .get(pk=payment_id)
        )
    except PayPalPayment.DoesNotExist:
        raise PaymentError("Order not found", code="not_found").with_status(404)


def _locked_payment_for_shopper(payment_id, user):
    payment = _locked_payment(payment_id)
    if payment.order.user_id != getattr(user, "id", None):
        # Do not distinguish "not yours" from "not found".
        raise PaymentError("Order not found", code="not_found").with_status(404)
    return payment


def _get_owned_saved_card(card_id, user):
    try:
        return SavedCard.objects.get(pk=card_id, user=user)
    except SavedCard.DoesNotExist:
        raise PaymentError("Saved card not found", code="not_found").with_status(404)


def _parse_money(value, currency):
    try:
        return money.quantize(Decimal(str(value)), currency)
    except (InvalidOperation, ValueError):
        raise PaymentError("Invalid amount")


def _parse_iso(value, field):
    if not value:
        raise PaymentError(f"'{field}' is required (ISO-8601 date-time)")
    dt = parse_datetime(value)
    if dt is None and " " in value:
        # A "+" offset that arrived un-encoded (decoded to a space) is the
        # common case; recover it before giving up.
        dt = parse_datetime(value.replace(" ", "+"))
    if dt is None:
        raise PaymentError(f"'{field}' is not a valid ISO-8601 date-time")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, dt_timezone.utc)
    return dt


def _paypal_time(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


def _advance_order_status(order, target):
    """Best-effort Oscar order-status move; never blocks the money operation."""
    try:
        available = order.available_statuses()
        if target in available:
            order.set_status(target)
    except Exception:  # pragma: no cover - status pipeline is advisory here
        pass


# --- Oscar payment.Source mirroring ---------------------------------------
def _paypal_source_type():
    st, _ = SourceType.objects.get_or_create(name=PAYPAL_SOURCE_NAME)
    return st


def _record_source_allocation(payment):
    source = Source.objects.create(
        order=payment.order,
        source_type=_paypal_source_type(),
        currency=payment.currency,
        reference=payment.authorization_id,
        label=_source_label(payment),
    )
    source.allocate(
        payment.amount,
        reference=payment.authorization_id,
        status=payment.authorization_status,
    )
    return source


def _record_source_debit(payment):
    if payment.source is None:
        return
    payment.source.debit(
        payment.captured_amount,
        reference=payment.capture_id,
        status=payment.capture_status,
    )


def _record_source_refund(payment, amount, reference, status):
    if payment.source is None:
        return
    payment.source.refund(amount, reference=reference or "", status=status or "")


def _source_label(payment):
    if payment.saved_card is not None:
        return payment.saved_card.label
    return "PayPal card"
