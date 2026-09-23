"""Order + payment orchestration.

Orders reuse Oscar's ``Order``/``Line`` (built through ``OrderCreator`` from a
real Oscar basket) and Oscar's ``payment.Source``/``Transaction`` ledger. This
module adds the PayPal money movement on top and keeps the two in step, with
idempotent pay/fulfil/refund and per-order row locking so a double-click never
authorizes or captures twice.
"""

from decimal import Decimal

from django.conf import settings
from django.db import transaction

from oscar.apps.checkout.calculators import OrderTotalCalculator
from oscar.apps.partner.strategy import Selector
from oscar.apps.shipping.methods import NoShippingRequired
from oscar.core.loading import get_class, get_model

from .. import errors
from ..models import PayPalPayment, PayPalRefund
from . import gateway

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Order = get_model("order", "Order")
Source = get_model("payment", "Source")
SourceType = get_model("payment", "SourceType")
Bankcard = get_model("payment", "Bankcard")
OrderCreator = get_class("order.utils", "OrderCreator")

# Order.status values used to mirror the payment lifecycle for operators.
STATUS_AWAITING = "Awaiting payment"
STATUS_AUTHORIZED = "Payment authorized"
STATUS_FULFILLED = "Fulfilled"
STATUS_CANCELLED = "Cancelled"

# PayPal authorization statuses.
AUTH_CREATED = "CREATED"
AUTH_CAPTURED = "CAPTURED"
AUTH_VOIDED = "VOIDED"


def _source_type():
    st, _ = SourceType.objects.get_or_create(code="paypal", defaults={"name": "PayPal"})
    return st


def _set_order_status(order, status):
    """Set the Oscar order status directly.

    The payment lifecycle is tracked authoritatively on ``PayPalPayment``; the
    order status simply mirrors it for operators. We assign it directly rather
    than through ``set_status`` so this app does not have to take over the global
    ``OSCAR_ORDER_STATUS_PIPELINE`` used elsewhere in the sandbox.
    """
    order.status = status
    order.save(update_fields=["status", "date_updated"] if hasattr(order, "date_updated") else ["status"])


# ---------------------------------------------------------------------------
# Place an order (awaiting payment)
# ---------------------------------------------------------------------------

def place_order(user, items):
    """Create an Oscar order for ``user`` from ``items`` (product id + quantity).

    Returns the created :class:`PayPalPayment` (which links to the Oscar order).
    """
    if not items:
        raise errors.ApiValidationError("An order must contain at least one item.")

    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(user=user)

    for item in items:
        product_id = item.get("product_id")
        try:
            quantity = int(item.get("quantity", 1))
        except (TypeError, ValueError):
            raise errors.ApiValidationError("Quantity must be a whole number.")
        if quantity < 1:
            raise errors.ApiValidationError("Quantity must be at least 1.")
        try:
            product = Product.objects.get(id=product_id)
        except Product.DoesNotExist:
            raise errors.ApiValidationError("No product with id %s." % product_id)

        info = basket.strategy.fetch_for_product(product)
        if info.stockrecord is None:
            raise errors.ApiValidationError(
                "Product %s is not purchasable." % product_id
            )
        basket.add_product(product, quantity=quantity)

    if basket.is_empty:
        raise errors.ApiValidationError("An order must contain at least one item.")

    shipping_method = NoShippingRequired()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)

    try:
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            status=STATUS_AWAITING,
        )
    except ValueError as e:
        raise errors.ApiValidationError(str(e))

    currency = settings.PAYPAL_CURRENCY
    amount = order.total_incl_tax.quantize(Decimal("0.01"))
    payment = PayPalPayment.objects.create(
        order=order,
        state=PayPalPayment.AWAITING_PAYMENT,
        currency=currency,
        amount=amount,
    )
    return payment


# ---------------------------------------------------------------------------
# Pay (authorize) — put a hold on the money
# ---------------------------------------------------------------------------

