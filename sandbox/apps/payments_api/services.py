"""Business logic for the PayPal flows.

Every payment-mutating function follows the durable-row pattern: the ``OrderPayment`` /
``PaymentRefund`` row is claimed (a conditional single-statement write) before the provider
call, the provider is called with a stored idempotency key, and the row is settled from what
PayPal *said* — never from the fact that the call returned. These functions run under views
decorated ``@transaction.non_atomic_requests`` so each write commits immediately (the sandbox
sets ``ATOMIC_REQUESTS=True``, under which an in-view ``atomic()`` would only be a savepoint).
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from oscar.core.loading import get_class, get_model

from paypal.core import UNSET

from . import paypal_client as pp
from .models import OrderPayment, PaymentRefund, PayPalCustomer, SavedCard

log = logging.getLogger("payments_api")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Selector = get_class("partner.strategy", "Selector")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
NoShippingRequired = get_class("shipping.methods", "NoShippingRequired")

# Issue codes that mean the authorization is no longer directly capturable but may be renewed,
# or can no longer be renewed at all. Read from PayPal's error details.
_EXPIRED_AUTH_ISSUES = {
    "AUTHORIZATION_EXPIRED",
    "AUTH_CAPTURE_CURRENCY_MISMATCH",  # not expiry, excluded below
    "PAYMENT_ALREADY_CAPTURED",
    "AUTHORIZATION_ALREADY_CAPTURED",
}
_RENEWABLE_ISSUES = {"AUTHORIZATION_EXPIRED"}


class ServiceError(Exception):
    """A caller-facing error with an HTTP status code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _val(x):
    """Return None for the SDK's UNSET sentinel, else the value."""
    return None if x is UNSET else x


def _currency() -> str:
    return getattr(settings, "PAYPAL_CURRENCY", "USD") or "USD"


# ---------------------------------------------------------------------------
# Order creation (reuses Oscar's own models)
# ---------------------------------------------------------------------------
def create_order(user, items):
    """items: list of {'productId': int, 'quantity': int}. Returns the OrderPayment."""
    if not items:
        raise ServiceError(400, "No items supplied.")
    basket = Basket()
    basket.owner = user
    basket.save()
    basket.strategy = Selector().strategy(user=user)
    for item in items:
        try:
            product_id = int(item["productId"])
            quantity = int(item.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            raise ServiceError(400, "Each item needs a numeric productId and quantity.")
        if quantity < 1:
            raise ServiceError(400, "Quantity must be at least 1.")
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise ServiceError(404, f"Product {product_id} does not exist.")
        try:
            basket.add_product(product, quantity=quantity)
        except ValueError as e:
            raise ServiceError(400, f"Product {product_id} cannot be purchased: {e}")

    if basket.is_empty:
        raise ServiceError(400, "Basket is empty.")

    shipping_method = NoShippingRequired()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)

    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        user=user,
    )
    basket.submit()

    currency = _currency()
    amount = Decimal(pp.format_amount(order.total_incl_tax, currency))
    invoice_id = f"OSC-{order.number}-{uuid.uuid4().hex[:6]}"
    op = OrderPayment.objects.create(
        order=order,
        user=user,
        status=OrderPayment.AWAITING_PAYMENT,
        currency=currency,
        amount=amount,
        invoice_id=invoice_id,
    )
    return op


# ---------------------------------------------------------------------------
# Payment source builders
# ---------------------------------------------------------------------------
def _card_source_from_input(card: dict) -> dict:
    required = ["number", "expiry"]
    for f in required:
        if not card.get(f):
            raise ServiceError(400, f"card.{f} is required.")
    src: dict = {"number": str(card["number"]), "expiry": str(card["expiry"])}
    if card.get("name"):
        src["name"] = str(card["name"])
    cvc = card.get("securityCode") or card.get("cvc") or card.get("cvv")
    if cvc:
        src["security_code"] = str(cvc)
    billing = card.get("billingAddress")
    if billing:
        src["billing_address"] = _billing_address(billing)
    return src


