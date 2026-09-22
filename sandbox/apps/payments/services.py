"""Application services: order placement and the PayPal payment lifecycle.

The HTTP layer (``views.py``) calls these functions; they own the Oscar-side work
(building a basket, placing the order, moving order/payment state) and delegate every
PayPal interaction to ``gateway.py``. PayPal calls are deliberately made *outside*
database transactions so a provider effect is never lost to a rollback; the durable
state that makes each action idempotent is written in short ``atomic`` blocks.
"""

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from oscar.apps.checkout.calculators import OrderTotalCalculator
from oscar.apps.order.utils import OrderCreator
from oscar.apps.shipping import methods as shipping_methods
from oscar.core import prices
from oscar.core.loading import get_class, get_model

from . import gateway
from .models import PayPalPayment, PayPalRefund, SavedCard

logger = logging.getLogger("sandbox.payments")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Selector = get_class("partner.strategy", "Selector")

MAX_RECON_PAGES = 200
RECON_WINDOW_DAYS = 30


class ServiceError(Exception):
    """A business-rule failure with an HTTP status for the API boundary."""

    def __init__(self, message, status_code=400, *, issues=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.issues = issues or []


def invoice_id_for(order):
    """Stable external id linking a PayPal transaction back to this app's order."""
    return f"oscar-{order.number}"


# ---------------------------------------------------------------------------
# order placement
# ---------------------------------------------------------------------------


def _default_country():
    country = (
        Country.objects.filter(iso_3166_1_a2="US").first()
        or Country.objects.filter(is_shipping_country=True).first()
        or Country.objects.first()
    )
    if country is None:
        raise ServiceError(
            "No countries are configured; run oscar_populate_countries.", 500
        )
    return country


def place_order(user, items):
    """Place an Oscar order for ``user`` from catalogue item ids and quantities.

    ``items`` is a list of ``{"product_id": int, "quantity": int}``. Reuses Oscar's
    basket/order machinery; the order starts awaiting payment.
    """
    if not items:
        raise ServiceError("At least one order line is required.", 400)

    strategy = Selector().strategy(user=user)
    basket = Basket()
    basket.strategy = strategy
    basket.save()
    if user is not None:
        basket.owner = user
        basket.save()

    for entry in items:
        try:
            product_id = int(entry["product_id"])
            quantity = int(entry.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            raise ServiceError(
                "Each item needs an integer product_id and quantity.", 400
            )
        if quantity < 1:
            raise ServiceError("Quantity must be at least 1.", 400)
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise ServiceError(f"Product {product_id} does not exist.", 404)

        info = strategy.fetch_for_product(product)
        if info.stockrecord is None or not info.availability.is_purchase_permitted(
            quantity
        )[0]:
            raise ServiceError(
                f"Product {product_id} ({product.get_title()}) is not purchasable.",
                409,
            )
        basket.add_product(product, quantity=quantity)

    if basket.is_empty:
        raise ServiceError("The order is empty.", 400)

    currency = settings.PAYPAL_CURRENCY
    shipping_method = shipping_methods.Free()
    shipping_charge = prices.Price(
        currency=currency, excl_tax=Decimal("0.00"), incl_tax=Decimal("0.00")
    )
    # Value from catalogue prices; currency from configuration (PAYPAL_CURRENCY).
    base_total = OrderTotalCalculator().calculate(basket, shipping_charge)
    total = prices.Price(
        currency=currency,
        excl_tax=base_total.excl_tax,
        incl_tax=base_total.incl_tax,
    )

    with transaction.atomic():
        shipping_address = ShippingAddress(
            first_name=(getattr(user, "first_name", "") or "Sandbox"),
            last_name=(getattr(user, "last_name", "") or "Shopper"),
            line1="1 Sandbox Way",
            line4="San Jose",
            state="CA",
            postcode="95131",
            country=_default_country(),
        )
        shipping_address.save()
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=shipping_address,
            status=settings.OSCAR_INITIAL_ORDER_STATUS,
        )
        basket.submit()
        PayPalPayment.objects.create(
            order=order,
            currency=currency,
            status=PayPalPayment.AWAITING_PAYMENT,
        )
    return order


# ---------------------------------------------------------------------------
# helpers for the payment lifecycle
# ---------------------------------------------------------------------------


def _get_source(order, payment):
    if payment.source_id:
        return payment.source
    source_type, _ = SourceType.objects.get_or_create(
        code="paypal", defaults={"name": "PayPal"}
    )
    source = Source.objects.create(
        order=order,
        source_type=source_type,
        currency=payment.currency,
        amount_allocated=Decimal("0.00"),
    )
    return source


def _order_total(order):
    return order.total_incl_tax


def order_payment_state(order):
    """A JSON-safe view of an order plus its PayPal payment state."""
    payment = getattr(order, "paypal_payment", None)
    data = {
        "orderId": order.number,
        "status": order.status,
        "currency": order.currency,
        "total": f"{order.total_incl_tax:.2f}",
        "datePlaced": order.date_placed.isoformat() if order.date_placed else None,
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "linePrice": f"{line.line_price_incl_tax:.2f}",
            }
            for line in order.lines.all()
        ],
    }
    if payment is not None:
        data["payment"] = {
            "state": payment.status,
            "paypalOrderId": payment.paypal_order_id,
            "authorizationId": payment.authorization_id,
            "authorizationStatus": payment.authorization_status,
            "captureId": payment.capture_id,
            "captureStatus": payment.capture_status,
            "capturedAmount": (
                f"{payment.gross_amount:.2f}" if payment.gross_amount is not None else None
            ),
            "paypalFee": (
                f"{payment.paypal_fee:.2f}" if payment.paypal_fee is not None else None
            ),
            "netAmount": (
                f"{payment.net_amount:.2f}" if payment.net_amount is not None else None
            ),
            "totalRefunded": f"{payment.total_refunded:.2f}",
            "refundableAmount": f"{payment.refundable_amount:.2f}",
            "refunds": [r.as_dict() for r in payment.refunds.all()],
        }
    return data


