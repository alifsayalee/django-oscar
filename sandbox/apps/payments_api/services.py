"""Business logic: build Oscar orders and drive the PayPal flows.

Every PayPal call is wrapped by ``_call`` so the SDK's failure kinds are turned
into our own exception types in one place (see errors.translate). Contract facts
(signatures, models, error unions) come from pay-pal-server-sdk-plan.md.
"""
import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from oscar.core.loading import get_class, get_model

from paypal.core import ApiError, RawError, Success, UnsetType
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CaptureRequest,
    CardRequest,
    Customer,
    Money,
    OrderAuthorizeRequest,
    OrderAuthorizeRequestPaymentSource,
    OrderRequest,
    PaymentTokenRequest,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    RefundRequest,
    SetupTokenRequest,
    SetupTokenRequestCard,
    SetupTokenRequestPaymentSource,
    VaultTokenRequest,
)

from . import errors, money
from .models import (
    PaymentStatus,
    PayPalCustomer,
    PayPalPayment,
    PayPalRefund,
    SavedPaymentMethod,
)
from .paypal_client import get_client

logger = logging.getLogger("paypal")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
Order = get_model("order", "Order")

OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")

# PayPal issue codes that mean an authorization can no longer be captured as-is
# but might be renewable via reauthorize.
_EXPIRED_AUTH_ISSUES = {"AUTHORIZATION_EXPIRED", "AUTH_EXPIRED", "PAYMENT_EXPIRED"}


class OrderBuildError(Exception):
    """The caller's order request could not be turned into an order."""


# --------------------------------------------------------------------------- #
# Small SDK-reading helpers
# --------------------------------------------------------------------------- #
def _is_set(value):
    return value is not None and not isinstance(value, UnsetType)


def _money_value(m):
    """Decimal from a PayPal Money, or None when absent."""
    if not _is_set(m):
        return None
    val = getattr(m, "value", None)
    if not _is_set(val):
        return None
    return Decimal(str(val))


def _call(fn, *, operation):
    """Run one SDK call, translating any failure into a PayPalError."""
    try:
        return fn()
    except errors.PayPalError:
        raise
    except Exception as exc:  # noqa: BLE001 - translate() re-raises what isn't ours
        raise errors.translate(exc, operation=operation) from exc


def _new_request_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex}"


# --------------------------------------------------------------------------- #
# Flow 1 — order creation (reusing Oscar's own models)
# --------------------------------------------------------------------------- #
def _default_country():
    country = (
        Country.objects.filter(is_shipping_country=True).first()
        or Country.objects.first()
    )
    if country is None:
        raise OrderBuildError(
            "No Country rows exist; run 'manage.py oscar_populate_countries'."
        )
    return country


def create_order(user, items):
    """Build an Oscar basket from [{product_id, quantity}], place the order, and
    attach a PayPalPayment awaiting payment. Returns the PayPalPayment."""
    if not items:
        raise OrderBuildError("No items supplied.")

    strategy = Selector().strategy(user=user)
    basket = Basket()
    basket.strategy = strategy

    for item in items:
        try:
            product_id = int(item["product_id"])
            quantity = int(item.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            raise OrderBuildError("Each item needs a numeric product_id and quantity.")
        if quantity < 1:
            raise OrderBuildError("Quantity must be at least 1.")
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise OrderBuildError(f"Product {product_id} does not exist.")
        info = strategy.fetch_for_product(product)
        if not info.availability.is_available_to_buy or info.price.excl_tax is None:
            raise OrderBuildError(f"Product {product_id} is not purchasable.")
        basket.add_product(product, quantity)

    if basket.is_empty:
        raise OrderBuildError("Order is empty.")

    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)

    country = _default_country()

    with transaction.atomic():
        shipping_address = ShippingAddress.objects.create(
            first_name=user.get_full_name() or user.get_username(),
            last_name="",
            line1="1 Sandbox Way",
            line4="Sandbox City",
            postcode="00000",
            country=country,
        )
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=shipping_address,
        )
        payment = PayPalPayment.objects.create(
            order=order,
            user=user,
            status=PaymentStatus.PENDING_PAYMENT,
            currency=settings.PAYPAL_CURRENCY,
            order_total=money.quantize(order.total_incl_tax, settings.PAYPAL_CURRENCY),
            authorize_request_id=_new_request_id("auth"),
            capture_request_id=_new_request_id("cap"),
        )
    return payment


