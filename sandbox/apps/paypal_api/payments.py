"""
Money movement for an order: hold it (authorize), take it (capture, renewing a
stale hold first), release it (void) and give it back (refund).

Every PayPal write goes through ``safe_write``; this module decides the
references, builds the requests, reads the answers and records the local
effects exactly once.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from paypal.core import UNSET, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Money,
    Order as PayPalOrder,
    OrderRequest,
    OrdersCapture,
    PaymentAuthorization,
    PaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    Refund,
    RefundRequest,
)
from paypal.models.enums import AuthorizationStatus, CheckoutPaymentIntent, OrderStatus

from . import gateway
from .errors import ApiProblem, ProviderError, call_paypal
from .models import PayPalOperation as Op
from .models import PayPalPayment
from .money import to_minor, wire
from .safe_write import (
    Answer,
    authorization_outcome,
    capture_outcome,
    complete,
    order_outcome,
    provider_time,
    refund_outcome,
    safe_write,
    void_outcome,
)

logger = logging.getLogger(__name__)

Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
EventHandler = get_class("order.processing", "EventHandler")

# Reauthorize docstring: a 3-day honor period, then reauthorization from day 4 to day 29.
HONOR_PERIOD = timedelta(days=3)
PAYPAL_SOURCE_TYPE = "PayPal"


# --- small helpers ---------------------------------------------------------


def v(value: Any) -> Any:
    """An SDK optional member as a plain value (UNSET -> None)."""
    return None if isinstance(value, UnsetType) else value


def ref(*parts: object) -> str:
    return ":".join([gateway.reference_prefix(), *map(str, parts)])


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def idempotency_key(request_headers: Any) -> str:
    key = (request_headers.get("Idempotency-Key") or "").strip()
    if not key or len(key) > 255:
        raise ApiProblem(400, "idempotency_key_required", "Send an Idempotency-Key header (1-255 characters).")
    return key


def outcome_payload(op: Op) -> dict[str, Any]:
    payload: dict[str, Any] = {"outcome": op.outcome, "paypalStatus": op.provider_status or None}
    message = op.detail.get("message") if isinstance(op.detail, dict) else None
    if message:
        payload["message"] = message
    return payload


# --- card input (never stored, never logged) -------------------------------

_DIGITS = re.compile(r"^\d{12,19}$")
_EXPIRY = re.compile(r"^(\d{4})-(\d{2})$")


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def parse_card(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ApiProblem(400, "invalid_card", "card must be an object {number, expiry, securityCode, name?, billingAddress?}.")
    number = re.sub(r"[\s-]", "", str(data.get("number", "")))
    if not _DIGITS.match(number) or not _luhn_ok(number):
        raise ApiProblem(400, "invalid_card", "card.number is not a valid card number.")
    match = _EXPIRY.match(str(data.get("expiry", "")))
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise ApiProblem(400, "invalid_card", "card.expiry must be YYYY-MM.")
    today = timezone.now().date()
    if (int(match.group(1)), int(match.group(2))) < (today.year, today.month):
        raise ApiProblem(400, "invalid_card", "The card has expired.")
    security_code = str(data.get("securityCode", ""))
    if not re.match(r"^\d{3,4}$", security_code):
        raise ApiProblem(400, "invalid_card", "card.securityCode must be 3 or 4 digits.")
    card: dict[str, Any] = {"number": number, "expiry": match.group(0), "security_code": security_code}
    name = str(data.get("name", "")).strip()
    if name:
        card["name"] = name[:300]
    billing = data.get("billingAddress")
    if billing is not None:
        if not isinstance(billing, dict) or not re.match(r"^[A-Za-z]{2}$", str(billing.get("countryCode", ""))):
            raise ApiProblem(400, "invalid_card", "card.billingAddress.countryCode must be a 2-letter country code.")
        address: dict[str, Any] = {"country_code": str(billing["countryCode"]).upper()}
        for src, dst in (("line1", "address_line_1"), ("line2", "address_line_2"), ("city", "admin_area_2"),
                         ("state", "admin_area_1"), ("postcode", "postal_code")):
            if billing.get(src):
                address[dst] = str(billing[src])[:300]
        card["billing_address"] = Address(**address)
    return card


# --- local bookkeeping -----------------------------------------------------


def _apply_once(op: Op) -> bool:
    """True exactly once per operation: the caller then records its local effects."""
    return Op.objects.filter(pk=op.pk, applied=False).update(applied=True) == 1


def _source(payment: PayPalPayment) -> Any:
    if payment.source_id:
        return payment.source
    source_type, _ = SourceType.objects.get_or_create(name=PAYPAL_SOURCE_TYPE)
    source = Source.objects.create(
        order=payment.order, source_type=source_type, currency=payment.currency,
        reference=payment.paypal_order_id,
        label=f"{payment.card_brand} ending {payment.card_last_digits}".strip(),
    )
    payment.source = source
    payment.save(update_fields=["source", "updated_at"])
    return source


def _decimal(minor: int, currency: str) -> Decimal:
    return Decimal(wire(minor, currency))


def _transition(payment: PayPalPayment, from_states: tuple[str, ...], to_state: str) -> bool:
    """Compare-and-set on the payment state: exactly one concurrent request wins."""
    won = PayPalPayment.objects.filter(pk=payment.pk, state__in=from_states).update(
        state=to_state, updated_at=timezone.now()
    ) == 1
    payment.refresh_from_db()
    return won


# --- authorize (pay) -------------------------------------------------------


def _read_order(result: Any) -> Answer:
    """The single-step create order, or the authorization found by the lookup."""
    auth: Any
    card: Any = None
    if isinstance(result, PaymentAuthorization):
        supplementary = v(result.supplementary_data)
        related = v(supplementary.related_ids) if supplementary else None
        auth, order_status = result, OrderStatus.COMPLETED
        paypal_order_id = v(related.order_id) if related else None
    else:
        order: PayPalOrder = result
        units = v(order.purchase_units) or []
        unit = units[0] if units else None
        payments = v(unit.payments) if unit else None
        auths = (v(payments.authorizations) or []) if payments else []
        auth = auths[0] if auths else None
        order_status, paypal_order_id = v(order.status), v(order.id)
        source = v(order.payment_source)
        card = v(source.card) if source else None
        if auth is None:
            amount = v(unit.amount) if unit else None
            return Answer(
                provider_id=paypal_order_id or "",
                status=(order_status, None),
                provider_time=provider_time(v(order.create_time)),
                amount=amount.value if amount else None,
                currency=amount.currency_code if amount else None,
                detail={"paypal_order_id": paypal_order_id},
            )
    money = v(auth.amount)
    return Answer(
        provider_id=v(auth.id) or "",
        status=(order_status, v(auth.status)),
        provider_time=provider_time(v(auth.create_time)),
        amount=money.value if money else None,
        currency=money.currency_code if money else None,
        detail={
            "paypal_order_id": paypal_order_id,
            "authorization_status": str(v(auth.status) or ""),
            "created": v(auth.create_time),
            "expires": v(auth.expiration_time),
            "card_brand": str(v(card.brand) or "") if card else "",
            "card_last_digits": (v(card.last_digits) or "") if card else "",
        },
    )


def _find_authorization_by_invoice(invoice_id: str, since: datetime) -> PaymentAuthorization | None:
    """Look an authorization up by the invoice id it was created with (PayPal's reporting)."""
    client = gateway.get_client()
    now = timezone.now().replace(microsecond=0)
    start = (since - timedelta(minutes=10)).replace(microsecond=0)
    end = min(now, start + timedelta(days=30))
    page, pages = 1, 1
    while page <= pages:
        result = call_paypal(lambda: gateway.read_with_retry(lambda: client.transaction_search.search_transactions(
            start.isoformat(), end.isoformat(), balance_affecting_records_only="N", page_size=100, page=page)))
        pages = v(result.total_pages) or 1
        for details in v(result.transaction_details) or []:
            info = v(details.transaction_info)
            if info and v(info.invoice_id) == invoice_id and v(info.transaction_id):
                auth_id = info.transaction_id
                try:
                    return call_paypal(lambda: gateway.read_with_retry(
                        lambda: client.payments.get_authorized_payment(auth_id)))
                except ProviderError as e:
                    if e.status_code == 404:
                        continue  # not an authorization id (e.g. a capture on the same invoice)
                    raise
        page += 1
    return None


def pay_order(user: Any, order: Any, payload: dict[str, Any]) -> tuple[Op | None, PayPalPayment]:
    """Put a hold on the order total (single-step create order with a card or a saved card)."""
    payment = order.paypal_payment
    if payment.state in (PayPalPayment.CANCELLED, PayPalPayment.VOIDED, PayPalPayment.VOIDING):
        raise ApiProblem(409, "order_cancelled", "This order was cancelled.")
    if payment.state in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING, PayPalPayment.CAPTURED):
        return None, payment  # already paid: the repeat answers from the stored state

    card_data, method_id = payload.get("card"), payload.get("paymentMethodId")
    if (card_data is None) == (method_id is None):
        raise ApiProblem(400, "invalid_request", "Send exactly one of card or paymentMethodId.")
    saved = None
    if method_id is not None:
        from .cards import usable_card

        saved = usable_card(user, method_id)
        card_request = CardRequest(vault_id=saved.partner_reference)
    else:
        card_request = CardRequest(**parse_card(card_data))

    latest = Op.objects.filter(order=order, kind=Op.AUTHORIZE).order_by("-attempt").first()
    attempt = 1
    if latest is not None:
        abandoned = latest.outcome == Op.PENDING and str(OrderStatus.PAYER_ACTION_REQUIRED) in latest.provider_status
        expired = payment.state == PayPalPayment.AUTHORIZATION_EXPIRED
        attempt = latest.attempt + 1 if (latest.outcome == Op.FAILED or abandoned or expired) else latest.attempt

    cur = payment.currency
    op_ref = ref("order", order.number, "authorize", attempt)
    invoice_id = f"{gateway.reference_prefix()}-{order.number}-{attempt}"
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                amount=AmountWithBreakdown(currency_code=cur, value=wire(payment.amount_minor, cur)),
                invoice_id=invoice_id,
                custom_id=f"{gateway.reference_prefix()}-{order.number}",
                description=f"Order {order.number}",
            )
        ],
        payment_source=PaymentSource(card=card_request),
    )
    client = gateway.get_client()
    claimed_at = timezone.now()

    op, _ = safe_write(
        op_ref,
        send=lambda key: client.orders.create_order(body, pay_pal_request_id=key, prefer="return=representation"),
        find=lambda _ref: _find_authorization_by_invoice(invoice_id, claimed_at),
        read=_read_order,
        outcome_of=order_outcome,
        repeat_is_safe=False,  # smoke: a same-key repeat is refused, so only a lookup is safe
        sent=(payment.amount_minor, cur),
        claim={"kind": Op.AUTHORIZE, "order": order, "user": user, "attempt": attempt,
               "amount_minor": payment.amount_minor, "currency": cur, "invoice_id": invoice_id},
    )
    if op.outcome == Op.DONE:
        _record_authorization(payment, op, saved)
    elif op.outcome == Op.PENDING and str(OrderStatus.PAYER_ACTION_REQUIRED) in op.provider_status:
        op.detail = {**op.detail, "message": "PayPal asked for shopper authentication (3-D Secure), which this API "
                     "does not support. Pay again with another card."}
    payment.refresh_from_db()
    return op, payment


