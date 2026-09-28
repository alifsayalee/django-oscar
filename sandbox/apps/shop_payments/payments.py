"""
Money movement for an Oscar order through PayPal.

* pay       — one single-step card order with intent AUTHORIZE: PayPal holds the
              order total on the card (one-off card details or a saved card's vault id).
* fulfil    — captures the held authorization (renewing it first if its honor
              period has passed), then records PayPal's gross, fee and net.
* cancel    — voids the authorization before fulfilment, so no money moves.
* refunds   — refunds all or part of the capture, keyed by the caller's idempotency key.

Every PayPal write goes through ``safe_write`` with a reference derived from the
order and the step; Oscar's ``Source``/``Transaction`` ledger records the
amounts, and the order moves through Oscar's status pipeline.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from paypal.core import UNSET, UnsetType
from paypal.models import (
    AuthorizationWithAdditionalData,
    CapturedPayment,
    CaptureRequest,
    CardRequest,
    Money,
    Order as PayPalOrder,
    OrderRequest,
    PaymentAuthorization,
    PaymentSource,
    PurchaseUnitRequest,
    AmountWithBreakdown,
    ReauthorizeRequest,
    Refund,
    RefundRequest,
)
from paypal.models.enums import AuthorizationStatus, CheckoutPaymentIntent

from . import errors, outcomes
from .cards import CardInput, billing_address, usable_cards
from .errors import ApiProblem
from .models import PaymentWrite, PayPalPayment, PayPalRefund
from .money import is_exact, parse_amount, to_paypal
from .paypal_client import get_client
from .safe_write import Answer, WriteResult, answer_for, deterministic_ref, provider_time, refresh, safe_write

logger = logging.getLogger(__name__)

Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Bankcard = get_model("payment", "Bankcard")
EventHandler = get_class("order.processing", "EventHandler")

# From the reauthorize_payment operation's documentation: reauthorize after the
# initial three-day honor period; within the 29-day authorization period.
HONOR_PERIOD = timedelta(days=3)

# Oscar order statuses (sandbox OSCAR_ORDER_STATUS_PIPELINE).
AWAITING_PAYMENT_STATUS = "Pending"
PAID_STATUS = "Being processed"
FULFILLED_STATUS = "Complete"
CANCELLED_STATUS = "Cancelled"


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _money(value: object) -> tuple[str | None, str | None]:
    if isinstance(value, (Money, AmountWithBreakdown)):
        return value.value, value.currency_code
    return None, None


def _decimal(value: object) -> Decimal | None:
    amount, _ = _money(value)
    return Decimal(amount) if amount is not None else None


def _source_type() -> Any:
    source_type, _ = SourceType.objects.get_or_create(name="PayPal")
    return source_type


def _transition(payment: PayPalPayment, allowed: tuple[str, ...], to: str) -> bool:
    """Compare-and-set the payment state in the database; False if another request moved it."""
    moved = PayPalPayment.objects.filter(pk=payment.pk, state__in=allowed).update(state=to, updated=timezone.now())
    payment.refresh_from_db()
    return bool(moved)


def order_reference(payment: PayPalPayment) -> str:
    return deterministic_ref(f"o{payment.order.number}")


# --- reading PayPal responses -----------------------------------------------------------


def _first_authorization(order: PayPalOrder) -> AuthorizationWithAdditionalData | None:
    units = order.purchase_units
    if not isinstance(units, list) or not units:
        return None
    payments = units[0].payments
    if isinstance(payments, UnsetType):
        return None
    auths = payments.authorizations
    if isinstance(auths, list) and auths:
        return auths[0]
    return None


def read_authorize(order: PayPalOrder) -> Answer:
    auth = _first_authorization(order)
    if auth is not None:
        amount, currency = _money(auth.amount)
        return Answer(_text(auth.id), f"AUTH:{_text(auth.status)}", provider_time(auth.create_time), amount, currency)
    amount = currency = None
    units = order.purchase_units
    if isinstance(units, list) and units:
        amount, currency = _money(units[0].amount)
    return Answer(_text(order.id), f"ORDER:{_text(order.status)}", provider_time(order.create_time), amount, currency)


def authorize_step_outcome(status: str) -> str:
    scope, _, value = status.partition(":")
    if scope == "AUTH":
        return outcomes.authorization_outcome(value)
    return outcomes.order_without_authorization_outcome(value)


def read_authorization(auth: PaymentAuthorization) -> Answer:
    amount, currency = _money(auth.amount)
    return Answer(_text(auth.id), _text(auth.status), provider_time(auth.create_time), amount, currency)


def read_capture(capture: CapturedPayment) -> Answer:
    amount, currency = _money(capture.amount)
    return Answer(_text(capture.id), _text(capture.status), provider_time(capture.create_time), amount, currency)


def read_refund(refund: Refund) -> Answer:
    amount, currency = _money(refund.amount)
    return Answer(_text(refund.id), _text(refund.status), provider_time(refund.create_time), amount, currency)


# --- pay (authorize) --------------------------------------------------------------------------


def pay(order: Any, user: Any, payload: dict[str, Any]) -> PayPalPayment:
    payment = _payment_for(order)
    if payment.state in (PayPalPayment.AUTHORIZED, PayPalPayment.REAUTHORIZING, PayPalPayment.CAPTURING,
                         PayPalPayment.CAPTURE_PENDING, PayPalPayment.CAPTURED,
                         PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        return payment  # already paid: a repeat answers with the current state
    if payment.state in (PayPalPayment.VOIDING, PayPalPayment.VOIDED) or order.status == CANCELLED_STATUS:
        raise ApiProblem(409, "order_cancelled", "This order was cancelled and cannot be paid.")
    if payment.state == PayPalPayment.NEEDS_REVIEW:
        raise ApiProblem(409, "needs_review", "This order's payment needs operator review.")
    if order.status != AWAITING_PAYMENT_STATUS and payment.state == PayPalPayment.AWAITING_PAYMENT:
        raise ApiProblem(409, "not_awaiting_payment", f"Order is '{order.status}', not awaiting payment.")

    attempt = payment.authorize_attempt
    reference = deterministic_ref(f"o{order.number}", "auth", attempt)
    existing = PaymentWrite.objects.filter(reference=reference).first()
    if existing is not None and existing.outcome == PaymentWrite.PENDING:
        return _refresh_pending_authorization(payment, existing)

    # A repeat of the request carries the same card, so a check of an earlier attempt
    # re-sends the same body under the same PayPal-Request-Id.
    card_source: CardRequest | None = None
    bankcard, label = None, ""
    if existing is None or payload.get("card") is not None or payload.get("paymentMethodId") is not None:
        card_source, bankcard, label = _payment_source(user, payload)
    amount = payment.amount
    currency = payment.currency
    custom = order_reference(payment)

    def send(ref: str) -> PayPalOrder:
        if card_source is None:
            # A check of an earlier attempt: the same request id returns PayPal's original answer.
            raise errors.OutcomeUnknown(ref)
        body = OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[
                PurchaseUnitRequest(
                    reference_id=f"o{order.number}",
                    custom_id=custom,
                    invoice_id=custom,
                    description=f"Order {order.number}",
                    amount=AmountWithBreakdown(currency_code=currency, value=to_paypal(amount, currency)),
                )
            ],
            payment_source=PaymentSource(card=card_source),
        )
        return get_client().orders.create_order(body, pay_pal_request_id=ref, prefer="return=representation")

    if existing is None:
        _transition(payment, (PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZATION_PENDING),
                    PayPalPayment.AUTHORIZING)
        PayPalPayment.objects.filter(pk=payment.pk).update(bankcard=bankcard, card_label=label)
        payment.refresh_from_db()

    try:
        result = safe_write(
            reference=reference,
            kind=PaymentWrite.AUTHORIZE,
            send=send,
            read=read_authorize,
            outcome_of=authorize_step_outcome,
            sent=(amount, currency),
            release_on_refusal=False,
            claim_fields={"user": user, "order": order, "amount": amount, "currency": currency},
        )
    except errors.OutcomeUnknown:
        raise
    except ApiProblem as problem:
        if problem.code == "amount_mismatch":
            _mark_review(payment, problem.message)
        else:
            _authorization_failed(payment, attempt, problem.message)
        if problem.code == "paypal_rejected":
            # Whatever PayPal refused about it, the payment was not accepted.
            problem.status_code, problem.code = 402, "payment_declined"
        raise
    return _apply_authorization(payment, result)


def _payment_source(user: Any, payload: dict[str, Any]) -> tuple[CardRequest, Any, str]:
    method_id = payload.get("paymentMethodId")
    card_payload = payload.get("card")
    if (method_id is None) == (card_payload is None):
        raise ApiProblem(400, "invalid_payment", "Send exactly one of: card (card details) or paymentMethodId.")
    if method_id is not None:
        bankcard = usable_cards(user).filter(pk=str(method_id)).first() if str(method_id).isdigit() else None
        if bankcard is None:
            raise ApiProblem(404, "payment_method_not_found", "No such saved card.")
        return CardRequest(vault_id=bankcard.partner_reference), bankcard, _label(bankcard.card_type, bankcard.number[-4:])
    card = CardInput.parse(card_payload)
    source = CardRequest(
        name=card.name or UNSET,
        number=card.number,
        expiry=card.expiry,
        security_code=card.security_code,
        billing_address=billing_address(card.billing) or UNSET,
    )
    return source, None, _label(card.brand, card.number[-4:])


def _label(brand: str, last4: str) -> str:
    return f"{brand} ending {last4}"


def _refresh_pending_authorization(payment: PayPalPayment, write: PaymentWrite) -> PayPalPayment:
    client = get_client()
    if payment.authorization_id:
        result: WriteResult[Any] = refresh(
            write,
            lambda: errors.read(lambda: client.payments.get_authorized_payment(payment.authorization_id),
                                what="authorization lookup"),
            lambda a: _prefixed(read_authorization(a), "AUTH"),
            authorize_step_outcome,
        )
        return _apply_authorization(payment, result)
    result = refresh(
        write,
        lambda: errors.read(lambda: client.orders.get_order(write.provider_id), what="order lookup"),
        read_authorize,
        authorize_step_outcome,
    )
    return _apply_authorization(payment, result)


def _prefixed(answer: Answer, scope: str) -> Answer:
    return Answer(answer.provider_id, f"{scope}:{answer.status}", answer.provider_time, answer.amount, answer.currency)


def _apply_authorization(payment: PayPalPayment, result: WriteResult[Any]) -> PayPalPayment:
    write = result.write
    response = result.response
    auth: Any = None
    if isinstance(response, PayPalOrder):
        payment.paypal_order_id = _text(response.id) or payment.paypal_order_id
        auth = _first_authorization(response)
        source = response.payment_source
        card = UNSET if isinstance(source, UnsetType) else source.card
        if not isinstance(card, UnsetType) and not payment.card_label:
            payment.card_label = _label(_text(card.brand) or "Card", _text(card.last_digits))
    elif isinstance(response, PaymentAuthorization):
        auth = response
    elif write.outcome == PaymentWrite.DONE and not payment.authorization_id and write.provider_id:
        # Settled by an earlier request that did not get to record it: read it back.
        auth = errors.read(lambda: get_client().payments.get_authorized_payment(write.provider_id),
                           what="authorization lookup")

    if auth is not None:
        payment.authorization_id = _text(auth.id)
        payment.authorization_status = _text(auth.status)
        payment.authorization_created_at = provider_time(auth.create_time)
        payment.authorization_expires_at = provider_time(auth.expiration_time)
    payment.save()

    if write.outcome == PaymentWrite.DONE:
        with transaction.atomic():
            locked = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
            if locked.state in (PayPalPayment.AUTHORIZING, PayPalPayment.AUTHORIZATION_PENDING,
                                PayPalPayment.AWAITING_PAYMENT):
                source = Source.objects.create(
                    order=locked.order,
                    source_type=_source_type(),
                    currency=locked.currency,
                    reference=locked.paypal_order_id,
                    label=locked.card_label,
                )
                source.allocate(locked.amount, reference=locked.authorization_id, status=locked.authorization_status)
                locked.source = source
                locked.state = PayPalPayment.AUTHORIZED
                locked.last_error = ""
                locked.save()
                if locked.order.status == AWAITING_PAYMENT_STATUS:
                    locked.order.set_status(PAID_STATUS)
        payment.refresh_from_db()
        return payment
    if write.outcome == PaymentWrite.PENDING:
        _transition(payment, (PayPalPayment.AUTHORIZING,), PayPalPayment.AUTHORIZATION_PENDING)
    elif write.outcome == PaymentWrite.FAILED:
        reason = _decline_reason(write)
        _authorization_failed(payment, payment.authorize_attempt, reason)
        write.error_message = reason
        write.error_code = (
            "payer_action_required" if write.provider_status == "ORDER:PAYER_ACTION_REQUIRED" else "card_declined"
        )
        write.save(update_fields=["error_message", "error_code", "updated"])
    elif write.outcome == PaymentWrite.NEEDS_REVIEW:
        _mark_review(payment, "PayPal reports the authorization was already captured.")
    answer_for(write, what="The card authorization")
    return payment


def _decline_reason(write: PaymentWrite) -> str:
    if write.provider_status == "ORDER:PAYER_ACTION_REQUIRED":
        return ("The card issuer requires the shopper to authenticate in a browser (3-D Secure), "
                "which this API does not support. Try another card.")
    if write.provider_status.startswith("AUTH:"):
        return f"PayPal did not hold the funds (authorization {write.provider_status[5:].lower()})."
    return write.error_message or "The card was declined."


def _authorization_failed(payment: PayPalPayment, attempt: int, reason: str) -> None:
    """A definite failure: nothing is held, so the shopper may try again under a new reference."""
    PayPalPayment.objects.filter(pk=payment.pk, authorize_attempt=attempt).update(
        authorize_attempt=F("authorize_attempt") + 1,
        state=PayPalPayment.AWAITING_PAYMENT,
        last_error=reason[:512],
        updated=timezone.now(),
    )
    payment.refresh_from_db()


def _mark_review(payment: PayPalPayment, reason: str) -> None:
    PayPalPayment.objects.filter(pk=payment.pk).update(
        state=PayPalPayment.NEEDS_REVIEW, last_error=reason[:512], updated=timezone.now()
    )
    payment.refresh_from_db()


def _payment_for(order: Any) -> PayPalPayment:
    payment, _ = PayPalPayment.objects.get_or_create(
        order=order, defaults={"amount": order.total_incl_tax, "currency": order.currency}
    )
    return payment


# --- fulfil (capture) -----------------------------------------------------------------------


def fulfil(order: Any, operator: Any) -> PayPalPayment:
    payment = _payment_for(order)
    if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        return payment
    if payment.state == PayPalPayment.CAPTURE_PENDING:
        return _refresh_pending_capture(payment, operator)
    if payment.state not in (PayPalPayment.AUTHORIZED, PayPalPayment.REAUTHORIZING, PayPalPayment.CAPTURING):
        raise ApiProblem(
            409,
            "not_authorized",
            f"Order cannot be fulfilled: payment is '{payment.state}'. Only an order with authorized "
            "funds can be fulfilled.",
        )

    client = get_client()
    auth = errors.read(lambda: client.payments.get_authorized_payment(payment.authorization_id),
                       what="authorization lookup")
    payment.authorization_status = _text(auth.status)
    payment.authorization_expires_at = provider_time(auth.expiration_time) or payment.authorization_expires_at
    payment.save(update_fields=["authorization_status", "authorization_expires_at", "updated"])

    status = auth.status
    if status in (AuthorizationStatus.VOIDED, AuthorizationStatus.DENIED):
        raise ApiProblem(
            409,
            "authorization_not_renewable",
            f"The authorization is {_text(status).lower()} at PayPal, so there is nothing to capture. "
            "Cancel this order and ask the shopper to place and pay for a new one.",
        )
    if status == AuthorizationStatus.PENDING:
        raise ApiProblem(202, "authorization_pending",
                         "PayPal has not finished the authorization yet; try fulfilment again later.")

    now = timezone.now()
    already_capturing = status in (AuthorizationStatus.CAPTURED, AuthorizationStatus.PARTIALLY_CAPTURED)
    if not already_capturing:
        expires = payment.authorization_expires_at
        if expires is not None and now >= expires:
            raise ApiProblem(
                409,
                "authorization_expired",
                f"The authorization expired at {expires.isoformat()} and can no longer be renewed. "
                "Cancel this order and ask the shopper to place and pay for a new one.",
            )
        created = payment.authorization_created_at
        if created is not None and now > created + HONOR_PERIOD:
            if payment.reauthorized_at is None:
                _reauthorize(payment)
            else:
                logger.info("Order %s: honor period passed after a reauthorization; capturing anyway", order.number)

    if not _transition(payment, (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING), PayPalPayment.CAPTURING):
        raise ApiProblem(409, "conflict", f"Payment is now '{payment.state}'; it cannot be captured.")

    amount, currency = payment.amount, payment.currency
    authorization_id = payment.authorization_id
    custom = order_reference(payment)

    def send(ref: str) -> CapturedPayment:
        return client.payments.capture_authorized_payment(
            authorization_id,
            pay_pal_request_id=ref,
            prefer="return=representation",
            body=CaptureRequest(
                amount=Money(currency_code=currency, value=to_paypal(amount, currency)),
                final_capture=True,
                invoice_id=custom,
            ),
        )

    try:
        result = safe_write(
            reference=deterministic_ref(f"o{order.number}", "capture", authorization_id),
            kind=PaymentWrite.CAPTURE,
            send=send,
            read=read_capture,
            outcome_of=outcomes.capture_outcome,
            sent=(amount, currency),
            claim_fields={"user": operator, "order": order, "amount": amount, "currency": currency},
        )
    except errors.OutcomeUnknown:
        raise
    except ApiProblem as problem:
        if problem.code == "amount_mismatch":
            _mark_review(payment, problem.message)
        else:
            _transition(payment, (PayPalPayment.CAPTURING,), PayPalPayment.AUTHORIZED)
            PayPalPayment.objects.filter(pk=payment.pk).update(last_error=problem.message[:512])
            problem.code = "capture_failed" if problem.code == "paypal_rejected" else problem.code
        raise
    return _apply_capture(payment, result)


def _reauthorize(payment: PayPalPayment) -> None:
    """Renew a stale authorization; PayPal answers with a new authorization id."""
    if not _transition(payment, (PayPalPayment.AUTHORIZED, PayPalPayment.REAUTHORIZING), PayPalPayment.REAUTHORIZING):
        raise ApiProblem(409, "conflict", f"Payment is now '{payment.state}'.")
    amount, currency = payment.amount, payment.currency
    old_id = payment.authorization_id

    def send(ref: str) -> PaymentAuthorization:
        return get_client().payments.reauthorize_payment(
            old_id,
            pay_pal_request_id=ref,
            prefer="return=representation",
            body=ReauthorizeRequest(amount=Money(currency_code=currency, value=to_paypal(amount, currency))),
        )

    try:
        result = safe_write(
            reference=deterministic_ref(f"o{payment.order.number}", "reauth", old_id),
            kind=PaymentWrite.REAUTHORIZE,
            send=send,
            read=read_authorization,
            outcome_of=outcomes.authorization_outcome,
            sent=(amount, currency),
            claim_fields={"order": payment.order, "amount": amount, "currency": currency},
        )
    except errors.OutcomeUnknown:
        raise
    except ApiProblem as problem:
        _transition(payment, (PayPalPayment.REAUTHORIZING,), PayPalPayment.AUTHORIZED)
        if problem.code == "paypal_rejected":
            raise ApiProblem(
                409,
                "authorization_not_renewable",
                f"The authorization is past its 3-day honor period and PayPal refused to renew it "
                f"({problem.message}). Cancel this order and ask the shopper to pay again.",
                details=problem.details,
            ) from problem
        raise

    write = result.write
    response = result.response
    if write.outcome == PaymentWrite.DONE:
        auth = response if isinstance(response, PaymentAuthorization) else errors.read(
            lambda: get_client().payments.get_authorized_payment(write.provider_id), what="authorization lookup")
        payment.original_authorization_id = payment.original_authorization_id or old_id
        payment.authorization_id = _text(auth.id)
        payment.authorization_status = _text(auth.status)
        payment.authorization_created_at = provider_time(auth.create_time) or timezone.now()
        payment.authorization_expires_at = provider_time(auth.expiration_time) or payment.authorization_expires_at
        payment.reauthorized_at = timezone.now()
        payment.state = PayPalPayment.AUTHORIZED
        payment.save()
        if payment.source is not None:
            payment.source.transactions.create(
                txn_type="Reauthorise", amount=amount, reference=payment.authorization_id,
                status=payment.authorization_status,
            )
        return
    _transition(payment, (PayPalPayment.REAUTHORIZING,), PayPalPayment.AUTHORIZED)
    if write.outcome == PaymentWrite.FAILED:
        raise ApiProblem(
            409,
            "authorization_not_renewable",
            f"PayPal would not renew the authorization (status {write.provider_status}). "
            "Cancel this order and ask the shopper to pay again.",
        )
    answer_for(write, what="Renewing the authorization")


def _apply_capture(payment: PayPalPayment, result: WriteResult[Any]) -> PayPalPayment:
    write = result.write
    capture = result.response
    if capture is None and write.provider_id and write.outcome in (PaymentWrite.DONE, PaymentWrite.PENDING):
        capture = errors.read(lambda: get_client().payments.get_captured_payment(write.provider_id),
                              what="capture lookup")
    if isinstance(capture, CapturedPayment):
        payment.capture_id = _text(capture.id)
        payment.capture_status = _text(capture.status)
        breakdown = capture.seller_receivable_breakdown
        if not isinstance(breakdown, UnsetType):
            payment.paypal_fee = _decimal(breakdown.paypal_fee)
            payment.net_amount = _decimal(breakdown.net_amount)
        payment.captured_at = provider_time(capture.create_time)
        payment.save()

    if write.outcome == PaymentWrite.DONE:
        with transaction.atomic():
            locked = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
            if locked.state in (PayPalPayment.CAPTURING, PayPalPayment.CAPTURE_PENDING):
                captured = Decimal(write.amount) if write.amount is not None else locked.amount
                locked.captured_amount = captured
                locked.state = PayPalPayment.CAPTURED
                locked.authorization_status = AuthorizationStatus.CAPTURED.value
                locked.last_error = ""
                locked.save()
                if locked.source is not None:
                    locked.source.debit(captured, reference=locked.capture_id, status=locked.capture_status)
                order = locked.order
                EventHandler().consume_stock_allocations(order)
                if order.status == AWAITING_PAYMENT_STATUS:
                    order.set_status(PAID_STATUS)
                if order.status != FULFILLED_STATUS:
                    order.set_status(FULFILLED_STATUS)
        payment.refresh_from_db()
        return payment
    if write.outcome == PaymentWrite.PENDING:
        _transition(payment, (PayPalPayment.CAPTURING,), PayPalPayment.CAPTURE_PENDING)
    elif write.outcome == PaymentWrite.FAILED:
        _transition(payment, (PayPalPayment.CAPTURING, PayPalPayment.CAPTURE_PENDING), PayPalPayment.NEEDS_REVIEW)
        PayPalPayment.objects.filter(pk=payment.pk).update(
            last_error=f"PayPal reported the capture as {write.provider_status}."
        )
    elif write.outcome == PaymentWrite.NEEDS_REVIEW:
        _mark_review(payment, "PayPal captured a different amount than requested.")
    answer_for(write, what="The capture")
    return payment


def _refresh_pending_capture(payment: PayPalPayment, operator: Any) -> PayPalPayment:
    write = PaymentWrite.objects.filter(kind=PaymentWrite.CAPTURE, provider_id=payment.capture_id).first()
    if write is None:
        raise ApiProblem(409, "needs_review", "The pending capture has no record; an operator must review it.")
    client = get_client()
    result: WriteResult[Any] = refresh(
        write,
        lambda: errors.read(lambda: client.payments.get_captured_payment(payment.capture_id), what="capture lookup"),
        read_capture,
        outcomes.capture_outcome,
    )
    return _apply_capture(payment, result)


# --- cancel (void) -------------------------------------------------------------------------


def cancel(order: Any, operator: Any) -> PayPalPayment:
    payment = _payment_for(order)
    if payment.state == PayPalPayment.VOIDED:
        return payment
    if payment.state == PayPalPayment.AWAITING_PAYMENT:
        # Nothing was ever held: cancelling is local only.
        with transaction.atomic():
            if order.status != CANCELLED_STATUS:
                EventHandler().cancel_stock_allocations(order)
                order.set_status(CANCELLED_STATUS)
        return payment
    if payment.state in (PayPalPayment.CAPTURED, PayPalPayment.CAPTURE_PENDING, PayPalPayment.CAPTURING,
                         PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        raise ApiProblem(409, "already_fulfilled",
                         "The payment has been captured; the order can no longer be cancelled. Refund it instead.")
    if payment.state in (PayPalPayment.AUTHORIZING, PayPalPayment.AUTHORIZATION_PENDING):
        raise ApiProblem(409, "authorization_in_progress",
                         "The card authorization has not settled yet; repeat the pay request to settle it first.")
    if not _transition(payment, (PayPalPayment.AUTHORIZED, PayPalPayment.VOIDING), PayPalPayment.VOIDING):
        raise ApiProblem(409, "conflict", f"Payment is now '{payment.state}'; it cannot be cancelled.")

    client = get_client()
    authorization_id = payment.authorization_id

    def lookup(_: str) -> PaymentAuthorization:
        return client.payments.get_authorized_payment(authorization_id)

    def send(ref: str) -> PaymentAuthorization:
        voided = client.payments.void_payment(authorization_id, pay_pal_request_id=ref, prefer="return=representation")
        # A minimal (empty) answer carries no status: read the authorization itself.
        return voided if _text(voided.status) else lookup(ref)

    try:
        result = safe_write(
            reference=deterministic_ref(f"o{order.number}", "void", authorization_id),
            kind=PaymentWrite.VOID,
            send=send,
            find=lookup,
            read=read_authorization,
            outcome_of=outcomes.void_outcome,
            claim_fields={"user": operator, "order": order, "amount": payment.amount, "currency": payment.currency},
        )
    except errors.OutcomeUnknown:
        raise
    except ApiProblem:
        _transition(payment, (PayPalPayment.VOIDING,), PayPalPayment.AUTHORIZED)
        raise

    write = result.write
    if isinstance(result.response, PaymentAuthorization):
        payment.authorization_status = _text(result.response.status)
        payment.save(update_fields=["authorization_status", "updated"])
    if write.outcome == PaymentWrite.DONE:
        with transaction.atomic():
            locked = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
            if locked.state == PayPalPayment.VOIDING:
                locked.state = PayPalPayment.VOIDED
                locked.voided_at = write.provider_time or timezone.now()
                locked.save()
                if locked.source is not None:
                    locked.source.transactions.create(
                        txn_type="Void", amount=locked.source.amount_allocated,
                        reference=authorization_id, status=write.provider_status,
                    )
                    locked.source.amount_allocated = Decimal("0.00")
                    locked.source.save()
                if locked.order.status != CANCELLED_STATUS:
                    EventHandler().cancel_stock_allocations(locked.order)
                    locked.order.set_status(CANCELLED_STATUS)
        payment.refresh_from_db()
        return payment
    if write.outcome == PaymentWrite.FAILED:
        _mark_review(payment, "PayPal reports the authorization was captured; it could not be voided.")
        raise ApiProblem(409, "already_captured",
                         "PayPal reports the funds were already captured, so the hold could not be released. "
                         "Refund the order instead.")
    answer_for(write, what="Releasing the hold")
    return payment


# --- refunds -------------------------------------------------------------------------------


def refund(order: Any, user: Any, payload: dict[str, Any], idempotency_key: str) -> tuple[PayPalRefund, bool]:
    """Refund the capture. Returns the refund and whether this request created it."""
    payment = _payment_for(order)
    if not idempotency_key or len(idempotency_key) > 255:
        raise ApiProblem(400, "idempotency_key_required",
                         "Send an Idempotency-Key header (1-255 characters) with every refund request.")
    raw_amount = payload.get("amount")
    requested = None
    if raw_amount is not None:
        requested = parse_amount(raw_amount)
        if requested is None or not is_exact(requested, payment.currency):
            raise ApiProblem(400, "invalid_amount",
                             f"amount must be a positive amount in {payment.currency}, e.g. \"5.00\".")
    raw_reason = payload.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) else ""

    existing = PayPalRefund.objects.filter(payment=payment, idempotency_key=idempotency_key).first()
    if existing is not None:
        return _repeat_refund(existing, requested), False

    if payment.state not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.REFUNDED):
        raise ApiProblem(409, "not_captured", "Only a fulfilled (captured) order can be refunded.")

    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
    reference = deterministic_ref(f"o{order.number}", "refund", key_hash)
    try:
        with transaction.atomic():
            payment.refresh_from_db()
            amount = requested if requested is not None else payment.captured_amount - payment.refund_reserved
            if amount <= 0:
                raise ApiProblem(409, "nothing_to_refund", "The captured amount has already been refunded in full.")
            # Reserve the amount: the database refuses to reserve past the captured total.
            reserved = PayPalPayment.objects.filter(
                pk=payment.pk, refund_reserved__lte=F("captured_amount") - amount
            ).update(refund_reserved=F("refund_reserved") + amount, updated=timezone.now())
            if not reserved:
                available = payment.captured_amount - payment.refund_reserved
                raise ApiProblem(
                    409,
                    "exceeds_refundable",
                    f"Refund of {amount} exceeds the refundable balance of {max(available, Decimal(0))} "
                    f"{payment.currency}.",
                    details={"refundableAmount": str(max(available, Decimal(0)))},
                )
            write = PaymentWrite.objects.create(
                reference=reference, kind=PaymentWrite.REFUND, outcome=PaymentWrite.SENDING,
                claimed_at=timezone.now(), user=user, order=order, amount=amount, currency=payment.currency,
            )
            record = PayPalRefund.objects.create(
                payment=payment, idempotency_key=idempotency_key, amount=amount, currency=payment.currency,
                reason=reason[:255], write=write,
            )
    except IntegrityError:
        # The same key arrived concurrently; the reservation above was rolled back.
        record = PayPalRefund.objects.get(payment=payment, idempotency_key=idempotency_key)
        return _repeat_refund(record, requested), False

    return _send_refund(record), True


def _send_refund(record: PayPalRefund, *, claimed: bool = True) -> PayPalRefund:
    payment = record.payment
    capture_id = payment.capture_id
    custom = order_reference(payment)
    amount, currency = record.amount, record.currency

    def send(ref: str) -> Refund:
        return get_client().payments.refund_captured_payment(
            capture_id,
            pay_pal_request_id=ref,
            prefer="return=representation",
            body=RefundRequest(
                amount=Money(currency_code=currency, value=to_paypal(amount, currency)),
                custom_id=custom,
                invoice_id=f"{custom}:r{str(record.public_id)[:8]}",
            ),
        )

    try:
        result = safe_write(
            reference=record.write.reference,
            kind=PaymentWrite.REFUND,
            send=send,
            read=read_refund,
            outcome_of=outcomes.refund_outcome,
            sent=(amount, currency),
            claim=record.write if claimed else None,
            release_on_refusal=False,
        )
    except ApiProblem as problem:
        record.write.refresh_from_db()
        _settle_refund(record, record.write, answer=False)
        if problem.code == "paypal_rejected":
            problem.code = "refund_rejected"
        raise
    return _settle_refund(record, result.write, result.response)


def _settle_refund(
    record: PayPalRefund, write: PaymentWrite, response: Any = None, *, answer: bool = True
) -> PayPalRefund:
    with transaction.atomic():
        record = PayPalRefund.objects.select_for_update().get(pk=record.pk)
        record.outcome = write.outcome
        if write.provider_id:
            record.paypal_refund_id = write.provider_id
        if isinstance(response, Refund):
            record.paypal_status = _text(response.status)
        elif write.provider_status:
            record.paypal_status = write.provider_status
        if write.outcome == PaymentWrite.FAILED and record.reserved:
            PayPalPayment.objects.filter(pk=record.payment_id).update(
                refund_reserved=F("refund_reserved") - record.amount, updated=timezone.now()
            )
            record.reserved = False
        if write.outcome == PaymentWrite.DONE and not record.recorded:
            payment = PayPalPayment.objects.select_for_update().get(pk=record.payment_id)
            payment.refunded_amount += record.amount
            payment.state = (
                PayPalPayment.REFUNDED if payment.refunded_amount >= payment.captured_amount
                else PayPalPayment.PARTIALLY_REFUNDED
            )
            payment.save()
            if payment.source is not None:
                payment.source.refund(record.amount, reference=record.paypal_refund_id, status=record.paypal_status)
            record.recorded = True
        record.save()
    if answer:
        answer_for(write, what="The refund")
    return record


def _repeat_refund(record: PayPalRefund, requested: Decimal | None) -> PayPalRefund:
    if requested is not None and requested != record.amount:
        raise ApiProblem(
            409,
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a refund of a different amount.",
            details={"refundId": str(record.public_id)},
        )
    write = record.write
    write.refresh_from_db()
    if write.outcome == PaymentWrite.PENDING and write.provider_id:
        client = get_client()
        result: WriteResult[Any] = refresh(
            write,
            lambda: errors.read(lambda: client.payments.get_refund(write.provider_id), what="refund lookup"),
            read_refund,
            outcomes.refund_outcome,
        )
        return _settle_refund(record, result.write, result.response)
    if write.outcome in (PaymentWrite.SENDING, PaymentWrite.UNKNOWN):
        # The first request is in flight or its answer was lost: safe_write checks, never re-creates.
        return _send_refund(record, claimed=False)
    return _settle_refund(record, write)
