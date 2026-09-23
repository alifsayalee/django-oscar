"""Business orchestration for the PayPal checkout API.

Reuses Oscar's own basket/order/order-line models (via OrderCreator) and drives
PayPal through :class:`~apps.paypal_checkout.gateway.PayPalGateway`. All payment
operations are idempotent in effect: the ``PayPalPayment`` row (and, for refunds,
the ``PayPalRefund`` unique key) is the durable claim that a double-click cannot
get past.
"""
import logging
import re
from datetime import timezone as _dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from oscar.core.loading import get_class, get_model

from . import money
from .exceptions import ApiClientError, ChallengeRequired, ProviderError, ProviderRejected
from .gateway import PayPalGateway, _enum_str, _parse_dt, _val, new_request_id
from .models import PayPalPayment, PayPalRefund, SavedCard

logger = logging.getLogger("paypal_checkout")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Order = get_model("order", "Order")

Default = get_class("partner.strategy", "Default")
Free = get_class("shipping.methods", "Free")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
SurchargeApplicator = get_class("checkout.applicator", "SurchargeApplicator")


def _currency():
    from django.conf import settings
    return (getattr(settings, "PAYPAL_CURRENCY", "USD") or "USD").upper()


# ---------------------------------------------------------------------------
# card input handling
# ---------------------------------------------------------------------------

_EXPIRY_RE = re.compile(r"^\d{4}-\d{2}$")


def _normalise_expiry(raw):
    """Return expiry as PayPal's ``YYYY-MM``. Accepts ``YYYY-MM``, ``MM/YY``,
    ``MM/YYYY``."""
    if not raw or not isinstance(raw, str):
        raise ApiClientError("Card expiry is required (format YYYY-MM).")
    raw = raw.strip()
    if _EXPIRY_RE.match(raw):
        return raw
    m = re.match(r"^(\d{1,2})\s*/\s*(\d{2}|\d{4})$", raw)
    if m:
        month = int(m.group(1))
        year = m.group(2)
        if len(year) == 2:
            year = "20" + year
        if 1 <= month <= 12:
            return f"{year}-{month:02d}"
    raise ApiClientError("Card expiry must be in YYYY-MM format.")


def _card_request_from_input(card):
    """Build a raw-card ``CardRequest`` from caller input. The PAN/CVV are used to
    talk to PayPal and never stored or logged."""
    from paypal.models import CardRequest

    if not isinstance(card, dict):
        raise ApiClientError("`card` must be an object with card details.")
    number = str(card.get("number", "")).replace(" ", "")
    if not number.isdigit() or not (12 <= len(number) <= 19):
        raise ApiClientError("A valid card `number` is required.")
    expiry = _normalise_expiry(card.get("expiry"))
    security_code = str(card.get("security_code") or card.get("cvv") or "").strip()
    if security_code and not security_code.isdigit():
        raise ApiClientError("Card `security_code` must be numeric.")
    name = (card.get("name") or "").strip() or None
    kwargs = dict(number=number, expiry=expiry)
    if security_code:
        kwargs["security_code"] = security_code
    if name:
        kwargs["name"] = name
    return CardRequest(**kwargs)


def _card_request_from_saved(user, payment_method_id):
    from paypal.models import CardRequest

    try:
        card = SavedCard.objects.get(pk=payment_method_id, user=user)
    except (SavedCard.DoesNotExist, ValueError, TypeError):
        raise ApiClientError("Saved card not found.", status_code=404)
    return CardRequest(vault_id=card.paypal_token_id)


# ---------------------------------------------------------------------------
# order creation (Flow 1, POST /api/orders)
# ---------------------------------------------------------------------------

def _validate_items(items):
    if not isinstance(items, list) or not items:
        raise ApiClientError("`items` must be a non-empty list of {id, quantity}.")
    parsed = []
    for entry in items:
        if not isinstance(entry, dict):
            raise ApiClientError("Each item must be an object {id, quantity}.")
        pid = entry.get("id") if "id" in entry else entry.get("productId")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            raise ApiClientError("Each item needs a numeric product `id`.")
        try:
            qty = int(entry.get("quantity", 1))
        except (TypeError, ValueError):
            raise ApiClientError("`quantity` must be an integer.")
        if qty < 1:
            raise ApiClientError("`quantity` must be at least 1.")
        parsed.append((pid, qty))
    return parsed