def _record_authorization(payment: PayPalPayment, op: Op, saved: Any) -> None:
    if not _apply_once(op):
        return
    d = op.detail
    with transaction.atomic():
        payment.paypal_order_id = d.get("paypal_order_id") or ""
        payment.authorization_id = op.provider_id
        payment.authorization_status = d.get("authorization_status", "")
        payment.authorization_created_at = provider_time(d.get("created"))
        payment.authorization_expires_at = provider_time(d.get("expires"))
        payment.original_authorization_id = ""
        payment.reauthorized_at = None
        payment.card_brand = d.get("card_brand", "")[:32]
        payment.card_last_digits = d.get("card_last_digits", "")[:4]
        if saved is not None:
            payment.card_brand = payment.card_brand or saved.card_type[:32]
            payment.card_last_digits = payment.card_last_digits or saved.number[-4:]
        payment.saved_card = saved
        payment.state = PayPalPayment.AUTHORIZED
        payment.save()
        _source(payment).allocate(_decimal(payment.amount_minor, payment.currency),
                                  reference=op.provider_id, status=op.provider_status)
        order = payment.order
        if order.status != "Pending":
            order.set_status("Pending")


# --- capture (fulfil), renewing a stale authorization first ------------------


def _read_authorization(result: PaymentAuthorization) -> Answer:
    money = v(result.amount)
    return Answer(
        provider_id=v(result.id) or "",
        status=v(result.status),
        provider_time=provider_time(v(result.create_time)),
        amount=money.value if money else None,
        currency=money.currency_code if money else None,
        detail={"created": v(result.create_time), "expires": v(result.expiration_time),
                "authorization_status": str(v(result.status) or "")},
    )