def pay(user, order, card=None, payment_method_id=None):
    with transaction.atomic():
        payment = _locked_payment(order)

        if payment.state in (
            PayPalPayment.CAPTURED,
            PayPalPayment.PARTIALLY_REFUNDED,
            PayPalPayment.REFUNDED,
        ):
            raise errors.Conflict("This order has already been paid and fulfilled.")
        if payment.state == PayPalPayment.CANCELLED:
            raise errors.Conflict("This order has been cancelled.")

        # Idempotent: an existing live authorization is returned rather than
        # creating a second hold.
        if payment.authorization_id and payment.authorization_status != AUTH_VOIDED:
            return payment

        # invoice_id is unique per order but stable across retries (payment.pk is
        # committed at order creation); custom_id carries the order number so a
        # reconciliation match survives even if the invoice is not echoed back.
        invoice_id = "%s-%s" % (order.number, payment.pk)
        custom_id = "ORDER-%s" % order.number

        # A PayPal-Request-Id derived from the stable payment pk makes the
        # authorization idempotent: a double-click (or a retry after a rolled-back
        # transaction that left an orphaned hold) reuses the same key, so PayPal
        # collapses the duplicate instead of holding the money twice.
        request_id = "auth-%s" % payment.pk
        payment.authorize_request_id = request_id

        bankcard = None
        if payment_method_id is not None:
            bankcard = _saved_card(user, payment_method_id)
            result = gateway.authorize_with_vault(
                bankcard.partner_reference,
                payment.amount,
                payment.currency,
                invoice_id,
                custom_id,
                request_id,
            )
        elif card is not None:
            result = gateway.authorize_with_card(
                card, payment.amount, payment.currency, invoice_id, custom_id, request_id
            )
        else:
            raise errors.ApiValidationError(
                "Provide either card details or a saved paymentMethodId."
            )

        payment.paypal_order_id = result["paypal_order_id"]
        payment.authorization_id = result["authorization_id"]
        payment.authorization_status = result["authorization_status"]
        payment.authorization_expiry = result["expiration_time"]
        payment.bankcard = bankcard
        payment.state = PayPalPayment.AUTHORIZED
        payment.save()

        source = _ensure_source(payment)
        source.reference = payment.paypal_order_id
        source.amount_allocated = Decimal("0.00")
        source.save()
        source.allocate(
            payment.amount,
            reference=payment.authorization_id,
            status=payment.authorization_status,
        )

        _set_order_status(order, STATUS_AUTHORIZED)
        return payment


# ---------------------------------------------------------------------------
# Fulfil (capture) — take the money, renewing a stale authorization if needed
# ---------------------------------------------------------------------------

def fulfil(order):
    with transaction.atomic():
        payment = _locked_payment(order)

        if payment.capture_id:
            return payment  # already captured — idempotent
        if payment.state == PayPalPayment.CANCELLED:
            raise errors.Conflict("This order was cancelled and cannot be fulfilled.")
        if payment.state != PayPalPayment.AUTHORIZED or not payment.authorization_id:
            raise errors.Conflict("This order has not been paid yet.")

        auth_id = _fresh_authorization(payment)

        # Deterministic (stable) capture idempotency key, as for authorize.
        capture_request_id = "cap-%s" % payment.pk
        payment.capture_request_id = capture_request_id

        try:
            result = gateway.capture(auth_id, capture_request_id)
        except errors.PayPalRejected as e:
            # A capture can fail because the hold went stale between our check and
            # the capture. Renew once and retry rather than failing the fulfilment.
            if _looks_expired(e):
                auth_id = _renew_authorization(payment)
                result = gateway.capture(auth_id, capture_request_id)
            else:
                raise

        captured = result["amount"] if result["amount"] is not None else payment.amount
        payment.capture_id = result["capture_id"]
        payment.capture_status = result["status"]
        payment.captured_amount = captured
        payment.paypal_fee = result["paypal_fee"]
        payment.net_amount = result["net_amount"]
        payment.authorization_status = AUTH_CAPTURED
        payment.state = PayPalPayment.CAPTURED
        payment.save()

        source = _ensure_source(payment)
        source.debit(captured, reference=payment.capture_id, status=payment.capture_status)

        _set_order_status(order, STATUS_FULFILLED)
        return payment


def _fresh_authorization(payment):
    """Return a capturable authorization id, renewing a stale hold if possible."""
    info = gateway.get_authorization(payment.authorization_id)
    status = info["status"]
    if status == AUTH_CREATED:
        return payment.authorization_id
    if status == AUTH_VOIDED:
        raise errors.Conflict(
            "The authorization for this order was cancelled; re-collect payment "
            "from the shopper."
        )
    if status == AUTH_CAPTURED:
        # Money already held-and-taken upstream but we have no capture record;
        # treat as a conflict an operator can investigate.
        raise errors.Conflict(
            "PayPal reports this authorization is already captured; reconcile the "
            "order before fulfilling again."
        )
    # Any other status (e.g. EXPIRED / PENDING): try to renew.
    return _renew_authorization(payment)