# ---------------------------------------------------------------------------
# pay / fulfil / cancel / refund
# ---------------------------------------------------------------------------


def pay_order(order, *, raw_card=None, saved_card_id=None):
    """Authorize the order total: place a hold on the money without taking it."""
    payment = order.paypal_payment
    if payment.status in (
        PayPalPayment.AUTHORIZED,
        PayPalPayment.CAPTURED,
        PayPalPayment.PARTIALLY_REFUNDED,
        PayPalPayment.REFUNDED,
    ):
        # Idempotent: a double-click never authorizes twice.
        return payment
    if payment.status == PayPalPayment.VOIDED:
        raise ServiceError("This order was cancelled and cannot be paid.", 409)

    vault_id = None
    if saved_card_id:
        card_row = SavedCard.objects.filter(
            user=order.user, vault_id=saved_card_id
        ).first()
        if card_row is None:
            raise ServiceError("Saved card not found for this shopper.", 404)
        vault_id = card_row.vault_id
    elif raw_card:
        _validate_raw_card(raw_card)
    else:
        raise ServiceError(
            "Provide either card details or a saved paymentMethodId.", 400
        )

    result = gateway.create_authorized_order(
        amount=_order_total(order),
        currency=payment.currency,
        invoice_id=invoice_id_for(order),
        custom_id=str(order.number),
        request_id=f"pay-{order.number}",
        card=raw_card,
        vault_id=vault_id,
    )

    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        source = _get_source(order, payment)
        source.allocate(
            _order_total(order),
            reference=result["authorization_id"],
            status=result["authorization_status"] or "",
        )
        payment.source = source
        payment.paypal_order_id = result["paypal_order_id"] or ""
        payment.authorization_id = result["authorization_id"] or ""
        payment.authorization_status = result["authorization_status"] or ""
        payment.authorization_expiry = result["authorization_expiry"]
        payment.status = PayPalPayment.AUTHORIZED
        payment.save()
        order.refresh_from_db()
        if order.status == settings.OSCAR_INITIAL_ORDER_STATUS:
            order.set_status("Being processed")
    return payment