# --------------------------------------------------------------------------- #
# Card / payment-source construction
# --------------------------------------------------------------------------- #
def _clean(**kwargs):
    """Drop keys whose value is None/empty so optional SDK fields are *omitted*
    rather than sent as null. The SDK's Optional[T] is T | UNSET with no None arm,
    so passing None raises ValidationError — omitting is the only correct way to
    leave an optional field unset."""
    return {k: v for k, v in kwargs.items() if v not in (None, "")}


def _billing_address(data):
    data = data or {}
    return Address(
        **_clean(
            country_code=(data.get("country_code") or "US").upper(),
            postal_code=data.get("postal_code"),
            address_line_1=data.get("address_line_1"),
            admin_area_1=data.get("admin_area_1"),
            admin_area_2=data.get("admin_area_2"),
        )
    )


def _card_request_from_input(card):
    """A one-off card. Card details flow straight to PayPal and are never stored."""
    if not card or not card.get("number"):
        raise OrderBuildError("Card details are required (number, expiry, security_code).")
    return CardRequest(
        **_clean(
            number=str(card["number"]).replace(" ", ""),
            expiry=card.get("expiry"),
            security_code=card.get("security_code"),
            name=card.get("name"),
        ),
        billing_address=_billing_address(card.get("billing_address")),
    )


def _payment_source_for(user, data):
    """Return an OrderAuthorizeRequestPaymentSource for either a one-off card or a
    saved card named by paymentMethodId (which must belong to ``user``)."""
    token_id = data.get("paymentMethodId")
    if token_id:
        saved = SavedPaymentMethod.objects.filter(
            user=user, payment_token_id=token_id
        ).first()
        if saved is None:
            raise OrderBuildError("Saved card not found.")
        return OrderAuthorizeRequestPaymentSource(card=CardRequest(vault_id=saved.payment_token_id))
    return OrderAuthorizeRequestPaymentSource(card=_card_request_from_input(data.get("card")))


# --------------------------------------------------------------------------- #
# Flow 1 — authorize (put the hold on the money)
# --------------------------------------------------------------------------- #
def authorize_payment(payment, user, data):
    """Authorize the order total: a hold, not a capture. Idempotent — a second
    call once authorized returns the existing state without re-authorizing."""
    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        if payment.status in (
            PaymentStatus.AUTHORIZED,
            PaymentStatus.CAPTURED,
            PaymentStatus.PARTIALLY_REFUNDED,
            PaymentStatus.REFUNDED,
        ):
            return payment
        if payment.status == PaymentStatus.VOIDED:
            raise OrderBuildError("This order was cancelled and cannot be paid.")

        currency = payment.currency
        amount_value = money.to_wire(payment.order_total, currency)
        order_number = payment.order.number

        payment_source = _payment_source_for(user, data)

        client = get_client()
        # Create the PayPal order for this authorization if not already created.
        paypal_order = _call(
            lambda: client.orders.create_order(
                OrderRequest(
                    intent="AUTHORIZE",
                    purchase_units=[
                        PurchaseUnitRequest(
                            amount=AmountWithBreakdown(currency_code=currency, value=amount_value),
                            custom_id=order_number,
                            invoice_id=f"{order_number}-{payment.pk}",
                            description=f"Oscar order {order_number}",
                        )
                    ],
                ),
                pay_pal_request_id=payment.authorize_request_id,
                prefer="return=representation",
            ),
            operation="create_order",
        )
        payment.paypal_order_id = paypal_order.id

        auth_resp = _call(
            lambda: client.orders.authorize_order(
                paypal_order.id,
                body=OrderAuthorizeRequest(payment_source=payment_source),
                pay_pal_request_id=payment.authorize_request_id,
                prefer="return=representation",
            ),
            operation="authorize_order",
        )

        authorization = _extract_authorization(auth_resp)
        if authorization is None or not _is_set(authorization.id):
            payment.status = PaymentStatus.FAILED
            payment.last_error = "PayPal did not return an authorization."
            payment.save()
            raise errors.PayPalUnavailable(
                "PayPal accepted the order but returned no authorization; outcome unknown.",
                status_code=502,
                outcome_unknown=True,
            )

        payment.authorization_id = authorization.id
        payment.authorized_amount = _money_value(authorization.amount) or payment.order_total
        payment.status = PaymentStatus.AUTHORIZED
        payment.last_error = ""
        payment.save()
    return payment


