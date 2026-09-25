"""Payment flows: place → pay (authorize) → fulfil (capture) / cancel (void) → refund; saved cards; reconciliation.

Every provider write goes through ``safe_write.run`` with a ref derived from the order (or user) and the step.
Domain state changes happen in the write's ``on_complete`` — in the same DB transaction as the outcome.
"""

import calendar
import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from oscar.core import prices
from oscar.core.loading import get_class, get_model

from . import money
from . import paypal_gateway as gw
from . import safe_write
from .models import PaymentOperation, PayPalCustomer, PayPalPayment

log = logging.getLogger(__name__)

Basket = get_model("basket", "Basket")
Order = get_model("order", "Order")
ShippingAddress = get_model("order", "ShippingAddress")
Country = get_model("address", "Country")
Product = get_model("catalogue", "Product")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Bankcard = get_model("payment", "Bankcard")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
ShippingRepository = get_class("shipping.repository", "Repository")
Selector = get_class("partner.strategy", "Selector")

# Order statuses (sandbox settings extend OSCAR_ORDER_STATUS_PIPELINE with these).
STATUS_AWAITING_PAYMENT = "Awaiting payment"
STATUS_AUTHORISED = "Payment authorised"
STATUS_FULFILLED = "Complete"
STATUS_CANCELLED = "Cancelled"

# PayPal-Request-Id retention (operation docstrings): payments ops 45 days, vault 3 hours. Margins kept.
PAYMENTS_KEY_WINDOW = timedelta(days=44)
VAULT_KEY_WINDOW = timedelta(hours=2, minutes=45)

_UNRESOLVED = (PaymentOperation.SENDING, PaymentOperation.UNKNOWN, PaymentOperation.NEEDS_REVIEW)


class ApiProblem(Exception):
    """A request this app refuses on its own terms (validation, ownership, state)."""

    def __init__(self, status_code, code, message, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


def config() -> gw.PayPalConfig:
    return gw.PayPalConfig.from_values(
        client_id=settings.PAYPAL_CLIENT_ID,
        client_secret=settings.PAYPAL_CLIENT_SECRET,
        environment=settings.PAYPAL_ENVIRONMENT,
        currency=settings.PAYPAL_CURRENCY,
        base_url=settings.PAYPAL_BASE_URL,
        timeout=settings.PAYPAL_TIMEOUT_SECONDS,
    )


def currency() -> str:
    cur = (settings.PAYPAL_CURRENCY or "").strip().upper()
    if not cur:
        raise gw.ConfigurationError("PayPal is not configured: set PAYPAL_CURRENCY")
    return cur


def prefix() -> str:
    return settings.PAYMENTS_REFERENCE_PREFIX


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:40]


def _transitioned(op):
    """True when this completion changed the outcome (on_complete also runs on a re-check)."""
    return op.previous_outcome != op.outcome


# --------------------------------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderItem:
    product_id: int
    quantity: int


def place_order(user, items, shipping_address=None, idempotency_key=None):
    """Create an Oscar order (status ``Awaiting payment``) from catalogue ids and quantities."""
    if not items:
        raise ApiProblem(400, "invalid_request", "An order needs at least one item.")
    cur = currency()
    ref = f"{prefix()}:user:{user.pk}:place:{_digest(idempotency_key)}" if idempotency_key else None
    if ref:
        existing = PaymentOperation.objects.filter(ref=ref).select_related("order").first()
        if existing is not None:
            return existing.order, False

    try:
        with transaction.atomic():
            if ref:
                claim = PaymentOperation.objects.create(
                    ref=ref, kind=PaymentOperation.PLACE_ORDER, user=user, claimed_at=timezone.now()
                )
            order = _create_order(user, items, shipping_address, cur)
            if ref:
                claim.order = order
                claim.outcome = PaymentOperation.DONE
                claim.save(update_fields=["order", "outcome", "updated_at"])
    except IntegrityError:
        if not ref:
            raise
        existing = PaymentOperation.objects.select_related("order").get(ref=ref)
        return existing.order, False
    return order, True


def _create_order(user, items, shipping_address, cur):
    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(user=user)
    for item in items:
        product = Product.objects.filter(pk=item.product_id, is_public=True).first()
        if product is None or product.is_parent:
            raise ApiProblem(404, "item_not_found", f"Catalogue item {item.product_id} does not exist.")
        info = basket.strategy.fetch_for_product(product)
        if info.price is None or not info.price.exists:
            raise ApiProblem(409, "item_unavailable", f"Catalogue item {item.product_id} has no price.")
        permitted, reason = info.availability.is_purchase_permitted(item.quantity)
        if not permitted:
            raise ApiProblem(409, "item_unavailable", f"Catalogue item {item.product_id}: {reason}")
        basket.add_product(product, item.quantity)

    address = _shipping_address(shipping_address) if shipping_address else None
    method = ShippingRepository().get_default_shipping_method(basket=basket, shipping_addr=address, user=user)
    shipping_charge = method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)
    if total.incl_tax is None:
        raise ApiProblem(409, "tax_unknown", "The order total cannot be calculated.")
    try:
        money.to_wire(total.incl_tax, cur)  # the held amount must equal the total exactly
    except money.AmountError as e:
        raise ApiProblem(409, "amount_not_representable", str(e)) from e
    total = prices.Price(currency=cur, excl_tax=total.excl_tax, incl_tax=total.incl_tax)

    basket.freeze()
    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=method,
        shipping_charge=shipping_charge,
        user=user,
        shipping_address=address,
        order_number=f"P{basket.id:07d}",
        status=STATUS_AWAITING_PAYMENT,
    )
    basket.submit()
    source_type, _ = SourceType.objects.get_or_create(code="paypal", defaults={"name": "PayPal"})
    source = Source.objects.create(
        order=order, source_type=source_type, currency=cur, amount_allocated=Decimal("0.00"), label="PayPal card"
    )
    PayPalPayment.objects.create(order=order, source=source, currency=cur, amount=order.total_incl_tax)
    return order