def create_order(user, items):
    """Place an Oscar order (awaiting payment) and open a PayPal order for it."""
    parsed = _validate_items(items)

    basket = Basket.objects.create(owner=user)
    basket.strategy = Default()
    for pid, qty in parsed:
        try:
            product = Product.objects.get(pk=pid, is_public=True)
        except Product.DoesNotExist:
            raise ApiClientError(f"Product {pid} not found.", status_code=404)
        info = basket.strategy.fetch_for_product(product)
        if not info.availability.is_available_to_buy or not info.price.exists:
            raise ApiClientError(f"Product {pid} is not purchasable.")
        basket.add_product(product, quantity=qty)

    if basket.is_empty:
        raise ApiClientError("The order would be empty.")

    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    surcharges = SurchargeApplicator().get_applicable_surcharges(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge, surcharges)

    currency = _currency()
    amount = money.quantize(total.incl_tax, currency)

    with transaction.atomic():
        order = OrderCreator().place_order(
            user=user,
            basket=basket,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            total=total,
            status="Pending",
            surcharges=surcharges,
        )
        basket.set_as_submitted()
        payment = PayPalPayment.objects.create(
            order=order, currency=currency, amount=amount,
            status=PayPalPayment.AWAITING_PAYMENT,
        )

    # Open the PayPal order (external call, outside the DB transaction). If this
    # fails the Oscar order + claim row still exist; `pay` re-opens it lazily.
    _ensure_paypal_order(payment)
    return order, payment


def _ensure_paypal_order(payment):
    if payment.paypal_order_id:
        return payment.paypal_order_id
    # A globally-unique invoice id (stable across idempotent retries via storage).
    if not payment.paypal_invoice_id:
        payment.paypal_invoice_id = f"{payment.order.number}-{new_request_id()[:12]}"
        PayPalPayment.objects.filter(pk=payment.pk).update(
            paypal_invoice_id=payment.paypal_invoice_id)
    gateway = PayPalGateway()
    order = gateway.create_order(
        currency=payment.currency,
        value=money.format_amount(payment.amount, payment.currency),
        invoice_id=payment.paypal_invoice_id,
        custom_id=str(payment.order.number),
        request_id=f"{payment.order.number}-create",
    )
    paypal_order_id = _val(order.id)
    if not paypal_order_id:
        raise ProviderError("PayPal did not return an order id.")
    PayPalPayment.objects.filter(pk=payment.pk).update(paypal_order_id=paypal_order_id)
    payment.paypal_order_id = paypal_order_id
    return paypal_order_id


# ---------------------------------------------------------------------------
# pay / authorize (Flow 1, POST /api/orders/{id}/pay)
# ---------------------------------------------------------------------------

def _get_locked_payment(order_number, user=None):
    qs = PayPalPayment.objects.select_for_update().select_related("order")
    try:
        payment = qs.get(order__number=order_number)
    except PayPalPayment.DoesNotExist:
        raise ApiClientError("Order not found.", status_code=404)
    if user is not None and payment.order.user_id != user.id:
        # Do not reveal another shopper's order — same answer as absent.
        raise ApiClientError("Order not found.", status_code=404)
    return payment


def _extract_authorization(auth_response):
    """Pull the single authorization out of an OrderAuthorizeResponse."""
    units = _val(auth_response.purchase_units) or []
    for unit in units:
        payments = _val(unit.payments)
        if not payments:
            continue
        authorizations = _val(payments.authorizations) or []
        if authorizations:
            return authorizations[0]
    return None


def _detect_challenge(auth_response):
    status = _enum_str(auth_response.status)
    if status == "PAYER_ACTION_REQUIRED":
        raise ChallengeRequired()
    for link in (_val(auth_response.links) or []):
        rel = _enum_str(getattr(link, "rel", None)).lower()
        if rel in ("payer-action", "approve", "3ds-contingency-resolution"):
            raise ChallengeRequired()


