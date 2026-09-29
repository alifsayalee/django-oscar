"""
Order placement and the payment lifecycle: authorize at checkout, capture at
fulfilment (renewing a stale hold first), void on cancel, refund on return.

Every PayPal write goes through ``safe_write``; every outcome reaches the caller
through ``outcomes.http_status``.
"""

import logging
from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from pay_pal_server_sdk.core import ApiError
from pay_pal_server_sdk.models import (
    AmountWithBreakdown,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Order as PayPalOrder,
    OrderAuthorizeResponse,
    OrderRequest,
    PaymentAuthorization,
    PaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    Refund,
    RefundRequest,
)
from pay_pal_server_sdk.models.enums import AuthorizationStatus, CheckoutPaymentIntent, OrderStatus

from . import outcomes
from .cardinput import CardInput
from .common import ClientError, ServiceResult, digest, reference_prefix
from .models import OrderPayment, Outcome, PaymentRefund, PaymentState, ProviderWrite, SavedCard
from .paypal import (
    currency,
    describe_error,
    format_amount,
    get_client,
    money,
    quantize,
    translate,
    unset_to_none,
)
from .safe_write import Answer, OutcomeUnknown, WriteResult, provider_time, safe_write

logger = logging.getLogger("apps.payments")

Order = get_model("order", "Order")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Transaction = get_model("payment", "Transaction")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")

# PayPal: the honor period of an authorization is three days; within 29 days of
# the original authorization it can be reauthorized; after that a new
# authorization is needed (payments.reauthorize_payment docstring).
HONOR_PERIOD = timedelta(days=3)
REAUTHORIZE_LIMIT = timedelta(days=29)

STATUS_PROCESSING = "Being processed"
STATUS_COMPLETE = "Complete"
STATUS_CANCELLED = "Cancelled"

PAID_STATES = (PaymentState.CAPTURED, PaymentState.PARTIALLY_REFUNDED, PaymentState.REFUNDED)

PREFER = "return=representation"


# --- placing an order --------------------------------------------------------

def place_order(user: Any, request: Any, lines: Iterable[tuple[int, int]]) -> Any:
    """Create an Oscar order (awaiting payment) from catalogue product ids and quantities."""
    cur = currency()
    with transaction.atomic():
        # A SAVED basket is editable but never picked up (or merged) by the
        # session basket middleware.
        basket = Basket.objects.create(owner=user, status=Basket.SAVED)
        basket.strategy = Selector().strategy(request=request, user=user)
        for product_id, quantity in lines:
            product = Product.objects.filter(pk=product_id).first()
            if product is None:
                raise ClientError(422, "unknown_product", f"No catalogue item with id {product_id}")
            info = basket.strategy.fetch_for_product(product)
            if not info.availability.is_available_to_buy:
                raise ClientError(422, "not_available", f"Catalogue item {product_id} is not available to buy")
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ClientError(422, "not_available", f"Catalogue item {product_id}: {reason}")
            basket.add_product(product, quantity)
        shipping_method = Free()
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        amount = quantize(Decimal(total.incl_tax), cur)
        if amount <= 0:
            raise ClientError(422, "empty_order", "The order total must be greater than zero")
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            request=request,
        )
        basket.submit()
        # Amounts are charged in the configured PayPal currency.
        order.currency = cur
        order.save(update_fields=["currency"])
        OrderPayment.objects.create(order=order, amount=amount, currency=cur)
    return order


# --- shared helpers ------------------------------------------------------------

def _ref(payment: OrderPayment, step: str) -> str:
    return f"{reference_prefix()}:o{payment.order_id}:a{payment.attempt}:{step}"


def _invoice_id(payment: OrderPayment) -> str:
    return f"{reference_prefix()}-{payment.order.number}-a{payment.attempt}"


def _set_order_status(order: Any, status: str) -> None:
    if order.status != status and status in order.available_statuses():
        order.set_status(status)


def _source(payment: OrderPayment) -> Any:
    if payment.source_id:
        return payment.source
    source_type, _ = SourceType.objects.get_or_create(name="PayPal card")
    source = Source.objects.create(
        order=payment.order, source_type=source_type, currency=payment.currency, label=payment.card_label[:128]
    )
    payment.source = source
    return source


def _first_authorization(response: Any) -> Any:
    units = unset_to_none(response.purchase_units) or []
    for unit in units:
        payments = unset_to_none(unit.payments)
        auths = unset_to_none(payments.authorizations) if payments is not None else None
        if auths:
            return auths[0]
    return None


def _card_label(card_response: Any) -> str:
    if card_response is None:
        return ""
    brand = unset_to_none(card_response.brand) or "Card"
    last = unset_to_none(card_response.last_digits) or ""
    return f"{brand} ending {last}".strip()