def _renew_authorization(payment):
    try:
        info = gateway.reauthorize(payment.authorization_id)
    except errors.PayPalError as e:
        raise errors.Conflict(
            "The authorization for this order has expired and can no longer be "
            "renewed; re-collect payment from the shopper.",
            detail=getattr(e, "detail", None),
        ) from e
    payment.authorization_id = info["id"]
    payment.authorization_status = info["status"]
    payment.authorization_expiry = info["expiration_time"]
    payment.save(
        update_fields=[
            "authorization_id",
            "authorization_status",
            "authorization_expiry",
            "date_updated",
        ]
    )
    return payment.authorization_id


def _looks_expired(exc):
    text = ("%s %s" % (exc.message or "", exc.detail or "")).upper()
    return "EXPIR" in text or "AUTHORIZATION" in text and "COMPLETED" not in text


# ---------------------------------------------------------------------------
# Cancel (void) before fulfilment — release the held funds
# ---------------------------------------------------------------------------

def cancel(order):
    with transaction.atomic():
        payment = _locked_payment(order)

        if payment.state == PayPalPayment.CANCELLED:
            return payment  # idempotent
        if payment.capture_id or payment.state in (
            PayPalPayment.CAPTURED,
            PayPalPayment.PARTIALLY_REFUNDED,
            PayPalPayment.REFUNDED,
        ):
            raise errors.Conflict(
                "This order has already been fulfilled; issue a refund instead of "
                "cancelling."
            )
        if payment.state != PayPalPayment.AUTHORIZED or not payment.authorization_id:
            raise errors.Conflict("This order has no payment to cancel.")

        status = gateway.void(payment.authorization_id)
        payment.authorization_status = status or AUTH_VOIDED
        payment.state = PayPalPayment.CANCELLED
        payment.save()

        _set_order_status(order, STATUS_CANCELLED)
        return payment


# ---------------------------------------------------------------------------
# Refund after fulfilment
# ---------------------------------------------------------------------------

def refund(order, idempotency_key, amount=None):
    if not idempotency_key:
        raise errors.ApiValidationError("A refund requires an idempotency key.")

    with transaction.atomic():
        payment = _locked_payment(order)

        if payment.state not in (
            PayPalPayment.CAPTURED,
            PayPalPayment.PARTIALLY_REFUNDED,
        ):
            raise errors.Conflict("This order has not been captured, so it cannot be refunded.")

        # Idempotent: the same key never refunds twice.
        existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return payment, existing

        refundable = payment.refundable_amount
        if amount is None:
            amount = refundable
        else:
            amount = Decimal(amount).quantize(Decimal("0.01"))
        if amount <= Decimal("0.00"):
            raise errors.ApiValidationError("Refund amount must be positive.")
        if amount > refundable:
            raise errors.Conflict(
                "Refund of %s exceeds the %s still available to refund on this "
                "capture." % (amount, refundable)
            )

        # The PayPal-Request-Id must be globally unique per distinct request but
        # stable across retries of *this* refund. The caller's key is only unique
        # within an order (two orders may legitimately reuse a key), so scope it by
        # the payment to avoid colliding with a different capture at PayPal.
        result = gateway.refund(
            payment.capture_id,
            amount,
            payment.currency,
            request_id="rf-%s-%s" % (payment.pk, idempotency_key),
        )
        refund_row = PayPalRefund.objects.create(
            payment=payment,
            refund_id=result["refund_id"],
            amount=result["amount"] if result["amount"] is not None else amount,
            currency=payment.currency,
            status=result["status"] or "COMPLETED",
            idempotency_key=idempotency_key,
        )

        source = _ensure_source(payment)
        source.refund(refund_row.amount, reference=refund_row.refund_id, status=refund_row.status)

        payment.recompute_refund_state()
        payment.save(update_fields=["state", "date_updated"])
        return payment, refund_row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _locked_payment(order):
    try:
        return PayPalPayment.objects.select_for_update().get(order=order)
    except PayPalPayment.DoesNotExist:
        raise errors.NotFound("This order has no PayPal payment record.")


def _ensure_source(payment):
    source = payment.order.sources.filter(source_type__code="paypal").first()
    if source is None:
        source = Source.objects.create(
            order=payment.order,
            source_type=_source_type(),
            currency=payment.currency,
            amount_allocated=Decimal("0.00"),
            label="PayPal",
        )
    return source


def _saved_card(user, payment_method_id):
    try:
        bankcard = Bankcard.objects.get(id=payment_method_id, user=user)
    except (Bankcard.DoesNotExist, ValueError):
        raise errors.NotFound("No saved card with that id.")
    if not bankcard.partner_reference:
        raise errors.Conflict("This saved card can no longer be used to pay.")
    return bankcard
