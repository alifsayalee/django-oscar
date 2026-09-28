"""The payment flows: Oscar orders + PayPal hold / capture / void / refund, and saved cards.

Every PayPal write goes through ``safe_write`` with a reference derived from the operation and step.
Nothing in this module runs inside an outer transaction (the API views opt out of ATOMIC_REQUESTS),
so a claim is committed before PayPal is called and survives whatever happens afterwards.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from paypal.models.enums import AuthorizationStatus

from .claims import DatabaseClaimStore, install_prefix
from .models import PayPalPayment, PayPalRefund, ProviderWrite, SavedCard
from .paypal_gateway import operations as ops
from .paypal_gateway.client import PayPalConfig, get_client
from .paypal_gateway.errors import NEVER_SENT, AmountMismatch, OutcomeUnknown, ProviderError
from .paypal_gateway.money import quantize
from .paypal_gateway.outcomes import (
    capture_outcome, captured_money_landed, hold_outcome, pay_outcome, refund_outcome, vault_outcome,
    void_outcome)
from .paypal_gateway.safe_write import safe_write

log = logging.getLogger("apps.payments_api")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Order = get_model("order", "Order")
ShippingAddress = get_model("order", "ShippingAddress")
Country = get_model("address", "Country")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Selector = get_class("partner.strategy", "Selector")
Applicator = get_class("offer.applicator", "Applicator")
Repository = get_class("shipping.repository", "Repository")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")

# From reauthorize_payment's own documentation: a hold is honoured for 3 days; it can be renewed up to
# 29 days after the original authorization, and after that a new authorization is required.
HONOR_PERIOD = timedelta(days=3)
REAUTHORIZE_LIMIT = timedelta(days=29)

STATUS_PENDING = "Pending"
STATUS_PROCESSING = "Being processed"
STATUS_COMPLETE = "Complete"
STATUS_CANCELLED = "Cancelled"

store = DatabaseClaimStore()


class ApiProblem(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


@dataclass
class Outcome:
    """What a flow hands back to the view: the HTTP status and the object to describe."""

    http_status: int
    obj: object
    extra: dict = field(default_factory=dict)


def paypal_config():
    return PayPalConfig(
        client_id=settings.PAYPAL_CLIENT_ID,
        client_secret=settings.PAYPAL_CLIENT_SECRET,
        environment=settings.PAYPAL_ENVIRONMENT,
        currency=settings.PAYPAL_CURRENCY,
        base_url=settings.PAYPAL_BASE_URL or None,
        timeout=float(getattr(settings, "PAYPAL_TIMEOUT", 20.0)),
    )


def client():
    return get_client(paypal_config())


def currency():
    return settings.PAYPAL_CURRENCY.upper()


def custom_id_for(order):
    return f"{install_prefix()}-{order.number}"


# --- orders -------------------------------------------------------------------------------------------

@dataclass
class ShippingInput:
    first_name: str
    last_name: str
    line1: str
    line2: str
    city: str
    state: str
    postcode: str
    country_code: str
    phone_number: str = ""


def place_order(user, items, shipping, request=None):
    """Create an Oscar order (Order + Lines, via OrderCreator) from catalogue ids and quantities."""
    cur = currency()
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for product_id, quantity in items:
            product = Product.objects.filter(pk=product_id).first()
            if product is None or not product.is_public or product.is_parent:
                raise ApiProblem(422, "unknown_product", f"Product {product_id} cannot be ordered.")
            info = basket.strategy.fetch_for_product(product)
            if not info.availability.is_available_to_buy:
                raise ApiProblem(422, "unavailable_product", f"Product {product_id} is not available to buy.")
            already = basket.product_quantity(product)
            permitted, reason = info.availability.is_purchase_permitted(already + quantity)
            if not permitted:
                raise ApiProblem(422, "quantity_not_permitted", f"Product {product_id}: {reason}")
            basket.add_product(product, quantity)
        basket.reset_offer_applications()
        Applicator().apply(basket, user, request)

        shipping_address = None
        if shipping is not None:
            country = Country.objects.filter(iso_3166_1_a2=shipping.country_code.upper()).first()
            if country is None or not country.is_shipping_country:
                raise ApiProblem(422, "invalid_country", "shippingAddress.countryCode is not a shipping country.")
            shipping_address = ShippingAddress(
                first_name=shipping.first_name, last_name=shipping.last_name, line1=shipping.line1,
                line2=shipping.line2, line4=shipping.city, state=shipping.state, postcode=shipping.postcode,
                country=country, phone_number=shipping.phone_number,
            )
        if basket.is_shipping_required() and shipping_address is None:
            raise ApiProblem(422, "shipping_address_required", "These items need a shippingAddress.")
        if shipping_address is not None:
            shipping_address.save()

        method = Repository().get_default_shipping_method(
            basket=basket, shipping_addr=shipping_address, request=request)
        charge = method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, charge)
        if total.incl_tax <= 0:
            raise ApiProblem(422, "nothing_to_pay", "The order total is zero; there is nothing to pay.")

        number = OrderNumberGenerator().order_number(basket)
        basket.freeze()
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=method, shipping_charge=charge, user=user,
            shipping_address=shipping_address, order_number=number, status=STATUS_PENDING, request=request,
        )
        basket.submit()
        # Amounts are the catalogue's; the currency the shop charges in is configuration.
        order.currency = cur
        order.save(update_fields=["currency"])
        PayPalPayment.objects.create(order=order, currency=cur, amount=quantize(order.total_incl_tax, cur))
    return order


def order_for_shopper(user, number):
    order = Order.objects.filter(number=number, user=user).select_related("paypal_payment").first()
    if order is None:
        raise ApiProblem(404, "order_not_found", "No such order.")
    return order


def order_for_operator(number):
    order = Order.objects.filter(number=number).select_related("paypal_payment").first()
    if order is None:
        raise ApiProblem(404, "order_not_found", "No such order.")
    return order


def payment_of(order):
    payment = getattr(order, "paypal_payment", None)
    if payment is None:
        raise ApiProblem(409, "not_a_paypal_order", "This order was not placed through the payments API.")
    return payment


def _advance_order(order, target):
    """Walk the sandbox pipeline (Pending -> Being processed -> Complete | Cancelled)."""
    order.refresh_from_db(fields=["status"])
    if order.status == target:
        return
    if target == STATUS_COMPLETE and order.status == STATUS_PENDING:
        order.set_status(STATUS_PROCESSING)
    order.set_status(target)


def _source_for(payment):
    if payment.source_id:
        return payment.source
    source_type, _ = SourceType.objects.get_or_create(name="PayPal")
    source = Source.objects.create(order=payment.order, source_type=source_type, currency=payment.currency,
                                   reference=payment.paypal_order_id, label="PayPal card")
    PayPalPayment.objects.filter(pk=payment.pk).update(source=source)
    payment.source = source
    return source


def _set_error(payment, code, message):
    payment.last_error_code = code[:64]
    payment.last_error_message = message[:255]
    PayPalPayment.objects.filter(pk=payment.pk).update(last_error_code=payment.last_error_code,
                                                      last_error_message=payment.last_error_message)


# --- pay (hold) ---------------------------------------------------------------------------------------

def pay(user, number, card=None, payment_method_id=None):
    order = order_for_shopper(user, number)
    payment = payment_of(order)

    if payment.status == PayPalPayment.AUTH_PENDING:
        _refresh_hold(payment)
    if payment.status in (PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURE_PENDING, PayPalPayment.CAPTURED):
        return Outcome(200, order)  # already paid: a repeat answers from the record
    if payment.status == PayPalPayment.AUTH_PENDING:
        return Outcome(202, order)
    if payment.status in (PayPalPayment.VOIDED, PayPalPayment.CANCELLED) or order.status == STATUS_CANCELLED:
        raise ApiProblem(409, "order_cancelled", "This order has been cancelled.")
    if payment.status == PayPalPayment.NEEDS_REVIEW:
        raise ApiProblem(409, "needs_review", "This payment needs an operator's review before anything else.")

    saved = None
    if payment_method_id is not None:
        saved = SavedCard.objects.filter(pk=payment_method_id, user=user, status=SavedCard.ACTIVE).first()
        if saved is None:
            raise ApiProblem(404, "payment_method_not_found", "No such saved card.")

    # Same attempt number -> same reference: a double-click lands on the same claim. It moves on only
    # when an attempt definitively failed.
    reference = f"{install_prefix()}:o{order.number}:pay:{payment.attempt}"
    custom_id = custom_id_for(order)
    try:
        result = safe_write(
            store, reference, "pay",
            send=lambda key: ops.create_hold(
                client(), key, amount=payment.amount, currency=payment.currency, order_number=order.number,
                custom_id=custom_id, card=card, vault_id=saved.paypal_token_id if saved else None),
            read=ops.read_hold,
            outcome_of=pay_outcome,
            repeat_is_safe=True,
            resend_window=ops.REQUEST_ID_RETENTION["pay"],
            sent=(payment.amount, payment.currency),
        )
    except ProviderError as exc:
        _fail_attempt(payment, exc.code, exc.message)
        if exc.status_code in (422, 409) and not exc.outcome_unknown:
            raise ApiProblem(402, "payment_declined", exc.message, issues=list(exc.issues)) from exc
        raise
    except OutcomeUnknown:
        _set_status(payment, PayPalPayment.UNKNOWN)
        raise
    except AmountMismatch:
        _set_status(payment, PayPalPayment.NEEDS_REVIEW)
        raise

    if result.outcome == "sending":
        return Outcome(202, order, {"inProgress": True})

    if result.payload is not None:
        hold = _hold_from_order(result.payload)
        paypal_order_id = ops._str(result.payload.id) or ""
        brand, last4 = ops.card_summary(result.payload)
    elif result.record.provider_id and result.outcome in ("done", "pending"):
        # Answered from the claim (e.g. the first request died after PayPal answered): re-read the hold.
        hold = _hold_from_authorization(ops.get_authorization(client(), result.record.provider_id))
        paypal_order_id, brand, last4 = payment.paypal_order_id, payment.card_brand, payment.card_last_digits
    else:
        hold = None
        paypal_order_id, brand, last4 = "", "", ""

    if result.outcome in ("done", "pending") and hold is not None:
        payment.saved_card = saved
        payment.card_brand, payment.card_last_digits = brand or payment.card_brand, last4 or payment.card_last_digits
        payment.paypal_order_id = paypal_order_id or payment.paypal_order_id
        _apply_hold(payment, hold, pending=result.outcome == "pending")
        order.refresh_from_db()
        return Outcome(200 if result.outcome == "done" else 202, order)

    if result.outcome == "failed":
        order_status = getattr(result.payload, "status", None)
        _fail_attempt(payment, "payment_declined", "PayPal did not authorize the payment.")
        if order_status is not None and str(order_status) == "PAYER_ACTION_REQUIRED":
            raise ApiProblem(402, "payer_action_required",
                             "PayPal requires the card holder to complete a browser challenge; this integration "
                             "does not support that. Try another card.")
        raise ApiProblem(402, "payment_declined", "PayPal did not authorize the payment.",
                         authorizationStatus=str(hold.status) if hold else None)
    if result.outcome == "needs_review":
        _set_status(payment, PayPalPayment.NEEDS_REVIEW)
        raise ApiProblem(409, "needs_review", "PayPal's answer did not match the order; an operator must review it.")
    _set_status(payment, PayPalPayment.UNKNOWN)
    raise ApiProblem(504, "outcome_unknown", "PayPal's answer could not be confirmed; retry the same request.",
                     outcomeUnknown=True)


@dataclass
class Hold:
    authorization_id: str
    status: object
    created_at: object
    expires_at: object


def _hold_from_order(paypal_order):
    auth = ops.authorization_of(paypal_order)
    if auth is None:
        return None
    return Hold(ops._str(auth.id) or "", auth.status, ops.parse_time(auth.create_time),
                ops.parse_time(auth.expiration_time))


def _hold_from_authorization(auth):
    return Hold(ops._str(auth.id) or "", auth.status, ops.parse_time(auth.create_time),
                ops.parse_time(auth.expiration_time))


def _set_status(payment, status):
    payment.status = status
    PayPalPayment.objects.filter(pk=payment.pk).update(status=status)


def _fail_attempt(payment, code, message):
    """This attempt is over; the next /pay uses a fresh reference. Conditional, so it moves once."""
    PayPalPayment.objects.filter(pk=payment.pk, attempt=payment.attempt).update(
        attempt=F("attempt") + 1, status=PayPalPayment.AWAITING,
        last_error_code=code[:64], last_error_message=message[:255])
    payment.refresh_from_db()


def _apply_hold(payment, hold, *, pending):
    with transaction.atomic():
        first_hold = not payment.authorized_at
        payment.authorization_id = hold.authorization_id
        payment.authorization_status = str(hold.status)
        if first_hold:
            payment.authorized_at = hold.created_at
        payment.authorization_renewed_at = hold.created_at
        payment.authorization_expires_at = hold.expires_at
        payment.status = PayPalPayment.AUTH_PENDING if pending else PayPalPayment.AUTHORIZED
        payment.last_error_code = payment.last_error_message = ""
        payment.save()
        if not pending:
            source = _source_for(payment)
            if not source.transactions.filter(txn_type="Authorise", reference=hold.authorization_id).exists():
                source.allocate(payment.amount, reference=hold.authorization_id, status=str(hold.status))
            _advance_order(payment.order, STATUS_PROCESSING)


def _refresh_hold(payment):
    if not payment.authorization_id:
        return
    auth = ops.get_authorization(client(), payment.authorization_id)
    outcome = hold_outcome(auth.status)
    if outcome in ("done", "pending"):
        _apply_hold(payment, _hold_from_authorization(auth), pending=outcome == "pending")
    elif outcome == "failed":
        _fail_attempt(payment, "authorization_denied", f"PayPal reported the hold as {auth.status}.")


# --- fulfil (capture) ---------------------------------------------------------------------------------

def fulfil(number):
    order = order_for_operator(number)
    payment = payment_of(order)

    if payment.status == PayPalPayment.CAPTURED:
        return Outcome(200, order)
    if payment.status == PayPalPayment.CAPTURE_PENDING and payment.capture_id:
        _apply_capture(payment, ops.get_capture(client(), payment.capture_id))
        order.refresh_from_db()
        return Outcome(200 if payment.status == PayPalPayment.CAPTURED else 202, order)
    if payment.status == PayPalPayment.AUTH_PENDING:
        _refresh_hold(payment)
    if payment.status != PayPalPayment.AUTHORIZED:
        raise ApiProblem(409, "not_authorized",
                         f"The order has no authorized payment to capture (payment status: {payment.status}).")

    auth = ops.get_authorization(client(), payment.authorization_id)
    status = auth.status
    now = timezone.now()
    if status in (AuthorizationStatus.CREATED, AuthorizationStatus.PENDING):
        if payment.authorized_at and now - payment.authorized_at >= REAUTHORIZE_LIMIT:
            _hold_gone(payment, "The original authorization is more than 29 days old and can no longer be "
                                "renewed.")
        current = ops.parse_time(auth.create_time) or payment.authorization_renewed_at
        if current and now - current > HONOR_PERIOD:
            _reauthorize(payment)
    elif status in (AuthorizationStatus.CAPTURED, AuthorizationStatus.PARTIALLY_CAPTURED):
        pass  # a capture already landed (e.g. an earlier fulfil lost its answer); the capture step finds it
    else:
        _hold_gone(payment, f"PayPal reports the authorization as {status}.")

    return _capture(payment)


def _hold_gone(payment, why):
    _fail_attempt(payment, "authorization_expired", why)
    raise ApiProblem(
        409, "authorization_expired",
        f"{why} The money is no longer held: ask the shopper to pay again "
        f"(POST /api/orders/{payment.order.number}/pay), or cancel the order.")


def _reauthorize(payment):
    old_id = payment.authorization_id
    reference = f"{install_prefix()}:o{payment.order.number}:reauth:{old_id}"
    try:
        result = safe_write(
            store, reference, "reauthorize",
            send=lambda key: ops.reauthorize(client(), old_id, key, amount=payment.amount,
                                             currency=payment.currency),
            read=ops.read_authorization,
            outcome_of=hold_outcome,
            resend_window=ops.REQUEST_ID_RETENTION["reauthorize"],
            sent=(payment.amount, payment.currency),
        )
    except ProviderError as exc:
        if exc.outcome_unknown or exc.status_code >= 500:
            raise
        _hold_gone(payment, f"PayPal refused to renew the stale authorization ({exc.message}).")
    auth = result.payload
    if auth is None and result.record.provider_id:
        auth = ops.get_authorization(client(), result.record.provider_id)
    if result.outcome == "sending":
        raise ApiProblem(409, "in_progress", "A renewal of this authorization is already in progress.")
    if result.outcome == "failed":
        _hold_gone(payment, "PayPal did not renew the stale authorization.")
    if result.outcome == "pending":
        raise ApiProblem(409, "reauthorization_pending", "PayPal is still processing the renewed authorization; "
                                                         "retry fulfilment shortly.")
    if result.outcome != "done" or auth is None:
        raise ApiProblem(504, "outcome_unknown", "The renewal could not be confirmed; retry fulfilment.",
                         outcomeUnknown=True)
    hold = _hold_from_authorization(auth)
    with transaction.atomic():
        payment.authorization_id = hold.authorization_id
        payment.authorization_status = str(hold.status)
        payment.authorization_renewed_at = hold.created_at
        payment.authorization_expires_at = hold.expires_at
        payment.save()
        source = _source_for(payment)
        if not source.transactions.filter(txn_type="Reauthorise", reference=hold.authorization_id).exists():
            source.transactions.create(txn_type="Reauthorise", amount=payment.amount,
                                       reference=hold.authorization_id, status=str(hold.status))


def _capture(payment):
    auth_id = payment.authorization_id
    reference = f"{install_prefix()}:o{payment.order.number}:capture:{auth_id}"
    try:
        result = safe_write(
            store, reference, "capture",
            send=lambda key: ops.capture(client(), auth_id, key, amount=payment.amount, currency=payment.currency),
            read=ops.read_capture,
            outcome_of=capture_outcome,
            resend_window=ops.REQUEST_ID_RETENTION["capture"],
            sent=(payment.amount, payment.currency),
        )
    except ProviderError as exc:
        _set_error(payment, exc.code, exc.message)
        raise
    except AmountMismatch:
        _set_status(payment, PayPalPayment.NEEDS_REVIEW)
        raise
    if result.outcome == "sending":
        return Outcome(202, payment.order, {"inProgress": True})
    captured = result.payload
    if captured is None and result.record.provider_id:
        captured = ops.get_capture(client(), result.record.provider_id)
    if captured is None:
        raise ApiProblem(504, "outcome_unknown", "The capture could not be confirmed; retry fulfilment.",
                         outcomeUnknown=True)
    if result.outcome == "failed" and not captured_money_landed(captured.status):
        _set_error(payment, "capture_failed", f"PayPal reported the capture as {captured.status}.")
        raise ApiProblem(409, "capture_failed", f"PayPal did not take the money (capture {captured.status}).")
    _apply_capture(payment, captured)
    payment.order.refresh_from_db()
    return Outcome(200 if payment.status == PayPalPayment.CAPTURED else 202, payment.order)


def _apply_capture(payment, captured):
    status = captured.status
    breakdown = ops.breakdown_of(captured)
    landed = captured_money_landed(status)
    pending = capture_outcome(status) == "pending"
    with transaction.atomic():
        fields = dict(
            capture_id=ops._str(captured.id) or payment.capture_id,
            capture_status=str(status),
            captured_at=ops.parse_time(captured.create_time),
        )
        if landed:
            fields.update(
                captured_amount=payment.amount,
                paypal_fee=Decimal(breakdown.fee) if breakdown.fee else None,
                net_amount=Decimal(breakdown.net) if breakdown.net else None,
            )
            # Conditional: only the first request to record this capture debits Oscar's source.
            moved = PayPalPayment.objects.filter(pk=payment.pk).exclude(status=PayPalPayment.CAPTURED).update(
                status=PayPalPayment.CAPTURED, last_error_code="", last_error_message="", **fields)
            payment.refresh_from_db()
            if moved:
                _source_for(payment).debit(payment.amount, reference=payment.capture_id, status=str(status))
                _advance_order(payment.order, STATUS_COMPLETE)
        elif pending:
            PayPalPayment.objects.filter(pk=payment.pk).update(status=PayPalPayment.CAPTURE_PENDING, **fields)
            payment.refresh_from_db()


# --- cancel (void) ------------------------------------------------------------------------------------

def cancel(number):
    order = order_for_operator(number)
    payment = payment_of(order)
    if payment.status in (PayPalPayment.CANCELLED, PayPalPayment.VOIDED):
        return Outcome(200, order)
    if payment.status in (PayPalPayment.CAPTURED, PayPalPayment.CAPTURE_PENDING):
        raise ApiProblem(409, "already_fulfilled", "The order was fulfilled and the money taken; use refunds.")
    if payment.status in (PayPalPayment.UNKNOWN, PayPalPayment.NEEDS_REVIEW):
        raise ApiProblem(409, "payment_unsettled",
                         "The last payment attempt is unsettled; resolve it (the shopper's retry of /pay settles "
                         "an unknown outcome) before cancelling.")
    if payment.status == PayPalPayment.AWAITING or not payment.authorization_id:
        with transaction.atomic():
            _set_status(payment, PayPalPayment.CANCELLED)
            _advance_order(order, STATUS_CANCELLED)
        order.refresh_from_db()
        return Outcome(200, order)

    auth_id = payment.authorization_id
    reference = f"{install_prefix()}:o{order.number}:void:{auth_id}"
    try:
        result = safe_write(
            store, reference, "void",
            send=lambda key: ops.void(client(), auth_id, key),
            read=ops.read_void,
            outcome_of=void_outcome,
            find=lambda ref: ops.get_authorization(client(), auth_id),
            resend_window=ops.REQUEST_ID_RETENTION["void"],
        )
    except ProviderError as exc:
        if "PREVIOUSLY_CAPTURED" in exc.issues:
            raise ApiProblem(409, "already_captured", "The authorization was already captured; use refunds.") from exc
        raise
    if result.outcome == "sending":
        return Outcome(202, order, {"inProgress": True})
    if result.outcome == "failed":
        raise ApiProblem(409, "already_captured", "The authorization was already captured; use refunds.")
    if result.outcome != "done":
        raise ApiProblem(504, "outcome_unknown", "The void could not be confirmed; retry the cancel.",
                         outcomeUnknown=True)
    with transaction.atomic():
        moved = PayPalPayment.objects.filter(pk=payment.pk).exclude(status=PayPalPayment.VOIDED).update(
            status=PayPalPayment.VOIDED, authorization_status=str(AuthorizationStatus.VOIDED))
        payment.refresh_from_db()
        if moved:
            _source_for(payment).transactions.create(txn_type="Void", amount=payment.amount, reference=auth_id,
                                                      status="VOIDED")
            _advance_order(order, STATUS_CANCELLED)
    order.refresh_from_db()
    return Outcome(200, order)


# --- refunds ------------------------------------------------------------------------------------------

def refund(user, number, key, amount=None, note=None):
    order = order_for_operator(number) if user.is_staff else order_for_shopper(user, number)
    payment = payment_of(order)
    if payment.status != PayPalPayment.CAPTURED or not payment.capture_id:
        raise ApiProblem(409, "not_captured", "Only a fulfilled (captured) order can be refunded.")

    existing = PayPalRefund.objects.filter(payment=payment, idempotency_key=key).first()
    if existing is not None:
        return _repeat_refund(existing, amount)

    amount = amount if amount is not None else payment.captured_amount - payment.refund_reserved
    if amount <= 0:
        raise ApiProblem(409, "nothing_to_refund", "The capture has been refunded in full already.")
    try:
        with transaction.atomic():
            r = PayPalRefund.objects.create(payment=payment, idempotency_key=key, amount=amount,
                                            currency=payment.currency, requested_by=user)
            # The database, not a read, decides whether this refund still fits in what was captured.
            reserved = PayPalPayment.objects.filter(
                pk=payment.pk, refund_reserved__lte=F("captured_amount") - amount,
            ).update(refund_reserved=F("refund_reserved") + amount)
            if not reserved:
                raise ApiProblem(409, "exceeds_refundable",
                                 "That amount is more than what remains refundable on this order.",
                                 refundable=str(payment.captured_amount - payment.refund_reserved))
    except IntegrityError:
        return _repeat_refund(PayPalRefund.objects.get(payment=payment, idempotency_key=key), amount)
    return _send_refund(r)


def _repeat_refund(r, amount):
    if amount is not None and amount != r.amount:
        raise ApiProblem(422, "idempotency_key_reused",
                         "This idempotency key was already used for a refund of a different amount.")
    if r.status == "pending" and r.paypal_refund_id:
        _apply_refund(r, ops.get_refund(client(), r.paypal_refund_id))
    elif r.status in ("sending", "unknown"):
        return _send_refund(r)  # the safe write answers "in progress" or checks under the same reference
    return _refund_result(r)


def _refund_reference(r):
    digest = hashlib.sha256(r.idempotency_key.encode()).hexdigest()[:20]
    return f"{install_prefix()}:o{r.payment.order.number}:refund:{digest}"


def _send_refund(r):
    payment = r.payment
    capture_id = payment.capture_id
    custom_id = custom_id_for(payment.order)
    try:
        result = safe_write(
            store, _refund_reference(r), "refund",
            send=lambda key: ops.refund(client(), capture_id, key, amount=r.amount, currency=r.currency,
                                        custom_id=custom_id),
            read=ops.read_refund,
            outcome_of=refund_outcome,
            resend_window=ops.REQUEST_ID_RETENTION["refund"],
            sent=(r.amount, r.currency),
        )
    except ProviderError as exc:
        if not exc.outcome_unknown:
            _finish_refund(r, "failed")
        raise
    except OutcomeUnknown:
        PayPalRefund.objects.filter(pk=r.pk).update(status="unknown")
        raise
    except AmountMismatch:
        PayPalRefund.objects.filter(pk=r.pk).update(status="needs_review")
        raise
    if result.outcome == "sending":
        r.refresh_from_db()
        return Outcome(202, r, {"inProgress": True})
    paypal_refund = result.payload
    if paypal_refund is None and result.record.provider_id:
        paypal_refund = ops.get_refund(client(), result.record.provider_id)
    if paypal_refund is not None:
        _apply_refund(r, paypal_refund)
    elif result.outcome == "failed":
        _finish_refund(r, "failed")
    return _refund_result(r)


def _apply_refund(r, paypal_refund):
    outcome = refund_outcome(paypal_refund.status)
    PayPalRefund.objects.filter(pk=r.pk).update(
        paypal_refund_id=ops._str(paypal_refund.id) or r.paypal_refund_id,
        paypal_status=str(paypal_refund.status),
        provider_time=ops.parse_time(paypal_refund.create_time),
    )
    _finish_refund(r, outcome)


def _finish_refund(r, outcome):
    """Move a refund to its outcome exactly once, with its bookkeeping."""
    with transaction.atomic():
        if outcome == "done":
            moved = PayPalRefund.objects.filter(pk=r.pk).exclude(status="done").update(status="done")
            if moved:
                payment = PayPalPayment.objects.get(pk=r.payment_id)
                PayPalPayment.objects.filter(pk=payment.pk).update(refunded_amount=F("refunded_amount") + r.amount)
                r.refresh_from_db()
                _source_for(payment).refund(r.amount, reference=r.paypal_refund_id, status=r.paypal_status)
        elif outcome == "failed":
            moved = PayPalRefund.objects.filter(pk=r.pk).exclude(status__in=("done", "failed")).update(
                status="failed")
            if moved:  # nothing was refunded: give the reservation back
                PayPalPayment.objects.filter(pk=r.payment_id).update(refund_reserved=F("refund_reserved") - r.amount)
        else:
            PayPalRefund.objects.filter(pk=r.pk).exclude(status__in=("done", "failed")).update(status=outcome)
    r.refresh_from_db()


def _refund_result(r):
    r.refresh_from_db()
    if r.status == "done":
        return Outcome(201, r)
    if r.status in ("pending", "sending"):
        return Outcome(202, r)
    if r.status == "failed":
        raise ApiProblem(409, "refund_failed", f"PayPal did not complete the refund ({r.paypal_status or 'refused'}).",
                         refundId=str(r.pk))
    if r.status == "needs_review":
        raise ApiProblem(409, "needs_review", "PayPal's refund did not match the request; an operator must review it.",
                         refundId=str(r.pk))
    raise ApiProblem(504, "outcome_unknown", "The refund could not be confirmed; repeat the request with the same "
                                             "idempotency key.", refundId=str(r.pk), outcomeUnknown=True)


# --- saved cards --------------------------------------------------------------------------------------

def _fingerprint(card):
    return hmac.new(settings.SECRET_KEY.encode(), f"{card.number}|{card.expiry}".encode(), hashlib.sha256).hexdigest()


def save_card(user, card):
    prefix = install_prefix()
    fingerprint = _fingerprint(card)
    generation = SavedCard.objects.filter(user=user, fingerprint=fingerprint, status=SavedCard.DELETED).count()
    reference = f"{prefix}:u{user.pk}:card:{fingerprint[:24]}:{generation}"

    existing = SavedCard.objects.filter(reference=reference).first()
    if existing is not None:
        if existing.status != SavedCard.ACTIVE:
            raise ApiProblem(409, "card_being_removed", "This card is being removed; try again shortly.")
        return Outcome(200, existing)

    customer_id = (SavedCard.objects.filter(user=user).exclude(vault_customer_id="")
                   .values_list("vault_customer_id", flat=True).first())
    result = safe_write(
        store, reference, "vault",
        send=lambda key: ops.vault_card(client(), key, card, merchant_customer_id=f"{prefix}-u{user.pk}",
                                        customer_id=customer_id),
        read=ops.read_vault,
        outcome_of=vault_outcome,
        resend_window=ops.REQUEST_ID_RETENTION["vault"],
    )
    if result.outcome == "sending":
        raise ApiProblem(409, "in_progress", "This card is already being saved.")
    if result.outcome != "done":
        raise ApiProblem(504, "outcome_unknown", "Saving the card could not be confirmed; repeat the request.",
                         outcomeUnknown=True)
    token = result.payload
    if token is None and result.record.provider_id:
        token = ops.get_token(client(), result.record.provider_id)
    vaulted = ops.vaulted_card_of(token) if token is not None else None
    if vaulted is None:
        raise ApiProblem(504, "outcome_unknown", "PayPal's answer did not describe the saved card.",
                         outcomeUnknown=True)
    saved, created = SavedCard.objects.get_or_create(
        reference=reference,
        defaults=dict(user=user, paypal_token_id=vaulted.token_id, vault_customer_id=vaulted.customer_id or "",
                      brand=vaulted.brand, last_digits=vaulted.last_digits, expiry=vaulted.expiry,
                      fingerprint=fingerprint),
    )
    return Outcome(201 if created else 200, saved)


def list_cards(user):
    return SavedCard.objects.filter(user=user, status=SavedCard.ACTIVE)


def delete_card(user, card_id):
    card = SavedCard.objects.filter(pk=card_id, user=user).exclude(status=SavedCard.DELETED).first()
    if card is None:
        raise ApiProblem(404, "payment_method_not_found", "No such saved card.")
    # From here on the card is hidden and unusable, whatever PayPal says.
    SavedCard.objects.filter(pk=card.pk, status=SavedCard.ACTIVE).update(status=SavedCard.DELETING)
    try:
        ops.delete_token(client(), card.paypal_token_id)
    except ProviderError as exc:
        if exc.outcome_unknown or exc.status_code >= 500:
            raise ApiProblem(504, "outcome_unknown", "PayPal did not confirm the removal; repeat the request.",
                             outcomeUnknown=True) from exc
        SavedCard.objects.filter(pk=card.pk, status=SavedCard.DELETING).update(status=SavedCard.ACTIVE)
        raise
    except NEVER_SENT as exc:
        SavedCard.objects.filter(pk=card.pk, status=SavedCard.DELETING).update(status=SavedCard.ACTIVE)
        raise ProviderError(502, "paypal_unreachable", "PayPal could not be reached; the card was not removed.") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise ApiProblem(504, "outcome_unknown", "PayPal did not confirm the removal; repeat the request.",
                         outcomeUnknown=True) from exc
    SavedCard.objects.filter(pk=card.pk).update(status=SavedCard.DELETED, deleted_at=timezone.now())


# --- reconciliation -----------------------------------------------------------------------------------

def reconcile(start, end):
    """Line PayPal's own transaction records for [start, end] up against this app's captures and refunds.

    Both sides are filtered on PayPal's clock: local records by the provider time PayPal reported.
    """
    prefix = install_prefix()
    provider: dict[str, list] = {}
    last_refreshed = None
    pages = 0
    for page in ops.search_transactions(client(), start, end):
        pages += 1
        if page.last_refreshed and (last_refreshed is None or page.last_refreshed < last_refreshed):
            last_refreshed = page.last_refreshed
        for info in page.transactions:
            tid = ops._str(info.transaction_id)
            if tid:
                provider.setdefault(tid, []).append(info)

    expected = []  # (transaction id, kind, order number, amount, currency, provider time)
    for p in PayPalPayment.objects.filter(captured_at__gte=start, captured_at__lte=end).exclude(capture_id="") \
            .select_related("order"):
        expected.append((p.capture_id, "capture", p.order.number, p.captured_amount, p.currency, p.captured_at))
    for r in PayPalRefund.objects.filter(provider_time__gte=start, provider_time__lte=end,
                                         status__in=("done", "pending")).exclude(paypal_refund_id="") \
            .select_related("payment__order"):
        expected.append((r.paypal_refund_id, "refund", r.payment.order.number, r.amount, r.currency, r.provider_time))

    matched, local_only, not_yet_reported = [], [], []
    for tid, kind, number, amount, cur, when in expected:
        records = provider.pop(tid, [])
        entry = {"transactionId": tid, "kind": kind, "orderId": number, "amount": str(amount), "currency": cur,
                 "time": when.isoformat()}
        if records:
            reported = records[0].transaction_amount
            reported_value = getattr(reported, "value", None)
            entry["paypalAmount"] = reported_value
            entry["paypalStatus"] = ops._str(records[0].transaction_status)
            entry["amountMatches"] = (reported_value is not None and abs(Decimal(reported_value)) == amount)
            matched.append(entry)
        elif last_refreshed is not None and when > last_refreshed:
            not_yet_reported.append(entry)
        else:
            local_only.append(entry)

    provider_only = []
    for tid, records in provider.items():
        for info in records:
            custom = ops._str(info.custom_field) or ""
            amount = info.transaction_amount
            provider_only.append({
                "transactionId": tid,
                "eventCode": ops._str(info.transaction_event_code),
                "status": ops._str(info.transaction_status),
                "time": ops._str(info.transaction_initiation_date),
                "amount": getattr(amount, "value", None),
                "currency": getattr(amount, "currency_code", None),
                "customId": custom or None,
                "invoiceId": ops._str(info.invoice_id),
                "attributedToThisShop": custom.startswith(f"{prefix}-"),
            })

    unsettled = [
        {"reference": w.reference, "operation": w.operation, "outcome": w.outcome, "claimedAt": w.claimed_at.isoformat(),
         "paypalId": w.provider_id or None}
        for w in ProviderWrite.objects.filter(outcome__in=("sending", "unknown", "needs_review", "pending"),
                                              claimed_at__gte=start, claimed_at__lte=end)
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "paypalPagesRead": pages,
        "paypalLastRefreshed": last_refreshed.isoformat() if last_refreshed else None,
        "summary": {
            "matched": len(matched),
            "localOnly": len(local_only),
            "providerOnly": len(provider_only),
            "providerOnlyForThisShop": sum(1 for x in provider_only if x["attributedToThisShop"]),
            "notYetReported": len(not_yet_reported),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "localOnly": local_only,
        "providerOnly": provider_only,
        "notYetReported": not_yet_reported,
        "unsettled": unsettled,
    }