def _unknown_body(record: ProviderWrite, message: str) -> dict[str, Any]:
    return {
        "error": "outcome_unknown",
        "outcomeUnknown": True,
        "message": message,
        "reference": record.ref,
    }


def _money_str(value: Decimal | None, cur: str) -> str | None:
    return None if value is None else format_amount(value, cur)


# --- serialization -------------------------------------------------------------

def serialize_order(order: Any) -> dict[str, Any]:
    payment: OrderPayment | None = getattr(order, "paypal_payment", None)
    lines = [
        {
            "productId": line.product_id,
            "title": line.title,
            "quantity": line.quantity,
            "lineTotal": str(line.line_price_incl_tax),
        }
        for line in order.lines.all()
    ]
    body: dict[str, Any] = {
        "orderId": str(order.number),
        "status": order.status,
        "placedAt": order.date_placed.isoformat() if order.date_placed else None,
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "lines": lines,
        "payment": serialize_payment(payment) if payment is not None else None,
    }
    return body


def serialize_payment(payment: OrderPayment) -> dict[str, Any]:
    cur = payment.currency
    refunds = list(payment.refunds.all())
    return {
        "state": payment.state,
        "amount": format_amount(payment.amount, cur),
        "currency": cur,
        "card": payment.card_label or None,
        "savedCardId": str(payment.saved_card.public_id) if payment.saved_card else None,
        "paypalOrderId": payment.paypal_order_id or None,
        "authorization": {
            "id": payment.authorization_id,
            "status": payment.authorization_status or None,
            "authorizedAt": payment.authorized_at.isoformat() if payment.authorized_at else None,
            "expiresAt": payment.authorization_expires_at.isoformat() if payment.authorization_expires_at else None,
            "reauthorizations": payment.reauthorizations,
        }
        if payment.authorization_id
        else None,
        "capture": {
            "id": payment.capture_id,
            "status": payment.capture_status or None,
            "amount": _money_str(payment.captured_amount, cur),
            "paypalFee": _money_str(payment.paypal_fee, cur),
            "netAmount": _money_str(payment.net_amount, cur),
            "capturedAt": payment.captured_at.isoformat() if payment.captured_at else None,
        }
        if payment.capture_id
        else None,
        "refundedAmount": format_amount(payment.refunded_amount, cur),
        "refundableAmount": format_amount(refundable_amount(payment), cur) if payment.captured_amount else "0.00",
        "refunds": [serialize_refund(r) for r in refunds],
        "lastError": payment.last_error or None,
    }


def serialize_refund(refund: PaymentRefund) -> dict[str, Any]:
    return {
        "refundId": str(refund.public_id),
        "amount": format_amount(refund.amount, refund.payment.currency),
        "status": refund.outcome,
        "paypalRefundId": refund.paypal_refund_id or None,
        "paypalStatus": refund.paypal_status or None,
        "createdAt": refund.created_at.isoformat(),
    }


def _result(order: Any, status: int, **extra: Any) -> ServiceResult:
    order.refresh_from_db()
    body = serialize_order(order)
    body.update(extra)
    return ServiceResult(status, body)


# --- pay: create the PayPal order, then authorize it -----------------------------

def pay(order: Any, *, card: CardInput | None, saved_card: SavedCard | None) -> ServiceResult:
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().select_related("order").get(order=order)
        if payment.state in (PaymentState.AUTHORIZED, PaymentState.CAPTURING, PaymentState.CAPTURE_PENDING, *PAID_STATES):
            # Already paid for: a double-click is answered from what is recorded.
            return _result(order, 200)
        if payment.state == PaymentState.CANCELLED:
            raise ClientError(409, "order_cancelled", "This order has been cancelled")
        if payment.state == PaymentState.NEEDS_REVIEW:
            raise ClientError(409, "needs_review", "This order's payment needs operator review", detail=payment.last_error)
        if payment.state in (PaymentState.AWAITING_PAYMENT, PaymentState.PAYMENT_FAILED):
            if payment.state == PaymentState.PAYMENT_FAILED:
                payment.attempt += 1  # a new attempt after a definitive failure: fresh references
            payment.state = PaymentState.AUTHORIZING
            payment.saved_card = saved_card
            payment.card_label = (
                f"{saved_card.brand or 'Card'} ending {saved_card.last_digits}" if saved_card else
                f"Card ending {card.last_digits}" if card else ""
            )
            payment.last_error = ""
            payment.save()
        # AUTHORIZING: a repeat of an attempt in progress continues under the same references.

    if payment.saved_card is not None and not payment.saved_card.is_usable:
        _payment_failed(payment, "The saved card used for this attempt has been removed")
        raise ClientError(409, "payment_method_removed", "That saved card has been removed; pay with another card")

    card_request = (
        CardRequest(vault_id=payment.saved_card.paypal_token_id)
        if payment.saved_card is not None
        else card.to_card_request() if card is not None else None
    )
    if card_request is None:
        raise ClientError(422, "payment_source_required", "Provide card details or a paymentMethodId")

    try:
        created = _create_paypal_order(payment, card_request)
        if created.outcome != Outcome.DONE:
            return _not_done(order, payment, created.record, step="create")
        payment.refresh_from_db()
        if payment.state == PaymentState.AUTHORIZED:
            return _result(order, 200)  # the create step already carried the authorization
        authorized = _authorize(payment)
        if authorized.outcome != Outcome.DONE:
            return _not_done(order, payment, authorized.record, step="authorize")
        return _result(order, 200)
    except OutcomeUnknown as e:
        return _result(order, 504, **_unknown_body(e.record, "PayPal's answer was lost; repeat the request to settle it"))
    except ApiError as e:
        # A first send PayPal refused: nothing is held.
        _payment_failed(payment, describe_error(e.error))
        err = translate(e)
        return _result(order, err.status_code, error=err.code, message=err.message)