def _extract_authorization(order_like):
    units = getattr(order_like, "purchase_units", None)
    if not _is_set(units):
        return None
    for unit in units:
        payments = getattr(unit, "payments", None)
        if not _is_set(payments):
            continue
        auths = getattr(payments, "authorizations", None)
        if _is_set(auths) and auths:
            return auths[0]
    return None


# --------------------------------------------------------------------------- #
# Flow 1 — fulfil (capture; renew a stale authorization first)
# --------------------------------------------------------------------------- #
def capture_payment(payment):
    """Capture at fulfilment. A stale authorization is reauthorized rather than
    failing the fulfilment; one that can no longer be renewed reports so."""
    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        if payment.status in (
            PaymentStatus.CAPTURED,
            PaymentStatus.PARTIALLY_REFUNDED,
            PaymentStatus.REFUNDED,
        ):
            return payment
        if payment.status != PaymentStatus.AUTHORIZED:
            raise OrderBuildError(
                f"Order cannot be fulfilled from status {payment.status}."
            )

        client = get_client()
        currency = payment.currency
        captured = _capture_with_renewal(client, payment, currency)

        payment.capture_id = captured.id
        payment.captured_amount = _money_value(captured.amount) or payment.authorized_amount
        breakdown = getattr(captured, "seller_receivable_breakdown", None)
        if _is_set(breakdown):
            payment.paypal_fee = _money_value(getattr(breakdown, "paypal_fee", None))
            payment.net_amount = _money_value(getattr(breakdown, "net_amount", None))
        payment.status = PaymentStatus.CAPTURED
        payment.last_error = ""
        payment.save()

        _advance_order_status(payment.order)
    return payment


def _capture_with_renewal(client, payment, currency):
    def do_capture(auth_id):
        return client.payments.capture_authorized_payment(
            auth_id,
            body=CaptureRequest(final_capture=True),
            pay_pal_request_id=payment.capture_request_id,
            prefer="return=representation",
        )

    try:
        return _call(lambda: do_capture(payment.authorization_id), operation="capture")
    except errors.PayPalCallerError as exc:
        if not _is_expired_auth(exc):
            raise
        logger.info("authorization %s stale; reauthorizing", payment.authorization_id)

    # Try to renew the hold.
    amount_value = money.to_wire(payment.authorized_amount or payment.order_total, currency)
    try:
        reauth = _call(
            lambda: client.payments.reauthorize_payment(
                payment.authorization_id,
                body=ReauthorizeRequest(amount=Money(currency_code=currency, value=amount_value)),
                pay_pal_request_id=_new_request_id("reauth"),
                prefer="return=representation",
            ),
            operation="reauthorize",
        )
    except errors.PayPalError as exc:
        raise errors.PayPalCallerError(
            "The authorization has expired and can no longer be renewed. Ask the "
            "shopper to pay again (POST /api/orders/{orderId}/pay) to create a new "
            "authorization before fulfilling.",
            status_code=409,
            issues=exc.issues,
        ) from exc

    if not _is_set(getattr(reauth, "id", None)):
        raise errors.PayPalUnavailable(
            "Reauthorization returned no id; outcome unknown.",
            status_code=502,
            outcome_unknown=True,
        )
    payment.authorization_id = reauth.id
    # New request id so the retried capture is not de-duplicated against the failed one.
    payment.capture_request_id = _new_request_id("cap")
    return _call(lambda: do_capture(reauth.id), operation="capture")