def _billing_address(b: dict) -> dict:
    if not b.get("countryCode"):
        raise ServiceError(400, "billingAddress.countryCode is required.")
    addr = {"country_code": str(b["countryCode"])}
    for src_key, dst_key in [
        ("addressLine1", "address_line_1"),
        ("addressLine2", "address_line_2"),
        ("city", "admin_area_2"),
        ("state", "admin_area_1"),
        ("postalCode", "postal_code"),
    ]:
        if b.get(src_key):
            addr[dst_key] = str(b[src_key])
    return addr


# ---------------------------------------------------------------------------
# Flow 1 — authorize (pay)
# ---------------------------------------------------------------------------
def authorize(op: OrderPayment, *, card: dict | None = None, saved_card_id=None):
    if op.status == OrderPayment.AUTHORIZED:
        return op  # idempotent: hold already placed
    if op.status in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED,
                     OrderPayment.REFUNDED):
        raise ServiceError(409, "Order has already been captured.")
    if op.status in (OrderPayment.VOIDED, OrderPayment.CANCELLING):
        raise ServiceError(409, "Order has been cancelled.")

    # Build the card payment source (raw card or a saved card owned by this shopper).
    if saved_card_id is not None:
        try:
            saved = SavedCard.objects.get(pk=saved_card_id, user=op.user)
        except SavedCard.DoesNotExist:
            raise ServiceError(404, "Saved card not found.")
        card_source = {"vault_id": saved.paypal_token_id}
    elif card is not None:
        card_source = _card_source_from_input(card)
    else:
        raise ServiceError(400, "Provide either 'card' details or a 'savedCardId'.")

    # Claim: only one caller transitions awaiting/failed -> authorizing. Reuse the request id
    # for a retry of an in-flight or unknown attempt so PayPal dedupes it.
    new_request_id = f"auth-{op.invoice_id}-{uuid.uuid4().hex[:10]}"
    claimed = OrderPayment.objects.filter(
        pk=op.pk, status__in=[OrderPayment.AWAITING_PAYMENT, OrderPayment.FAILED]
    ).update(status=OrderPayment.AUTHORIZING, authorize_request_id=new_request_id)
    if claimed:
        request_id = new_request_id
    else:
        op.refresh_from_db()
        if op.status == OrderPayment.AUTHORIZED:
            return op
        if op.status in (OrderPayment.AUTHORIZING, OrderPayment.UNKNOWN) and op.authorize_request_id:
            request_id = op.authorize_request_id
        else:
            raise ServiceError(409, f"Order cannot be paid from state '{op.status}'.")

    currency = op.currency
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [{
            "amount": {"currency_code": currency,
                       "value": pp.format_amount(op.amount, currency)},
            "custom_id": op.order.number,
            "invoice_id": op.invoice_id,
        }],
        "payment_source": {"card": card_source},
    }
    client = pp.get_client()
    try:
        order = pp.call(
            client.orders.create_order,
            body=body,
            pay_pal_request_id=request_id,
            prefer="return=representation",
            write=True,
        )
    except pp.ProviderError as e:
        if e.outcome_unknown:
            OrderPayment.objects.filter(pk=op.pk).update(status=OrderPayment.UNKNOWN)
        else:
            OrderPayment.objects.filter(pk=op.pk).update(status=OrderPayment.FAILED)
        raise ServiceError(e.status_code, e.message)

    # A returned order id means accepted, not authorized. Read the authorization.
    auth = _extract_authorization(order)
    if auth is None:
        if str(_val(order.status)) == "PAYER_ACTION_REQUIRED":
            OrderPayment.objects.filter(pk=op.pk).update(status=OrderPayment.NEEDS_REVIEW)
            raise ServiceError(
                409,
                "PayPal requires the buyer to approve this card in a browser (a challenge such "
                "as 3-D Secure). This unbranded flow does not support a browser approval step.",
            )
        OrderPayment.objects.filter(pk=op.pk).update(status=OrderPayment.UNKNOWN)
        raise ServiceError(502, "PayPal did not return an authorization for this order.")

    auth_amount = _val(getattr(auth, "amount", None))
    if auth_amount is not None and _val(auth_amount.value) is not None:
        if not pp.amounts_equal(auth_amount.value, op.amount, currency):
            OrderPayment.objects.filter(pk=op.pk).update(
                status=OrderPayment.NEEDS_REVIEW,
                paypal_order_id=_val(order.id) or "",
                authorization_id=_val(auth.id) or "",
            )
            raise ServiceError(
                502,
                f"PayPal held {auth_amount.value} {getattr(auth_amount, 'currency_code', '')} "
                f"but the order total is {op.amount} {currency}; flagged for review.",
            )

    outcome = pp.authorization_outcome(_val(auth.status))
    status = OrderPayment.AUTHORIZED if outcome == "authorized" else (
        OrderPayment.FAILED if outcome == "failed" else OrderPayment.UNKNOWN
    )
    op.paypal_order_id = _val(order.id) or ""
    op.authorization_id = _val(auth.id) or ""
    op.auth_status = str(_val(auth.status) or "")
    op.auth_expiry = str(_val(auth.expiration_time) or "")
    op.status = status
    op.save(update_fields=[
        "paypal_order_id", "authorization_id", "auth_status", "auth_expiry", "status", "updated",
    ])

    if status == OrderPayment.AUTHORIZED:
        _advance_oscar_status(op.order, "Being processed")
    elif status == OrderPayment.FAILED:
        raise ServiceError(402, "The card authorization was declined by PayPal.")
    return op