def _read_capture(result: CapturedPayment | OrdersCapture) -> Answer:
    money = v(result.amount)
    breakdown = v(result.seller_receivable_breakdown)
    fee = v(breakdown.paypal_fee) if breakdown else None
    net = v(breakdown.net_amount) if breakdown else None
    return Answer(
        provider_id=v(result.id) or "",
        status=v(result.status),
        provider_time=provider_time(v(result.create_time)),
        amount=money.value if money else None,
        currency=money.currency_code if money else None,
        detail={"fee": fee.value if fee else None, "net": net.value if net else None,
                "fee_currency": fee.currency_code if fee else None},
    )


def _find_capture(paypal_order_id: str) -> OrdersCapture | None:
    client = gateway.get_client()
    order = call_paypal(lambda: gateway.read_with_retry(lambda: client.orders.get_order(paypal_order_id)))
    for unit in v(order.purchase_units) or []:
        payments = v(unit.payments)
        captures = (v(payments.captures) or []) if payments else []
        if captures:
            first: OrdersCapture = captures[0]
            return first
    return None


def _renew_if_stale(payment: PayPalPayment) -> tuple[str, str]:
    """
    Bring the authorization up to date before capturing. Returns (state, note):
    ``ready`` to capture, ``pending`` while PayPal works on a renewal.
    Raises ApiProblem when the hold can no longer be collected.
    """
    client = gateway.get_client()
    auth = call_paypal(lambda: gateway.read_with_retry(
        lambda: client.payments.get_authorized_payment(payment.authorization_id)))
    status = v(auth.status)
    payment.authorization_status = str(status or "")
    expires = provider_time(v(auth.expiration_time)) or payment.authorization_expires_at
    created = provider_time(v(auth.create_time)) or payment.authorization_created_at
    now = timezone.now()

    if status in (AuthorizationStatus.VOIDED, AuthorizationStatus.DENIED):
        raise ApiProblem(409, "authorization_not_active",
                         f"PayPal reports the authorization as {status}; there is nothing to capture. "
                         "Cancel the order, and ask the shopper to pay again if it should proceed.")
    if status != AuthorizationStatus.CREATED:
        raise ApiProblem(409, "authorization_not_capturable",
                         f"PayPal reports the authorization as {status or 'unknown'}; review it in PayPal before fulfilling.")
    if expires is not None and now >= expires:
        payment.state = PayPalPayment.AUTHORIZATION_EXPIRED
        payment.save(update_fields=["state", "authorization_status", "updated_at"])
        raise ApiProblem(
            409, "authorization_expired",
            f"The authorization expired at {expires.isoformat()} and can no longer be renewed or captured; "
            "PayPal no longer holds the shopper's funds. Ask the shopper to pay again "
            f"(POST /api/orders/{payment.order.number}/pay), then fulfil; or cancel the order.")
    if created is None or now < created + HONOR_PERIOD:
        return "ready", ""
    if payment.reauthorized_at is not None:
        # PayPal allows one reauthorization; its own honor period has passed too.
        return "ready", "The renewed authorization's honor period has passed; capturing without a funds guarantee."

    cur = payment.currency
    auth_id = payment.authorization_id
    try:
        op, _ = safe_write(
            ref("order", payment.order.number, "reauthorize", auth_id),
            send=lambda key: client.payments.reauthorize_payment(
                auth_id, pay_pal_request_id=key, prefer="return=representation",
                body=ReauthorizeRequest(amount=Money(currency_code=cur, value=wire(payment.amount_minor, cur)))),
            find=lambda key: client.payments.reauthorize_payment(
                auth_id, pay_pal_request_id=key, prefer="return=representation",
                body=ReauthorizeRequest(amount=Money(currency_code=cur, value=wire(payment.amount_minor, cur)))),
            read=_read_authorization,
            outcome_of=authorization_outcome,
            repeat_is_safe=True,  # PayPal-Request-Id kept 45 days
            sent=(payment.amount_minor, cur),
            claim={"kind": Op.REAUTHORIZE, "order": payment.order, "amount_minor": payment.amount_minor,
                   "currency": cur},
        )
    except ProviderError as e:
        if e.outcome_unknown:
            raise
        # Renewal refused, but the original hold has not expired: still try to collect it.
        logger.warning("Reauthorization of order %s refused: %s", payment.order.number, e.message)
        return "ready", (f"PayPal refused to renew the stale authorization ({e.message}); "
                         "capturing the original authorization without a funds guarantee.")
    if op.outcome == Op.PENDING:
        return "pending", "PayPal is still processing the authorization renewal."
    if op.outcome != Op.DONE:
        raise ApiProblem(409, "authorization_not_renewable",
                         f"PayPal did not renew the stale authorization (status {op.provider_status or op.outcome}). "
                         "Cancel the order and ask the shopper to pay again.")
    if _apply_once(op):
        d = op.detail
        payment.original_authorization_id = payment.authorization_id
        payment.authorization_id = op.provider_id
        payment.authorization_status = d.get("authorization_status", "")
        payment.authorization_created_at = provider_time(d.get("created"))
        payment.authorization_expires_at = provider_time(d.get("expires"))
        payment.reauthorized_at = timezone.now()
        payment.save()
        _source(payment).allocate(Decimal(0), reference=op.provider_id, status="REAUTHORIZED")
    return "ready", "The stale authorization was renewed before capture."