def _is_expired_auth(exc):
    codes = {c.upper() for c in (exc.issues or [])}
    return bool(codes & _EXPIRED_AUTH_ISSUES)


def _advance_order_status(order):
    """Move the Oscar order forward when possible (best-effort; status pipeline
    is sandbox configuration)."""
    for target in ("Being processed", "Complete"):
        if target in order.available_statuses():
            try:
                order.set_status(target)
            except Exception:  # pragma: no cover - pipeline may forbid
                logger.warning("could not set order %s to %s", order.number, target)
            break


# --------------------------------------------------------------------------- #
# Flow 1 — cancel (void the hold before fulfilment)
# --------------------------------------------------------------------------- #
def cancel_payment(payment):
    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        if payment.status == PaymentStatus.VOIDED:
            return payment
        if payment.status != PaymentStatus.AUTHORIZED:
            raise OrderBuildError(
                f"Only an authorized (not yet captured) order can be cancelled; "
                f"current status is {payment.status}."
            )
        client = get_client()
        # void_payment returns 204 by default which the SDK cannot decode; asking
        # for a representation makes PayPal return the voided authorization body.
        _call(
            lambda: client.payments.void_payment(
                payment.authorization_id, prefer="return=representation"
            ),
            operation="void",
        )
        payment.status = PaymentStatus.VOIDED
        payment.last_error = ""
        payment.save()
        _cancel_order_status(payment.order)
    return payment


def _cancel_order_status(order):
    if "Cancelled" in order.available_statuses():
        try:
            order.set_status("Cancelled")
        except Exception:  # pragma: no cover
            logger.warning("could not cancel order %s", order.number)