@transaction.atomic
def pay_order(user, order_number, *, card=None, payment_method_id=None):
    payment = _get_locked_payment(order_number, user=user)

    # Idempotent: a hold already placed is returned, never placed twice.
    if payment.authorization_id:
        return payment
    if payment.status in (PayPalPayment.CAPTURED, PayPalPayment.REFUNDED,
                          PayPalPayment.PARTIALLY_REFUNDED, PayPalPayment.VOIDED):
        raise ApiClientError(
            f"Order cannot be paid from state '{payment.status}'.", status_code=409)

    if payment_method_id is not None:
        card_request = _card_request_from_saved(user, payment_method_id)
    elif card is not None:
        card_request = _card_request_from_input(card)
    else:
        raise ApiClientError("Provide `card` details or a `paymentMethodId`.")

    paypal_order_id = _ensure_paypal_order(payment)

    gateway = PayPalGateway()
    auth_response = gateway.authorize_order(
        paypal_order_id, card_request=card_request,
        request_id=f"{order_number}-auth",
    )
    _detect_challenge(auth_response)

    authorization = _extract_authorization(auth_response)
    if authorization is None or not _val(authorization.id):
        payment.status = PayPalPayment.NEEDS_REVIEW
        payment.save(update_fields=["status", "updated"])
        raise ProviderError(
            "PayPal accepted the order but returned no authorization; needs review.")

    # The hold must equal the order total to the cent.
    amount = _val(authorization.amount)
    if amount is not None and not money.amounts_equal(
            _val(amount.value), payment.amount, payment.currency):
        payment.status = PayPalPayment.NEEDS_REVIEW
        payment.authorization_id = _val(authorization.id) or ""
        payment.save(update_fields=["status", "authorization_id", "updated"])
        raise ProviderError(
            f"Authorized amount {_val(amount.value)} {_val(amount.currency_code)} does not "
            f"match order total {payment.amount} {payment.currency}; needs review.")

    payment.authorization_id = _val(authorization.id)
    payment.authorization_status = _enum_str(authorization.status)
    payment.authorization_expiry = _parse_dt(authorization.expiration_time)
    payment.status = PayPalPayment.AUTHORIZED
    payment.save(update_fields=[
        "authorization_id", "authorization_status", "authorization_expiry",
        "status", "updated",
    ])
    return payment


# ---------------------------------------------------------------------------
# fulfil / capture (Flow 1, POST /api/orders/{id}/fulfil)
# ---------------------------------------------------------------------------

def _apply_capture(payment, captured):
    payment.capture_id = _val(captured.id) or ""
    payment.capture_status = _enum_str(captured.status)
    srb = _val(captured.seller_receivable_breakdown)
    if srb is not None:
        gross = _val(srb.gross_amount)
        fee = _val(srb.paypal_fee)
        net = _val(srb.net_amount)
        if gross is not None:
            payment.captured_value = money.quantize(_val(gross.value), payment.currency)
        if fee is not None:
            payment.paypal_fee = money.quantize(_val(fee.value), payment.currency)
        if net is not None:
            payment.net_amount = money.quantize(_val(net.value), payment.currency)
    if payment.captured_value is None:
        # Fall back to the authorized amount if the breakdown was absent (pending).
        payment.captured_value = payment.amount
    payment.provider_time = _parse_dt(captured.create_time)
    payment.status = PayPalPayment.CAPTURED


def _reauthorize(payment, gateway, attempt):
    logger.info("reauthorizing order %s (attempt %s)", payment.order.number, attempt)
    reauth = gateway.reauthorize(
        payment.authorization_id,
        currency=payment.currency,
        value=money.format_amount(payment.amount, payment.currency),
        request_id=f"{payment.order.number}-reauth-{attempt}",
    )
    new_id = _val(reauth.id)
    if not new_id:
        raise ProviderError("Reauthorization returned no authorization id.")
    payment.authorization_id = new_id
    payment.authorization_status = _enum_str(reauth.status)
    payment.authorization_expiry = _parse_dt(reauth.expiration_time)
    payment.save(update_fields=[
        "authorization_id", "authorization_status", "authorization_expiry", "updated"])
    return new_id