def _extract_authorization(order):
    pus = _val(order.purchase_units)
    if not pus:
        return None
    payments = _val(getattr(pus[0], "payments", None))
    if payments is None:
        return None
    auths = _val(getattr(payments, "authorizations", None))
    if not auths:
        return None
    return auths[0]


# ---------------------------------------------------------------------------
# Flow 1 — fulfil (capture), with stale-authorization renewal
# ---------------------------------------------------------------------------
def capture(op: OrderPayment):
    if op.status in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED,
                     OrderPayment.REFUNDED):
        return op  # idempotent: already captured
    if op.status == OrderPayment.VOIDED:
        raise ServiceError(409, "Order was cancelled; nothing to fulfil.")
    if op.status not in (OrderPayment.AUTHORIZED, OrderPayment.CAPTURING, OrderPayment.UNKNOWN):
        raise ServiceError(409, f"Order is not authorized (state '{op.status}').")
    if not op.authorization_id:
        raise ServiceError(409, "Order has no authorization to capture.")

    new_capture_id = f"cap-{op.invoice_id}-{uuid.uuid4().hex[:10]}"
    claimed = OrderPayment.objects.filter(
        pk=op.pk, status__in=[OrderPayment.AUTHORIZED, OrderPayment.UNKNOWN]
    ).update(status=OrderPayment.CAPTURING, capture_request_id=new_capture_id)
    if claimed:
        capture_request_id = new_capture_id
    else:
        op.refresh_from_db()
        if op.status in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED,
                         OrderPayment.REFUNDED):
            return op
        if op.status == OrderPayment.CAPTURING and op.capture_request_id:
            capture_request_id = op.capture_request_id
        else:
            raise ServiceError(409, f"Order cannot be fulfilled from state '{op.status}'.")

    captured = _capture_with_renewal(op, capture_request_id)

    amount = _val(getattr(captured, "amount", None))
    srb = _val(getattr(captured, "seller_receivable_breakdown", None))
    captured_value = _money(amount)
    fee = _money(_val(getattr(srb, "paypal_fee", None))) if srb else None
    net = _money(_val(getattr(srb, "net_amount", None))) if srb else None
    outcome = pp.capture_outcome(_val(captured.status))
    op.capture_id = _val(captured.id) or ""
    op.capture_status = str(_val(captured.status) or "")
    op.captured_value = captured_value
    op.paypal_fee = fee
    op.net_amount = net
    op.capture_time = _parse_time(_val(getattr(captured, "create_time", None))) or timezone.now()
    op.status = OrderPayment.CAPTURED if outcome in ("captured", "partially_refunded") else (
        OrderPayment.FAILED if outcome == "failed" else OrderPayment.UNKNOWN
    )
    op.save(update_fields=[
        "capture_id", "capture_status", "captured_value", "paypal_fee", "net_amount",
        "capture_time", "status", "updated",
    ])
    if op.status == OrderPayment.CAPTURED:
        _advance_oscar_status(op.order, "Complete")
    elif op.status == OrderPayment.FAILED:
        raise ServiceError(402, "PayPal declined the capture.")
    return op