def _validate_raw_card(raw_card):
    for field in ("number", "expiry"):
        if not raw_card.get(field):
            raise ServiceError(f"Card '{field}' is required.", 400)
    billing = raw_card.get("billing_address")
    if billing is not None and not billing.get("country_code"):
        raise ServiceError("billing_address.country_code is required.", 400)


def _stale_authorization(payment):
    expiry = payment.authorization_expiry
    return expiry is not None and expiry <= timezone.now()


def _looks_expired(error):
    text = " ".join(error.issues).upper() + " " + error.message.upper()
    return "EXPIR" in text or "AUTHORIZATION" in text and error.status_code in (422, 400)


def _renew_authorization(order, payment):
    try:
        renewed = gateway.reauthorize(
            authorization_id=payment.authorization_id,
            amount=_order_total(order),
            currency=payment.currency,
            request_id=f"reauth-{order.number}",
        )
    except gateway.PayPalError as exc:
        raise ServiceError(
            "The authorization has expired and could not be renewed. Ask the "
            "shopper to place the order and pay again.",
            409,
            issues=exc.issues,
        )
    with transaction.atomic():
        payment.authorization_id = renewed["authorization_id"] or payment.authorization_id
        payment.authorization_status = renewed["status"] or payment.authorization_status
        payment.authorization_expiry = renewed["expiry"]
        payment.save(
            update_fields=[
                "authorization_id",
                "authorization_status",
                "authorization_expiry",
                "updated",
            ]
        )
    return payment.authorization_id


def fulfil_order(order):
    """Mark the order fulfilled and take the money (capture the authorization).

    A stale authorization is renewed rather than failing the fulfilment; one that
    can no longer be renewed raises an operator-actionable error.
    """
    payment = order.paypal_payment
    if payment.status in (
        PayPalPayment.CAPTURED,
        PayPalPayment.PARTIALLY_REFUNDED,
        PayPalPayment.REFUNDED,
    ):
        return payment  # Idempotent: already captured.
    if payment.status != PayPalPayment.AUTHORIZED:
        raise ServiceError(
            "Order is not awaiting fulfilment; it must be authorized first.", 409
        )

    auth_id = payment.authorization_id
    if not auth_id:
        raise ServiceError("This order has no authorization to capture.", 409)

    if _stale_authorization(payment):
        auth_id = _renew_authorization(order, payment)

    request_id = f"capture-{order.number}"
    try:
        result = gateway.capture(authorization_id=auth_id, request_id=request_id)
    except gateway.PayPalError as exc:
        if exc.outcome_unknown:
            raise ServiceError(exc.message, 502, issues=exc.issues)
        if _looks_expired(exc):
            auth_id = _renew_authorization(order, payment)
            result = gateway.capture(authorization_id=auth_id, request_id=request_id)
        else:
            raise ServiceError(exc.message, exc.status_code, issues=exc.issues)

    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        source = _get_source(order, payment)
        payment.capture_id = result["capture_id"] or ""
        payment.capture_status = result["status"] or ""
        payment.gross_amount = result["gross_amount"]
        payment.paypal_fee = result["paypal_fee"]
        payment.net_amount = result["net_amount"]
        payment.status = PayPalPayment.CAPTURED
        payment.source = source
        payment.save()
        source.debit(
            _order_total(order),
            reference=result["capture_id"] or "",
            status=result["status"] or "",
        )
        order.refresh_from_db()
        if order.status in ("Pending", "Being processed"):
            order.set_status("Complete")
    return payment