def _shipping_address(data):
    country = Country.objects.filter(iso_3166_1_a2=(data.get("country") or "").upper()).first()
    if country is None:
        raise ApiProblem(400, "invalid_address", "shippingAddress.country must be an ISO 3166-1 alpha-2 code.")
    if not data.get("line1"):
        raise ApiProblem(400, "invalid_address", "shippingAddress.line1 is required.")
    address = ShippingAddress(
        first_name=data.get("firstName", ""),
        last_name=data.get("lastName", ""),
        line1=data["line1"],
        line2=data.get("line2", ""),
        line4=data.get("city", ""),
        state=data.get("state", ""),
        postcode=data.get("postcode", ""),
        country=country,
    )
    address.save()
    return address


def order_for(user, number, *, staff=False):
    qs = Order.objects.select_related("paypal_payment")
    order = qs.filter(number=number).first() if staff else qs.filter(number=number, user=user).first()
    if order is None or not hasattr(order, "paypal_payment"):
        raise ApiProblem(404, "order_not_found", "Order not found.")
    return order


def _payment(order):
    return PayPalPayment.objects.select_related("order", "source").get(pk=order.paypal_payment.pk)


def _set_order_status(order, status):
    """Move the Oscar order along its pipeline; an order changed elsewhere (e.g. the dashboard) is left as is."""
    if order.status == status:
        return True
    if status not in order.available_statuses():
        log.warning("order %s is %r; not moving it to %r", order.number, order.status, status)
        return False
    order.set_status(status)
    return True


def _transition(payment, from_state, to_state):
    """The order-level mutex: a conditional UPDATE, so only one request moves the payment on."""
    moved = PayPalPayment.objects.filter(pk=payment.pk, lifecycle=from_state).update(
        lifecycle=to_state, updated_at=timezone.now()
    )
    payment.refresh_from_db()
    return moved == 1


# --------------------------------------------------------------------------------------------------
# Pay — authorize the order total (single-step card order with intent AUTHORIZE)
# --------------------------------------------------------------------------------------------------


def _on_authorized(payment_pk, bankcard_pk=None):
    def on_complete(op, answer):
        payment = PayPalPayment.objects.select_for_update().select_related("order", "source").get(pk=payment_pk)
        detail = op.detail
        if detail.get("paypal_order_id"):
            payment.paypal_order_id = detail["paypal_order_id"]
        card = detail.get("card") or {}
        payment.card_brand = card.get("brand") or payment.card_brand
        payment.card_last_digits = card.get("last_digits") or payment.card_last_digits
        if bankcard_pk:
            payment.bankcard_id = bankcard_pk
        if op.outcome == PaymentOperation.DONE and _transitioned(op):
            if payment.lifecycle == PayPalPayment.AUTHORIZING:
                payment.authorization_id = op.provider_id
                payment.authorization_status = op.provider_status
                payment.authorized_at = op.provider_time
                payment.authorization_expires_at = gw.parse_time(detail.get("expiration_time"))
                payment.lifecycle = PayPalPayment.AUTHORIZED  # money is held: record it whatever the order says
                payment.save()
                payment.source.allocate(payment.amount, reference=op.provider_id, status=op.provider_status)
                _set_order_status(payment.order, STATUS_AUTHORISED)
                return
            if op.provider_id != payment.authorization_id:
                # A hold that landed after the order moved on (e.g. a late lookup): never drop it silently.
                op.outcome = PaymentOperation.NEEDS_REVIEW
                op.detail = {**op.detail, "orphan_hold": True, "lifecycle_at_landing": payment.lifecycle}
                op.save(update_fields=["outcome", "detail", "updated_at"])
                log.warning("orphan PayPal authorization %s for order %s", op.provider_id, payment.order.number)
        elif op.outcome == PaymentOperation.FAILED and payment.lifecycle == PayPalPayment.AUTHORIZING:
            payment.lifecycle = PayPalPayment.AWAITING_PAYMENT  # declined/refused: the shopper may try again
            if op.provider_id:
                payment.authorization_status = op.provider_status
        payment.save()

    return on_complete