def _payment_failed(payment: OrderPayment, message: str) -> None:
    OrderPayment.objects.filter(pk=payment.pk).update(state=PaymentState.PAYMENT_FAILED, last_error=message[:500])


def _not_done(order: Any, payment: OrderPayment, record: ProviderWrite, *, step: str) -> ServiceResult:
    status = outcomes.http_status(record.outcome)
    if record.outcome in (Outcome.FAILED,):
        message = {
            OrderStatus.PAYER_ACTION_REQUIRED.value: (
                "The card issuer requires the shopper to approve this payment in a browser "
                "(3-D Secure); that flow is not supported. Use a different card."
            ),
        }.get(record.provider_status, f"PayPal did not {step} the payment (status {record.provider_status or 'n/a'}).")
        _payment_failed(payment, message)
        return _result(order, status, error="payment_failed", message=message)
    if record.outcome == Outcome.NEEDS_REVIEW:
        OrderPayment.objects.filter(pk=payment.pk).update(state=PaymentState.NEEDS_REVIEW, last_error=record.detail)
        return _result(order, status, error="needs_review", message=record.detail)
    if record.outcome == Outcome.UNKNOWN:
        return _result(order, status, **_unknown_body(record, f"PayPal's {step} outcome is not known yet"))
    return _result(order, status, message=f"PayPal has not finished the {step} step yet; repeat the request")


def _create_paypal_order(payment: OrderPayment, card_request: CardRequest) -> WriteResult:
    cur = payment.currency
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=payment.order.number,
                invoice_id=_invoice_id(payment),
                custom_id=f"{reference_prefix()}:o{payment.order_id}",
                description=f"Order {payment.order.number}",
                amount=AmountWithBreakdown(currency_code=cur, value=format_amount(payment.amount, cur)),
            )
        ],
        payment_source=PaymentSource(card=card_request),
    )

    def send(key: str) -> PayPalOrder:
        return get_client().orders.create_order(body, pay_pal_request_id=key, prefer=PREFER)

    def read(o: PayPalOrder) -> Answer:
        units = unset_to_none(o.purchase_units) or []
        amount = unset_to_none(units[0].amount) if units else None
        return Answer(
            unset_to_none(o.id),
            unset_to_none(o.status),
            provider_time(unset_to_none(o.create_time)),
            unset_to_none(amount.value) if amount is not None else None,
            unset_to_none(amount.currency_code) if amount is not None else None,
        )

    def on_complete(record: ProviderWrite, o: PayPalOrder) -> None:
        p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        p.paypal_order_id = unset_to_none(o.id) or p.paypal_order_id
        p.paypal_order_status = str(unset_to_none(o.status) or "")
        source = unset_to_none(o.payment_source)
        label = _card_label(unset_to_none(source.card) if source is not None else None)
        if label and p.saved_card is None:
            p.card_label = label
        p.save()
        if record.outcome == Outcome.DONE:
            auth = _first_authorization(o)
            if auth is not None and outcomes.authorization_outcome(unset_to_none(auth.status)) == Outcome.DONE:
                _record_authorization(p, auth)

    return safe_write(
        _ref(payment, "create"),
        kind="create_order",
        send=send,
        read=read,
        outcome_of=outcomes.order_create_outcome,
        repeat_is_safe=True,  # PayPal-Request-Id returns the original order (6 h)
        sent=(payment.amount, cur),
        on_complete=on_complete,
    )


