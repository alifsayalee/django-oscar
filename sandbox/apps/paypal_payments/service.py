"""
Order payment and saved-card flows.

Each flow that writes to PayPal goes through ``safe_write``, one step at a time,
each step under its own reference. Oscar's ``Order`` carries the order status;
``payment.Source``/``Transaction`` carry the money ledger; ``PayPalPayment``
carries the PayPal ids and statuses a later request needs.
"""

import hashlib
import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.http import HttpRequest
from django.utils import timezone
from oscar.apps.partner.strategy import Selector
from oscar.core import prices
from oscar.core.loading import get_class, get_model
from paypal.core import ApiError, UnsetType
from paypal.models import (
    AmountWithBreakdown,
    AuthorizationWithAdditionalData,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Customer,
    Money,
    OrderAuthorizeRequest,
    OrderAuthorizeRequestPaymentSource,
    OrderAuthorizeResponse,
    OrderRequest,
    PaymentAuthorization,
    PaymentTokenRequest,
    PaymentTokenRequestPaymentSource,
    PaymentTokenResponse,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    Refund,
    RefundRequest,
)
from paypal.models import (
    Order as PayPalOrder,
)
from paypal.models.enums import AuthorizationStatus, CheckoutPaymentIntent, OrderStatus

from . import money, outcomes
from .cards import CardInput
from .errors import ApiProblem, OutcomeUnknown, provider_issues, provider_message, translate
from .gateway import configured_currency, get_client
from .models import (
    InstallIdentity,
    Outcome,
    PayPalCustomer,
    PayPalOperation,
    PayPalPayment,
    PayPalRefund,
    SavedCard,
)
from .safewrite import Answer, safe_write

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
Free = get_class("shipping.methods", "Free")

T = TypeVar("T")
User = AbstractBaseUser | AnonymousUser

PENDING_PAYMENT_STATUS = "Pending payment"
AUTHORIZED_STATUS = "Pending"
FULFILLING_STATUS = "Being processed"
FULFILLED_STATUS = "Complete"
CANCELLED_STATUS = "Cancelled"

# From the reauthorize operation's documentation: a 3-day honor period, and a
# 29-day window in which a reauthorization is possible.
HONOR_PERIOD = timedelta(days=3)

MAX_LINES = 50
MAX_QUANTITY = 100


# ---------------------------------------------------------------- helpers


def install_prefix() -> str:
    identity = InstallIdentity.objects.order_by("pk").first()
    if identity is None:
        try:
            with transaction.atomic():
                identity = InstallIdentity.objects.create(pk=1, token=secrets.token_hex(4))
        except IntegrityError:
            identity = InstallIdentity.objects.get(pk=1)
    prefix = getattr(settings, "PAYPAL_REFERENCE_PREFIX", "") or "oscar"
    return f"{prefix}-{identity.token}"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def provider_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _money_fields(m: object) -> tuple[str | None, str | None]:
    if isinstance(m, (Money, AmountWithBreakdown)):
        return m.value, m.currency_code
    return None, None


def _set(value: object) -> Any:
    """``UNSET`` → ``None`` before a value crosses into our own records."""
    return None if isinstance(value, UnsetType) else value


def _call(fn: Callable[[], T]) -> T:
    """A provider read, with its failures translated at the boundary."""
    try:
        return fn()
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise translate(exc) from exc


def _order_for(user: User, number: str, *, staff_ok: bool) -> Any:
    qs = Order.objects.filter(number=number)
    if not (staff_ok and getattr(user, "is_staff", False)):
        qs = qs.filter(user_id=user.pk)
    order = qs.first()
    if order is None:
        raise ApiProblem(404, "not_found", "No such order.")
    return order


def _payment_for(order: Any, *, lock: bool = False) -> PayPalPayment:
    qs = PayPalPayment.objects.filter(order=order)
    if lock:
        qs = qs.select_for_update()
    payment = qs.first()
    if payment is None:
        raise ApiProblem(409, "not_payable", "This order was not placed through the payments API.")
    return payment


def _set_order_status(order: Any, *statuses: str) -> None:
    for status in statuses:
        if order.status != status and status in order.available_statuses():
            order.set_status(status)


def _source_for(payment: PayPalPayment) -> Any:
    if payment.source_id is not None:
        return Source.objects.get(pk=payment.source_id)
    source_type, _ = SourceType.objects.get_or_create(name="PayPal")
    source = Source.objects.create(
        order_id=payment.order_id, source_type=source_type, currency=payment.currency, label=payment.card_label
    )
    payment.source = source
    return source


def _step_failed(payment_id: int, reference: str, problem: ApiProblem, fallback_state: str | None) -> None:
    """After a step raised: put the payment in the state its operation now implies."""
    op = PayPalOperation.objects.filter(reference=reference).first()
    updates: dict[str, Any] = {"last_error": problem.message[:255]}
    if op is not None and op.outcome == Outcome.NEEDS_REVIEW:
        updates["state"] = PayPalPayment.NEEDS_REVIEW
    elif op is not None and op.outcome == Outcome.FAILED and fallback_state is not None:
        updates["state"] = fallback_state
    elif op is None and fallback_state is not None:
        updates["state"] = fallback_state
    PayPalPayment.objects.filter(pk=payment_id).update(**updates)