@sensitive_variables()
def pay(order, user, *, card=None, bankcard_id=None):
    """Authorize the order total on a one-off card or a saved card. Returns the create-step operation."""
    payment = _payment(order)
    cur = payment.currency
    if payment.lifecycle == PayPalPayment.CANCELLED or payment.lifecycle == PayPalPayment.VOIDED:
        raise ApiProblem(409, "order_cancelled", "This order has been cancelled.")
    last = payment.operations.filter(kind=PaymentOperation.CREATE_ORDER).order_by("-pk").first()
    if payment.lifecycle not in (PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZING):
        return last  # already authorized (or further): the repeat answers from the stored outcome

    unresolved = last is not None and last.outcome != PaymentOperation.FAILED
    bankcard = None
    if not unresolved:  # a new attempt: validate everything before taking the order's mutex
        if order.status != STATUS_AWAITING_PAYMENT:  # e.g. cancelled from Oscar's dashboard meanwhile
            raise ApiProblem(409, "order_not_payable", f"The order is {order.status!r}; it cannot be paid.")
        if bankcard_id is not None:
            bankcard = Bankcard.objects.filter(pk=bankcard_id, user=user).exclude(partner_reference="").first()
            if bankcard is None:
                raise ApiProblem(404, "payment_method_not_found", "Saved card not found.")
        elif card is None:
            raise ApiProblem(400, "invalid_request", "Provide card details or a paymentMethodId.")

    if not unresolved:
        # Only the request that wins the order's mutex may start an attempt; every other one answers
        # "in progress". The winner re-reads: an attempt that appeared meanwhile is re-checked, not repeated.
        if not _start_attempt(payment):
            latest = payment.operations.filter(kind=PaymentOperation.CREATE_ORDER).order_by("-pk").first()
            return latest if latest is not None and latest.outcome != PaymentOperation.FAILED else None
        last = payment.operations.filter(kind=PaymentOperation.CREATE_ORDER).order_by("-pk").first()
        unresolved = last is not None and last.outcome != PaymentOperation.FAILED

    if unresolved:
        ref = last.ref  # a repeat of an unresolved attempt: re-check it, never start another
    else:
        # Attempt n: one more than every attempt so far (all failed, all settled while we hold the mutex).
        attempt = payment.operations.filter(kind=PaymentOperation.CREATE_ORDER).count() + 1
        ref = f"{prefix()}:{order.number}:pay:{attempt}"

    attempt_no = ref.rsplit(":", 1)[1]
    invoice_id = f"{prefix()}-{order.number}-{attempt_no}"
    body = None
    if not unresolved:
        body = gw.checkout_order_request(
            amount=money.to_wire(payment.amount, cur),
            currency=cur,
            invoice_id=invoice_id,
            custom_id=f"{prefix()}-{order.number}",
            reference_id=order.number,
            description=f"Order {order.number}",
            source=gw.card_source(card, bankcard.partner_reference if bankcard else None),
        )
    claimed_at = last.claimed_at if unresolved else timezone.now()

    def send(key):
        # Never reached for an unresolved attempt: its claim is held, so run() only looks it up.
        assert body is not None
        return gw.send_create_order(body, key)

    op = safe_write.run(
        safe_write.Write(
            ref=ref,
            kind=PaymentOperation.CREATE_ORDER,
            send=send,
            read=gw.read_checkout_order,
            find=lambda: gw.find_authorization_by_invoice(invoice_id, claimed_at),
            repeat_is_safe=False,  # a same-key resend is refused by PayPal; only the lookup is safe
            # Reporting is complete after at most 3 hours (search_transactions docstring); margin doubled.
            absent_after=2 * gw.REPORTING_LAG,
            sent=(payment.amount, cur),
            already_landed=gw.has_issue("DUPLICATE_INVOICE_ID"),
            claim_fields={"user": user, "order": order, "payment": payment, "detail": {"invoice_id": invoice_id}},
            on_complete=_on_authorized(payment.pk, bankcard.pk if bankcard else None),
        )
    )
    if op.outcome == PaymentOperation.PENDING and op.detail.get("paypal_order_status") == "APPROVED":
        op = _authorize_approved(payment, op)
    return op


def _start_attempt(payment):
    """Win the right to start a new authorization attempt (awaiting → authorizing, a conditional UPDATE).

    An ``authorizing`` payment with no unresolved attempt can only be one whose process died between
    taking the mutex and writing its claim; it is re-taken once it has been idle longer than any send.
    """
    if _transition(payment, PayPalPayment.AWAITING_PAYMENT, PayPalPayment.AUTHORIZING):
        return True
    return _retake_idle(payment, PayPalPayment.AUTHORIZING)


def _authorize_approved(payment, create_op):
    """Second step, only when PayPal answered APPROVED instead of authorizing in one step."""
    paypal_order_id = create_op.detail["paypal_order_id"]
    authorized = _on_authorized(payment.pk)

    def on_complete(op, answer):
        authorized(op, answer)
        if op.outcome in (PaymentOperation.DONE, PaymentOperation.FAILED):
            # The attempt is the pair: settle its first step too, so a repeat answers from it and a
            # failed attempt lets the shopper try another card.
            PaymentOperation.objects.filter(pk=create_op.pk, outcome=PaymentOperation.PENDING).update(
                outcome=op.outcome, updated_at=timezone.now()
            )

    return safe_write.run(
        safe_write.Write(
            ref=f"{create_op.ref}:authorize",
            kind=PaymentOperation.AUTHORIZE,
            send=lambda key: gw.send_authorize_order(paypal_order_id, key),
            read=gw.read_checkout_order,
            find=lambda: _authorized_or_none(gw.get_checkout_order(paypal_order_id)),
            sent=(payment.amount, payment.currency),
            claim_fields={"user": create_op.user, "order": payment.order, "payment": payment},
            on_complete=on_complete,
        )
    )


def _authorized_or_none(order):
    return order if gw.first_authorization(order) is not None else None


# --------------------------------------------------------------------------------------------------
# Fulfil — capture (renewing a stale authorization first)
# --------------------------------------------------------------------------------------------------


class AuthorizationNotRenewable(ApiProblem):
    pass


def _honor_period():
    return timedelta(days=settings.PAYMENTS_AUTH_HONOR_PERIOD_DAYS)


def _on_reauthorized(payment_pk):
    def on_complete(op, answer):
        if op.outcome != PaymentOperation.DONE or not _transitioned(op):
            return
        payment = PayPalPayment.objects.select_for_update().select_related("source").get(pk=payment_pk)
        if payment.lifecycle != PayPalPayment.CAPTURING:
            # Renewed after fulfilment gave up on it (e.g. the order was cancelled): a hold to review.
            op.outcome = PaymentOperation.NEEDS_REVIEW
            op.detail = {**op.detail, "orphan_hold": True, "lifecycle_at_landing": payment.lifecycle}
            op.save(update_fields=["outcome", "detail", "updated_at"])
            return
        payment.authorization_id = op.provider_id
        payment.authorization_status = op.provider_status
        payment.authorized_at = op.provider_time or timezone.now()
        payment.authorization_expires_at = gw.parse_time(op.detail.get("expiration_time"))
        payment.reauthorized = True
        payment.save()
        payment.source.transactions.create(
            txn_type="Reauthorise", amount=payment.amount, reference=op.provider_id, status=op.provider_status
        )

    return on_complete