@transaction.atomic
def fulfil_order(order_number):
    payment = _get_locked_payment(order_number)

    if payment.capture_id:
        return payment  # already fulfilled — idempotent
    if payment.status not in (PayPalPayment.AUTHORIZED, PayPalPayment.NEEDS_REVIEW):
        raise ApiClientError(
            f"Order cannot be fulfilled from state '{payment.status}'.", status_code=409)
    if not payment.authorization_id:
        raise ApiClientError("Order has no authorization to capture.", status_code=409)

    gateway = PayPalGateway()

    # Proactively renew a hold that has already gone stale.
    if payment.authorization_expiry and payment.authorization_expiry <= timezone.now():
        try:
            _reauthorize(payment, gateway, attempt=1)
        except ProviderRejected as exc:
            raise ApiClientError(
                "The payment authorization has expired and can no longer be renewed. "
                "Ask the shopper to pay again.", status_code=409) from exc

    try:
        captured = gateway.capture(
            payment.authorization_id, request_id=f"{order_number}-capture")
    except ProviderRejected as exc:
        # A rejection here is usually a stale/uncapturable authorization: try to
        # renew once, then capture again.
        try:
            new_id = _reauthorize(payment, gateway, attempt=2)
        except ProviderRejected as reauth_exc:
            raise ApiClientError(
                "The payment authorization can no longer be captured or renewed. "
                "Ask the shopper to pay again.", status_code=409) from reauth_exc
        captured = gateway.capture(new_id, request_id=f"{order_number}-capture-2")

    _apply_capture(payment, captured)
    payment.save()
    _advance_order_status(payment.order, "Being processed")
    _advance_order_status(payment.order, "Complete")
    return payment


def _advance_order_status(order, new_status):
    """Best-effort Oscar order status transition (never breaks the money flow)."""
    try:
        if order.status != new_status:
            order.set_status(new_status)
    except Exception as exc:  # pragma: no cover - status pipeline is advisory here
        logger.warning("could not set order %s status to %s: %s",
                       order.number, new_status, exc)


# ---------------------------------------------------------------------------
# cancel / void (Flow 1, POST /api/orders/{id}/cancel)
# ---------------------------------------------------------------------------

@transaction.atomic
def cancel_order(order_number):
    payment = _get_locked_payment(order_number)

    if payment.status == PayPalPayment.VOIDED:
        return payment  # idempotent no-op
    if payment.capture_id:
        raise ApiClientError(
            "Order has already been fulfilled; use a refund instead.", status_code=409)
    if payment.status != PayPalPayment.AUTHORIZED or not payment.authorization_id:
        raise ApiClientError(
            f"Order cannot be cancelled from state '{payment.status}'.", status_code=409)

    gateway = PayPalGateway()
    gateway.void(payment.authorization_id, request_id=f"{order_number}-void")

    payment.status = PayPalPayment.VOIDED
    payment.authorization_status = "VOIDED"
    payment.save(update_fields=["status", "authorization_status", "updated"])
    _advance_order_status(payment.order, "Cancelled")
    return payment


# ---------------------------------------------------------------------------
# refund (Flow 1, POST /api/orders/{id}/refunds)
# ---------------------------------------------------------------------------

def _parse_amount(raw):
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise ApiClientError("`amount` must be a decimal value.")
    if value <= 0:
        raise ApiClientError("`amount` must be positive.")
    return value