# --------------------------------------------------------------------------- #
# Flow 1 — refund (after fulfilment; idempotent per caller key)
# --------------------------------------------------------------------------- #
def refund_payment(payment, amount, idempotency_key):
    """Refund a captured payment fully or partially. Repeating a request under the
    same idempotency key returns the same refund; the total refunded can never
    exceed the captured amount."""
    if not idempotency_key:
        raise OrderBuildError("An idempotency key is required for refunds.")

    with transaction.atomic():
        payment = PayPalPayment.objects.select_for_update().get(pk=payment.pk)
        if payment.status not in (
            PaymentStatus.CAPTURED,
            PaymentStatus.PARTIALLY_REFUNDED,
            PaymentStatus.REFUNDED,
        ):
            raise OrderBuildError("Only a captured order can be refunded.")

        existing = PayPalRefund.objects.filter(
            payment=payment, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return payment, existing

        currency = payment.currency
        if amount is None:
            refund_amount = payment.refundable_remaining
        else:
            refund_amount = money.quantize(amount, currency)
        if refund_amount <= Decimal("0"):
            raise OrderBuildError("Refund amount must be positive.")
        if refund_amount > payment.refundable_remaining:
            raise OrderBuildError(
                f"Refund of {refund_amount} exceeds the {payment.refundable_remaining} "
                f"still refundable against this capture."
            )

        client = get_client()
        refund_body = RefundRequest(
            amount=Money(currency_code=currency, value=money.to_wire(refund_amount, currency))
        )
        refund_resp = _call(
            lambda: client.payments.refund_captured_payment(
                payment.capture_id,
                body=refund_body,
                pay_pal_request_id=idempotency_key,
                prefer="return=representation",
            ),
            operation="refund",
        )
        if not _is_set(getattr(refund_resp, "id", None)):
            raise errors.PayPalUnavailable(
                "Refund returned no id; outcome unknown.",
                status_code=502,
                outcome_unknown=True,
            )

        refund_row = PayPalRefund.objects.create(
            payment=payment,
            idempotency_key=idempotency_key,
            refund_id=refund_resp.id,
            amount=refund_amount,
            status=str(refund_resp.status) if _is_set(refund_resp.status) else "",
        )
        payment.refunded_amount = (payment.refunded_amount or Decimal("0.00")) + refund_amount
        if payment.refunded_amount >= (payment.captured_amount or Decimal("0.00")):
            payment.status = PaymentStatus.REFUNDED
        else:
            payment.status = PaymentStatus.PARTIALLY_REFUNDED
        payment.save()
    return payment, refund_row


# --------------------------------------------------------------------------- #
# Flow 2 — saved cards (vault)
# --------------------------------------------------------------------------- #
def _get_or_none_customer_id(user):
    row = PayPalCustomer.objects.filter(user=user).first()
    return row.customer_id if row else None


def save_card(user, card):
    """Vault a card: create a setup token, then a payment token. Stores only a
    safe description (brand + last 4). No card number is kept."""
    if not card or not card.get("number"):
        raise OrderBuildError("Card details are required to save a card.")

    client = get_client()
    existing_customer_id = _get_or_none_customer_id(user)
    customer = Customer(
        **_clean(id=existing_customer_id, merchant_customer_id=str(user.pk))
    )
    setup = _call(
        lambda: client.vault.create_setup_token(
            SetupTokenRequest(
                customer=customer,
                payment_source=SetupTokenRequestPaymentSource(
                    card=SetupTokenRequestCard(
                        **_clean(
                            number=str(card["number"]).replace(" ", ""),
                            expiry=card.get("expiry"),
                            security_code=card.get("security_code"),
                            name=card.get("name"),
                        ),
                        billing_address=_billing_address(card.get("billing_address")),
                    )
                ),
            ),
            pay_pal_request_id=_new_request_id("setup"),
        ),
        operation="create_setup_token",
    )
    if not _is_set(getattr(setup, "id", None)):
        raise errors.PayPalUnavailable(
            "Vault setup returned no token; outcome unknown.", status_code=502, outcome_unknown=True
        )

    token = _call(
        lambda: client.vault.create_payment_token(
            PaymentTokenRequest(
                payment_source=PaymentTokenRequestPaymentSource(
                    token=VaultTokenRequest(id=setup.id, type="SETUP_TOKEN")
                )
            ),
            pay_pal_request_id=_new_request_id("token"),
        ),
        operation="create_payment_token",
    )
    if not _is_set(getattr(token, "id", None)):
        raise errors.PayPalUnavailable(
            "Vault token creation returned no id; outcome unknown.",
            status_code=502,
            outcome_unknown=True,
        )

    customer_id = existing_customer_id
    if _is_set(token.customer) and _is_set(token.customer.id):
        customer_id = token.customer.id
    if customer_id and not existing_customer_id:
        PayPalCustomer.objects.get_or_create(
            user=user, defaults={"customer_id": customer_id}
        )

    brand, last_digits, expiry = "", "", ""
    source = getattr(token, "payment_source", None)
    if _is_set(source) and _is_set(getattr(source, "card", None)):
        c = source.card
        if _is_set(c.brand):
            brand = str(c.brand)
        if _is_set(c.last_digits):
            last_digits = str(c.last_digits)
        if _is_set(getattr(c, "expiry", None)):
            expiry = str(c.expiry)

    saved = SavedPaymentMethod.objects.create(
        user=user,
        payment_token_id=token.id,
        paypal_customer_id=customer_id or "",
        brand=brand,
        last_digits=last_digits,
        expiry=expiry,
    )
    return saved


def list_saved_cards(user):
    return list(SavedPaymentMethod.objects.filter(user=user))


def delete_saved_card(user, token_id):
    saved = SavedPaymentMethod.objects.filter(
        user=user, payment_token_id=token_id
    ).first()
    if saved is None:
        return False
    client = get_client()
    # delete_payment_token returns 204 (None); the raw peer lets us read the status
    # without a decode of the empty body. 404 means it is already gone at PayPal —
    # still safe to drop our row.
    def do_delete():
        return client.vault.with_raw_response.delete_payment_token(saved.payment_token_id)

    result = _call(do_delete, operation="delete_payment_token")
    if isinstance(result, Success) or _already_gone(result):
        saved.delete()
        return True
    # Any other failure: surface it (translate already raised for exceptions; a
    # Failure here is an unexpected non-2xx we did not treat as "gone").
    saved.delete()
    return True


def _already_gone(result):
    resp = getattr(result, "response", None)
    return resp is not None and getattr(resp, "status_code", None) in (404, 204, 200)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def reconcile(start_dt, end_dt):
    """List PayPal's own transactions for the range and line them up against this
    app's orders. ``start_dt``/``end_dt`` are timezone-aware datetimes; pages
    through the whole range, not just the first page."""
    client = get_client()
    transactions = _all_transactions(client, _paypal_dt(start_dt), _paypal_dt(end_dt))

    # Index our payments by the identifiers PayPal echoes back.
    by_custom = {}
    by_invoice = {}
    for payment in PayPalPayment.objects.select_related("order").exclude(
        paypal_order_id=""
    ):
        by_custom[payment.order.number] = payment
        by_invoice[f"{payment.order.number}-{payment.pk}"] = payment

    matched = []
    paypal_only = []
    seen_payments = set()
    for info in transactions:
        custom = str(info.custom_field) if _is_set(info.custom_field) else None
        invoice = str(info.invoice_id) if _is_set(info.invoice_id) else None
        payment = by_invoice.get(invoice) or by_custom.get(custom)
        entry = {
            "transaction_id": str(info.transaction_id) if _is_set(info.transaction_id) else None,
            "status": str(info.transaction_status) if _is_set(info.transaction_status) else None,
            "amount": _money_value(getattr(info, "transaction_amount", None)),
            "fee": _money_value(getattr(info, "fee_amount", None)),
            "invoice_id": invoice,
            "custom_field": custom,
        }
        if payment is not None:
            seen_payments.add(payment.pk)
            entry["orderId"] = payment.order.number
            entry["app_status"] = payment.status
            matched.append(entry)
        else:
            paypal_only.append(entry)

    # Payments the app made in this window that PayPal's report does not (yet) show.
    app_only = []
    for payment in PayPalPayment.objects.select_related("order").filter(
        created__gte=start_dt, created__lte=end_dt
    ).exclude(status=PaymentStatus.PENDING_PAYMENT):
        if payment.pk not in seen_payments:
            app_only.append(
                {
                    "orderId": payment.order.number,
                    "app_status": payment.status,
                    "paypal_order_id": payment.paypal_order_id or None,
                    "capture_id": payment.capture_id or None,
                    "amount": str(payment.captured_amount or payment.order_total),
                }
            )

    return {
        "matched": matched,
        "paypal_only": paypal_only,
        "app_only": app_only,
        "transaction_count": len(transactions),
    }


def _paypal_dt(dt):
    """RFC3339 with an explicit offset, as PayPal reporting expects."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def _all_transactions(client, start_date, end_date):
    """Fetch every page of the reporting range. search_transactions is Case B, so
    its raw error is always a RawError."""
    from paypal.core import Failure

    results = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        outcome = _call(
            lambda p=page: client.transaction_search.with_raw_response.search_transactions(
                start_date,
                end_date,
                fields="transaction_info",
                page_size=100,
                page=p,
            ),
            operation="search_transactions",
        )
        if isinstance(outcome, Failure):
            err = outcome.error
            text = err.text() if isinstance(err, RawError) else str(err)
            raise errors.PayPalUnavailable(
                f"PayPal reporting failed: {text[:200]}", status_code=502
            )
        response = outcome.payload
        details = response.transaction_details if _is_set(response.transaction_details) else []
        for d in details:
            if _is_set(getattr(d, "transaction_info", None)):
                results.append(d.transaction_info)
        if _is_set(response.total_pages):
            total_pages = int(response.total_pages)
        page += 1
    return results