def _on_captured(payment_pk):
    def on_complete(op, answer):
        payment = PayPalPayment.objects.select_for_update().select_related("order", "source").get(pk=payment_pk)
        if op.provider_id:
            payment.capture_id = op.provider_id
            payment.capture_status = op.provider_status
        if op.outcome == PaymentOperation.DONE and _transitioned(op):
            payment.captured_amount = op.provider_amount
            payment.paypal_fee = Decimal(op.detail["fee"]) if op.detail.get("fee") else None
            payment.net_amount = Decimal(op.detail["net"]) if op.detail.get("net") else None
            payment.captured_at = op.provider_time or timezone.now()
            payment.lifecycle = PayPalPayment.CAPTURED
            payment.save()
            payment.source.debit(op.provider_amount, reference=op.provider_id, status=op.provider_status)
            _set_order_status(payment.order, STATUS_FULFILLED)
            return
        if op.outcome == PaymentOperation.FAILED and payment.lifecycle == PayPalPayment.CAPTURING:
            payment.lifecycle = PayPalPayment.AUTHORIZED  # nothing taken: fulfilment may be retried
        payment.save()

    return on_complete


def _capture_write(payment, authorization_id):
    amount = money.to_wire(payment.amount, payment.currency)
    return safe_write.Write(
        ref=f"{prefix()}:{payment.order.number}:capture:{authorization_id}",
        kind=PaymentOperation.CAPTURE,
        send=lambda key: gw.send_capture(authorization_id, amount, payment.currency, key),
        read=gw.read_capture,
        repeat_is_safe=True,  # PayPal returns the original capture for a repeated PayPal-Request-Id
        resend_window=PAYMENTS_KEY_WINDOW,
        sent=(payment.amount, payment.currency),
        claim_fields={"order": payment.order, "payment": payment, "detail": {"authorization_id": authorization_id}},
        on_complete=_on_captured(payment.pk),
    )


def _refresh_pending(write_factory, op, fetch, read):
    """A pending capture/refund: re-read it (a read, not a write) and settle the record from PayPal's word."""
    if op.outcome != PaymentOperation.PENDING or not op.provider_id:
        return op
    try:
        answer = read(fetch(op.provider_id))
    except Exception as e:  # noqa: BLE001 — a failed refresh leaves the record pending, as it was
        gw.log.warning("refresh of %s failed: %s", op.ref, type(e).__name__)
        return op
    if answer.outcome == op.outcome and answer.provider_status == op.provider_status:
        return op
    return safe_write.complete(write_factory(), answer.outcome, answer)


def fulfil(order):
    """Operator action: mark the order fulfilled, which is when the money is taken."""
    payment = _payment(order)
    if payment.lifecycle == PayPalPayment.CAPTURED:
        return payment
    if payment.lifecycle == PayPalPayment.CAPTURING:
        op = PaymentOperation.objects.filter(ref=_capture_write(payment, payment.authorization_id).ref).first()
        if op is not None:
            write = _capture_write(payment, payment.authorization_id)
            if op.outcome == PaymentOperation.PENDING:
                _refresh_pending(lambda: write, op, gw.get_capture, gw.read_capture)
            elif op.outcome in (PaymentOperation.SENDING, PaymentOperation.UNKNOWN):
                safe_write.run(write)  # the claim exists: this re-checks under the same key
            return _payment(order)
        # No capture yet: a renewal is unresolved, or the request holding the mutex died. Resume once idle.
        if not _retake_idle(payment, PayPalPayment.CAPTURING):
            return payment  # another operator request is fulfilling it right now
    elif payment.lifecycle != PayPalPayment.AUTHORIZED:
        raise ApiProblem(
            409, "not_fulfillable", f"Order cannot be fulfilled while its payment is {payment.lifecycle}."
        )
    elif not _transition(payment, PayPalPayment.AUTHORIZED, PayPalPayment.CAPTURING):
        return payment  # another operator request is already fulfilling it

    try:
        renewal_refused = _renew_if_stale(payment)
        payment.refresh_from_db()
        try:
            op = safe_write.run(_capture_write(payment, payment.authorization_id))
        except gw.ProviderRejected as e:
            if renewal_refused is not None or _is_stale(payment):
                raise _not_renewable(payment, renewal_refused, e) from e
            raise
    except (ApiProblem, gw.ProviderError):
        _release_unless_captured(payment)
        raise
    if op.outcome == PaymentOperation.FAILED:
        raise ApiProblem(
            402,
            "capture_declined",
            f"PayPal declined the capture ({op.provider_status or 'refused'}); no money was taken. "
            "Cancel the order to release the hold.",
        )
    return _payment(order)


def _release_unless_captured(payment):
    """Undo the CAPTURING mutex only when neither a capture nor a renewal can be in doubt."""
    payment.refresh_from_db()
    capture = PaymentOperation.objects.filter(ref=_capture_write(payment, payment.authorization_id).ref).first()
    renewal = PaymentOperation.objects.filter(ref=_reauth_ref(payment, payment.authorization_id)).first()
    renewal_in_doubt = renewal is not None and renewal.outcome not in (PaymentOperation.DONE, PaymentOperation.FAILED)
    capture_in_doubt = capture is not None and not (
        capture.outcome == PaymentOperation.FAILED and not capture.provider_id
    )
    if not renewal_in_doubt and not capture_in_doubt:
        _transition(payment, PayPalPayment.CAPTURING, PayPalPayment.AUTHORIZED)