def _refresh_pending(op: Op, fetch: Any, read: Any, outcome_of: Any) -> Op:
    """Re-read a write PayPal accepted but had not finished."""
    if op.outcome != Op.PENDING or not op.provider_id:
        return op
    result = call_paypal(lambda: gateway.read_with_retry(lambda: fetch(op.provider_id)))
    got = read(result)
    return complete(op.ref, outcome_of(got.status), got.provider_id, got.provider_time,
                    status=got.status, detail={**op.detail, **(got.detail or {})})


def fulfil_order(order: Any) -> tuple[Op | None, PayPalPayment, str]:
    payment = order.paypal_payment
    if payment.state == PayPalPayment.CAPTURED:
        return None, payment, ""
    if payment.state not in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING):
        raise ApiProblem(409, "not_capturable",
                         f"The order's payment is '{payment.state}'; only an authorized payment can be fulfilled.")
    if payment.state == PayPalPayment.AUTHORIZED and not _transition(
            payment, (PayPalPayment.AUTHORIZED,), PayPalPayment.CAPTURING):
        raise ApiProblem(409, "payment_busy", f"The payment is now '{payment.state}'; try again.")

    try:
        readiness, note = _renew_if_stale(payment)
    except ApiProblem:
        _transition(payment, (PayPalPayment.CAPTURING,), PayPalPayment.AUTHORIZED)
        raise
    if readiness == "pending":
        return None, payment, note

    client = gateway.get_client()
    cur = payment.currency
    auth_id = payment.authorization_id
    op, _ = safe_write(
        ref("order", order.number, "capture", auth_id),
        send=lambda key: client.payments.capture_authorized_payment(
            auth_id, pay_pal_request_id=key, prefer="return=representation",
            body=CaptureRequest(amount=Money(currency_code=cur, value=wire(payment.amount_minor, cur)),
                                final_capture=True)),
        find=lambda _ref: _find_capture(payment.paypal_order_id),
        read=_read_capture,
        outcome_of=capture_outcome,
        sent=(payment.amount_minor, cur),
        claim={"kind": Op.CAPTURE, "order": order, "amount_minor": payment.amount_minor, "currency": cur},
    )
    op = _refresh_pending(op, client.payments.get_captured_payment, _read_capture, capture_outcome)

    if op.outcome == Op.DONE:
        _record_capture(payment, op)
    elif op.outcome == Op.FAILED:
        _transition(payment, (PayPalPayment.CAPTURING,), PayPalPayment.AUTHORIZED)
    payment.refresh_from_db()
    return op, payment, note