@transaction.atomic
def refund_order(user, order_number, *, amount=None, idempotency_key=None):
    if not idempotency_key or not str(idempotency_key).strip():
        raise ApiClientError("An `idempotencyKey` is required for refunds.")
    idempotency_key = str(idempotency_key).strip()

    payment = _get_locked_payment(order_number, user=user)

    if payment.status not in (PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED):
        raise ApiClientError(
            f"Order cannot be refunded from state '{payment.status}'.", status_code=409)
    if not payment.capture_id or payment.captured_value is None:
        raise ApiClientError("Order has no captured payment to refund.", status_code=409)

    # Idempotent replay under the same key returns the existing refund.
    existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return payment, existing

    requested = _parse_amount(amount)
    remaining = payment.refundable_remaining
    if requested is None:
        requested = remaining
    requested = money.quantize(requested, payment.currency)
    if requested <= 0:
        raise ApiClientError("Nothing left to refund on this order.", status_code=409)
    if requested > remaining:
        raise ApiClientError(
            f"Refund of {requested} exceeds the refundable remaining {remaining}.",
            status_code=409)

    # Claim first: the unique (payment, key) row decides the winner.
    try:
        with transaction.atomic():
            refund_row = PayPalRefund.objects.create(
                payment=payment, idempotency_key=idempotency_key,
                amount=requested, currency=payment.currency,
                status=PayPalRefund.PENDING,
            )
    except IntegrityError:
        existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return payment, existing
        raise

    gateway = PayPalGateway()
    # The PayPal-Request-Id must be globally unique per logical refund (the header
    # dedups per-merchant), yet stable for THIS (payment, key) so a crash-recovery
    # retry is de-duplicated by PayPal. Deriving it from the payment pk + the
    # caller's key gives both; the DB unique (payment, key) is the real guard.
    paypal_request_id = f"refund-{payment.pk}-{idempotency_key}"
    try:
        refund = gateway.refund(
            payment.capture_id,
            currency=payment.currency,
            value=money.format_amount(requested, payment.currency),
            request_id=paypal_request_id,
        )
    except ProviderRejected as exc:
        # A definite rejection: release the claim so a fresh key can be tried.
        refund_row.status = PayPalRefund.FAILED
        refund_row.save(update_fields=["status"])
        raise
    except ProviderError:
        # Outcome unknown (transport/unreadable): leave the row PENDING so it is
        # visible and can be reconciled; do not release the claim.
        raise

    refund_row.refund_id = _val(refund.id) or ""
    refund_row.status = _map_refund_status(_enum_str(refund.status))
    refund_row.save(update_fields=["refund_id", "status"])

    # Recompute payment state from the refunds actually recorded.
    payment.refresh_from_db()
    if payment.refundable_remaining <= 0:
        payment.status = PayPalPayment.REFUNDED
    else:
        payment.status = PayPalPayment.PARTIALLY_REFUNDED
    payment.save(update_fields=["status", "updated"])
    return payment, refund_row


def _map_refund_status(wire):
    if wire == "COMPLETED":
        return PayPalRefund.COMPLETED
    if wire in ("CANCELLED", "FAILED"):
        return PayPalRefund.FAILED
    return PayPalRefund.PENDING


# ---------------------------------------------------------------------------
# my-orders (Flow 1, GET /api/my-orders)
# ---------------------------------------------------------------------------

def list_orders(user):
    return (
        Order.objects.filter(user=user)
        .select_related("paypal_payment")
        .prefetch_related("lines", "paypal_payment__refunds")
        .order_by("-date_placed")
    )


# ---------------------------------------------------------------------------
# saved cards (Flow 2)
# ---------------------------------------------------------------------------

def save_card(user, card):
    from paypal.models import PaymentTokenRequestCard

    if not isinstance(card, dict):
        raise ApiClientError("`card` must be an object with card details.")
    number = str(card.get("number", "")).replace(" ", "")
    if not number.isdigit() or not (12 <= len(number) <= 19):
        raise ApiClientError("A valid card `number` is required.")
    expiry = _normalise_expiry(card.get("expiry"))
    security_code = str(card.get("security_code") or card.get("cvv") or "").strip()
    name = (card.get("name") or "").strip() or None

    card_kwargs = dict(number=number, expiry=expiry)
    if security_code:
        card_kwargs["security_code"] = security_code
    if name:
        card_kwargs["name"] = name
    card_request = PaymentTokenRequestCard(**card_kwargs)

    customer_id = f"oscar-cust-{user.pk}"
    gateway = PayPalGateway()
    token = gateway.create_payment_token(
        customer_id=customer_id, card_request=card_request, request_id=new_request_id())

    token_id = _val(token.id)
    if not token_id:
        raise ProviderError("PayPal did not return a vault token id.")
    source = _val(token.payment_source)
    card_entity = _val(source.card) if source is not None else None
    brand = _enum_str(card_entity.brand) if card_entity is not None else ""
    last_digits = _val(card_entity.last_digits) if card_entity is not None else ""
    exp = _val(card_entity.expiry) if card_entity is not None else expiry
    customer = _val(token.customer)
    paypal_customer_id = _val(customer.id) if customer is not None else customer_id

    saved = SavedCard.objects.create(
        user=user, paypal_token_id=token_id, paypal_customer_id=paypal_customer_id or "",
        brand=brand or "", last_digits=last_digits or "", expiry=exp or "",
    )
    return saved