def _reauth_ref(payment, authorization_id):
    return f"{prefix()}:{payment.order.number}:reauthorize:{authorization_id}"


def _retake_idle(payment, lifecycle):
    """Re-take an order-level mutex whose holder has been idle longer than any send (it died)."""
    now = timezone.now()
    moved = PayPalPayment.objects.filter(
        pk=payment.pk, lifecycle=lifecycle, updated_at__lt=now - safe_write.SEND_WINDOW
    ).update(updated_at=now)
    payment.refresh_from_db()
    return moved == 1


def _is_stale(payment):
    return payment.authorized_at is not None and timezone.now() - payment.authorized_at >= _honor_period()


def _renew_if_stale(payment):
    """Reauthorize a stale hold before capturing it. Returns PayPal's refusal, if renewal was refused."""
    now = timezone.now()
    if payment.authorization_expires_at and now >= payment.authorization_expires_at:
        raise _not_renewable(payment, None, None)
    if not _is_stale(payment) or payment.reauthorized:
        return None
    amount = money.to_wire(payment.amount, payment.currency)
    authorization_id = payment.authorization_id

    def read(r):
        return gw.read_authorization(r, gw.authorization_outcome)

    write = safe_write.Write(
        ref=_reauth_ref(payment, authorization_id),
        kind=PaymentOperation.REAUTHORIZE,
        send=lambda key: gw.send_reauthorize(authorization_id, amount, payment.currency, key),
        read=read,
        repeat_is_safe=True,
        resend_window=PAYMENTS_KEY_WINDOW,
        sent=(payment.amount, payment.currency),
        claim_fields={"order": payment.order, "payment": payment},
        on_complete=_on_reauthorized(payment.pk),
    )
    try:
        op = safe_write.run(write)
    except gw.ProviderRejected as e:
        return e  # e.g. REAUTHORIZATION_TOO_SOON / already reauthorized: try the existing hold
    op = _refresh_pending(lambda: write, op, gw.get_authorization, read)  # a pending renewal: re-read it
    if op.outcome == PaymentOperation.DONE:
        return None
    if op.outcome in (PaymentOperation.PENDING, PaymentOperation.SENDING):
        raise ApiProblem(
            409, "reauthorization_pending", "PayPal is still renewing the authorization; retry fulfilment shortly."
        )
    if op.outcome == PaymentOperation.FAILED:
        return gw.ProviderRejected(422, "reauthorization_failed", f"PayPal reported {op.provider_status}.")
    raise gw.OutcomeUnknown(op.ref)


def _not_renewable(payment, renewal_error, capture_error):
    expires = payment.authorization_expires_at
    reasons = []
    if expires and timezone.now() >= expires:
        reasons.append(f"the authorization expired on {expires.isoformat()}")
    if renewal_error is not None:
        reasons.append(f"PayPal refused to renew it ({', '.join(renewal_error.issues) or renewal_error.message})")
    if capture_error is not None:
        reasons.append(f"PayPal refused the capture ({', '.join(capture_error.issues) or capture_error.message})")
    return AuthorizationNotRenewable(
        409,
        "authorization_not_renewable",
        f"The payment hold for order {payment.order.number} (authorization {payment.authorization_id}, "
        f"placed {payment.authorized_at.isoformat() if payment.authorized_at else 'unknown'}) is stale and can no "
        f"longer be renewed: {'; '.join(reasons) or 'unknown reason'}. No money was taken. Cancel the order to "
        f"release the hold, then ask the shopper to place and pay for the order again.",
        authorizationId=payment.authorization_id,
        authorizationExpiresAt=expires.isoformat() if expires else None,
    )


# --------------------------------------------------------------------------------------------------
# Cancel — void the hold before fulfilment
# --------------------------------------------------------------------------------------------------


def _on_voided(payment_pk):
    def on_complete(op, answer):
        payment = PayPalPayment.objects.select_for_update().select_related("order", "source").get(pk=payment_pk)
        if op.provider_status:
            payment.authorization_status = op.provider_status
        if op.outcome == PaymentOperation.DONE and _transitioned(op):
            payment.lifecycle = PayPalPayment.VOIDED
            payment.save()
            source = payment.source
            if source.amount_allocated >= payment.amount:  # a needs-review hold was never allocated
                source.amount_allocated -= payment.amount
                source.save()
            source.transactions.create(
                txn_type="Void", amount=payment.amount, reference=payment.authorization_id, status=op.provider_status
            )
            _set_order_status(payment.order, STATUS_CANCELLED)
            return
        payment.save()

    return on_complete


def _void_write(payment):
    authorization_id = payment.authorization_id
    return safe_write.Write(
        ref=f"{prefix()}:{payment.order.number}:void:{authorization_id}",
        kind=PaymentOperation.VOID,
        send=lambda key: gw.send_void(authorization_id, key),
        read=lambda r: gw.read_authorization(r, gw.void_outcome),
        find=lambda: gw.get_authorization(authorization_id),
        repeat_is_safe=True,
        resend_window=PAYMENTS_KEY_WINDOW,
        already_landed=gw.has_issue("PREVIOUSLY_VOIDED"),
        claim_fields={"order": payment.order, "payment": payment},
        on_complete=_on_voided(payment.pk),
    )