def _run_step(payment: PayPalPayment, reference: str, fallback_state: str | None, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except ApiProblem as problem:
        _step_failed(payment.pk, reference, problem, fallback_state)
        raise
    except Exception as exc:
        translated = translate(exc)  # re-raises anything that is not a provider failure
        _step_failed(payment.pk, reference, translated, fallback_state)
        raise translated from exc


def _in_progress() -> ApiProblem:
    return ApiProblem(409, "in_progress", "Another request for this payment is in progress; retry shortly.")


def _answer_from_stored(op: PayPalOperation) -> None:
    """A step answered from an earlier request's record, when it is not done."""
    match op.outcome:
        case Outcome.SENDING:
            raise _in_progress()
        case Outcome.UNKNOWN:
            raise OutcomeUnknown(op.reference)
        case Outcome.NEEDS_REVIEW:
            raise ApiProblem(
                502, "needs_review", "PayPal recorded this differently from what was asked; an operator must review it."
            )
        case Outcome.FAILED:
            raise ApiProblem(402, "provider_failed", op.error or "PayPal reported this operation as failed.")


# ---------------------------------------------------------------- orders


@dataclass(frozen=True)
class OrderItem:
    product_id: int
    quantity: int


def parse_items(data: object) -> list[OrderItem]:
    if not isinstance(data, list) or not data:
        raise ApiProblem(400, "invalid_request", "'items' must be a non-empty list.")
    if len(data) > MAX_LINES:
        raise ApiProblem(400, "invalid_request", f"At most {MAX_LINES} items per order.")
    merged: dict[int, int] = {}
    for item in data:
        if not isinstance(item, dict):
            raise ApiProblem(400, "invalid_request", "Each item must be an object.")
        pid, qty = item.get("productId"), item.get("quantity", 1)
        if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(qty, int) or isinstance(qty, bool):
            raise ApiProblem(400, "invalid_request", "'productId' and 'quantity' must be integers.")
        if qty < 1 or qty > MAX_QUANTITY:
            raise ApiProblem(400, "invalid_request", f"'quantity' must be between 1 and {MAX_QUANTITY}.")
        merged[pid] = merged.get(pid, 0) + qty
    return [OrderItem(pid, qty) for pid, qty in merged.items()]


def place_order(user: Any, items: list[OrderItem], request: HttpRequest) -> Any:
    """Create an Oscar order (status 'Pending payment') from catalogue items."""
    currency = configured_currency()
    strategy = Selector().strategy(request=request, user=user)
    products = {p.pk: p for p in Product.objects.filter(pk__in=[i.product_id for i in items])}
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for item in items:
            product = products.get(item.product_id)
            if product is None or not product.is_public:
                raise ApiProblem(404, "product_not_found", f"No such product: {item.product_id}.")
            if product.is_parent:
                raise ApiProblem(
                    422, "product_not_purchasable", f"Product {product.pk} is a parent; order one of its variants."
                )
            info = strategy.fetch_for_product(product)
            if not info.price.exists:
                raise ApiProblem(422, "product_not_purchasable", f"Product {product.pk} has no price.")
            permitted, reason = info.availability.is_purchase_permitted(item.quantity)
            if not permitted:
                raise ApiProblem(422, "product_not_purchasable", f"Product {product.pk}: {reason}")
            basket.add_product(product, item.quantity)
        excl_tax = basket.total_excl_tax
        incl_tax = basket.total_incl_tax if basket.is_tax_known else excl_tax
        amount = money.quantize(Decimal(incl_tax), currency)
        if amount != Decimal(incl_tax) or amount <= 0:
            raise ApiProblem(422, "unpayable_total", f"The order total cannot be charged in {currency}.")
        total = prices.Price(currency=currency, excl_tax=excl_tax, incl_tax=incl_tax)
        shipping = prices.Price(currency=currency, excl_tax=Decimal("0.00"), tax=Decimal("0.00"))
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=Free(),
            shipping_charge=shipping,
            user=user,
            order_number=str(OrderNumberGenerator().order_number(basket)),
            status=PENDING_PAYMENT_STATUS,
            request=request,
        )
        basket.submit()
        PayPalPayment.objects.create(order=order, amount=amount, currency=currency)
    return order


# ---------------------------------------------------------------- pay (authorize)


def _begin_pay_attempt(payment: PayPalPayment, saved_card: SavedCard | None, label: str) -> None:
    """Move to 'authorizing' for exactly one request; a repeat resumes the same attempt."""
    started = PayPalPayment.objects.filter(
        pk=payment.pk, state__in=[PayPalPayment.AWAITING_PAYMENT, PayPalPayment.DECLINED]
    ).update(
        state=PayPalPayment.AUTHORIZING,
        attempt=F("attempt") + 1,
        saved_card=saved_card,
        card_label=label,
        last_error="",
    )
    payment.refresh_from_db()
    if started or payment.state == PayPalPayment.AUTHORIZING:
        return
    if payment.state in (PayPalPayment.CANCELLED, PayPalPayment.VOIDED, PayPalPayment.VOIDING):
        raise ApiProblem(409, "order_cancelled", "This order has been cancelled.")
    raise AlreadyDone(payment)