def _record_capture(payment: PayPalPayment, op: Op) -> None:
    if not _apply_once(op):
        return
    cur = payment.currency
    d = op.detail
    with transaction.atomic():
        payment.capture_id = op.provider_id
        payment.capture_status = op.provider_status
        payment.captured_minor = payment.amount_minor
        payment.paypal_fee_minor = to_minor(d["fee"], cur) if d.get("fee") is not None else None
        payment.net_minor = to_minor(d["net"], cur) if d.get("net") is not None else None
        payment.captured_at = op.provider_time or timezone.now()
        payment.state = PayPalPayment.CAPTURED
        payment.save()
        _source(payment).debit(_decimal(payment.amount_minor, cur), reference=op.provider_id,
                               status=op.provider_status)
        order = payment.order
        for status in ("Being processed", "Complete"):
            if status in order.available_statuses():
                order.set_status(status)
        EventHandler().consume_stock_allocations(order)


# --- void (cancel) ---------------------------------------------------------


def cancel_order(order: Any) -> tuple[Op | None, PayPalPayment]:
    payment = order.paypal_payment
    if payment.state in (PayPalPayment.CANCELLED, PayPalPayment.VOIDED):
        return None, payment
    if payment.state in (PayPalPayment.CAPTURING, PayPalPayment.CAPTURED):
        raise ApiProblem(409, "already_fulfilled",
                         "The payment has been (or is being) captured; return money with POST /refunds instead.")

    if payment.state in (PayPalPayment.AWAITING, PayPalPayment.AUTHORIZATION_EXPIRED):
        unresolved = Op.objects.filter(order=order, kind=Op.AUTHORIZE,
                                       outcome__in=(Op.SENDING, Op.UNKNOWN, Op.PENDING, Op.NEEDS_REVIEW))
        if payment.state == PayPalPayment.AWAITING and unresolved.exists():
            raise ApiProblem(409, "payment_unresolved",
                             "A payment attempt for this order has no final answer from PayPal yet; "
                             "cancelling now could leave funds held. Retry once it is settled.")
        # No hold exists (never authorized, or the hold expired): nothing to release at PayPal.
        if _transition(payment, (payment.state,), PayPalPayment.CANCELLED):
            _cancel_locally(order)
        return None, payment

    if payment.state == PayPalPayment.AUTHORIZED and not _transition(
            payment, (PayPalPayment.AUTHORIZED,), PayPalPayment.VOIDING):
        raise ApiProblem(409, "payment_busy", f"The payment is now '{payment.state}'; try again.")

    client = gateway.get_client()
    auth_id = payment.authorization_id
    try:
        op, _ = safe_write(
            ref("order", order.number, "void", auth_id),
            send=lambda key: client.payments.void_payment(auth_id, pay_pal_request_id=key,
                                                          prefer="return=representation"),
            find=lambda key: client.payments.void_payment(auth_id, pay_pal_request_id=key,
                                                          prefer="return=representation"),
            read=_read_authorization,
            outcome_of=void_outcome,
            repeat_is_safe=True,  # smoke: a same-key repeat answers VOIDED again
            claim={"kind": Op.VOID, "order": order},
        )
    except ProviderError as e:
        if not e.outcome_unknown:
            _transition(payment, (PayPalPayment.VOIDING,), PayPalPayment.AUTHORIZED)
        raise
    if op.outcome == Op.DONE and _apply_once(op):
        with transaction.atomic():
            payment.state = PayPalPayment.VOIDED
            payment.authorization_status = op.provider_status
            payment.save()
            Transaction.objects.create(source=_source(payment), txn_type="Void",
                                       amount=_decimal(payment.amount_minor, payment.currency),
                                       reference=op.provider_id, status=op.provider_status)
            _cancel_locally(order)
    elif op.outcome == Op.FAILED:
        # PayPal says the money was already captured: never report it as released.
        op.detail = {**op.detail, "message": "PayPal reports this authorization as captured; review it in PayPal."}
    payment.refresh_from_db()
    return op, payment