def cancel(order):
    """Operator action: cancel before fulfilment; any held funds are released."""
    payment = _payment(order)
    if payment.lifecycle in (PayPalPayment.CANCELLED, PayPalPayment.VOIDED):
        return payment
    if payment.lifecycle in (PayPalPayment.CAPTURING, PayPalPayment.CAPTURED):
        raise ApiProblem(409, "already_fulfilled", "The payment has been captured; issue a refund instead.")
    if payment.lifecycle == PayPalPayment.AWAITING_PAYMENT:
        if not _transition(payment, PayPalPayment.AWAITING_PAYMENT, PayPalPayment.CANCELLED):
            return cancel(order)
        _set_order_status(order, STATUS_CANCELLED)
        return _payment(order)
    if payment.lifecycle == PayPalPayment.AUTHORIZING:
        last = payment.operations.filter(kind=PaymentOperation.CREATE_ORDER).order_by("-pk").first()
        if last is not None and last.outcome == PaymentOperation.PENDING and last.detail.get("payer_action_required"):
            # Nothing was ever held: the shopper never completed PayPal's challenge.
            if _transition(payment, PayPalPayment.AUTHORIZING, PayPalPayment.CANCELLED):
                _set_order_status(order, STATUS_CANCELLED)
            return _payment(order)
        if last is not None and last.outcome == PaymentOperation.NEEDS_REVIEW and last.provider_id:
            # A hold PayPal placed for an amount other than the total: release it.
            PayPalPayment.objects.filter(pk=payment.pk, lifecycle=PayPalPayment.AUTHORIZING).update(
                authorization_id=last.provider_id
            )
            if _transition(payment, PayPalPayment.AUTHORIZING, PayPalPayment.VOIDING):
                return _void(order, payment)
            return cancel(order)
        raise ApiProblem(
            409,
            "payment_unresolved",
            "A payment attempt for this order is still unresolved; repeat the pay request to re-check it, "
            "or see the reconciliation report.",
        )
    if payment.lifecycle == PayPalPayment.VOIDING:
        write = _void_write(payment)
        op = PaymentOperation.objects.filter(ref=write.ref).first()
        if op is not None and op.outcome == PaymentOperation.PENDING:
            _refresh_pending(lambda: write, op, gw.get_authorization, write.read)
        else:
            safe_write.run(write)  # re-check an earlier void under the same key
        return _payment(order)
    if payment.authorization_expires_at and timezone.now() >= payment.authorization_expires_at:
        # PayPal lets an authorization lapse after its 29-day period: nothing is held any more.
        if _transition(payment, PayPalPayment.AUTHORIZED, PayPalPayment.CANCELLED):
            PayPalPayment.objects.filter(pk=payment.pk).update(authorization_status="EXPIRED")
            _set_order_status(order, STATUS_CANCELLED)
            return _payment(order)
        return cancel(order)
    if not _transition(payment, PayPalPayment.AUTHORIZED, PayPalPayment.VOIDING):
        return cancel(order)
    return _void(order, payment)


def _void(order, payment):
    payment.refresh_from_db()
    try:
        op = safe_write.run(_void_write(payment))
    except gw.ProviderError:
        op = payment.operations.filter(kind=PaymentOperation.VOID).order_by("-pk").first()
        if op is None or (op.outcome == PaymentOperation.FAILED and not op.provider_id):
            _transition(payment, PayPalPayment.VOIDING, PayPalPayment.AUTHORIZED)  # refused: still held
        raise
    if op.outcome == PaymentOperation.FAILED:
        raise ApiProblem(
            409,
            "void_refused",
            f"PayPal reports the authorization as {op.provider_status}; the hold cannot be released. "
            "See the reconciliation report.",
        )
    return _payment(order)


# --------------------------------------------------------------------------------------------------
# Refunds — after fulfilment, never beyond what was captured
# --------------------------------------------------------------------------------------------------

_RESERVING = (
    PaymentOperation.SENDING,
    PaymentOperation.PENDING,
    PaymentOperation.DONE,
    PaymentOperation.UNKNOWN,
    PaymentOperation.NEEDS_REVIEW,
)


def _on_refunded(payment_pk):
    def on_complete(op, answer):
        if op.outcome != PaymentOperation.DONE or not _transitioned(op):
            return
        payment = PayPalPayment.objects.select_for_update().select_related("source").get(pk=payment_pk)
        payment.refunded_amount += op.amount
        payment.save()
        payment.source.refund(op.amount, reference=op.provider_id, status=op.provider_status)

    return on_complete


def _refund_write(payment, ref, *, amount=None, note=None, fingerprint="", claim=None):
    write = safe_write.Write(
        ref=ref,
        kind=PaymentOperation.REFUND,
        send=lambda key: gw.send_refund(
            payment.capture_id, money.to_wire(write.sent[0], write.sent[1]), write.sent[1], note, key
        ),
        read=gw.read_refund,
        repeat_is_safe=True,  # PayPal returns the original refund for a repeated PayPal-Request-Id
        resend_window=PAYMENTS_KEY_WINDOW,
        sent=(amount, payment.currency) if amount is not None else None,
        fingerprint=fingerprint,
        claim_fields={"order": payment.order, "payment": payment},
        claim=claim,
        on_complete=_on_refunded(payment.pk),
    )
    return write