def cancel_order(order):
    """Cancel before fulfilment: release the held funds so no money ever moved."""
    payment = order.paypal_payment
    if payment.status == PayPalPayment.VOIDED:
        return payment  # Idempotent.
    if payment.status in (
        PayPalPayment.CAPTURED,
        PayPalPayment.PARTIALLY_REFUNDED,
        PayPalPayment.REFUNDED,
    ):
        raise ServiceError(
            "This order has already been fulfilled; issue a refund instead.", 409
        )
    if payment.status != PayPalPayment.AUTHORIZED or not payment.authorization_id:
        raise ServiceError("There is no held payment to cancel.", 409)

    gateway.void(payment.authorization_id)

    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        payment.status = PayPalPayment.VOIDED
        payment.authorization_status = "VOIDED"
        payment.save(update_fields=["status", "authorization_status", "updated"])
        order.refresh_from_db()
        if order.status in ("Pending", "Being processed"):
            order.set_status("Cancelled")
    return payment


def refund_order(order, *, amount=None, idempotency_key):
    """Refund a captured payment, fully or partially. The idempotency key makes a
    repeated request return the same refund; two distinct keys are two refunds."""
    payment = order.paypal_payment
    if payment.status not in (
        PayPalPayment.CAPTURED,
        PayPalPayment.PARTIALLY_REFUNDED,
    ):
        raise ServiceError("This order has no captured payment to refund.", 409)
    if not idempotency_key:
        raise ServiceError("An idempotency key is required for refunds.", 400)
    if not payment.capture_id:
        raise ServiceError("This order has no capture to refund.", 409)

    # Fast path: a completed refund already exists for this key.
    existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
    if existing is not None and existing.refund_id:
        return existing

    # Claim first, under a row lock, so the refund cap cannot be raced.
    with transaction.atomic():
        locked = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        existing = locked.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None and existing.refund_id:
            return existing
        refundable = locked.refundable_amount
        if amount is None:
            refund_amount = refundable
        else:
            try:
                refund_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                raise ServiceError("Refund amount is not a valid number.", 400)
        if refund_amount <= 0:
            raise ServiceError("Refund amount must be positive.", 400)
        if refund_amount > refundable:
            raise ServiceError(
                f"Refund of {refund_amount} exceeds the refundable amount "
                f"({refundable}).",
                409,
            )
        if existing is None:
            try:
                row = PayPalRefund.objects.create(
                    payment=locked,
                    idempotency_key=idempotency_key,
                    amount=refund_amount,
                    currency=locked.currency,
                    status="PENDING",
                )
            except IntegrityError:
                row = locked.refunds.get(idempotency_key=idempotency_key)
        else:
            row = existing

    if row.refund_id:
        return row

    # App-level idempotency is guaranteed by the unique (payment, idempotency_key)
    # row; the PayPal-Request-Id is derived from that row's id so it is stable across
    # retries of *this* refund yet never collides with another refund's key.
    try:
        result = gateway.refund(
            capture_id=payment.capture_id,
            amount=row.amount,
            currency=payment.currency,
            request_id=f"refund-{order.number}-{row.pk}",
            invoice_id=invoice_id_for(order),
            note="Refund issued by store operator.",
        )
    except gateway.PayPalError as exc:
        if not exc.outcome_unknown:
            # A definite rejection: no money moved, so release the claim to restore
            # the refundable amount and let the same key be retried.
            row.delete()
        raise ServiceError(exc.message, exc.status_code, issues=exc.issues)

    with transaction.atomic():
        locked = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        row.refund_id = result["refund_id"] or ""
        row.status = result["status"] or ""
        row.save(update_fields=["refund_id", "status"])
        source = _get_source(order, locked)
        source.refund(
            row.amount,
            reference=row.refund_id,
            status=row.status,
        )
        if locked.total_refunded >= locked.captured_amount:
            locked.status = PayPalPayment.REFUNDED
        else:
            locked.status = PayPalPayment.PARTIALLY_REFUNDED
        locked.capture_status = (
            "REFUNDED"
            if locked.status == PayPalPayment.REFUNDED
            else "PARTIALLY_REFUNDED"
        )
        locked.save(update_fields=["status", "capture_status", "updated"])
    row.refresh_from_db()
    return row


# ---------------------------------------------------------------------------
# saved cards
# ---------------------------------------------------------------------------