def _record_authorization(p: OrderPayment, auth: Any) -> None:
    """Book a hold that PayPal reports as in effect."""
    placed = provider_time(unset_to_none(auth.create_time)) or timezone.now()
    p.authorization_id = unset_to_none(auth.id) or ""
    p.authorization_status = str(unset_to_none(auth.status) or "")
    p.authorized_at = p.authorized_at or placed
    p.honor_period_start = placed
    p.authorization_expires_at = provider_time(unset_to_none(auth.expiration_time))
    p.state = PaymentState.AUTHORIZED
    source = _source(p)
    p.save()
    source.allocate(p.amount, reference=p.authorization_id, status=p.authorization_status)
    _set_order_status(p.order, STATUS_PROCESSING)


def _authorize(payment: OrderPayment) -> WriteResult:
    payment.refresh_from_db()
    cur = payment.currency
    paypal_order_id = payment.paypal_order_id

    def send(key: str) -> OrderAuthorizeResponse:
        return get_client().orders.authorize_order(paypal_order_id, pay_pal_request_id=key, prefer=PREFER)

    def read(r: OrderAuthorizeResponse) -> Answer:
        auth = _first_authorization(r)
        if auth is None:
            return Answer(None, None, None)
        amount = unset_to_none(auth.amount)
        return Answer(
            unset_to_none(auth.id),
            unset_to_none(auth.status),
            provider_time(unset_to_none(auth.create_time)),
            amount.value if amount is not None else None,
            amount.currency_code if amount is not None else None,
        )

    def on_complete(record: ProviderWrite, r: OrderAuthorizeResponse) -> None:
        p = OrderPayment.objects.select_for_update().select_related("order").get(pk=payment.pk)
        auth = _first_authorization(r)
        if auth is None:
            return
        if record.outcome == Outcome.DONE:
            _record_authorization(p, auth)
        else:
            p.authorization_id = unset_to_none(auth.id) or ""
            p.authorization_status = str(unset_to_none(auth.status) or "")
            p.save()

    return safe_write(
        _ref(payment, "authorize"),
        kind="authorize",
        send=send,
        read=read,
        outcome_of=outcomes.authorization_outcome,
        repeat_is_safe=True,
        sent=(payment.amount, cur),
        on_complete=on_complete,
    )


# --- fulfil: renew a stale hold if needed, then capture ------------------------

def fulfil(order: Any) -> ServiceResult:
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().select_related("order").get(order=order)
        if payment.state in PAID_STATES:
            return _result(order, 200)
        refresh = payment.state == PaymentState.CAPTURE_PENDING
        if not refresh and payment.state not in (PaymentState.AUTHORIZED, PaymentState.CAPTURING):
            raise ClientError(
                409, "not_authorized",
                f"The order cannot be fulfilled: its payment is '{payment.state}', not authorized",
            )
        if not refresh:
            payment.state = PaymentState.CAPTURING
            payment.save(update_fields=["state", "updated_at"])
    if refresh:
        return _refresh_capture(order, payment)

    capture_ref = _ref(payment, "capture")
    try:
        if not ProviderWrite.objects.filter(ref=capture_ref).exists():
            # Only before the first capture attempt: afterwards the capture claim settles it.
            renewed = _ensure_fresh_authorization(order, payment)
            if renewed is not None:
                return renewed
        captured = _capture(payment)
    except OutcomeUnknown as e:
        return _result(order, 504, **_unknown_body(e.record, "PayPal's answer was lost; repeat the request to settle it"))
    except ApiError as e:
        OrderPayment.objects.filter(pk=payment.pk).update(state=PaymentState.AUTHORIZED, last_error=describe_error(e.error))
        err = translate(e)
        return _result(
            order, 409 if err.status_code in (409, 422) else err.status_code,
            error="capture_refused",
            message=(
                f"PayPal refused the capture ({describe_error(e.error)}). If the authorization has expired, "
                "cancel the order and ask the shopper to pay again."
            ),
        )
    if captured.outcome == Outcome.DONE:
        _fill_fee_breakdown(payment)
        return _result(order, 200)
    return _capture_not_done(order, payment, captured.record)


def _capture_not_done(order: Any, payment: OrderPayment, record: ProviderWrite) -> ServiceResult:
    status = outcomes.http_status(record.outcome)
    if record.outcome == Outcome.PENDING:
        return _result(order, status, message="PayPal is still processing the capture; repeat the request to refresh")
    if record.outcome in (Outcome.FAILED, Outcome.NEEDS_REVIEW):
        message = f"PayPal reported the capture as {record.provider_status or record.outcome}. {record.detail}".strip()
        OrderPayment.objects.filter(pk=payment.pk).update(state=PaymentState.NEEDS_REVIEW, last_error=message[:500])
        return _result(order, status, error="capture_failed", message=message)
    if record.outcome == Outcome.UNKNOWN:
        return _result(order, status, **_unknown_body(record, "PayPal's capture outcome is not known yet"))
    return _result(order, status, message="The capture is in progress")