def list_saved_cards(user):
    return SavedCard.objects.filter(user=user).order_by("-created")


def delete_saved_card(user, payment_method_id):
    try:
        card = SavedCard.objects.get(pk=payment_method_id, user=user)
    except (SavedCard.DoesNotExist, ValueError, TypeError):
        raise ApiClientError("Saved card not found.", status_code=404)
    gateway = PayPalGateway()
    # 204 (deleted) and 404 (already gone at PayPal) are both fine; the local row
    # goes either way so the card can never be used to pay again.
    status = gateway.delete_payment_token(card.paypal_token_id)
    card.delete()
    return status


# ---------------------------------------------------------------------------
# reconciliation (Flow 1, GET /api/reconciliation)
# ---------------------------------------------------------------------------

_MAX_PAGES = 100
_PAGE_SIZE = 500


def _paypal_dt(dt):
    """Format an aware datetime as PayPal transaction-search expects (UTC)."""
    return dt.astimezone(_dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


def reconcile(from_dt, to_dt):
    """List PayPal's transactions over the whole range and line them up against
    this app's orders by invoice id (= order number)."""
    if from_dt >= to_dt:
        raise ApiClientError("`from` must be earlier than `to`.")

    gateway = PayPalGateway()
    provider_records = []
    truncated = False
    page = 1
    while page <= _MAX_PAGES:
        response = gateway.search_transactions(
            start_date=_paypal_dt(from_dt), end_date=_paypal_dt(to_dt),
            page=page, page_size=_PAGE_SIZE,
        )
        details = _val(response.transaction_details) or []
        for detail in details:
            info = _val(detail.transaction_info)
            if info is None:
                continue
            initiated = _parse_dt(info.transaction_initiation_date)
            # Defensive narrowing to the exact instants requested.
            if initiated is not None and not (from_dt <= initiated < to_dt):
                continue
            amount = _val(info.transaction_amount)
            fee = _val(info.fee_amount)
            provider_records.append({
                "transactionId": _val(info.transaction_id),
                "invoiceId": _val(info.invoice_id),
                "amount": _val(amount.value) if amount is not None else None,
                "currency": _val(amount.currency_code) if amount is not None else None,
                "fee": _val(fee.value) if fee is not None else None,
                "status": _val(info.transaction_status),
                "eventCode": _val(info.transaction_event_code),
                "initiationDate": initiated.isoformat() if initiated else None,
            })
        total_pages = _val(response.total_pages) or 1
        if page >= total_pages:
            break
        page += 1
    else:
        truncated = True

    # Group provider records by invoice id (an order owns auth + capture + refunds).
    by_invoice = {}
    for record in provider_records:
        by_invoice.setdefault(record["invoiceId"], []).append(record)

    # Local side: orders we attempted to charge, on the provider clock where we
    # have it (capture time), else creation time for still-authorized ones.
    local_payments = (
        PayPalPayment.objects.select_related("order")
        .exclude(paypal_order_id="")
    )
    matched, app_only = [], []
    for payment in local_payments:
        number = str(payment.order.number)
        in_window = False
        if payment.provider_time is not None:
            in_window = from_dt <= payment.provider_time < to_dt
        else:
            in_window = from_dt <= payment.created < to_dt
        if not in_window:
            continue
        # Match on the unique invoice id we sent (falls back to order number).
        group = by_invoice.pop(payment.paypal_invoice_id, None)
        if group is None:
            group = by_invoice.pop(number, [])
        entry = {
            "orderId": number,
            "invoiceId": payment.paypal_invoice_id or number,
            "state": payment.status,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "captureId": payment.capture_id or None,
            "providerTransactions": group,
        }
        if group:
            matched.append(entry)
        else:
            app_only.append(entry)

    # Anything PayPal reported that no local order claimed.
    provider_only = [rec for group in by_invoice.values() for rec in group]

    return {
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "truncated": truncated,
        "counts": {
            "providerTransactions": len(provider_records),
            "matched": len(matched),
            "appOnly": len(app_only),
            "providerOnly": len(provider_only),
        },
        "matched": matched,
        "appOnly": app_only,
        "providerOnly": provider_only,
    }