def save_card(user, raw_card):
    """Vault a card for ``user`` and store only a safe descriptor of it."""
    _validate_raw_card(raw_card)
    import uuid

    result = gateway.vault_card(
        merchant_customer_id=f"oscar-{user.pk}",
        card=raw_card,
        request_id=f"vault-{user.pk}-{uuid.uuid4().hex[:16]}",
    )
    card = SavedCard.objects.create(
        user=user,
        vault_id=result["vault_id"],
        paypal_customer_id=result["customer_id"],
        brand=result["brand"],
        last_digits=result["last_digits"],
        expiry=result["expiry"],
        cardholder_name=result["name"] or (raw_card.get("name") or ""),
    )
    return card


def list_cards(user):
    return list(SavedCard.objects.filter(user=user))


def delete_card(user, vault_id):
    card = SavedCard.objects.filter(user=user, vault_id=vault_id).first()
    if card is None:
        raise ServiceError("Saved card not found for this shopper.", 404)
    gateway.delete_vault_token(card.vault_id)
    card.delete()


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------


def _paypal_format(dt):
    from datetime import timezone as std_timezone

    return dt.astimezone(std_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


def reconcile(from_dt, to_dt):
    """List PayPal's own transaction records for a date range and line them up
    against this app's orders. Covers the whole range (all pages, chunked to stay
    within PayPal's per-request window), not just the first page."""
    if to_dt <= from_dt:
        raise ServiceError("'to' must be after 'from'.", 400)

    transactions = []
    truncated = False
    window_start = from_dt
    pages_fetched = 0
    while window_start < to_dt:
        window_end = min(window_start + timedelta(days=RECON_WINDOW_DAYS), to_dt)
        page = 1
        total_pages = 1
        while page <= total_pages:
            if pages_fetched >= MAX_RECON_PAGES:
                truncated = True
                break
            items, total_pages = gateway.search_transactions_page(
                start_date=_paypal_format(window_start),
                end_date=_paypal_format(window_end),
                page=page,
            )
            transactions.extend(items)
            pages_fetched += 1
            page += 1
        if truncated:
            break
        window_start = window_end

    # Line up against app orders by the invoice id we stamp on every payment.
    app_payments = {
        invoice_id_for(p.order): p
        for p in PayPalPayment.objects.select_related("order").exclude(
            paypal_order_id=""
        )
    }
    seen_invoices = set()
    matched = []
    paypal_only = []
    for txn in transactions:
        invoice = txn.get("invoice_id") or txn.get("custom_field")
        payment = app_payments.get(invoice) if invoice else None
        if payment is not None:
            seen_invoices.add(invoice)
            matched.append(
                {
                    "invoiceId": invoice,
                    "orderId": payment.order.number,
                    "appState": payment.status,
                    "paypalTransactionId": txn.get("transaction_id"),
                    "paypalStatus": txn.get("status"),
                    "amount": txn.get("amount"),
                    "currency": txn.get("currency"),
                }
            )
        else:
            paypal_only.append(
                {
                    "paypalTransactionId": txn.get("transaction_id"),
                    "invoiceId": invoice,
                    "status": txn.get("status"),
                    "amount": txn.get("amount"),
                    "currency": txn.get("currency"),
                    "eventCode": txn.get("event_code"),
                }
            )

    # App payments in the window that PayPal's report did not (yet) show.
    app_only = []
    for invoice, payment in app_payments.items():
        if invoice in seen_invoices:
            continue
        created = payment.created
        if from_dt <= created <= to_dt:
            app_only.append(
                {
                    "invoiceId": invoice,
                    "orderId": payment.order.number,
                    "appState": payment.status,
                    "capturedAmount": (
                        f"{payment.gross_amount:.2f}"
                        if payment.gross_amount is not None
                        else None
                    ),
                    "currency": payment.currency,
                }
            )

    return {
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "paypalTransactionCount": len(transactions),
        "pagesFetched": pages_fetched,
        "truncated": truncated,
        "matched": matched,
        "paypalOnly": paypal_only,
        "appOnly": app_only,
        "note": (
            "PayPal transaction reporting lags live activity, so a range covering "
            "very recent payments may legitimately return no PayPal transactions."
        ),
    }