def _ensure_fresh_authorization(order: Any, payment: OrderPayment) -> ServiceResult | None:
    """Renew a hold past its honor period; refuse (with an operator action) one that can no longer be renewed."""
    payment.refresh_from_db()
    try:
        current = get_client().payments.get_authorized_payment(payment.authorization_id)
    except Exception as exc:  # a read: translate every failure kind at the boundary
        _back_to_authorized(payment)
        err = translate(exc)
        return _result(order, err.status_code, error=err.code, message=f"Could not check the authorization: {err.message}")

    status = unset_to_none(current.status)
    expires = provider_time(unset_to_none(current.expiration_time)) or payment.authorization_expires_at
    now = timezone.now()
    if status in (AuthorizationStatus.VOIDED, AuthorizationStatus.DENIED):
        return _not_renewable(
            order, payment,
            f"PayPal reports the authorization as {status}: there is no hold left to capture or renew.",
        )
    if status in (AuthorizationStatus.CAPTURED, AuthorizationStatus.PARTIALLY_CAPTURED):
        return None  # proceed: the capture step's same-reference resend returns our capture
    if (expires is not None and expires <= now) or (
        payment.authorized_at is not None and now - payment.authorized_at >= REAUTHORIZE_LIMIT
    ):
        placed = payment.authorized_at.date().isoformat() if payment.authorized_at else "unknown date"
        return _not_renewable(
            order, payment,
            f"The authorization placed on {placed} is past PayPal's 29-day reauthorization window"
            + (f" (expired {expires.isoformat()})" if expires is not None and expires <= now else "")
            + " and can no longer be renewed.",
        )
    start = payment.honor_period_start or payment.authorized_at
    if start is None or now - start < HONOR_PERIOD:
        return None  # still inside the honor period: capture directly
    try:
        renewed = _reauthorize(payment)
    except ApiError as e:
        return _not_renewable(order, payment, f"PayPal refused to renew the authorization ({describe_error(e.error)}).")
    except OutcomeUnknown as e:
        _back_to_authorized(payment)
        return _result(order, 504, **_unknown_body(e.record, "PayPal's reauthorization outcome is not known yet"))
    if renewed.outcome == Outcome.DONE:
        return None
    _back_to_authorized(payment)
    if renewed.outcome == Outcome.FAILED:
        return _not_renewable(order, payment, f"PayPal answered the reauthorization with {renewed.record.provider_status}.")
    return _result(order, outcomes.http_status(renewed.outcome), message="The authorization renewal is not finished yet")


def _back_to_authorized(payment: OrderPayment) -> None:
    OrderPayment.objects.filter(pk=payment.pk, state=PaymentState.CAPTURING).update(state=PaymentState.AUTHORIZED)


def _not_renewable(order: Any, payment: OrderPayment, reason: str) -> ServiceResult:
    message = (
        f"{reason} The order cannot be fulfilled with this payment. Operator action: cancel the order "
        f"(POST /api/orders/{order.number}/cancel) and ask the shopper to place and pay for a new order."
    )
    OrderPayment.objects.filter(pk=payment.pk).update(state=PaymentState.AUTHORIZED, last_error=message[:500])
    return _result(order, 409, error="authorization_not_renewable", message=message)


def _reauthorize(payment: OrderPayment) -> WriteResult:
    cur = payment.currency
    n = payment.reauthorizations + 1
    authorization_id = payment.authorization_id

    def send(key: str) -> PaymentAuthorization:
        return get_client().payments.reauthorize_payment(
            authorization_id, pay_pal_request_id=key, prefer=PREFER,
            body=ReauthorizeRequest(amount=money(payment.amount, cur)),
        )

    def on_complete(record: ProviderWrite, a: PaymentAuthorization) -> None:
        if record.outcome != Outcome.DONE:
            return
        p = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        p.authorization_id = unset_to_none(a.id) or p.authorization_id
        p.authorization_status = str(unset_to_none(a.status) or "")
        p.honor_period_start = provider_time(unset_to_none(a.create_time)) or timezone.now()
        p.authorization_expires_at = provider_time(unset_to_none(a.expiration_time)) or p.authorization_expires_at
        p.reauthorizations = n
        p.save()

    return safe_write(
        _ref(payment, f"reauth{n}"),
        kind="reauthorize",
        send=send,
        read=_read_authorization,
        outcome_of=outcomes.authorization_outcome,
        repeat_is_safe=True,
        sent=(payment.amount, cur),
        on_complete=on_complete,
    )