def _cancel_locally(order: Any) -> None:
    with transaction.atomic():
        if "Cancelled" in order.available_statuses():
            order.set_status("Cancelled")
        EventHandler().cancel_stock_allocations(order)


# --- refund ----------------------------------------------------------------


def _read_refund(result: Refund) -> Answer:
    money = v(result.amount)
    return Answer(
        provider_id=v(result.id) or "",
        status=v(result.status),
        provider_time=provider_time(v(result.create_time)),
        amount=money.value if money else None,
        currency=money.currency_code if money else None,
    )


def refund_order(order: Any, payload: dict[str, Any], key: str) -> tuple[Op, PayPalPayment]:
    payment = order.paypal_payment
    if payment.state != PayPalPayment.CAPTURED:
        raise ApiProblem(409, "not_refundable", "Only a fulfilled (captured) order can be refunded.")
    cur = payment.currency

    raw_amount = payload.get("amount")
    if raw_amount is None:
        requested: int | None = None
        fingerprint = "full"
    else:
        try:
            requested = to_minor(str(raw_amount), cur)
        except ValueError as e:
            raise ApiProblem(400, "invalid_amount", str(e)) from None
        if requested <= 0:
            raise ApiProblem(400, "invalid_amount", "amount must be greater than zero.")
        fingerprint = f"amount:{requested}"
    note = str(payload.get("note", ""))[:255]

    op_ref = ref("order", order.number, "refund", key_digest(key))
    existing = Op.objects.filter(ref=op_ref).first()
    if existing is not None and existing.fingerprint != fingerprint:
        raise ApiProblem(409, "idempotency_key_reused",
                         "This Idempotency-Key was already used for a different refund request.")
    amount = existing.amount_minor if existing is not None and existing.amount_minor else (
        requested if requested is not None else payment.captured_minor - payment.refund_reserved_minor)
    if amount is None or amount <= 0:
        raise ApiProblem(409, "nothing_to_refund", "Nothing remains refundable on this order.")

    def reserve() -> None:
        # Conditional UPDATE: concurrent refunds can never together exceed the capture.
        reserved = PayPalPayment.objects.filter(
            pk=payment.pk, refund_reserved_minor__lte=F("captured_minor") - amount
        ).update(refund_reserved_minor=F("refund_reserved_minor") + amount)
        if reserved != 1:
            payment.refresh_from_db()
            raise ApiProblem(409, "exceeds_refundable",
                             f"Only {wire(payment.captured_minor - payment.refund_reserved_minor, cur)} {cur} "
                             "remains refundable on this order.")
        Op.objects.filter(ref=op_ref).update(reserved_minor=amount)

    client = gateway.get_client()
    body = RefundRequest(amount=Money(currency_code=cur, value=wire(amount, cur)),
                         note_to_payer=note if note else UNSET)

    def send(k: str) -> Refund:
        return client.payments.refund_captured_payment(payment.capture_id, pay_pal_request_id=k,
                                                       prefer="return=representation", body=body)

    op, _ = safe_write(
        op_ref,
        send=send,
        find=send,  # smoke: a same-key repeat returns the same refund
        read=_read_refund,
        outcome_of=refund_outcome,
        repeat_is_safe=True,
        sent=(amount, cur),
        claim={"kind": Op.REFUND, "order": order, "user": order.user, "amount_minor": amount,
               "currency": cur, "fingerprint": fingerprint},
        on_claimed=reserve,
        release=lambda: _release_reservation(payment, op_ref),
    )
    op = _refresh_pending(op, client.payments.get_refund, _read_refund, refund_outcome)
    if op.outcome == Op.FAILED:
        _release_reservation(payment, op_ref)
    elif op.outcome == Op.DONE and _apply_once(op):
        with transaction.atomic():
            PayPalPayment.objects.filter(pk=payment.pk).update(refunded_minor=F("refunded_minor") + amount)
            _source(payment).refund(_decimal(amount, cur), reference=op.provider_id, status=op.provider_status)
    payment.refresh_from_db()
    return op, payment


def _release_reservation(payment: PayPalPayment, op_ref: str) -> None:
    """Give back a failed refund's reservation, exactly once."""
    op = Op.objects.filter(ref=op_ref).first()
    if op is None or op.reserved_minor <= 0:
        return
    if Op.objects.filter(pk=op.pk, reserved_minor=op.reserved_minor).update(reserved_minor=0) == 1:
        PayPalPayment.objects.filter(pk=payment.pk).update(
            refund_reserved_minor=F("refund_reserved_minor") - op.reserved_minor)

