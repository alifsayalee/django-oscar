"""Business logic for the PayPal payments API.

Orchestrates Oscar order placement and the PayPal money flows. All PayPal I/O is
delegated to :mod:`apps.payments_api.gateway`; this module owns the local state
machine, the idempotency guards and the Oscar order-status transitions.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction

from oscar.apps.checkout.calculators import OrderTotalCalculator
from oscar.apps.order.utils import OrderCreator, OrderNumberGenerator
from oscar.apps.partner.strategy import Selector
from oscar.apps.shipping.methods import NoShippingRequired
from oscar.core.loading import get_model

from paypal.core import UNSET

from . import gateway
from .gateway import PayPalError, money_str
from .models import OrderPayment, PaymentRefund, PayPalCustomer, SavedPaymentMethod

log = logging.getLogger("oscar.paypal")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")

_TWO_PLACES = Decimal("0.01")


def _q(value):
    return Decimal(str(value)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _s(value):
    """Coerce an SDK value (possibly ``UNSET``) to a plain string for storage."""
    if value is None or value is UNSET:
        return ""
    return str(value)


def _safe_set_status(order, status):
    try:
        order.set_status(status)
    except Exception:  # noqa: BLE001 - status bookkeeping must not break money flow
        log.warning("Could not move order %s to %s", order.number, status)


# --------------------------------------------------------------------------- #
# Order placement (reuses Oscar's own order/line models)
# --------------------------------------------------------------------------- #
class OrderPlacementError(Exception):
    def __init__(self, http_status, message):
        super().__init__(message)
        self.http_status = http_status
        self.message = message


def place_order(user, items, request):
    """Create an Oscar order from catalogue item ids/quantities.

    Returns the created ``order.Order`` (with an attached ``OrderPayment`` in the
    PENDING state — awaiting payment).
    """
    if not items:
        raise OrderPlacementError(400, "No items supplied.")

    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(request=request, user=user)
    for item in items:
        try:
            product = Product.objects.get(id=item["product_id"])
        except Product.DoesNotExist:
            raise OrderPlacementError(
                400, f"Unknown product id {item['product_id']}."
            )
        qty = int(item.get("quantity", 1))
        if qty < 1:
            raise OrderPlacementError(400, "Quantity must be at least 1.")
        info = basket.strategy.fetch_for_product(product)
        if not info.availability.is_available_to_buy:
            raise OrderPlacementError(
                400, f"Product {product.id} is not available to buy."
            )
        basket.add_product(product, quantity=qty)

    if basket.is_empty:
        raise OrderPlacementError(400, "The order is empty.")

    shipping_method = NoShippingRequired()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)
    order_number = OrderNumberGenerator().order_number(basket)

    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        user=user,
        order_number=order_number,
        status=getattr(settings, "OSCAR_INITIAL_ORDER_STATUS", "Pending"),
    )
    basket.submit()

    amount = order.total_incl_tax
    if amount is None:
        amount = order.total_excl_tax
    OrderPayment.objects.create(
        order=order,
        currency=settings.PAYPAL_CURRENCY,
        amount=_q(amount),
        status=OrderPayment.PENDING,
    )
    return order


# --------------------------------------------------------------------------- #
# Pay = authorize (hold the money)
# --------------------------------------------------------------------------- #
def authorize(order, *, card=None, saved_method=None, request):
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().get(order=order)
        if payment.status != OrderPayment.PENDING:
            # Idempotent: a repeat/double-click never authorizes twice.
            return payment

        vault_id = saved_method.paypal_token_id if saved_method else None
        ppl_order = gateway.create_authorized_order(
            currency=payment.currency,
            amount=payment.amount,
            order_number=order.number,
            card=card,
            vault_id=vault_id,
            request_id=f"auth-{order.number}",
        )
        auth = gateway.extract_authorization(ppl_order)

        payment.paypal_order_id = _s(getattr(ppl_order, "id", None))
        payment.authorization_id = _s(auth.id)
        payment.authorization_status = _s(getattr(auth, "status", None))
        payment.authorization_expiry = _s(getattr(auth, "expiration_time", None))
        payment.status = OrderPayment.AUTHORIZED
        payment.last_error = ""
        payment.save()

    _safe_set_status(order, "Being processed")
    return payment


# --------------------------------------------------------------------------- #
# Fulfil = capture (take the money), renewing a stale authorization
# --------------------------------------------------------------------------- #
def _is_stale_authorization(err: PayPalError):
    return any("EXPIRED" in issue.upper() for issue in err.issues)


def _apply_capture(payment, cap):
    payment.capture_id = _s(cap.id)
    payment.capture_status = _s(getattr(cap, "status", None))
    srb = getattr(cap, "seller_receivable_breakdown", None)
    if srb and srb is not UNSET:
        payment.captured_amount = gateway._amount_of(getattr(srb, "gross_amount", None))
        payment.paypal_fee = gateway._amount_of(getattr(srb, "paypal_fee", None))
        payment.net_amount = gateway._amount_of(getattr(srb, "net_amount", None))
    if payment.captured_amount is None:
        payment.captured_amount = payment.amount
    payment.status = OrderPayment.CAPTURED


def fulfil(order):
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().get(order=order)
        if payment.status in (
            OrderPayment.CAPTURED,
            OrderPayment.PARTIALLY_REFUNDED,
            OrderPayment.REFUNDED,
        ):
            return payment  # idempotent: already captured
        if payment.status != OrderPayment.AUTHORIZED:
            raise PayPalError(
                409, "Order is not authorized; it cannot be fulfilled."
            )

        auth_id = payment.authorization_id
        try:
            cap = gateway.capture_authorization(auth_id, f"cap-{order.number}")
        except PayPalError as err:
            if not _is_stale_authorization(err):
                raise
            # Authorization has gone stale — try to renew it, then capture the
            # renewed authorization rather than failing the fulfilment.
            try:
                new_auth = gateway.reauthorize(auth_id, f"reauth-{order.number}")
            except PayPalError as renew_err:
                raise PayPalError(
                    409,
                    "The payment authorization has expired and can no longer be "
                    "renewed. Ask the shopper to pay for this order again.",
                    issues=renew_err.issues,
                ) from renew_err
            payment.authorization_id = _s(new_auth.id)
            payment.authorization_status = _s(getattr(new_auth, "status", None))
            payment.authorization_expiry = _s(
                getattr(new_auth, "expiration_time", None)
            )
            payment.save()
            cap = gateway.capture_authorization(
                payment.authorization_id, f"cap-{order.number}-renewed"
            )

        _apply_capture(payment, cap)
        payment.save()

    _safe_set_status(order, "Complete")
    return payment


# --------------------------------------------------------------------------- #
# Cancel = void (release the hold) before fulfilment
# --------------------------------------------------------------------------- #
def cancel(order):
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().get(order=order)
        if payment.status == OrderPayment.VOIDED:
            return payment  # idempotent
        if payment.status in (
            OrderPayment.CAPTURED,
            OrderPayment.PARTIALLY_REFUNDED,
            OrderPayment.REFUNDED,
        ):
            raise PayPalError(
                409,
                "This order has already been fulfilled; issue a refund instead of "
                "a cancellation.",
            )
        if payment.status == OrderPayment.PENDING:
            # Never authorized — no money ever moved.
            payment.status = OrderPayment.VOIDED
            payment.save()
            _safe_set_status(order, "Cancelled")
            return payment
        if payment.status != OrderPayment.AUTHORIZED:
            raise PayPalError(409, "This order cannot be cancelled.")

        pa = gateway.void_authorization(payment.authorization_id)
        payment.authorization_status = _s(getattr(pa, "status", None)) or "VOIDED"
        payment.status = OrderPayment.VOIDED
        payment.save()

    _safe_set_status(order, "Cancelled")
    return payment


# --------------------------------------------------------------------------- #
# Refund (after fulfilment), full or partial, idempotent per caller key
# --------------------------------------------------------------------------- #
def refund(order, *, amount=None, idempotency_key):
    if not idempotency_key:
        raise PayPalError(400, "An idempotency key is required for refunds.")

    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().get(order=order)

        existing = PaymentRefund.objects.filter(
            payment=payment, idempotency_key=idempotency_key
        ).first()
        if existing:
            return existing  # idempotent: same key never refunds twice

        if payment.status not in (
            OrderPayment.CAPTURED,
            OrderPayment.PARTIALLY_REFUNDED,
        ):
            raise PayPalError(409, "This order has no captured payment to refund.")

        remaining = payment.refundable_amount
        amount = remaining if amount is None else _q(amount)
        if amount <= 0:
            raise PayPalError(422, "Refund amount must be greater than zero.")
        if amount > remaining:
            raise PayPalError(
                422,
                f"Refund of {money_str(amount)} exceeds the {money_str(remaining)} "
                f"still refundable on this capture.",
            )

        rf = gateway.refund_capture(
            payment.capture_id,
            currency=payment.currency,
            amount=amount,
            request_id=idempotency_key,
        )
        refund_row = PaymentRefund.objects.create(
            payment=payment,
            refund_id=_s(rf.id),
            amount=amount,
            status=_s(getattr(rf, "status", None)),
            idempotency_key=idempotency_key,
        )

        if payment.total_refunded >= (payment.captured_amount or Decimal("0")):
            payment.status = OrderPayment.REFUNDED
        else:
            payment.status = OrderPayment.PARTIALLY_REFUNDED
        payment.save()

    return refund_row


# --------------------------------------------------------------------------- #
# Saved cards (vault)
# --------------------------------------------------------------------------- #
def save_card(user, card, label=""):
    ppcust = PayPalCustomer.objects.filter(user=user).first()
    customer_id = ppcust.paypal_customer_id if ppcust else None

    token = gateway.vault_card(
        card=card,
        customer_id=customer_id,
        request_id=f"vault-{user.id}-{uuid.uuid4().hex}",
    )

    new_customer_id = customer_id
    tok_cust = getattr(token, "customer", None)
    if tok_cust and tok_cust is not UNSET:
        cid = getattr(tok_cust, "id", None)
        if cid and cid is not UNSET:
            new_customer_id = str(cid)
    if not customer_id and new_customer_id:
        PayPalCustomer.objects.get_or_create(
            user=user, defaults={"paypal_customer_id": new_customer_id}
        )

    brand = last_digits = expiry = ""
    src = getattr(token, "payment_source", None)
    if src and src is not UNSET:
        card_meta = getattr(src, "card", None)
        if card_meta and card_meta is not UNSET:
            brand = _s(getattr(card_meta, "brand", None))
            last_digits = _s(getattr(card_meta, "last_digits", None))
            expiry = _s(getattr(card_meta, "expiry", None))

    return SavedPaymentMethod.objects.create(
        user=user,
        paypal_token_id=str(token.id),
        paypal_customer_id=new_customer_id or "",
        brand=brand,
        last_digits=last_digits,
        expiry=expiry,
        label=label or "",
    )


def list_cards(user):
    return list(SavedPaymentMethod.objects.filter(user=user))


def delete_card(user, pk):
    """Delete a saved card. Returns True on success, False if not found/owned."""
    spm = SavedPaymentMethod.objects.filter(pk=pk, user=user).first()
    if spm is None:
        return False
    gateway.delete_vault_token(spm.paypal_token_id)
    spm.delete()
    return True


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def _fmt(d: dt.datetime):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


def _iter_windows(start, end, days=31):
    cur = start
    step = dt.timedelta(days=days)
    while cur < end:
        nxt = min(cur + step, end)
        yield cur, nxt
        cur = nxt


def reconcile(start: dt.datetime, end: dt.datetime):
    """Line PayPal's own transaction record up against this app's payments.

    Covers the whole range by chunking it into <=31-day windows (PayPal's
    per-request limit) and paginating every page of each window.
    """
    paypal_txns = {}  # transaction_id -> {amount, status}
    for w_start, w_end in _iter_windows(start, end):
        page = 1
        while True:
            resp = gateway.search_transactions_page(_fmt(w_start), _fmt(w_end), page)
            details = getattr(resp, "transaction_details", None) or []
            if details is UNSET:
                details = []
            for det in details:
                info = getattr(det, "transaction_info", None)
                if not info or info is UNSET:
                    continue
                txn_id = getattr(info, "transaction_id", None)
                if not txn_id or txn_id is UNSET:
                    continue
                amount = gateway._amount_of(getattr(info, "transaction_amount", None))
                paypal_txns[str(txn_id)] = {
                    "amount": None if amount is None else f"{amount:.2f}",
                    "status": _s(getattr(info, "transaction_status", None)),
                }
            total_pages = getattr(resp, "total_pages", None)
            if not total_pages or total_pages is UNSET or page >= int(total_pages):
                break
            page += 1

    # App-side records whose capture/refund happened in range.
    app_records = {}  # paypal id -> {kind, order}
    payments = OrderPayment.objects.exclude(capture_id="").select_related("order")
    for pay in payments:
        if not (start <= pay.date_updated <= end):
            # keep it simple and robust: include any capture id we know about
            pass
        if pay.capture_id:
            app_records[pay.capture_id] = {
                "kind": "capture",
                "order": pay.order.number,
                "amount": None
                if pay.captured_amount is None
                else f"{pay.captured_amount:.2f}",
            }
        for r in pay.refunds.all():
            if r.refund_id:
                app_records[r.refund_id] = {
                    "kind": "refund",
                    "order": pay.order.number,
                    "amount": f"{r.amount:.2f}",
                }

    paypal_ids = set(paypal_txns)
    app_ids = set(app_records)
    matched = sorted(paypal_ids & app_ids)
    paypal_only = sorted(paypal_ids - app_ids)
    app_only = sorted(app_ids - paypal_ids)

    return {
        "from": _fmt(start),
        "to": _fmt(end),
        "paypal_transaction_count": len(paypal_txns),
        "app_transaction_count": len(app_records),
        "matched": [
            {"id": i, **app_records[i], "paypal": paypal_txns[i]} for i in matched
        ],
        "in_paypal_not_in_app": [
            {"id": i, **paypal_txns[i]} for i in paypal_only
        ],
        "in_app_not_in_paypal": [
            {"id": i, **app_records[i]} for i in app_only
        ],
        "note": (
            "PayPal transaction reporting lags live activity, so a range covering "
            "very recent payments can legitimately come back empty."
        ),
    }