def _read_authorization(a: PaymentAuthorization) -> Answer:
    amount = unset_to_none(a.amount)
    return Answer(
        unset_to_none(a.id),
        unset_to_none(a.status),
        provider_time(unset_to_none(a.update_time) or unset_to_none(a.create_time)),
        amount.value if amount is not None else None,
        amount.currency_code if amount is not None else None,
    )


def _capture(payment: OrderPayment) -> WriteResult:
    payment.refresh_from_db()
    cur = payment.currency
    authorization_id = payment.authorization_id

    def send(key: str) -> CapturedPayment:
        return get_client().payments.capture_authorized_payment(
            authorization_id, pay_pal_request_id=key, prefer=PREFER,
            body=CaptureRequest(amount=money(payment.amount, cur), final_capture=True),
        )

    def read(c: CapturedPayment) -> Answer:
        amount = unset_to_none(c.amount)
        return Answer(
            unset_to_none(c.id),
            unset_to_none(c.status),
            provider_time(unset_to_none(c.create_time)),
            amount.value if amount is not None else None,
            amount.currency_code if amount is not None else None,
        )

    def on_complete(record: ProviderWrite, c: CapturedPayment) -> None:
        p = OrderPayment.objects.select_for_update().select_related("order").get(pk=payment.pk)
        _apply_capture(p, record.outcome, c)

    return safe_write(
        _ref(payment, "capture"),
        kind="capture",
        send=send,
        read=read,
        outcome_of=outcomes.capture_outcome,
        repeat_is_safe=True,  # PayPal-Request-Id returns the original capture (45 days)
        sent=(payment.amount, cur),
        on_complete=on_complete,
    )


def _apply_capture(p: OrderPayment, outcome: str, c: CapturedPayment) -> None:
    p.capture_id = unset_to_none(c.id) or p.capture_id
    p.capture_status = str(unset_to_none(c.status) or "")
    amount = unset_to_none(c.amount)
    if amount is not None:
        p.captured_amount = Decimal(amount.value)
    breakdown = unset_to_none(c.seller_receivable_breakdown)
    if breakdown is not None:
        fee = unset_to_none(breakdown.paypal_fee)
        net = unset_to_none(breakdown.net_amount)
        p.paypal_fee = Decimal(fee.value) if fee is not None else p.paypal_fee
        p.net_amount = Decimal(net.value) if net is not None else p.net_amount
    if outcome == Outcome.DONE and p.state not in PAID_STATES:
        p.state = PaymentState.CAPTURED
        p.authorization_status = AuthorizationStatus.CAPTURED.value
        p.captured_at = provider_time(unset_to_none(c.create_time)) or timezone.now()
        p.last_error = ""
        source = _source(p)
        p.save()
        source.debit(p.captured_amount or p.amount, reference=p.capture_id, status=p.capture_status)
        _set_order_status(p.order, STATUS_COMPLETE)
        return
    if outcome == Outcome.PENDING:
        p.state = PaymentState.CAPTURE_PENDING
    p.save()


def _fill_fee_breakdown(payment: OrderPayment) -> None:
    """PayPal's fee and net normally arrive with the capture; if not, read the capture once."""
    payment.refresh_from_db()
    if payment.paypal_fee is not None and payment.net_amount is not None:
        return
    try:
        c = get_client().payments.get_captured_payment(payment.capture_id)
    except Exception:
        logger.warning("Could not read capture %s for its fee breakdown", payment.capture_id, exc_info=True)
        return
    with transaction.atomic():
        p = OrderPayment.objects.select_for_update().select_related("order").get(pk=payment.pk)
        _apply_capture(p, Outcome.DONE if p.state in PAID_STATES else Outcome.PENDING, c)


def _refresh_capture(order: Any, payment: OrderPayment) -> ServiceResult:
    """A pending capture is re-read (never re-sent); PayPal's answer settles the claim."""
    try:
        c = get_client().payments.get_captured_payment(payment.capture_id)
    except Exception as exc:
        err = translate(exc)
        return _result(order, err.status_code, error=err.code, message=err.message)
    outcome = outcomes.capture_outcome(unset_to_none(c.status))
    with transaction.atomic():
        ProviderWrite.objects.filter(ref=_ref(payment, "capture")).update(
            outcome=outcome, provider_status=str(unset_to_none(c.status) or ""), completed_at=timezone.now()
        )
        p = OrderPayment.objects.select_for_update().select_related("order").get(pk=payment.pk)
        _apply_capture(p, outcome, c)
    if outcome == Outcome.DONE:
        return _result(order, 200)
    record = ProviderWrite.objects.get(ref=_ref(payment, "capture"))
    return _capture_not_done(order, payment, record)