def _capture_with_renewal(op: OrderPayment, capture_request_id: str):
    client = pp.get_client()

    def _do_capture(auth_id, req_id):
        return pp.call(
            client.payments.capture_authorized_payment,
            auth_id,
            pay_pal_request_id=req_id,
            prefer="return=representation",
            body={"final_capture": True},
            write=True,
        )

    try:
        return _do_capture(op.authorization_id, capture_request_id)
    except pp.ProviderError as e:
        renewable = any(i in _RENEWABLE_ISSUES for i in e.issues)
        expired = renewable or any(i in _EXPIRED_AUTH_ISSUES for i in e.issues)
        if not expired:
            raise ServiceError(e.status_code, e.message)
        if not renewable:
            raise ServiceError(e.status_code, e.message)
        # The honor period lapsed — renew the authorization, then capture the renewed one.
        try:
            reauth = pp.call(
                client.payments.reauthorize_payment,
                op.authorization_id,
                prefer="return=representation",
                body={"amount": {"currency_code": op.currency,
                                 "value": pp.format_amount(op.amount, op.currency)}},
                write=True,
            )
        except pp.ProviderError as re:
            # Cannot be renewed (e.g. beyond the 29-day window). Say so in operator terms.
            OrderPayment.objects.filter(pk=op.pk).update(status=OrderPayment.NEEDS_REVIEW)
            raise ServiceError(
                409,
                "The authorization has expired and can no longer be renewed "
                f"({re.message}). Re-collect payment from the shopper for this order.",
            )
        new_auth_id = _val(reauth.id) or op.authorization_id
        op.authorization_id = new_auth_id
        op.auth_status = str(_val(reauth.status) or "")
        op.auth_expiry = str(_val(reauth.expiration_time) or "")
        op.save(update_fields=["authorization_id", "auth_status", "auth_expiry", "updated"])
        return _do_capture(new_auth_id, f"{capture_request_id}-re")


# ---------------------------------------------------------------------------
# Flow 1 — cancel (void the hold)
# ---------------------------------------------------------------------------
def cancel(op: OrderPayment):
    if op.status == OrderPayment.VOIDED:
        return op  # idempotent
    if op.status in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED,
                     OrderPayment.REFUNDED):
        raise ServiceError(409, "Order is already captured; refund it instead of cancelling.")
    if op.status not in (OrderPayment.AUTHORIZED, OrderPayment.CANCELLING, OrderPayment.UNKNOWN):
        raise ServiceError(409, f"Order cannot be cancelled from state '{op.status}'.")
    if not op.authorization_id:
        # Never authorized: cancel the order outright, no money ever moved.
        OrderPayment.objects.filter(pk=op.pk).update(status=OrderPayment.VOIDED)
        _advance_oscar_status(op.order, "Cancelled")
        op.refresh_from_db()
        return op

    claimed = OrderPayment.objects.filter(
        pk=op.pk, status__in=[OrderPayment.AUTHORIZED, OrderPayment.UNKNOWN]
    ).update(status=OrderPayment.CANCELLING)
    if not claimed:
        op.refresh_from_db()
        if op.status == OrderPayment.VOIDED:
            return op
        if op.status != OrderPayment.CANCELLING:
            raise ServiceError(409, f"Order cannot be cancelled from state '{op.status}'.")

    client = pp.get_client()
    try:
        voided = pp.call(
            client.payments.void_payment,
            op.authorization_id,
            prefer="return=representation",  # a minimal (empty) body fails to decode
            write=True,
        )
    except pp.ProviderError as e:
        # If PayPal says the auth is already voided/captured, reconcile rather than fail blindly.
        raise ServiceError(e.status_code, e.message)

    op.auth_status = str(_val(voided.status) or op.auth_status)
    op.status = OrderPayment.VOIDED
    op.save(update_fields=["auth_status", "status", "updated"])
    _advance_oscar_status(op.order, "Cancelled")
    return op