def refund(order, user, *, idempotency_key, amount=None, note=None):
    """Refund the captured payment in full (no amount: whatever remains) or in part."""
    if not idempotency_key:
        raise ApiProblem(400, "idempotency_key_required", "Refunds need an Idempotency-Key header.")
    payment = _payment(order)
    if payment.lifecycle != PayPalPayment.CAPTURED or not payment.capture_id:
        raise ApiProblem(409, "not_refundable", "Only a fulfilled (captured) order can be refunded.")
    requested = None
    if amount is not None:
        try:
            # Normalized to the currency's scale, so "5" and "5.00" are the same request under one key.
            requested = money.parse(amount, payment.currency).quantize(money.quantum(payment.currency))
        except money.AmountError as e:
            raise ApiProblem(400, "invalid_amount", str(e)) from e
    ref = f"{prefix()}:{order.number}:refund:{_digest(idempotency_key)}"
    fingerprint = f"amount={requested if requested is not None else 'remaining'}"

    existing = PaymentOperation.objects.filter(ref=ref).first()
    if existing is not None and existing.fingerprint != fingerprint:
        raise safe_write.IdempotencyConflict()

    write = None

    def claim():
        """Ceiling check and claim (insert, or re-take a refused one) in one transaction on the payment row."""
        with transaction.atomic():
            PayPalPayment.objects.filter(pk=payment.pk).update(refund_lock=F("refund_lock") + 1)
            locked = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
            current = PaymentOperation.objects.filter(ref=ref).first()
            released = current is not None and current.outcome == PaymentOperation.FAILED and not current.provider_id
            if current is not None and not released:
                write.sent = (current.amount, current.currency)
                return False  # a repeat: answer from (or re-check) the record under this key
            reserved = PaymentOperation.objects.filter(
                payment=locked, kind=PaymentOperation.REFUND, outcome__in=_RESERVING
            ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
            # SQLite sums decimals as floats: bring the result back to the currency's exact scale.
            available = ((locked.captured_amount or Decimal("0.00")) - reserved).quantize(
                money.quantum(locked.currency)
            )
            wanted = requested if requested is not None else available
            if wanted <= 0 or wanted > available:
                raise ApiProblem(
                    422,
                    "refund_exceeds_captured",
                    f"At most {available} {locked.currency} can still be refunded.",
                    refundableAmount=str(available),
                )
            write.sent = (wanted, locked.currency)
            if released:  # never sent / refused earlier: nothing happened, so this key may try again
                PaymentOperation.objects.filter(pk=current.pk).update(
                    outcome=PaymentOperation.SENDING, amount=wanted, claimed_at=timezone.now(), detail={}
                )
                return True
            try:
                with transaction.atomic():
                    PaymentOperation.objects.create(
                        ref=ref,
                        kind=PaymentOperation.REFUND,
                        order=order,
                        payment=locked,
                        user=user,
                        amount=wanted,
                        currency=locked.currency,
                        fingerprint=fingerprint,
                        claimed_at=timezone.now(),
                        detail={"capture_id": locked.capture_id},
                    )
            except IntegrityError:  # the same key raced us: answer from (or re-check) its record
                winner = PaymentOperation.objects.get(ref=ref)
                write.sent = (winner.amount, winner.currency)
                return False
        return True

    write = _refund_write(payment, ref, note=note, fingerprint=fingerprint, claim=claim)
    op = safe_write.run(write)
    return _refresh_pending(lambda: write, op, gw.get_refund, gw.read_refund)


# --------------------------------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------------------------------


def _expiry_date(expiry):
    year, month = (int(p) for p in expiry.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1])


def _on_vaulted(user_pk):
    def on_complete(op, answer):
        if op.outcome != PaymentOperation.DONE:
            return
        detail = op.detail
        if detail.get("customer_id"):
            PayPalCustomer.objects.get_or_create(user_id=user_pk, defaults={"customer_id": detail["customer_id"]})
        if Bankcard.objects.filter(user_id=user_pk, partner_reference=op.provider_id).exists():
            return
        card = Bankcard(
            user_id=user_pk,
            name=detail.get("name") or "",
            number=f"XXXX-XXXX-XXXX-{detail['last_digits']}",  # masked: the full number is never stored
            expiry_date=_expiry_date(detail["expiry"]),
            partner_reference=op.provider_id,
        )
        card.card_type = detail.get("brand") or "Card"
        card.save()

    return on_complete


@sensitive_variables()
def save_card(user, card, idempotency_key):
    """Vault a card at PayPal for this shopper; keep only its token and a masked description."""
    if not idempotency_key:
        raise ApiProblem(400, "idempotency_key_required", "Saving a card needs an Idempotency-Key header.")
    ref = f"{prefix()}:user:{user.pk}:vault:{_digest(idempotency_key)}"
    customer = PayPalCustomer.objects.filter(user=user).first()
    body = gw.payment_token_request(card, customer.customer_id if customer else None)
    op = safe_write.run(
        safe_write.Write(
            ref=ref,
            kind=PaymentOperation.VAULT_CREATE,
            send=lambda key: gw.send_create_payment_token(body, key),
            read=gw.read_payment_token,
            repeat_is_safe=True,  # vault keys are kept 3 hours; past that the record stays unknown
            resend_window=VAULT_KEY_WINDOW,
            fingerprint=f"last4={card.last_digits};expiry={card.expiry}",
            claim_fields={"user": user},
            on_complete=_on_vaulted(user.pk),
        )
    )
    bankcard = Bankcard.objects.filter(user=user, partner_reference=op.provider_id).first() if op.provider_id else None
    return op, bankcard


def delete_card(user, bankcard_id):
    """Remove a saved card: unusable here immediately, then deleted from PayPal's vault."""
    with transaction.atomic():
        card = Bankcard.objects.select_for_update().filter(pk=bankcard_id, user=user).first()
        if card is None:
            raise ApiProblem(404, "payment_method_not_found", "Saved card not found.")
        token_id = card.partner_reference
        card.delete()
        op, _ = PaymentOperation.objects.get_or_create(
            ref=f"{prefix()}:user:{user.pk}:vault-delete:{token_id or bankcard_id}",
            defaults={
                "kind": PaymentOperation.VAULT_DELETE,
                "user": user,
                "provider_id": token_id,
                "claimed_at": timezone.now(),
            },
        )
    return delete_from_vault(op)


def delete_from_vault(op):
    if not op.provider_id:
        op.outcome = PaymentOperation.DONE
        op.save(update_fields=["outcome", "updated_at"])
        return op
    try:
        gw.delete_payment_token(op.provider_id)
        op.outcome = PaymentOperation.DONE
    except gw.ProviderError as e:
        op.outcome = PaymentOperation.UNKNOWN if e.outcome_unknown else PaymentOperation.FAILED
        op.detail = {**op.detail, "error": e.code, "issues": e.issues}
    op.save(update_fields=["outcome", "detail", "updated_at"])
    return op