# --- cancel: void the hold -------------------------------------------------------

def cancel(order: Any) -> ServiceResult:
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().select_related("order").get(order=order)
        if payment.state == PaymentState.CANCELLED:
            return _result(order, 200)
        if payment.state in (PaymentState.AWAITING_PAYMENT, PaymentState.PAYMENT_FAILED):
            payment.state = PaymentState.CANCELLED
            payment.save(update_fields=["state", "updated_at"])
            _set_order_status(order, STATUS_CANCELLED)
            return _result(order, 200, message="No payment was held; nothing was charged")
        if payment.capture_id or payment.state in (PaymentState.CAPTURING, PaymentState.CAPTURE_PENDING, *PAID_STATES):
            raise ClientError(409, "already_fulfilled", "The order has been fulfilled; return money with a refund instead")
        # A hold under review (e.g. PayPal held a different amount) can still be released.
        voidable = payment.state == PaymentState.AUTHORIZED or (
            payment.state == PaymentState.NEEDS_REVIEW and payment.authorization_id
        )
        if not voidable:
            raise ClientError(409, "payment_in_progress", f"The order's payment is '{payment.state}'; it cannot be cancelled now")

    authorization_id = payment.authorization_id

    def send(key: str) -> PaymentAuthorization:
        return get_client().payments.void_payment(authorization_id, pay_pal_request_id=key, prefer=PREFER)

    def landed(_: ApiError[Any]) -> PaymentAuthorization | None:
        # A rejection may mean an earlier attempt already voided it: ask PayPal.
        current = get_client().payments.get_authorized_payment(authorization_id)
        return current if unset_to_none(current.status) == AuthorizationStatus.VOIDED else None

    def on_complete(record: ProviderWrite, a: PaymentAuthorization) -> None:
        p = OrderPayment.objects.select_for_update().select_related("order").get(pk=payment.pk)
        p.authorization_status = str(unset_to_none(a.status) or p.authorization_status)
        if record.outcome == Outcome.DONE:
            p.state = PaymentState.CANCELLED
            source = _source(p)
            p.save()
            Transaction.objects.create(
                source=source, txn_type="Void", amount=p.amount, reference=authorization_id, status=p.authorization_status
            )
            _set_order_status(p.order, STATUS_CANCELLED)
        else:
            p.save()

    try:
        voided = safe_write(
            _ref(payment, "void"),
            kind="void",
            send=send,
            read=_read_authorization,
            outcome_of=outcomes.void_outcome,
            repeat_is_safe=True,
            on_complete=on_complete,
            landed_on_rejection=landed,
        )
    except OutcomeUnknown as e:
        return _result(order, 504, **_unknown_body(e.record, "PayPal's answer to the void was lost; repeat the request"))
    except ApiError as e:
        err = translate(e)
        return _result(order, err.status_code, error="void_refused", message=describe_error(e.error))
    if voided.outcome == Outcome.DONE:
        return _result(order, 200, message="The held funds were released; nothing was charged")
    if voided.outcome == Outcome.FAILED:
        OrderPayment.objects.filter(pk=payment.pk).update(
            state=PaymentState.NEEDS_REVIEW,
            last_error=f"Void refused: authorization is {voided.record.provider_status}; money was already taken",
        )
        return _result(order, 409, error="already_captured", message="PayPal reports the hold as captured; refund instead")
    return _result(order, outcomes.http_status(voided.outcome), message="The void is not finished yet")


# --- refunds -----------------------------------------------------------------------

def refundable_amount(payment: OrderPayment) -> Decimal:
    reserved = sum((r.amount for r in payment.refunds.all() if r.reserves_funds), Decimal("0"))
    return (payment.captured_amount or Decimal("0")) - reserved