# ---------------------------------------------------------------------------
# Flow 1 — refund (after fulfilment), idempotent per caller key
# ---------------------------------------------------------------------------
def refund(op: OrderPayment, *, amount, idempotency_key: str):
    if not idempotency_key:
        raise ServiceError(400, "An idempotencyKey is required for refunds.")
    if op.status not in (OrderPayment.CAPTURED, OrderPayment.PARTIALLY_REFUNDED):
        raise ServiceError(409, "Order has not been captured; nothing to refund.")
    if not op.capture_id:
        raise ServiceError(409, "Order has no capture to refund.")

    # Idempotency: a repeat under the same key returns the same refund, never a second one.
    existing = PaymentRefund.objects.filter(
        order_payment=op, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        return existing

    # Parse/validate amount (None => full remaining).
    remaining = op.captured_value - op.refunded_total if op.captured_value is not None else None
    amount_dec: Decimal | None
    if amount is not None:
        try:
            amount_dec = Decimal(pp.format_amount(Decimal(str(amount)), op.currency))
        except (InvalidOperation, ValueError):
            raise ServiceError(400, "Invalid refund amount.")
        if amount_dec <= 0:
            raise ServiceError(400, "Refund amount must be positive.")
    else:
        amount_dec = remaining if remaining is not None else None

    # Cap: never refund beyond what was captured.
    if remaining is not None and amount_dec is not None and amount_dec > remaining:
        raise ServiceError(
            422,
            f"Refund of {amount_dec} exceeds the remaining refundable amount {remaining}.",
        )
    if remaining is not None and remaining <= 0:
        raise ServiceError(422, "This capture has already been fully refunded.")

    # Claim the refund row before calling PayPal (unique_together makes a racing double a no-op).
    try:
        row = PaymentRefund.objects.create(
            order_payment=op,
            idempotency_key=idempotency_key,
            amount=amount_dec if amount_dec is not None else Decimal("0.00"),
            status="PENDING",
        )
    except Exception:
        row = PaymentRefund.objects.filter(
            order_payment=op, idempotency_key=idempotency_key
        ).first()
        if row is not None:
            return row
        raise

    body: dict = {}
    if amount is not None and amount_dec is not None:
        body["amount"] = {"currency_code": op.currency,
                          "value": pp.format_amount(amount_dec, op.currency)}
    body["invoice_id"] = op.invoice_id

    client = pp.get_client()
    try:
        result = pp.call(
            client.payments.refund_captured_payment,
            op.capture_id,
            pay_pal_request_id=f"ref-{op.invoice_id}-{idempotency_key}",
            prefer="return=representation",
            body=body or None,
            write=True,
        )
    except pp.ProviderError as e:
        if e.outcome_unknown:
            row.status = "UNKNOWN"
            row.save(update_fields=["status"])
            raise ServiceError(e.status_code, e.message)
        row.delete()  # rejected, nothing happened — release the key so it can be retried
        raise ServiceError(e.status_code, e.message)

    refunded_amount = _money(_val(getattr(result, "amount", None)))
    row.paypal_refund_id = _val(result.id) or ""
    row.status = str(_val(result.status) or "")
    if refunded_amount is not None:
        row.amount = refunded_amount
    row.save(update_fields=["paypal_refund_id", "status", "amount"])

    _refresh_refund_state(op)
    return row


def _refresh_refund_state(op: OrderPayment):
    if op.captured_value is None:
        return
    refunded = op.refunded_total
    if refunded >= op.captured_value:
        op.status = OrderPayment.REFUNDED
    elif refunded > 0:
        op.status = OrderPayment.PARTIALLY_REFUNDED
    op.save(update_fields=["status", "updated"])


# ---------------------------------------------------------------------------
# Flow 2 — saved cards
# ---------------------------------------------------------------------------
def save_card(user, card: dict) -> SavedCard:
    card_source = _card_source_from_input(card)
    # Card vault requires name/number/expiry/security_code; billing optional.
    vault_card = {k: v for k, v in card_source.items()
                  if k in ("name", "number", "expiry", "security_code", "billing_address")}

    profile = PayPalCustomer.objects.filter(user=user).first()
    body: dict = {"payment_source": {"card": vault_card}}
    if profile is not None:
        body["customer"] = {"id": profile.customer_id}

    client = pp.get_client()
    token = pp.call(
        client.vault.create_payment_token,
        body=body,
        pay_pal_request_id=f"vault-{user.pk}-{uuid.uuid4().hex[:12]}",
        write=True,
    )

    token_id = _val(token.id)
    if not token_id:
        raise ServiceError(502, "PayPal did not return a vaulted card id.")

    customer = _val(getattr(token, "customer", None))
    customer_id = _val(getattr(customer, "id", None)) if customer else None
    if customer_id and profile is None:
        PayPalCustomer.objects.get_or_create(user=user, defaults={"customer_id": customer_id})

    brand = last_digits = expiry = name = ""
    ps = _val(getattr(token, "payment_source", None))
    card_entity = _val(getattr(ps, "card", None)) if ps else None
    if card_entity is not None:
        brand = str(_val(getattr(card_entity, "brand", None)) or "")
        last_digits = str(_val(getattr(card_entity, "last_digits", None)) or "")
        expiry = str(_val(getattr(card_entity, "expiry", None)) or "")
        name = str(_val(getattr(card_entity, "name", None)) or "")

    saved = SavedCard.objects.create(
        user=user,
        paypal_token_id=token_id,
        paypal_customer_id=customer_id or "",
        brand=brand,
        last_digits=last_digits,
        expiry=expiry,
        name=name,
    )
    return saved


def list_cards(user):
    return list(SavedCard.objects.filter(user=user))


def delete_card(user, payment_method_id):
    try:
        saved = SavedCard.objects.get(pk=payment_method_id, user=user)
    except SavedCard.DoesNotExist:
        raise ServiceError(404, "Saved card not found.")

    client = pp.get_client()
    try:
        result = pp.call(
            client.vault.with_raw_response.delete_payment_token,
            saved.paypal_token_id,
            write=True,
        )
        status = getattr(getattr(result, "response", None), "status_code", None)
        if status is not None and status not in (200, 204, 404):
            raise ServiceError(502, f"PayPal returned {status} deleting the card.")
    except pp.ProviderError as e:
        # 404 at PayPal means it is already gone — proceed to drop our row.
        if e.provider_status != 404:
            raise ServiceError(e.status_code, e.message)

    saved.delete()
    return True


# ---------------------------------------------------------------------------
# Reconciliation (operator) — see plan for the clock/window/set-match rules
# ---------------------------------------------------------------------------
_MAX_WINDOW = timedelta(days=31)
_MAX_PAGES = 100
_PAGE_SIZE = 100


def _iso_paypal(dt) -> str:
    # PayPal reporting wants YYYY-MM-DDTHH:MM:SS-0000 style. Normalise to UTC.
    dt = dt.astimezone(dt_timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+0000"


def reconcile(from_dt, to_dt):
    if from_dt >= to_dt:
        raise ServiceError(400, "'from' must be before 'to'.")

    client = pp.get_client()
    provider_tx = []
    truncated = False

    window_start = from_dt
    while window_start < to_dt:
        window_end = min(window_start + _MAX_WINDOW, to_dt)
        page = 1
        while True:
            if page > _MAX_PAGES:
                truncated = True
                break
            try:
                resp = pp.call(
                    client.transaction_search.search_transactions,
                    _iso_paypal(window_start),
                    _iso_paypal(window_end),
                    fields="transaction_info",
                    page_size=_PAGE_SIZE,
                    page=page,
                )
            except pp.ProviderError as e:
                raise ServiceError(e.status_code, f"PayPal reporting error: {e.message}")
            details = _val(resp.transaction_details) or []
            for d in details:
                info = _val(getattr(d, "transaction_info", None))
                if info is None:
                    continue
                provider_tx.append(info)
            total_pages = _val(resp.total_pages) or 0
            if page >= total_pages or not details:
                break
            page += 1
        window_start = window_end

    # Narrow provider tx to the caller's exact instants and index by invoice.
    from collections import defaultdict

    by_invoice = defaultdict(list)
    provider_rows = []
    for info in provider_tx:
        init = _parse_time(_val(getattr(info, "transaction_initiation_date", None)))
        if init is not None and not (from_dt <= init < to_dt):
            continue
        amount = _money(_val(getattr(info, "transaction_amount", None)))
        row = {
            "transactionId": _val(getattr(info, "transaction_id", None)),
            "invoiceId": _val(getattr(info, "invoice_id", None)),
            "customId": _val(getattr(info, "custom_field", None)),
            "amount": str(amount) if amount is not None else None,
            "eventCode": _val(getattr(info, "transaction_event_code", None)),
            "status": _val(getattr(info, "transaction_status", None)),
            "initiationDate": init.isoformat() if init else None,
        }
        provider_rows.append(row)
        if row["invoiceId"]:
            by_invoice[row["invoiceId"]].append(row)

    # Local side on the provider clock (capture_time). Match against the SET.
    local_captured = OrderPayment.objects.filter(
        capture_time__gte=from_dt, capture_time__lt=to_dt
    ).select_related("order")
    matched = []
    local_only = []
    for op in local_captured:
        txns = by_invoice.pop(op.invoice_id, [])
        entry = {
            "orderId": op.order_id,
            "orderNumber": op.order.number,
            "invoiceId": op.invoice_id,
            "status": op.status,
            "capturedAmount": str(op.captured_value) if op.captured_value is not None else None,
            "providerTransactions": txns,
        }
        if txns:
            matched.append(entry)
        else:
            local_only.append(entry)

    # Orders authorized-but-not-captured in the created window: reported, never dropped.
    unsettled = [
        {
            "orderId": op.order_id,
            "orderNumber": op.order.number,
            "invoiceId": op.invoice_id,
            "status": op.status,
        }
        for op in OrderPayment.objects.filter(
            capture_time__isnull=True, created__gte=from_dt, created__lt=to_dt
        ).select_related("order")
    ]

    provider_only = [row for rows in by_invoice.values() for row in rows]
    # Provider transactions with no invoice we can key on are provider-only too.
    provider_only += [r for r in provider_rows if not r["invoiceId"]]

    return {
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "truncated": truncated,
        "matched": matched,
        "localOnly": local_only,
        "providerOnly": provider_only,
        "unsettled": unsettled,
        "counts": {
            "matched": len(matched),
            "localOnly": len(local_only),
            "providerOnly": len(provider_only),
            "unsettled": len(unsettled),
            "providerTransactions": len(provider_rows),
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _money(money_obj):
    if money_obj is None:
        return None
    value = _val(getattr(money_obj, "value", None))
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_time(s):
    if not s:
        return None
    dt = parse_datetime(s)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = timezone.make_aware(dt, dt_timezone.utc)
    return dt


def _advance_oscar_status(order, new_status: str):
    """Move the Oscar order along its pipeline; skip silently if the move isn't available."""
    from oscar.apps.order.exceptions import InvalidOrderStatus

    try:
        if order.status != new_status and new_status in order.available_statuses():
            order.set_status(new_status)
    except InvalidOrderStatus:
        log.warning("Could not move order %s to %s", order.number, new_status)