class AlreadyDone(Exception):
    """The payment is already past the step asked for; answer its current state."""

    def __init__(self, payment: PayPalPayment) -> None:
        super().__init__(payment.state)
        self.payment = payment


def _read_order_shell(o: PayPalOrder) -> Answer:
    if isinstance(o.id, UnsetType) or isinstance(o.purchase_units, UnsetType) or not o.purchase_units:
        return Answer("", Outcome.UNKNOWN, outcomes.status_text(o.status), None)
    value, currency = _money_fields(o.purchase_units[0].amount)
    return Answer(
        o.id,
        outcomes.order_outcome(o.status),
        outcomes.status_text(o.status),
        provider_time(o.create_time),
        value,
        currency,
    )


def _latest_authorization(o: PayPalOrder | OrderAuthorizeResponse) -> AuthorizationWithAdditionalData | None:
    if isinstance(o.purchase_units, UnsetType) or not o.purchase_units:
        return None
    payments = o.purchase_units[0].payments
    if isinstance(payments, UnsetType) or isinstance(payments.authorizations, UnsetType) or not payments.authorizations:
        return None
    return max(payments.authorizations, key=lambda a: _set(a.create_time) or "")


def _read_authorization(o: PayPalOrder | OrderAuthorizeResponse) -> Answer:
    auth = _latest_authorization(o)
    if o.status == OrderStatus.PAYER_ACTION_REQUIRED:
        # 3-D Secure or similar: a shopper must approve in a browser. Not supported here.
        return Answer(_set(o.id) or "", Outcome.PENDING, "PAYER_ACTION_REQUIRED", provider_time(o.update_time))
    if auth is None or isinstance(auth.id, UnsetType):
        return Answer(_set(o.id) or "", Outcome.UNKNOWN, outcomes.status_text(o.status), None)
    value, currency = _money_fields(auth.amount)
    return Answer(
        auth.id,
        outcomes.authorization_outcome(auth.status),
        outcomes.status_text(auth.status),
        provider_time(auth.create_time),
        value,
        currency,
    )


def _find_authorization(paypal_order_id: str) -> PayPalOrder | None:
    o = get_client().orders.get_order(paypal_order_id)
    if o.status == OrderStatus.PAYER_ACTION_REQUIRED or _latest_authorization(o) is not None:
        return o
    return None


def _apply_authorization(payment_id: int) -> Callable[[PayPalOperation, Answer, Any], None]:
    def apply(op: PayPalOperation, answer: Answer, result: Any) -> None:
        payment = PayPalPayment.objects.select_for_update().get(pk=payment_id)
        auth = _latest_authorization(result) if result is not None else None
        if auth is not None:
            payment.authorization_id = _set(auth.id) or ""
            payment.authorization_status = outcomes.status_text(auth.status)
            payment.authorized_at = provider_time(auth.create_time)
            payment.authorization_expires_at = provider_time(auth.expiration_time)
        match answer.outcome:
            case Outcome.DONE:
                payment.state = PayPalPayment.AUTHORIZED
                source = _source_for(payment)
                source.label = payment.card_label
                source.allocate(payment.amount, reference=payment.authorization_id, status=answer.provider_status)
                _set_order_status(payment.order, AUTHORIZED_STATUS)
            case Outcome.PENDING:
                payment.state = (
                    PayPalPayment.PAYER_ACTION_REQUIRED
                    if answer.provider_status == "PAYER_ACTION_REQUIRED"
                    else PayPalPayment.AUTHORIZATION_PENDING
                )
            case Outcome.FAILED:
                payment.state = PayPalPayment.DECLINED
                payment.last_error = f"Authorization {answer.provider_status.lower()}"
            case Outcome.NEEDS_REVIEW:
                payment.state = PayPalPayment.NEEDS_REVIEW
        payment.save()

    return apply


def pay_order(user: Any, number: str, card: CardInput | None, saved_card_id: uuid.UUID | None) -> PayPalPayment:
    order = _order_for(user, number, staff_ok=False)
    payment = _payment_for(order)
    saved_card: SavedCard | None = None
    if saved_card_id is not None:
        saved_card = SavedCard.objects.filter(public_id=saved_card_id, user_id=user.pk, deleted_at__isnull=True).first()
        if saved_card is None:
            raise ApiProblem(404, "payment_method_not_found", "No such saved card.")
        label = saved_card.label
    elif card is not None:
        label = f"Card ending {card.last_digits}"
    else:
        raise ApiProblem(400, "invalid_request", "Provide either 'card' or 'paymentMethodId'.")

    try:
        _begin_pay_attempt(payment, saved_card, label)
    except AlreadyDone as done:
        return done.payment

    client = get_client()
    prefix = install_prefix()
    attempt = payment.attempt
    currency = payment.currency
    amount_str = money.to_wire(payment.amount, currency)
    sent = (Decimal(amount_str), currency)

    # Step 1: a PayPal order holding the amount, with no payment source (no money moves).
    create_ref = f"{prefix}:{order.number}:create:{attempt}"
    order_body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=str(order.number),
                custom_id=f"{prefix}:{order.number}",
                invoice_id=f"{prefix}-{order.number}-{attempt}",
                description=f"Order {order.number}",
                amount=AmountWithBreakdown(currency_code=currency, value=amount_str),
            )
        ],
    )

    def send_create(key: str) -> PayPalOrder:
        return client.orders.create_order(order_body, pay_pal_request_id=key, prefer="return=representation")

    def apply_create(op: PayPalOperation, answer: Answer, result: Any) -> None:
        if answer.outcome == Outcome.DONE:
            PayPalPayment.objects.filter(pk=payment.pk).update(paypal_order_id=answer.provider_id)
        elif answer.outcome == Outcome.PENDING:
            PayPalPayment.objects.filter(pk=payment.pk).update(state=PayPalPayment.PAYER_ACTION_REQUIRED)
        elif answer.outcome == Outcome.FAILED:
            PayPalPayment.objects.filter(pk=payment.pk).update(state=PayPalPayment.DECLINED)

    created = _run_step(
        payment,
        create_ref,
        PayPalPayment.DECLINED,
        lambda: safe_write(
            reference=create_ref,
            kind=PayPalOperation.CREATE_ORDER,
            order_id=order.pk,
            send=send_create,
            find=send_create,
            read=_read_order_shell,
            repeat_is_safe=True,
            sent=sent,
            apply=apply_create,
        ),
    )
    payment.refresh_from_db()
    if created.operation.outcome != Outcome.DONE:
        if created.operation.outcome == Outcome.PENDING:
            return payment  # payer action required: reported, not handled
        _answer_from_stored(created.operation)
    paypal_order_id = created.operation.provider_id

    # Step 2: authorize that PayPal order with the card.
    auth_ref = f"{prefix}:{order.number}:authorize:{attempt}"
    if saved_card is not None:
        card_request = CardRequest(vault_id=saved_card.vault_token_id)
    else:
        assert card is not None
        card_request = card.to_order_card()
    auth_body = OrderAuthorizeRequest(payment_source=OrderAuthorizeRequestPaymentSource(card=card_request))

    def send_authorize(key: str) -> OrderAuthorizeResponse:
        return client.orders.authorize_order(
            paypal_order_id, pay_pal_request_id=key, prefer="return=representation", body=auth_body
        )

    # PayPal refuses a repeated authorize, so the check is a lookup of the order.
    authorized = _run_step(
        payment,
        auth_ref,
        PayPalPayment.DECLINED,
        lambda: safe_write(
            reference=auth_ref,
            kind=PayPalOperation.AUTHORIZE,
            order_id=order.pk,
            send=send_authorize,
            find=lambda _ref: _find_authorization(paypal_order_id),
            read=_read_authorization,
            repeat_is_safe=False,
            sent=sent,
            apply=_apply_authorization(payment.pk),
        ),
    )
    payment.refresh_from_db()
    if authorized.operation.outcome == Outcome.FAILED:
        raise ApiProblem(
            402,
            "payment_declined",
            f"PayPal did not authorize the card ({authorized.operation.provider_status or 'declined'}).",
        )
    if authorized.operation.outcome not in (Outcome.DONE, Outcome.PENDING):
        _answer_from_stored(authorized.operation)
    return payment


# ---------------------------------------------------------------- fulfil (capture)


def _read_payment_authorization(a: PaymentAuthorization | AuthorizationWithAdditionalData) -> Answer:
    if isinstance(a.id, UnsetType):
        return Answer("", Outcome.UNKNOWN, outcomes.status_text(a.status), None)
    value, currency = _money_fields(a.amount)
    return Answer(
        a.id,
        outcomes.authorization_outcome(a.status),
        outcomes.status_text(a.status),
        provider_time(a.create_time),
        value,
        currency,
    )


def _find_reauthorization(paypal_order_id: str, old_id: str) -> AuthorizationWithAdditionalData | None:
    o = get_client().orders.get_order(paypal_order_id)
    if isinstance(o.purchase_units, UnsetType) or not o.purchase_units:
        return None
    payments = o.purchase_units[0].payments
    if isinstance(payments, UnsetType) or isinstance(payments.authorizations, UnsetType):
        return None
    newer = [a for a in payments.authorizations if _set(a.id) and a.id != old_id]
    return max(newer, key=lambda a: _set(a.create_time) or "") if newer else None


def _not_renewable(reason: str, extra: dict[str, Any] | None = None) -> ApiProblem:
    return ApiProblem(
        409,
        "authorization_not_renewable",
        f"The payment hold on this order can no longer be renewed ({reason}). "
        "No money has been taken. Cancel this order and ask the shopper to place and pay for it again.",
        extra=extra,
    )


def _reauthorize(payment: PayPalPayment, prefix: str) -> None:
    client = get_client()
    old_id = payment.authorization_id
    ref = f"{prefix}:{payment.order.number}:reauthorize:{old_id}"
    amount_str = money.to_wire(payment.amount, payment.currency)
    body = ReauthorizeRequest(amount=Money(currency_code=payment.currency, value=amount_str))

    def apply(op: PayPalOperation, answer: Answer, result: Any) -> None:
        if answer.outcome != Outcome.DONE:
            return
        p = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        p.authorization_id = answer.provider_id
        p.authorization_status = answer.provider_status
        p.authorized_at = answer.provider_time
        if result is not None:
            p.authorization_expires_at = provider_time(result.expiration_time) or p.authorization_expires_at
        p.reauthorized = True
        p.save()
        if p.source_id:
            Source.objects.get(pk=p.source_id).transactions.create(
                txn_type="Reauthorise", amount=p.amount, reference=answer.provider_id, status=answer.provider_status
            )

    def send(key: str) -> PaymentAuthorization | AuthorizationWithAdditionalData:
        return client.payments.reauthorize_payment(
            old_id, pay_pal_request_id=key, prefer="return=representation", body=body
        )

    def find(_ref: str) -> PaymentAuthorization | AuthorizationWithAdditionalData | None:
        # PayPal lists the renewed authorization on the order beside the old one.
        return _find_reauthorization(payment.paypal_order_id, old_id)

    try:
        result = safe_write(
            reference=ref,
            kind=PayPalOperation.REAUTHORIZE,
            order_id=payment.order_id,
            send=send,
            find=find,
            read=_read_payment_authorization,
            repeat_is_safe=False,
            sent=(Decimal(amount_str), payment.currency),
            apply=apply,
        )
    except ApiError as exc:
        if 400 <= exc.status_code < 500 and exc.status_code not in (401, 403, 429):
            issues = provider_issues(exc)
            raise _not_renewable(f"PayPal: {provider_message(exc)}", {"paypalIssues": issues}) from exc
        raise
    op = result.operation
    if op.outcome == Outcome.FAILED:
        raise _not_renewable(f"PayPal reported the renewed hold as {op.provider_status or 'failed'}")
    if op.outcome != Outcome.DONE:
        _answer_from_stored(op)
        raise ApiProblem(
            409, "reauthorization_pending", "PayPal has not finished renewing the hold; fulfil again shortly."
        )
    payment.refresh_from_db()


def _ensure_fresh_authorization(payment: PayPalPayment, prefix: str) -> None:
    """Renew a hold that has passed its honor period; refuse one that cannot be renewed."""
    auth = _call(lambda: get_client().payments.get_authorized_payment(payment.authorization_id))
    now = timezone.now()
    expires = provider_time(auth.expiration_time) or payment.authorization_expires_at
    status = auth.status
    if status in (AuthorizationStatus.VOIDED, AuthorizationStatus.DENIED) or (expires is not None and now >= expires):
        raise _not_renewable(
            f"PayPal shows the authorization as {outcomes.status_text(status) or 'unknown'}"
            + (f", expired {expires.isoformat()}" if expires is not None and now >= expires else "")
        )
    if status in (AuthorizationStatus.CAPTURED, AuthorizationStatus.PARTIALLY_CAPTURED):
        return  # an earlier capture landed; the capture step's resend returns it
    created = provider_time(auth.create_time) or payment.authorized_at
    if created is not None and now - created > HONOR_PERIOD:
        logger.info("Authorization for order %s is past its honor period; reauthorizing", payment.order.number)
        _reauthorize(payment, prefix)


def _read_capture(c: CapturedPayment) -> Answer:
    if isinstance(c.id, UnsetType):
        return Answer("", Outcome.UNKNOWN, outcomes.status_text(c.status), None)
    value, currency = _money_fields(c.amount)
    return Answer(
        c.id,
        outcomes.capture_outcome(c.status),
        outcomes.status_text(c.status),
        provider_time(c.create_time),
        value,
        currency,
    )


def _apply_capture(payment_id: int) -> Callable[[PayPalOperation, Answer, Any], None]:
    def apply(op: PayPalOperation, answer: Answer, result: Any) -> None:
        p = PayPalPayment.objects.select_for_update().get(pk=payment_id)
        p.capture_id = answer.provider_id
        p.capture_status = answer.provider_status
        match answer.outcome:
            case Outcome.DONE:
                breakdown = result.seller_receivable_breakdown if result is not None else None
                if breakdown is not None and not isinstance(breakdown, UnsetType):
                    p.captured_amount = money.parse(breakdown.gross_amount.value)
                    p.paypal_fee = money.parse(_money_fields(breakdown.paypal_fee)[0])
                    p.net_amount = money.parse(_money_fields(breakdown.net_amount)[0])
                else:
                    p.captured_amount = money.parse(answer.amount)
                p.captured_at = answer.provider_time
                p.state = PayPalPayment.CAPTURED
                source = _source_for(p)
                source.debit(p.captured_amount or p.amount, reference=answer.provider_id, status=answer.provider_status)
                _set_order_status(p.order, FULFILLING_STATUS, FULFILLED_STATUS)
            case Outcome.PENDING:
                p.state = PayPalPayment.CAPTURE_PENDING
            case Outcome.FAILED:
                p.state = PayPalPayment.AUTHORIZED
                p.last_error = (
                    f"Capture {answer.provider_status.lower() or 'failed'}; cancel the order to release the hold."
                )
            case Outcome.NEEDS_REVIEW:
                p.state = PayPalPayment.NEEDS_REVIEW
        p.save()

    return apply


def _expired_authorization(exc: ApiError[Any]) -> bool:
    return exc.status_code == 422 and any("EXPIRED" in issue for issue in provider_issues(exc))


def fulfil_order(number: str) -> PayPalPayment:
    order = Order.objects.filter(number=number).first()
    if order is None:
        raise ApiProblem(404, "not_found", "No such order.")
    payment = _payment_for(order)
    if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        return payment
    moved = PayPalPayment.objects.filter(
        pk=payment.pk, state__in=[PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURE_PENDING]
    ).update(state=PayPalPayment.CAPTURING, last_error="")
    payment.refresh_from_db()
    if not moved and payment.state != PayPalPayment.CAPTURING:
        raise ApiProblem(
            409,
            "not_fulfillable",
            f"Only an authorized order can be fulfilled (payment state: {payment.state}).",
            extra={"paymentState": payment.state},
        )
    prefix = install_prefix()
    currency = payment.currency
    amount_str = money.to_wire(payment.amount, currency)

    def capture_ref() -> str:
        return f"{prefix}:{order.number}:capture:{payment.authorization_id}"

    def run_capture() -> PayPalOperation:
        auth_id = payment.authorization_id
        body = CaptureRequest(amount=Money(currency_code=currency, value=amount_str), final_capture=True)

        def send(key: str) -> CapturedPayment:
            return get_client().payments.capture_authorized_payment(
                auth_id, pay_pal_request_id=key, prefer="return=representation", body=body
            )

        return safe_write(
            reference=capture_ref(),
            kind=PayPalOperation.CAPTURE,
            order_id=order.pk,
            send=send,
            find=send,
            read=_read_capture,
            repeat_is_safe=True,
            sent=(Decimal(amount_str), currency),
            apply=_apply_capture(payment.pk),
            recheck_pending=True,
        ).operation

    def attempt() -> PayPalOperation:
        if not PayPalOperation.objects.filter(reference=capture_ref()).exists():
            _ensure_fresh_authorization(payment, prefix)
        try:
            return run_capture()
        except ApiError as exc:
            if not _expired_authorization(exc) or payment.reauthorized:
                raise
            logger.info("Capture for order %s refused as expired; reauthorizing once", order.number)
            _reauthorize(payment, prefix)
            return run_capture()

    try:
        op = _run_step(payment, capture_ref(), None, attempt)
    except ApiProblem:
        PayPalPayment.objects.filter(pk=payment.pk, state=PayPalPayment.CAPTURING).update(
            state=PayPalPayment.AUTHORIZED
        )
        raise
    payment.refresh_from_db()
    if op.outcome == Outcome.FAILED:
        raise ApiProblem(402, "capture_declined", payment.last_error or "PayPal declined the capture.")
    if op.outcome not in (Outcome.DONE, Outcome.PENDING):
        _answer_from_stored(op)
    return payment


# ---------------------------------------------------------------- cancel (void)


def _apply_void(payment_id: int) -> Callable[[PayPalOperation, Answer, Any], None]:
    def apply(op: PayPalOperation, answer: Answer, result: Any) -> None:
        p = PayPalPayment.objects.select_for_update().get(pk=payment_id)
        p.authorization_status = answer.provider_status
        match answer.outcome:
            case Outcome.DONE:
                p.state = PayPalPayment.VOIDED
                if p.source_id:
                    source = Source.objects.get(pk=p.source_id)
                    source.amount_allocated = Decimal("0.00")
                    source.save()
                    source.transactions.create(
                        txn_type="Void", amount=p.amount, reference=p.authorization_id, status=answer.provider_status
                    )
                _set_order_status(p.order, CANCELLED_STATUS)
            case Outcome.FAILED:
                p.state = PayPalPayment.NEEDS_REVIEW
                p.last_error = f"Void refused: authorization is {answer.provider_status}"
        p.save()

    return apply


def cancel_order(number: str) -> PayPalPayment:
    order = Order.objects.filter(number=number).first()
    if order is None:
        raise ApiProblem(404, "not_found", "No such order.")
    payment = _payment_for(order)
    if payment.state in (PayPalPayment.CANCELLED, PayPalPayment.VOIDED):
        return payment

    # Nothing is held at PayPal yet: cancel locally.
    with transaction.atomic():
        unpaid = PayPalPayment.objects.filter(
            pk=payment.pk,
            state__in=[PayPalPayment.AWAITING_PAYMENT, PayPalPayment.DECLINED, PayPalPayment.PAYER_ACTION_REQUIRED],
        ).update(state=PayPalPayment.CANCELLED)
        if unpaid:
            _set_order_status(order, CANCELLED_STATUS)
    payment.refresh_from_db()
    if unpaid:
        return payment

    moved = PayPalPayment.objects.filter(
        pk=payment.pk, state__in=[PayPalPayment.AUTHORIZED, PayPalPayment.AUTHORIZATION_PENDING]
    ).update(state=PayPalPayment.VOIDING)
    payment.refresh_from_db()
    if not moved and payment.state != PayPalPayment.VOIDING:
        if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
            raise ApiProblem(409, "already_fulfilled", "This order has been fulfilled and paid; use a refund instead.")
        raise ApiProblem(
            409, "not_cancellable", f"This order cannot be cancelled now (payment state: {payment.state})."
        )

    auth_id = payment.authorization_id
    ref = f"{install_prefix()}:{order.number}:void:{auth_id}"
    client = get_client()

    def read(a: PaymentAuthorization) -> Answer:
        return Answer(
            _set(a.id) or auth_id,
            outcomes.void_outcome(a.status),
            outcomes.status_text(a.status),
            provider_time(a.update_time),
        )

    op = _run_step(
        payment,
        ref,
        None,
        lambda: (
            safe_write(
                reference=ref,
                kind=PayPalOperation.VOID,
                order_id=order.pk,
                send=lambda key: client.payments.void_payment(
                    auth_id, pay_pal_request_id=key, prefer="return=representation"
                ),
                find=lambda _ref: client.payments.get_authorized_payment(auth_id),
                read=read,
                repeat_is_safe=False,
                sent=None,
                apply=_apply_void(payment.pk),
                recheck_pending=True,
            ).operation
        ),
    )
    payment.refresh_from_db()
    if op.outcome == Outcome.FAILED:
        raise ApiProblem(
            409, "already_captured", "PayPal has already captured this authorization; use a refund instead."
        )
    if op.outcome not in (Outcome.DONE, Outcome.PENDING):
        _answer_from_stored(op)
    return payment


# ---------------------------------------------------------------- refunds


_LIVE_REFUND = ~Q(outcome=Outcome.FAILED)


def _reserve_refund(payment: PayPalPayment, key: str, amount: Decimal | None, user: Any, prefix: str) -> PayPalRefund:
    """Check the refundable balance and insert the refund in one transaction, so
    two refunds can never together exceed what was captured."""
    with transaction.atomic():
        locked = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        locked.save(update_fields=["date_updated"])  # take the write lock (SQLite) before reading the balance
        existing = PayPalRefund.objects.filter(payment=locked, idempotency_key=key).first()
        if existing is not None:
            if amount is not None and existing.amount != amount:
                raise ApiProblem(
                    409, "idempotency_key_reused", "This Idempotency-Key was already used for a different refund."
                )
            if existing.outcome != Outcome.FAILED:
                return existing
        if locked.state not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED):
            raise ApiProblem(
                409, "not_refundable", f"Only a fulfilled order can be refunded (payment state: {locked.state})."
            )
        captured = locked.captured_amount or locked.amount
        reserved = locked.refunds.filter(_LIVE_REFUND).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        remaining = captured - reserved
        if amount is None:
            amount = remaining
        if existing is not None:
            amount = existing.amount
        if amount <= 0 or amount > remaining:
            raise ApiProblem(
                422,
                "refund_exceeds_captured",
                f"At most {money.to_wire(remaining, locked.currency)} {locked.currency} can still be refunded.",
                extra={"refundable": money.to_wire(remaining, locked.currency)},
            )
        if existing is not None:
            # A refund PayPal never recorded: re-reserve it and resend under the same reference.
            existing.outcome = Outcome.SENDING
            existing.save(update_fields=["outcome", "date_updated"])
            return existing
        return PayPalRefund.objects.create(
            payment=locked,
            idempotency_key=key,
            amount=amount,
            requested_by=user if user.pk else None,
            reference=f"{prefix}:{locked.order.number}:refund:{_hash_key(key)}",
        )


def _apply_refund(payment_id: int, refund_id: int) -> Callable[[PayPalOperation, Answer, Any], None]:
    def apply(op: PayPalOperation, answer: Answer, result: Any) -> None:
        p = PayPalPayment.objects.select_for_update().get(pk=payment_id)
        refund = PayPalRefund.objects.select_for_update().get(pk=refund_id)
        refund.outcome = answer.outcome
        refund.paypal_refund_id = answer.provider_id
        refund.paypal_status = answer.provider_status
        refund.save()
        if answer.outcome == Outcome.DONE:
            p.refunded_amount = (p.refunded_amount or Decimal("0")) + refund.amount
            captured = p.captured_amount or p.amount
            p.state = PayPalPayment.REFUNDED if p.refunded_amount >= captured else PayPalPayment.PARTIALLY_REFUNDED
            p.save()
            _source_for(p).refund(refund.amount, reference=answer.provider_id, status=answer.provider_status)

    return apply


def refund_order(user: Any, number: str, key: str, amount: Decimal | None) -> PayPalRefund:
    order = _order_for(user, number, staff_ok=True)
    payment = _payment_for(order)
    if amount is not None:
        if money.quantize(amount, payment.currency) != amount:
            raise ApiProblem(400, "invalid_amount", f"'amount' has too many decimal places for {payment.currency}.")
    prefix = install_prefix()
    refund = _reserve_refund(payment, key, amount, user, prefix)
    if not payment.capture_id:
        raise ApiProblem(409, "not_refundable", "There is no captured payment to refund.")
    capture_id = payment.capture_id
    amount_str = money.to_wire(refund.amount, payment.currency)
    body = RefundRequest(amount=Money(currency_code=payment.currency, value=amount_str))

    def send(ref: str) -> Refund:
        return get_client().payments.refund_captured_payment(
            capture_id, pay_pal_request_id=ref, prefer="return=representation", body=body
        )

    def read(r: Refund) -> Answer:
        if isinstance(r.id, UnsetType):
            return Answer("", Outcome.UNKNOWN, outcomes.status_text(r.status), None)
        value, currency = _money_fields(r.amount)
        return Answer(
            r.id,
            outcomes.refund_outcome(r.status),
            outcomes.status_text(r.status),
            provider_time(r.create_time),
            value,
            currency,
        )

    try:
        op = _run_step(
            payment,
            refund.reference,
            None,
            lambda: (
                safe_write(
                    reference=refund.reference,
                    kind=PayPalOperation.REFUND,
                    order_id=order.pk,
                    send=send,
                    find=send,
                    read=read,
                    repeat_is_safe=True,
                    sent=(Decimal(amount_str), payment.currency),
                    apply=_apply_refund(payment.pk, refund.pk),
                    recheck_pending=True,
                ).operation
            ),
        )
    finally:
        _sync_refund(refund)
    refund.refresh_from_db()
    if op.outcome not in (Outcome.DONE, Outcome.PENDING):
        _answer_from_stored(op)
    return refund


def _sync_refund(refund: PayPalRefund) -> None:
    """Mirror the operation's outcome onto the refund (a failed one releases its reservation)."""
    op = PayPalOperation.objects.filter(reference=refund.reference).first()
    if op is not None and op.outcome != refund.outcome:
        PayPalRefund.objects.filter(pk=refund.pk).update(outcome=op.outcome)


# ---------------------------------------------------------------- saved cards


def save_card(user: Any, card: CardInput, key: str) -> SavedCard:
    ref = f"{install_prefix()}:vault:{user.pk}:{_hash_key(key)}"
    customer = PayPalCustomer.objects.filter(user=user).first()
    body = PaymentTokenRequest(payment_source=PaymentTokenRequestPaymentSource(card=card.to_vault_card()))
    if customer is not None:
        # Group the shopper's cards under the vault customer PayPal created for the first one.
        body = body.model_copy(update={"customer": Customer(id=customer.customer_id)})

    def send(k: str) -> PaymentTokenResponse:
        return get_client().vault.create_payment_token(body, pay_pal_request_id=k)

    def read(t: PaymentTokenResponse) -> Answer:
        return Answer(_set(t.id) or "", outcomes.vault_outcome(t), "", None)

    def apply(op: PayPalOperation, answer: Answer, result: Any) -> None:
        if answer.outcome != Outcome.DONE or result is None:
            return
        vaulted = result.payment_source.card
        customer_id = _set(result.customer) and _set(result.customer.id)
        if customer_id:
            PayPalCustomer.objects.update_or_create(user=user, defaults={"customer_id": customer_id})
        SavedCard.objects.create(
            user=user,
            vault_token_id=answer.provider_id,
            reference=op.reference,
            brand=outcomes.status_text(vaulted.brand),
            last_digits=_set(vaulted.last_digits) or card.last_digits,
            expiry=_set(vaulted.expiry) or card.expiry,
            name=_set(vaulted.name) or card.name,
        )

    try:
        op = safe_write(
            reference=ref,
            kind=PayPalOperation.VAULT,
            order_id=None,
            send=send,
            find=send,
            read=read,
            repeat_is_safe=True,
            sent=None,
            apply=apply,
        ).operation
    except ApiProblem:
        raise
    except Exception as exc:
        raise translate(exc) from exc
    if op.outcome != Outcome.DONE:
        _answer_from_stored(op)
        raise OutcomeUnknown(ref)
    saved = SavedCard.objects.filter(reference=ref, user=user).first()
    if saved is None or saved.deleted_at is not None:
        raise ApiProblem(
            410, "payment_method_deleted", "The card saved under this Idempotency-Key has since been removed."
        )
    return saved


def delete_card(user: Any, public_id: uuid.UUID) -> None:
    with transaction.atomic():
        card = (
            SavedCard.objects.select_for_update()
            .filter(public_id=public_id, user_id=user.pk, deleted_at__isnull=True)
            .first()
        )
        if card is None:
            raise ApiProblem(404, "payment_method_not_found", "No such saved card.")
        # Hidden and unusable from here on, whatever PayPal answers below.
        card.deleted_at = timezone.now()
        card.save(update_fields=["deleted_at"])
    try:
        get_client().vault.delete_payment_token(card.vault_token_id)
    except Exception as exc:
        problem = translate(exc)
        logger.warning("PayPal vault delete for saved card %s failed (%s); will stay flagged", card.pk, problem.code)
        return
    SavedCard.objects.filter(pk=card.pk).update(provider_deleted=True)