def refund(order: Any, *, idempotency_key: str, amount: Decimal | None) -> ServiceResult:
    cur = None
    with transaction.atomic():
        payment = OrderPayment.objects.select_for_update().select_related("order").get(order=order)
        cur = payment.currency
        if payment.state not in PAID_STATES:
            raise ClientError(409, "not_captured", "Only a fulfilled (captured) order can be refunded; cancel it instead")
        existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if amount is not None and quantize(amount, cur) != existing.amount:
                raise ClientError(422, "idempotency_key_reused",
                                  "This Idempotency-Key was already used for a refund of a different amount")
            refund_row = existing
        else:
            available = refundable_amount(payment)
            value = quantize(amount if amount is not None else available, cur)
            if value <= 0:
                raise ClientError(409, "nothing_to_refund", "Nothing is left to refund on this order")
            if value > available:
                raise ClientError(409, "exceeds_refundable",
                                  f"Refund of {value} exceeds the refundable amount {format_amount(available, cur)}")
            # Creating the row reserves the amount (under the payment's row lock).
            refund_row = PaymentRefund.objects.create(
                payment=payment,
                idempotency_key=idempotency_key,
                ref=f"{reference_prefix()}:o{order.pk}:refund:{digest(idempotency_key)}",
                amount=value,
            )

    if refund_row.outcome in (Outcome.FAILED, Outcome.NEEDS_REVIEW):
        return _refund_result(order, refund_row, outcomes.http_status(refund_row.outcome))
    if refund_row.outcome == Outcome.PENDING and refund_row.paypal_refund_id:
        return _refresh_refund(order, refund_row)

    capture_id = payment.capture_id

    def send(key: str) -> Refund:
        return get_client().payments.refund_captured_payment(
            capture_id, pay_pal_request_id=key, prefer=PREFER,
            body=RefundRequest(amount=money(refund_row.amount, cur)),
        )

    def read(r: Refund) -> Answer:
        amt = unset_to_none(r.amount)
        return Answer(
            unset_to_none(r.id),
            unset_to_none(r.status),
            provider_time(unset_to_none(r.create_time)),
            amt.value if amt is not None else None,
            amt.currency_code if amt is not None else None,
        )

    def on_complete(record: ProviderWrite, r: Refund) -> None:
        _apply_refund(refund_row.pk, record.outcome, r)

    try:
        result = safe_write(
            refund_row.ref,
            kind="refund",
            send=send,
            read=read,
            outcome_of=outcomes.refund_outcome,
            repeat_is_safe=True,  # PayPal-Request-Id returns the original refund (45 days)
            sent=(refund_row.amount, cur),
            on_complete=on_complete,
        )
    except OutcomeUnknown as e:
        PaymentRefund.objects.filter(pk=refund_row.pk).update(outcome=Outcome.UNKNOWN)  # keeps its reservation
        refund_row.refresh_from_db()
        return _refund_result(order, refund_row, 504, **_unknown_body(
            e.record, "PayPal's refund answer was lost; repeat the request with the same Idempotency-Key"))
    except ApiError as e:
        PaymentRefund.objects.filter(pk=refund_row.pk).update(outcome=Outcome.FAILED)  # releases the reservation
        refund_row.refresh_from_db()
        err = translate(e)
        return _refund_result(order, refund_row, err.status_code, error="refund_refused", message=describe_error(e.error))
    PaymentRefund.objects.filter(pk=refund_row.pk).update(outcome=result.outcome)
    refund_row.refresh_from_db()
    return _refund_result(order, refund_row, outcomes.http_status(result.outcome))


def _apply_refund(refund_pk: int, outcome: str, r: Refund) -> None:
    row = PaymentRefund.objects.select_for_update().select_related("payment", "payment__order").get(pk=refund_pk)
    row.outcome = outcome
    row.paypal_refund_id = unset_to_none(r.id) or row.paypal_refund_id
    row.paypal_status = str(unset_to_none(r.status) or "")
    if outcome == Outcome.DONE and not row.booked:
        p = OrderPayment.objects.select_for_update().get(pk=row.payment_id)
        p.refunded_amount += row.amount
        fully = p.refunded_amount >= (p.captured_amount or p.amount)
        p.state = PaymentState.REFUNDED if fully else PaymentState.PARTIALLY_REFUNDED
        source = _source(p)
        p.save()
        source.refund(row.amount, reference=row.paypal_refund_id, status=row.paypal_status)
        row.booked = True
    row.save()


def _refresh_refund(order: Any, row: PaymentRefund) -> ServiceResult:
    try:
        r = get_client().payments.get_refund(row.paypal_refund_id)
    except Exception as exc:
        err = translate(exc)
        return _refund_result(order, row, err.status_code, error=err.code, message=err.message)
    outcome = outcomes.refund_outcome(unset_to_none(r.status))
    with transaction.atomic():
        ProviderWrite.objects.filter(ref=row.ref).update(
            outcome=outcome, provider_status=str(unset_to_none(r.status) or ""), completed_at=timezone.now()
        )
        _apply_refund(row.pk, outcome, r)
    row.refresh_from_db()
    return _refund_result(order, row, outcomes.http_status(outcome))


def _refund_result(order: Any, row: PaymentRefund, status: int, **extra: Any) -> ServiceResult:
    result = _result(order, status, **extra)
    result.body = {"refundId": str(row.public_id), "refund": serialize_refund(row), "order": result.body} | {
        k: v for k, v in extra.items()
    }
    return result