# --------------------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------------------

_RECONCILED_KINDS = (
    PaymentOperation.CREATE_ORDER,
    PaymentOperation.AUTHORIZE,
    PaymentOperation.REAUTHORIZE,
    PaymentOperation.CAPTURE,
    PaymentOperation.REFUND,
)


def reconcile(start, end):
    """Line PayPal's transaction records for [start, end) up against this app's payment operations."""
    if end <= start:
        raise ApiProblem(400, "invalid_range", "'to' must be after 'from'.")
    if end - start > timedelta(days=3 * 366):
        raise ApiProblem(400, "invalid_range", "PayPal reports at most three years of transactions.")
    ours = prefix()

    # Local side, on PayPal's clock (the provider time stored when each write completed).
    local = {}
    for op in (
        PaymentOperation.objects.filter(kind__in=_RECONCILED_KINDS, provider_time__gte=start, provider_time__lt=end)
        .exclude(provider_id="")
        .select_related("order")
    ):
        if op.kind == PaymentOperation.CREATE_ORDER and op.provider_id == op.detail.get("paypal_order_id"):
            continue  # a checkout-order id (no authorization yet) is not a transaction record
        local.setdefault(op.provider_id, op)
    unsettled = PaymentOperation.objects.filter(
        kind__in=_RECONCILED_KINDS, outcome__in=_UNRESOLVED, claimed_at__gte=start, claimed_at__lt=end
    ).select_related("order")

    # Provider side: every window, every page, narrowed back to the caller's instants.
    try:
        coverage = {}
        provider = list(gw.search_transactions(start, end, meta=coverage))
    except gw.ProviderError:
        raise
    except Exception as e:  # SDK/transport failure on a read: classify it, never a bare 500
        raise gw.translate(e) from e

    # Match against the set: an order owns every record for its authorization, capture and refunds, and a
    # local record stays available to every provider record naming it (by id, or as their reference).
    matched, provider_only, seen_local = [], [], set()
    for info in provider:
        txn_id = gw.value(info.transaction_id)
        ref_id = gw.value(info.paypal_reference_id)
        op = local.get(txn_id or "")
        if op is None and _owned(info, ours):
            op = local.get(ref_id or "")
        row = _provider_row(info, ours)
        if op is None:
            provider_only.append(row)
            continue
        if op.provider_id == txn_id:
            seen_local.add(op.provider_id)  # only a record of its own id accounts for a local write
        matched.append({**row, "local": _local_row(op)})
    # Past PayPal's own reporting horizon (when it told us) or, failing that, its documented maximum lag.
    horizon = gw.parse_time(coverage.get("last_refreshed"))
    if coverage.get("unavailable_from"):
        horizon = (
            min(horizon, gw.parse_time(coverage["unavailable_from"]))
            if horizon
            else gw.parse_time(coverage["unavailable_from"])
        )
    lag_edge = horizon or timezone.now() - gw.REPORTING_LAG
    local_only = [
        {**_local_row(op), "withinReportingLag": op.provider_time is not None and op.provider_time >= lag_edge}
        for pid, op in local.items()
        if pid not in seen_local
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "summary": {
            "providerRecords": len(provider),
            "matched": len(matched),
            "providerOnly": len(provider_only),
            "providerOnlyThisInstall": sum(1 for r in provider_only if r["thisInstall"]),
            "localOnly": len(local_only),
            "unsettled": unsettled.count(),
        },
        "matched": matched,
        "providerOnly": provider_only,
        "localOnly": local_only,
        "unsettled": [_local_row(op) for op in unsettled],
        "providerDataUntil": coverage.get("last_refreshed"),
        "providerDataUnavailableFrom": coverage.get("unavailable_from"),
        "note": "PayPal's reporting lags live activity by up to three hours; records newer than that may be "
        "missing from the provider side.",
    }


def _owned(info, ours):
    return any((gw.value(v) or "").startswith(f"{ours}-") for v in (info.invoice_id, info.custom_field))


def _provider_row(info, ours):
    amount, cur = gw.money(info.transaction_amount)
    fee, _ = gw.money(info.fee_amount)
    return {
        "transactionId": gw.value(info.transaction_id),
        "referenceId": gw.value(info.paypal_reference_id),
        "eventCode": gw.value(info.transaction_event_code),
        "status": gw.value(info.transaction_status),
        "initiatedAt": gw.value(info.transaction_initiation_date),
        "amount": None if amount is None else str(amount),
        "fee": None if fee is None else str(fee),
        "currency": cur,
        "invoiceId": gw.value(info.invoice_id),
        "customId": gw.value(info.custom_field),
        "thisInstall": _owned(info, ours),
    }


def _local_row(op):
    return {
        "orderId": op.order.number if op.order_id else None,
        "kind": op.kind,
        "outcome": op.outcome,
        "paypalId": op.provider_id or None,
        "paypalStatus": op.provider_status or None,
        "amount": (
            str(op.provider_amount if op.provider_amount is not None else op.amount)
            if (op.provider_amount is not None or op.amount is not None)
            else None
        ),
        "currency": op.currency or None,
        "paypalTime": op.provider_time.isoformat() if op.provider_time else None,
        "claimedAt": op.claimed_at.isoformat(),
    }


def parse_instant(value, name):
    if not value:
        raise ApiProblem(400, "invalid_range", f"'{name}' is required (ISO-8601 date-time).")
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "+"))
    except ValueError as e:
        raise ApiProblem(400, "invalid_range", f"'{name}' is not an ISO-8601 date-time.") from e
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed
