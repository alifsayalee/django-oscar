"""
Money movement for an order: authorize at checkout, capture at fulfilment,
void on cancel, refund after fulfilment.

Every PayPal write is one claimed step (``claims.safe_write``) whose reference
derives from the install prefix, the order number, the step and an attempt
number. A new attempt is only ever claimed after the previous attempt of that
step is recorded ``failed``, and the choice is made while holding the order's
payment-row lock, so a double click cannot authorize or capture twice.
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from django.db.models import Sum
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from oscar.core.loading import get_model
from paypal.core import ApiError, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Money,
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
from paypal.models.enums import CheckoutPaymentIntent, OrderStatus

from . import money, statuses
from .claims import Answer, AmountMismatch, OutcomeUnknown, complete, retake_released, safe_write, try_claim
from .errors import ApiProblem, bad_request, conflict, not_found
from .gateway import get_client, provider_issue, read_with_retry
from .models import PaymentOperation, PayPalPayment, SavedCard
from .orders import get_owned_payment, get_payment, locked_payment, reference_prefix

log = logging.getLogger(__name__)

SourceType = get_model("payment", "SourceType")
Source = get_model("payment", "Source")
Transaction = get_model("payment", "Transaction")

PREFER = "return=representation"  # the SDK default (minimal) omits amounts and breakdowns

# From the reauthorize_payment contract: a three-day honor period, after which
# an authorization should be reauthorized before capture; reauthorization is
# possible up to day 29; after that a new authorization is needed.
HONOR_PERIOD = timedelta(days=3)

# The shapes an authorization can be read from.
AuthorizationResult = PayPalOrder | OrderAuthorizeResponse | PaymentAuthorization

PAID_STATES = (
    PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING, PayPalPayment.CAPTURE_PENDING,
    PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED,
)
CAPTURED_STATES = (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED)

# PayPal error issues that mean "an earlier attempt already did this" (seen in
# the sandbox): a landing to look up, never a rejection.
ALREADY_AUTHORIZED = ("DUPLICATE_INVOICE_ID",)
ALREADY_CAPTURED = ("AUTHORIZATION_ALREADY_CAPTURED",)
ALREADY_VOIDED = ("PREVIOUSLY_VOIDED",)


@dataclass(frozen=True)
class CardInput:
    number: str
    expiry: str  # YYYY-MM
    security_code: str = ""
    name: str = ""
    billing_address: dict[str, str] = field(default_factory=dict)


@sensitive_variables()
def card_request(card: CardInput) -> CardRequest:
    kwargs: dict[str, Any] = {"number": card.number, "expiry": card.expiry}
    if card.security_code:
        kwargs["security_code"] = card.security_code
    if card.name:
        kwargs["name"] = card.name
    if card.billing_address:
        kwargs["billing_address"] = Address(**card.billing_address)
    return CardRequest(**kwargs)


def _latest(payment: PayPalPayment, kind: str) -> PaymentOperation | None:
    return payment.operations.filter(kind=kind).order_by("-attempt", "-pk").first()


def _ref(payment: PayPalPayment, step: str, attempt: int | str) -> str:
    return "%s-%s-%s-%s" % (reference_prefix(), payment.order.number, step, attempt)


def _money(amount: Decimal, currency: str) -> Money:
    return Money(currency_code=currency, value=money.to_wire(amount, currency))


# ---------------------------------------------------------------------------
# Reading PayPal responses
# ---------------------------------------------------------------------------


def _first_authorization(result: PayPalOrder | OrderAuthorizeResponse) -> Any:
    units = result.purchase_units
    if isinstance(units, UnsetType) or not units:
        return None
    payments = units[0].payments
    if isinstance(payments, UnsetType) or isinstance(payments.authorizations, UnsetType):
        return None
    return payments.authorizations[0] if payments.authorizations else None


def _auth_answer(auth: Any) -> Answer:
    provider_id = statuses.text(auth.id)
    amount, currency = statuses.money(auth.amount)
    return Answer(
        provider_id=provider_id,
        status=statuses.status_text(auth.status),
        outcome=statuses.authorization_outcome(auth.status) if provider_id else statuses.UNKNOWN,
        provider_time=statuses.parse_time(auth.create_time),
        amount=amount,
        currency=currency,
    )


def read_authorization(result: AuthorizationResult) -> Answer:
    """Outcome of the pay / authorize steps, from whichever shape PayPal returned."""
    if isinstance(result, PaymentAuthorization):
        return _auth_answer(result)
    auth = _first_authorization(result)
    if auth is not None:
        return _auth_answer(auth)
    outcome = statuses.order_outcome(result.status)
    if outcome == statuses.DONE:
        outcome = statuses.UNKNOWN  # COMPLETED yet no authorization to read
    amount = currency = None
    if not isinstance(result.purchase_units, UnsetType) and result.purchase_units:
        amount, currency = statuses.money(result.purchase_units[0].amount)
    detail = ""
    if result.status == OrderStatus.PAYER_ACTION_REQUIRED:
        detail = "payer_action_required"
    return Answer(
        provider_id=statuses.text(result.id),
        status=statuses.status_text(result.status),
        outcome=outcome if statuses.text(result.id) else statuses.UNKNOWN,
        provider_time=statuses.parse_time(result.update_time) or statuses.parse_time(result.create_time),
        amount=amount,
        currency=currency,
        detail=detail,
    )


def read_reauthorization(result: PaymentAuthorization) -> Answer:
    return _auth_answer(result)


def read_void(result: PaymentAuthorization) -> Answer:
    provider_id = statuses.text(result.id)
    return Answer(
        provider_id=provider_id,
        status=statuses.status_text(result.status),
        outcome=statuses.void_outcome(result.status) if provider_id else statuses.UNKNOWN,
        provider_time=statuses.parse_time(result.update_time),
    )


def read_capture(result: CapturedPayment) -> Answer:
    provider_id = statuses.text(result.id)
    amount, currency = statuses.money(result.amount)
    return Answer(
        provider_id=provider_id,
        status=statuses.status_text(result.status),
        outcome=statuses.capture_outcome(result.status) if provider_id else statuses.UNKNOWN,
        provider_time=statuses.parse_time(result.create_time),
        amount=amount,
        currency=currency,
    )


def read_refund(result: Refund) -> Answer:
    provider_id = statuses.text(result.id)
    amount, currency = statuses.money(result.amount)
    return Answer(
        provider_id=provider_id,
        status=statuses.status_text(result.status),
        outcome=statuses.refund_outcome(result.status) if provider_id else statuses.UNKNOWN,
        provider_time=statuses.parse_time(result.create_time),
        amount=amount,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# Oscar bookkeeping (order status, stock, payment source & transactions)
# ---------------------------------------------------------------------------


def _source(payment: PayPalPayment) -> Any:
    source_type, _ = SourceType.objects.get_or_create(name="PayPal")
    label = ""
    if payment.card_last_digits:
        label = "%s ending %s" % (payment.card_brand or "Card", payment.card_last_digits)
    source, created = Source.objects.get_or_create(
        order=payment.order, source_type=source_type,
        defaults={"currency": payment.currency, "label": label, "reference": payment.paypal_order_id},
    )
    if not created and label and source.label != label:
        source.label = label
        source.save(update_fields=["label"])
    return source


def _record_txn(payment: PayPalPayment, action: str, amount: Decimal, reference: str, status: str) -> None:
    source = _source(payment)
    txn_type = {"allocate": Transaction.AUTHORISE, "debit": Transaction.DEBIT, "refund": Transaction.REFUND}.get(
        action, action
    )
    if source.transactions.filter(txn_type=txn_type, reference=reference).exists():
        return
    if action == "allocate":
        source.allocate(amount, reference=reference, status=status)
    elif action == "debit":
        source.debit(amount, reference=reference, status=status)
    elif action == "refund":
        source.refund(amount, reference=reference, status=status)
    else:
        source.transactions.create(txn_type=txn_type, amount=amount, reference=reference, status=status)


def _advance_order_status(order: Any, *targets: str) -> None:
    for target in targets:
        if order.status != target and target in order.available_statuses():
            order.set_status(target)


def _adjust_stock(order: Any, consume: bool) -> None:
    for line in order.lines.all():
        try:
            if consume:
                line.consume_allocation(line.quantity)
            else:
                line.cancel_allocation(line.quantity)
        except Exception:  # Oscar's InvalidStockAdjustment: already consumed/cancelled
            log.info("Stock for order %s line %s already adjusted", order.number, line.pk)


# ---------------------------------------------------------------------------
# Recording results on the payment (always under the payment lock)
# ---------------------------------------------------------------------------


def _apply_authorization(payment_pk: int, op: PaymentOperation, result: Any) -> PayPalPayment:
    with locked_payment(payment_pk) as payment:
        if isinstance(result, (PayPalOrder, OrderAuthorizeResponse)):
            if statuses.text(result.id):
                payment.paypal_order_id = statuses.text(result.id)
            source = result.payment_source
            if not isinstance(source, UnsetType) and not isinstance(source.card, UnsetType):
                payment.card_brand = statuses.status_text(source.card.brand)[:32]
                payment.card_last_digits = statuses.text(source.card.last_digits)[:4]
            auth = _first_authorization(result)
        elif isinstance(result, PaymentAuthorization):
            auth = result
            supplementary = result.supplementary_data
            if not isinstance(supplementary, UnsetType) and not isinstance(supplementary.related_ids, UnsetType):
                payment.paypal_order_id = statuses.text(supplementary.related_ids.order_id) or payment.paypal_order_id
        else:
            auth = None
        if auth is not None and statuses.text(auth.id):
            payment.authorization_id = statuses.text(auth.id)
            payment.authorization_status = statuses.status_text(auth.status)[:32]
            payment.authorization_created_at = statuses.parse_time(auth.create_time) or timezone.now()
            payment.authorization_expires_at = statuses.parse_time(auth.expiration_time)
        _set_state_from_authorization(payment, op)
        payment.save()
        return payment


def _set_state_from_authorization(payment: PayPalPayment, op: PaymentOperation) -> None:
    if payment.state not in (
        PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZING, PayPalPayment.PAYMENT_FAILED,
        PayPalPayment.AUTHORIZATION_EXPIRED,
    ):
        return  # already moved on (captured, cancelled...): an old answer changes nothing
    if op.outcome == PaymentOperation.DONE:
        payment.state = PayPalPayment.AUTHORIZED
        payment.state_detail = ""
        _record_txn(payment, "allocate", payment.amount, payment.authorization_id, payment.authorization_status)
    elif op.outcome == PaymentOperation.FAILED:
        payment.state = PayPalPayment.PAYMENT_FAILED
        payment.state_detail = op.detail or "PayPal declined the authorization (%s)." % op.provider_status
    elif op.outcome == PaymentOperation.NEEDS_REVIEW:
        payment.state = PayPalPayment.NEEDS_REVIEW
        payment.state_detail = op.detail
    else:
        payment.state = PayPalPayment.AUTHORIZING
        payment.state_detail = {
            PaymentOperation.PENDING: "PayPal has not finished the authorization yet.",
            PaymentOperation.UNKNOWN: "Waiting for PayPal to confirm the authorization.",
        }.get(op.outcome, "")


def _mark(payment_pk: int, state: str, detail: str = "") -> PayPalPayment:
    with locked_payment(payment_pk) as payment:
        payment.state = state
        payment.state_detail = detail[:255]
        payment.save(update_fields=["state", "state_detail", "updated_at"])
        return payment


def _after_failed_write(payment_pk: int, op: PaymentOperation, exc: BaseException, *, on_refused: str) -> None:
    """Keep the payment state in step with a write that raised."""
    op.refresh_from_db()
    if isinstance(exc, OutcomeUnknown):
        return  # the state already says the step is in progress
    if isinstance(exc, AmountMismatch):
        _mark(payment_pk, PayPalPayment.NEEDS_REVIEW, op.detail)
    elif op.outcome == PaymentOperation.FAILED:
        detail = op.detail
        if isinstance(exc, ApiError):
            detail = "PayPal refused the request (%s)." % (provider_issue(exc) or exc.status_code)
        _mark(payment_pk, on_refused, detail)


# ---------------------------------------------------------------------------
# Pay: authorize the order total (hold, do not take)
# ---------------------------------------------------------------------------


def _find_authorization_by_invoice(reference: str, since: datetime) -> PaymentAuthorization | None:
    """The unknown-outcome lookup for the pay step: PayPal refuses a resend of
    a card create_order, so look the authorization up by the invoice id it
    carried. Reporting lags (up to three hours), so an empty result leaves
    the outcome unknown."""
    from .reconciliation import iterate_transactions

    client = get_client()
    start = since - timedelta(hours=1)
    end = min(timezone.now(), start + timedelta(days=31))
    for info in iterate_transactions(start, end):
        if statuses.text(info.invoice_id) != reference or not statuses.text(info.transaction_id):
            continue
        transaction_id = statuses.text(info.transaction_id)
        try:
            return read_with_retry(lambda: client.payments.get_authorized_payment(transaction_id))
        except ApiError as exc:
            if exc.status_code == 404:
                continue  # a capture or other row carrying the same invoice id
            raise
    return None


@sensitive_variables()
def pay(user: Any, order_number: str, *, card: CardInput | None, saved_card_id: str | None) -> tuple[PayPalPayment, PaymentOperation | None]:
    payment = get_owned_payment(user, order_number)
    saved: SavedCard | None = None
    if saved_card_id is not None:
        saved = SavedCard.objects.filter(public_id=saved_card_id, user=user, deleted_at__isnull=True).first()
        if saved is None:
            raise not_found("Payment method")

    with locked_payment(payment.pk) as payment:
        latest = _latest(payment, PaymentOperation.PAY)
        if payment.state in PAID_STATES:
            return payment, latest  # already authorized: a repeat changes nothing
        if payment.state in (PayPalPayment.VOIDING, PayPalPayment.CANCELLED):
            raise conflict("order_cancelled", "This order has been cancelled.")
        if payment.state == PayPalPayment.NEEDS_REVIEW:
            raise conflict("needs_review", "This order's payment is being reviewed by an operator.")
        retry_allowed = payment.state == PayPalPayment.AUTHORIZATION_EXPIRED
        if latest is not None and latest.outcome != PaymentOperation.FAILED and not retry_allowed:
            op, won = latest, False
        else:
            attempt = latest.attempt + 1 if latest is not None else 1
            op, won = try_claim(
                reference=_ref(payment, "pay", attempt), kind=PaymentOperation.PAY, attempt=attempt,
                user=user, payment=payment, saved_card=saved, amount=payment.amount, currency=payment.currency,
            )
            if won:
                payment.state = PayPalPayment.AUTHORIZING
                payment.state_detail = ""
                payment.saved_card = saved
                payment.save(update_fields=["state", "state_detail", "saved_card", "updated_at"])

    if won:
        if saved is not None:
            source = PaymentSource(card=CardRequest(vault_id=saved.paypal_token_id))
        elif card is not None:
            source = PaymentSource(card=card_request(card))
        else:
            complete(op, PaymentOperation.FAILED, detail="no payment source")
            _mark(payment.pk, PayPalPayment.PAYMENT_FAILED)
            raise bad_request("Provide either 'card' or 'paymentMethodId'.")
        body = OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[
                PurchaseUnitRequest(
                    amount=AmountWithBreakdown(
                        currency_code=payment.currency, value=money.to_wire(payment.amount, payment.currency)
                    ),
                    invoice_id=op.reference,
                    custom_id="%s-%s" % (reference_prefix(), payment.order.number),
                    description="Order %s" % payment.order.number,
                )
            ],
            payment_source=source,
        )
    client = get_client()
    paypal_order_id = payment.paypal_order_id

    def send(key: str) -> AuthorizationResult:
        return client.orders.create_order(body, pay_pal_request_id=key, prefer=PREFER)

    def find(ref: str) -> AuthorizationResult | None:
        return _find_authorization_by_invoice(ref, op.claimed_at)

    def refresh(_: PaymentOperation) -> AuthorizationResult | None:
        if not paypal_order_id:
            return None
        return read_with_retry(lambda: client.orders.get_order(paypal_order_id))

    try:
        op, result = safe_write(
            op, won,
            send=send,
            find=find,
            read=read_authorization,
            repeat_is_safe=False,  # a resent card create_order is refused, not de-duplicated
            sent=(payment.amount, payment.currency),
            landed_issues=ALREADY_AUTHORIZED,
            refresh=refresh,
        )
    except BaseException as exc:
        _after_failed_write(payment.pk, op, exc, on_refused=PayPalPayment.PAYMENT_FAILED)
        raise
    if result is not None:
        payment = _apply_authorization(payment.pk, op, result)
        if op.outcome == PaymentOperation.PENDING and op.provider_status == OrderStatus.APPROVED:
            return _authorize_approved(payment, op)
    payment.refresh_from_db()
    return payment, op


def _authorize_approved(payment: PayPalPayment, pay_op: PaymentOperation) -> tuple[PayPalPayment, PaymentOperation]:
    """PayPal approved the order without authorizing it: authorize it now, as
    its own claimed step."""
    with locked_payment(payment.pk) as payment:
        op, won = try_claim(
            reference=_ref(payment, "az", pay_op.attempt), kind=PaymentOperation.AUTHORIZE,
            attempt=pay_op.attempt, payment=payment, amount=payment.amount, currency=payment.currency,
        )
    client = get_client()
    paypal_order_id = payment.paypal_order_id

    def send(key: str) -> AuthorizationResult:
        return client.orders.authorize_order(paypal_order_id, pay_pal_request_id=key, prefer=PREFER)

    def find(_: str) -> AuthorizationResult | None:
        found = read_with_retry(lambda: client.orders.get_order(paypal_order_id))
        return found if _first_authorization(found) is not None else None

    try:
        op, result = safe_write(
            op, won,
            send=send,
            find=find,
            read=read_authorization,
            repeat_is_safe=False,
            sent=(payment.amount, payment.currency),
        )
    except BaseException as exc:
        _after_failed_write(payment.pk, op, exc, on_refused=PayPalPayment.PAYMENT_FAILED)
        raise
    if result is not None:
        payment = _apply_authorization(payment.pk, op, result)
        answer = read_authorization(result)
        complete(pay_op, op.outcome, answer)
    return payment, op


# ---------------------------------------------------------------------------
# Fulfil: take the money
# ---------------------------------------------------------------------------


def _expired_message(payment: PayPalPayment) -> str:
    when = payment.authorization_expires_at.isoformat() if payment.authorization_expires_at else "unknown"
    return (
        "The card authorization expired (%s) and PayPal can no longer renew it, so nothing was captured "
        "and no money was taken. Ask the shopper to pay again (POST /api/orders/%s/pay) and fulfil after "
        "that, or cancel the order." % (when, payment.order.number)
    )


def _authorization_expired(payment_pk: int) -> ApiProblem:
    payment = _mark(payment_pk, PayPalPayment.AUTHORIZATION_EXPIRED)
    payment.state_detail = _expired_message(payment)[:255]
    payment.save(update_fields=["state_detail"])
    return ApiProblem(409, "authorization_expired", _expired_message(payment), extra={"orderId": payment.order.number})


def _reauthorize(payment: PayPalPayment) -> PayPalPayment:
    """Renew an authorization past its honor period, as its own claimed step."""
    with locked_payment(payment.pk) as payment:
        latest = _latest(payment, PaymentOperation.REAUTHORIZE)
        if latest is not None and latest.outcome == PaymentOperation.DONE and latest.provider_id == payment.authorization_id:
            return payment  # this authorization is already the renewed one
        if latest is not None and latest.outcome not in (PaymentOperation.FAILED, PaymentOperation.DONE):
            op, won = latest, False
        else:
            attempt = latest.attempt + 1 if latest is not None else 1
            op, won = try_claim(
                reference=_ref(payment, "reauth", attempt), kind=PaymentOperation.REAUTHORIZE, attempt=attempt,
                payment=payment, amount=payment.amount, currency=payment.currency,
            )
    client = get_client()
    authorization_id = payment.authorization_id
    body = ReauthorizeRequest(amount=_money(payment.amount, payment.currency))

    def send(key: str) -> PaymentAuthorization:
        return client.payments.reauthorize_payment(authorization_id, pay_pal_request_id=key, prefer=PREFER, body=body)

    try:
        op, result = safe_write(
            op, won, send=send, find=send, read=read_reauthorization,
            repeat_is_safe=True,  # PayPal keeps reauthorize request ids for 45 days
            sent=(payment.amount, payment.currency),
        )
    except ApiError as exc:
        if provider_issue(exc) == "REAUTHORIZATION_TOO_SOON":
            return payment  # still inside the honor period: the current one is good
        log.warning("Reauthorization of order %s refused (%s); trying the capture anyway",
                    payment.order.number, provider_issue(exc))
        return payment
    if op.outcome == PaymentOperation.DONE and result is not None:
        with locked_payment(payment.pk) as payment:
            payment.reauthorized_from = authorization_id
            payment.authorization_id = statuses.text(result.id)
            payment.authorization_status = statuses.status_text(result.status)[:32]
            payment.authorization_created_at = statuses.parse_time(result.create_time) or timezone.now()
            payment.authorization_expires_at = (
                statuses.parse_time(result.expiration_time) or payment.authorization_expires_at
            )
            payment.save()
            _record_txn(payment, "allocate", payment.amount, payment.authorization_id, "reauthorized")
    return payment


def _apply_capture(payment_pk: int, op: PaymentOperation, result: CapturedPayment) -> PayPalPayment:
    with locked_payment(payment_pk) as payment:
        payment.capture_id = statuses.text(result.id) or payment.capture_id
        payment.capture_status = statuses.status_text(result.status)[:32]
        amount, _ = statuses.money(result.amount)
        if amount is not None:
            payment.captured_amount = amount
        breakdown = result.seller_receivable_breakdown
        if not isinstance(breakdown, UnsetType):
            fee, _ = statuses.money(breakdown.paypal_fee)
            net, _ = statuses.money(breakdown.net_amount)
            payment.paypal_fee = fee if fee is not None else payment.paypal_fee
            payment.net_amount = net if net is not None else payment.net_amount
        payment.captured_at = statuses.parse_time(result.create_time) or payment.captured_at
        if op.outcome == PaymentOperation.DONE:
            if payment.state in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING, PayPalPayment.CAPTURE_PENDING):
                payment.state = PayPalPayment.CAPTURED
                payment.state_detail = ""
                _record_txn(payment, "debit", payment.captured_amount or payment.amount, payment.capture_id,
                            payment.capture_status)
                _advance_order_status(payment.order, "Being processed", "Complete")
                _adjust_stock(payment.order, consume=True)
        elif op.outcome == PaymentOperation.PENDING:
            payment.state = PayPalPayment.CAPTURE_PENDING
            payment.state_detail = "PayPal accepted the capture but has not completed it yet."
        elif op.outcome == PaymentOperation.FAILED:
            payment.state = PayPalPayment.AUTHORIZED
            payment.state_detail = "PayPal did not complete the capture (%s)." % payment.capture_status
        payment.save()
        return payment


def _find_capture(client: Any, authorization_id: str, paypal_order_id: str, send: Any, reference: str) -> CapturedPayment | None:
    """Check a capture whose outcome is unknown: resend under the same request
    id (PayPal returns the original capture); if PayPal answers that the
    authorization is already captured, read that capture off the order."""
    try:
        result: CapturedPayment = send(reference)
        return result
    except ApiError as exc:
        if provider_issue(exc) not in ALREADY_CAPTURED or not paypal_order_id:
            raise
    order = read_with_retry(lambda: client.orders.get_order(paypal_order_id))
    units = order.purchase_units
    if isinstance(units, UnsetType) or not units or isinstance(units[0].payments, UnsetType):
        return None
    captures = units[0].payments.captures
    if isinstance(captures, UnsetType) or not captures:
        return None
    capture_id = statuses.text(captures[0].id)
    return read_with_retry(lambda: client.payments.get_captured_payment(capture_id)) if capture_id else None


def fulfil(order_number: str) -> tuple[PayPalPayment, PaymentOperation | None]:
    payment = get_payment(order_number)
    expired = False
    needs_reauth = False
    existing: PaymentOperation | None = None  # a capture step already under way
    with locked_payment(payment.pk) as payment:
        latest = _latest(payment, PaymentOperation.CAPTURE)
        if payment.state in CAPTURED_STATES:
            return payment, latest  # already fulfilled
        if payment.state == PayPalPayment.CAPTURE_PENDING and latest is not None:
            existing = latest
        elif payment.state == PayPalPayment.CAPTURING and latest is not None and latest.outcome != PaymentOperation.FAILED:
            existing = latest
        elif payment.state not in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING):
            if payment.state == PayPalPayment.AUTHORIZATION_EXPIRED:
                raise conflict("authorization_expired", _expired_message(payment))
            raise conflict(
                "not_authorized",
                "Only an authorized order can be fulfilled; this order's payment is '%s'." % payment.state,
            )
        else:
            now = timezone.now()
            if payment.authorization_expires_at is not None and now >= payment.authorization_expires_at:
                expired = True
            elif (
                payment.authorization_created_at is not None
                and now - payment.authorization_created_at > HONOR_PERIOD
            ):
                needs_reauth = True
    if expired:
        raise _authorization_expired(payment.pk)
    if needs_reauth:
        payment = _reauthorize(payment)

    if existing is not None:
        op, won = existing, False
    else:
        with locked_payment(payment.pk) as payment:
            latest = _latest(payment, PaymentOperation.CAPTURE)
            if latest is not None and latest.outcome != PaymentOperation.FAILED:
                op, won = latest, False
            else:
                attempt = latest.attempt + 1 if latest is not None else 1
                op, won = try_claim(
                    reference=_ref(payment, "cap", attempt), kind=PaymentOperation.CAPTURE, attempt=attempt,
                    payment=payment, amount=payment.amount, currency=payment.currency,
                )
                if won:
                    payment.state = PayPalPayment.CAPTURING
                    payment.state_detail = ""
                    payment.save(update_fields=["state", "state_detail", "updated_at"])

    client = get_client()
    authorization_id, paypal_order_id = payment.authorization_id, payment.paypal_order_id
    body = CaptureRequest(amount=_money(payment.amount, payment.currency), final_capture=True)

    def send(key: str) -> CapturedPayment:
        return client.payments.capture_authorized_payment(
            authorization_id, pay_pal_request_id=key, prefer=PREFER, body=body
        )

    try:
        op, result = safe_write(
            op, won,
            send=send,
            find=lambda ref: _find_capture(client, authorization_id, paypal_order_id, send, ref),
            read=read_capture,
            repeat_is_safe=True,  # PayPal returns the original capture for a repeated request id
            sent=(payment.amount, payment.currency),
            landed_issues=ALREADY_CAPTURED,
            refresh=lambda o: read_with_retry(lambda: client.payments.get_captured_payment(o.provider_id)),
        )
    except ApiError as exc:
        op.refresh_from_db()
        if op.outcome == PaymentOperation.FAILED:
            problem = _explain_capture_refusal(payment, exc)
            if problem is not None:
                raise problem from exc
        _after_failed_write(payment.pk, op, exc, on_refused=PayPalPayment.AUTHORIZED)
        raise
    except BaseException as exc:
        _after_failed_write(payment.pk, op, exc, on_refused=PayPalPayment.AUTHORIZED)
        raise
    if result is not None:
        payment = _apply_capture(payment.pk, op, result)
    payment.refresh_from_db()
    return payment, op


def _explain_capture_refusal(payment: PayPalPayment, exc: ApiError) -> ApiProblem | None:
    """A refused capture: re-read the authorization and, when it can no longer
    be captured, say so in terms an operator can act on."""
    client = get_client()
    try:
        auth = read_with_retry(lambda: client.payments.get_authorized_payment(payment.authorization_id))
    except (ApiError, httpx.RequestError, ValueError):
        return None
    expires = statuses.parse_time(auth.expiration_time)
    status = statuses.status_text(auth.status)
    with locked_payment(payment.pk) as locked:
        locked.authorization_status = status[:32]
        if expires is not None:
            locked.authorization_expires_at = expires
        locked.save(update_fields=["authorization_status", "authorization_expires_at", "updated_at"])
    if statuses.authorization_outcome(auth.status) == statuses.FAILED or (
        expires is not None and timezone.now() >= expires
    ) or status == "EXPIRED":
        return _authorization_expired(payment.pk)
    return None


# ---------------------------------------------------------------------------
# Cancel: release the hold
# ---------------------------------------------------------------------------


def _cancel_locally(payment: PayPalPayment) -> None:
    payment.state = PayPalPayment.CANCELLED
    payment.state_detail = ""
    payment.save(update_fields=["state", "state_detail", "updated_at"])
    _advance_order_status(payment.order, "Cancelled")
    _adjust_stock(payment.order, consume=False)


def cancel(order_number: str) -> tuple[PayPalPayment, PaymentOperation | None]:
    payment = get_payment(order_number)
    with locked_payment(payment.pk) as payment:
        latest = _latest(payment, PaymentOperation.VOID)
        if payment.state == PayPalPayment.CANCELLED:
            return payment, latest
        if payment.state in CAPTURED_STATES + (PayPalPayment.CAPTURING, PayPalPayment.CAPTURE_PENDING):
            raise conflict(
                "already_fulfilled",
                "The payment has been captured; return the money with POST /api/orders/%s/refunds." % order_number,
            )
        if payment.state == PayPalPayment.AUTHORIZING:
            raise conflict(
                "payment_in_progress",
                "PayPal has not confirmed this order's authorization yet; cancel once its outcome is known.",
            )
        if payment.state == PayPalPayment.NEEDS_REVIEW:
            raise conflict("needs_review", "This order's payment needs operator review before it can be cancelled.")
        if payment.state in (
            PayPalPayment.AWAITING_PAYMENT, PayPalPayment.PAYMENT_FAILED, PayPalPayment.AUTHORIZATION_EXPIRED,
        ):
            _cancel_locally(payment)  # nothing is held at PayPal
            return payment, None
        if latest is not None and latest.outcome != PaymentOperation.FAILED:
            op, won = latest, False
        else:
            attempt = latest.attempt + 1 if latest is not None else 1
            op, won = try_claim(
                reference=_ref(payment, "void", attempt), kind=PaymentOperation.VOID, attempt=attempt,
                payment=payment, amount=payment.amount, currency=payment.currency,
            )
            if won:
                payment.state = PayPalPayment.VOIDING
                payment.save(update_fields=["state", "updated_at"])

    client = get_client()
    authorization_id = payment.authorization_id

    def send(key: str) -> PaymentAuthorization:
        return client.payments.void_payment(authorization_id, pay_pal_request_id=key, prefer=PREFER)

    def find(key: str) -> PaymentAuthorization:
        try:
            return send(key)  # PayPal returns the original void for a repeated request id
        except ApiError as exc:
            if provider_issue(exc) not in ALREADY_VOIDED:
                raise
        return read_with_retry(lambda: client.payments.get_authorized_payment(authorization_id))

    try:
        op, result = safe_write(
            op, won, send=send, find=find, read=read_void, repeat_is_safe=True, sent=None,
            landed_issues=ALREADY_VOIDED,
            refresh=lambda _: read_with_retry(lambda: client.payments.get_authorized_payment(authorization_id)),
        )
    except BaseException as exc:
        _after_failed_write(payment.pk, op, exc, on_refused=PayPalPayment.AUTHORIZED)
        raise
    with locked_payment(payment.pk) as payment:
        if result is not None:
            payment.authorization_status = statuses.status_text(result.status)[:32]
        if op.outcome == PaymentOperation.DONE and payment.state != PayPalPayment.CANCELLED:
            _record_txn(payment, "Void", payment.amount, authorization_id, payment.authorization_status)
            _cancel_locally(payment)
        elif op.outcome == PaymentOperation.FAILED:
            # PayPal says the authorization was captured: money moved that
            # this app has no capture for. Surface it rather than guess.
            payment.state = PayPalPayment.NEEDS_REVIEW
            payment.state_detail = "Void refused: PayPal reports the authorization as %s." % op.provider_status
        payment.save()
    return payment, op


# ---------------------------------------------------------------------------
# Refund: give captured money back, in full or in part
# ---------------------------------------------------------------------------


def _committed_refunds(payment: PayPalPayment) -> Decimal:
    total = payment.operations.filter(
        kind=PaymentOperation.REFUND, outcome__in=PaymentOperation.LIVE_OUTCOMES
    ).aggregate(total=Sum("amount"))["total"]
    return Decimal(total or 0)


def refund(user: Any, order_number: str, idempotency_key: str, raw_amount: object) -> tuple[PayPalPayment, PaymentOperation]:
    payment = get_owned_payment(user, order_number)
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    try:
        amount = money.parse_amount(raw_amount, payment.currency) if raw_amount is not None else None
    except ValueError as exc:
        raise bad_request(str(exc)) from None
    with locked_payment(payment.pk) as payment:
        reference = _ref(payment, "rf", key_hash[:24])
        existing = PaymentOperation.objects.filter(reference=reference).first()
        if existing is not None and amount is not None and existing.amount != amount:
            raise ApiProblem(
                422, "idempotency_key_reused",
                "This Idempotency-Key was already used for a refund of %s %s." % (existing.amount, existing.currency),
            )
        if existing is not None and not (existing.outcome == PaymentOperation.FAILED and not existing.provider_id):
            op, won = existing, False
        else:
            if payment.state not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED):
                if payment.state == PayPalPayment.REFUNDED:
                    raise conflict("fully_refunded", "The captured payment has already been refunded in full.")
                raise conflict(
                    "not_refundable",
                    "Only a fulfilled (captured) order can be refunded; this order's payment is '%s'. "
                    "Before fulfilment, an operator cancels the order instead." % payment.state,
                )
            captured = payment.captured_amount or Decimal(0)
            remaining = captured - _committed_refunds(payment)
            if existing is not None and amount is None:
                amount = existing.amount
            if amount is None:
                amount = remaining
            if amount <= 0 or amount > remaining:
                raise ApiProblem(
                    422, "refund_exceeds_captured",
                    "At most %s %s can still be refunded on this order." % (remaining, payment.currency),
                    extra={"refundable": money.to_wire(max(remaining, Decimal(0)), payment.currency)},
                )
            if existing is not None:
                existing.amount = amount
                existing.save(update_fields=["amount"])
                won = retake_released(existing)
                op = existing
            else:
                op, won = try_claim(
                    reference=reference, kind=PaymentOperation.REFUND, user=user, payment=payment,
                    amount=amount, currency=payment.currency, request_key=key_hash,
                )

    client = get_client()
    capture_id = payment.capture_id
    assert op.amount is not None
    refund_amount = op.amount
    body = RefundRequest(
        amount=_money(refund_amount, payment.currency),
        custom_id="%s-%s" % (reference_prefix(), payment.order.number),
    )

    def send(key: str) -> Refund:
        return client.payments.refund_captured_payment(capture_id, pay_pal_request_id=key, prefer=PREFER, body=body)

    op, result = safe_write(
        op, won, send=send, find=send, read=read_refund,
        repeat_is_safe=True,  # PayPal returns the original refund for a repeated request id
        sent=(refund_amount, payment.currency),
        refresh=lambda o: read_with_retry(lambda: client.payments.get_refund(o.provider_id)),
    )
    return _apply_refund(payment.pk, op), op


def _apply_refund(payment_pk: int, op: PaymentOperation) -> PayPalPayment:
    client = get_client()
    capture_id = PayPalPayment.objects.values_list("capture_id", flat=True).get(pk=payment_pk)
    capture_status = ""
    try:
        capture = read_with_retry(lambda: client.payments.get_captured_payment(capture_id))
        capture_status = statuses.status_text(capture.status)
    except (ApiError, httpx.RequestError, ValueError):
        log.warning("Could not refresh capture status after refund %s", op.reference)
    with locked_payment(payment_pk) as payment:
        refunded = payment.operations.filter(
            kind=PaymentOperation.REFUND, outcome=PaymentOperation.DONE
        ).aggregate(total=Sum("amount"))["total"] or Decimal(0)
        refunded = money.quantize(Decimal(refunded), payment.currency)
        payment.refunded_amount = refunded
        if capture_status:
            payment.capture_status = capture_status[:32]
        if payment.state in CAPTURED_STATES and refunded > 0:
            captured = payment.captured_amount or payment.amount
            payment.state = PayPalPayment.REFUNDED if refunded >= captured else PayPalPayment.PARTIALLY_REFUNDED
        if op.outcome == PaymentOperation.DONE and op.amount is not None:
            _record_txn(payment, "refund", op.amount, op.provider_id, op.provider_status)
        payment.save()
        return payment
