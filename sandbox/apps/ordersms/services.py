"""Business logic: register numbers, place orders, and drive notifications.

Views call into here; this module calls the Twilio gateway. A messaging failure
must never fail the underlying order operation, so the notification helpers here
catch every gateway error and record it on the notification row instead of
propagating it — the order is still placed / dispatched / cancelled.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import twilio_gateway as gw
from .models import ContactNumber, OrderNotification

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
Basket = get_model("basket", "Basket")
ShippingAddress = get_model("order", "ShippingAddress")

Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")

# Order statuses reached by the two operator transitions (see settings pipeline).
STATUS_DISPATCHED = "Being processed"
STATUS_CANCELLED = "Cancelled"

# Provider statuses that will not change again — no point re-fetching them.
_TERMINAL_STATUSES = {
    "delivered",
    "undelivered",
    "failed",
    "canceled",
    "received",
    "read",
}


class ServiceError(Exception):
    """A caller-facing problem (bad input, not found, not allowed)."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# Contact numbers
# --------------------------------------------------------------------------- #
def register_contact_number(user, raw_number: str) -> ContactNumber:
    raw_number = (raw_number or "").strip()
    if not raw_number:
        raise ServiceError("a phone number is required")
    try:
        canonical = gw.canonicalize_number(raw_number)
    except gw.InvalidPhoneNumber:
        raise ServiceError("that number is not a usable destination", status_code=422)
    except gw.ConfigurationError:
        raise ServiceError("messaging is not configured", status_code=503)
    except gw.ProviderUnavailable:
        raise ServiceError("could not verify the number right now", status_code=502)
    obj, _created = ContactNumber.objects.get_or_create(user=user, e164=canonical)
    return obj


def list_contact_numbers(user):
    return list(ContactNumber.objects.filter(user=user))


def delete_contact_number(user, pk) -> None:
    deleted, _ = ContactNumber.objects.filter(user=user, pk=pk).delete()
    if not deleted:
        raise ServiceError("no such contact number", status_code=404)


def _recipient_for(order) -> str | None:
    """The number to text for this order: the owner's most recent contact number."""
    if order.user_id is None:
        return None
    number = (
        ContactNumber.objects.filter(user_id=order.user_id).order_by("-created").first()
    )
    return number.e164 if number else None


# --------------------------------------------------------------------------- #
# Placing an order (reuses Oscar's order/order-line model)
# --------------------------------------------------------------------------- #
def _build_shipping_address(user) -> ShippingAddress:
    country = (
        Country.objects.filter(is_shipping_country=True).first()
        or Country.objects.first()
    )
    if country is None:
        raise ServiceError("no shipping country configured", status_code=500)
    name = (user.get_full_name() or user.get_username() or "Customer").split(" ", 1)
    first = name[0]
    last = name[1] if len(name) > 1 else ""
    return ShippingAddress(
        first_name=first,
        last_name=last,
        line1="N/A",
        line4="N/A",
        postcode="0000",
        country=country,
    )


def place_order(user, items: list[dict]):
    """Place an order from catalogue item ids + quantities. Returns the Order.

    ``items`` is a list of {"product_id": int, "quantity": int}.
    """
    if not items:
        raise ServiceError("at least one item is required")

    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(user=user)

    for item in items:
        try:
            product_id = int(item["product_id"])
            quantity = int(item.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            raise ServiceError("each item needs a product_id and quantity")
        if quantity < 1:
            raise ServiceError("quantity must be at least 1")
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise ServiceError("no such product: %s" % product_id, status_code=404)
        info = basket.strategy.fetch_for_product(product)
        if not info.availability.is_available_to_buy:
            raise ServiceError("product %s is not available to buy" % product_id, status_code=422)
        basket.add_product(product, quantity)

    if basket.is_empty:
        raise ServiceError("no purchasable items")

    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)
    shipping_address = _build_shipping_address(user)
    shipping_address.save()  # FK target must be saved before the order references it

    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        user=user,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        shipping_address=shipping_address,
    )
    basket.submit()

    _record_and_send(
        order,
        OrderNotification.KIND_PLACED,
        _placed_body(order),
    )
    return order


# --------------------------------------------------------------------------- #
# Notification helpers (never raise: a messaging failure must not fail the op)
# --------------------------------------------------------------------------- #
def _apply_sent(notif: OrderNotification, sent: gw.SentMessage) -> None:
    notif.provider_sid = sent.sid
    notif.provider_status = sent.status
    notif.provider_error_code = sent.error_code
    notif.provider_error_message = sent.error_message or ""
    notif.provider_date_sent = sent.date_sent
    notif.local_outcome = OrderNotification.OUTCOME_SENT


def _record_and_send(order, kind, body, *, resend_of=None, idempotency_key=None) -> OrderNotification | None:
    to_number = _recipient_for(order)
    if not to_number:
        # Shopper has no number on file: simply not messaged, and no row is created.
        return None
    notif = OrderNotification.objects.create(
        order=order,
        kind=kind,
        to_number=to_number,
        resend_of=resend_of,
        idempotency_key=idempotency_key,
        local_outcome=OrderNotification.OUTCOME_PENDING,
    )
    try:
        sent = gw.send_sms(to_number, body)
    except gw.GatewayError as exc:
        notif.local_outcome = (
            OrderNotification.OUTCOME_UNKNOWN
            if getattr(exc, "outcome_unknown", False)
            else OrderNotification.OUTCOME_FAILED
        )
        notif.detail = str(exc)
        notif.save()
        return notif
    _apply_sent(notif, sent)
    notif.save()
    return notif


def _schedule_delivery_followup(order) -> OrderNotification | None:
    to_number = _recipient_for(order)
    if not to_number:
        return None
    delay = getattr(settings, "TWILIO_FOLLOWUP_DELAY_DAYS", 3)
    send_at = timezone.now() + dt.timedelta(days=delay)
    notif = OrderNotification.objects.create(
        order=order,
        kind=OrderNotification.KIND_DELIVERY_SURVEY,
        to_number=to_number,
        is_followup=True,
        scheduled_send_at=send_at,
        local_outcome=OrderNotification.OUTCOME_PENDING,
    )
    try:
        sent = gw.schedule_followup(to_number, _followup_body(order), send_at)
    except gw.GatewayError as exc:
        notif.local_outcome = (
            OrderNotification.OUTCOME_UNKNOWN
            if getattr(exc, "outcome_unknown", False)
            else OrderNotification.OUTCOME_FAILED
        )
        notif.detail = str(exc)
        notif.save()
        return notif
    _apply_sent(notif, sent)
    notif.save()
    return notif


def _cancel_pending_followups(order) -> None:
    pending = order.sms_notifications.filter(
        is_followup=True, followup_cancelled=False
    ).exclude(provider_sid="")
    for notif in pending:
        try:
            status = gw.cancel_scheduled(notif.provider_sid)
        except gw.GatewayError as exc:
            notif.detail = "cancel failed: %s" % exc
            notif.save(update_fields=["detail", "updated"])
            continue
        notif.provider_status = status
        notif.followup_cancelled = True
        notif.save(update_fields=["provider_status", "followup_cancelled", "updated"])


# --------------------------------------------------------------------------- #
# Operator transitions
# --------------------------------------------------------------------------- #
def dispatch_order(order):
    """Mark an order dispatched, tell the shopper, and queue the follow-up.

    No-op safe: a repeat dispatch does not re-message.
    """
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status == STATUS_DISPATCHED:
            return locked, False
        if STATUS_DISPATCHED not in locked.available_statuses():
            raise ServiceError(
                "cannot dispatch an order in status '%s'" % locked.status, status_code=409
            )
        locked.set_status(STATUS_DISPATCHED)
    _record_and_send(locked, OrderNotification.KIND_DISPATCHED, _dispatched_body(locked))
    _schedule_delivery_followup(locked)
    return locked, True


def cancel_order(order):
    """Cancel an order, call off any pending follow-up, and tell the shopper.

    No-op safe: a repeat cancel does not re-message. The follow-up is called off
    *before* the cancellation notice so a survey can never reach a cancelled order.
    """
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status == STATUS_CANCELLED:
            return locked, False
        if STATUS_CANCELLED not in locked.available_statuses():
            raise ServiceError(
                "cannot cancel an order in status '%s'" % locked.status, status_code=409
            )
        locked.set_status(STATUS_CANCELLED)
    _cancel_pending_followups(locked)
    _record_and_send(locked, OrderNotification.KIND_CANCELLED, _cancelled_body(locked))
    return locked, True


# --------------------------------------------------------------------------- #
# Operator: resend, dispose content, reconcile
# --------------------------------------------------------------------------- #
def resend_notification(source: OrderNotification, idempotency_key: str):
    """Re-send a message that did not reach the shopper.

    Idempotent on ``idempotency_key``: repeating under the same key returns the
    prior result without sending again; a fresh key sends a new message.
    Returns (notification, created_bool).
    """
    idempotency_key = (idempotency_key or "").strip()
    if not idempotency_key:
        raise ServiceError("an idempotency key is required")

    existing = OrderNotification.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, False

    if not source.to_number:
        raise ServiceError("the original message has no destination on file", status_code=422)

    # Claim the key first (unique constraint) so a concurrent duplicate cannot
    # produce a second message.
    try:
        with transaction.atomic():
            notif = OrderNotification.objects.create(
                order=source.order,
                kind=OrderNotification.KIND_RESEND,
                to_number=source.to_number,
                resend_of=source,
                idempotency_key=idempotency_key,
                local_outcome=OrderNotification.OUTCOME_PENDING,
            )
    except IntegrityError:
        return OrderNotification.objects.get(idempotency_key=idempotency_key), False

    body = _resend_body(source)
    try:
        sent = gw.send_sms(source.to_number, body)
    except gw.GatewayError as exc:
        notif.local_outcome = (
            OrderNotification.OUTCOME_UNKNOWN
            if getattr(exc, "outcome_unknown", False)
            else OrderNotification.OUTCOME_FAILED
        )
        notif.detail = str(exc)
        notif.save()
        # A send failure is a real operator-facing failure for this endpoint.
        raise ServiceError("could not re-send the message", status_code=502)
    _apply_sent(notif, sent)
    notif.save()
    return notif, True


def dispose_content(notification: OrderNotification) -> OrderNotification:
    """Dispose of a message's text at the provider (redact), keeping the record."""
    if notification.provider_sid:
        try:
            status = gw.redact_content(notification.provider_sid)
        except gw.ConfigurationError:
            raise ServiceError("messaging is not configured", status_code=503)
        except gw.GatewayError:
            raise ServiceError("could not dispose of the message content", status_code=502)
        notification.provider_status = status
    notification.content_disposed = True
    notification.save()
    return notification


def refresh_status(notification: OrderNotification) -> OrderNotification:
    """Best-effort refresh of a message's live delivery outcome from Twilio."""
    if not notification.provider_sid:
        return notification
    if notification.followup_cancelled:
        return notification
    if notification.provider_status in _TERMINAL_STATUSES:
        return notification
    try:
        sent = gw.fetch_status(notification.provider_sid)
    except gw.GatewayError:
        return notification
    changed = False
    if sent.status and sent.status != notification.provider_status:
        notification.provider_status = sent.status
        changed = True
    if sent.error_code != notification.provider_error_code:
        notification.provider_error_code = sent.error_code
        changed = True
    if sent.error_message and sent.error_message != notification.provider_error_message:
        notification.provider_error_message = sent.error_message
        changed = True
    if sent.date_sent and sent.date_sent != notification.provider_date_sent:
        notification.provider_date_sent = sent.date_sent
        changed = True
    if changed:
        notification.save()
    return notification


def _in_window(notif: OrderNotification, start: dt.datetime, end: dt.datetime) -> bool:
    when = notif.provider_date_sent or notif.created
    return when is not None and start <= when < end


def reconcile(date_from: dt.datetime, date_to: dt.datetime) -> dict:
    """Line up the provider's record of messages against the app's own.

    Counts only messages sent from TWILIO_FROM_NUMBER (asked of the provider,
    not filtered after). Immediate order notifications carry that From; scheduled
    follow-ups go via the Messaging Service and are out of this From-scoped report.
    """
    provider_messages = gw.list_from_number(date_from, date_to)
    provider_by_sid = {m.sid: m for m in provider_messages}

    # App records the app believes it sent from our number in this window.
    candidates = OrderNotification.objects.exclude(provider_sid="").filter(is_followup=False)
    app_in_window = [n for n in candidates if _in_window(n, date_from, date_to)]
    app_by_sid = {n.provider_sid: n for n in app_in_window}

    matched = sorted(set(app_by_sid) & set(provider_by_sid))
    app_only = sorted(set(app_by_sid) - set(provider_by_sid))
    provider_only = sorted(set(provider_by_sid) - set(app_by_sid))

    return {
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "fromNumber": settings.TWILIO_FROM_NUMBER,
        "counts": {
            "provider": len(provider_by_sid),
            "app": len(app_by_sid),
            "matched": len(matched),
            "appOnly": len(app_only),
            "providerOnly": len(provider_only),
        },
        "matched": [
            {
                "notificationId": app_by_sid[sid].pk,
                "providerSid": sid,
                "providerStatus": provider_by_sid[sid].status,
                "appOutcome": app_by_sid[sid].local_outcome,
            }
            for sid in matched
        ],
        "appOnly": [
            {
                "notificationId": app_by_sid[sid].pk,
                "providerSid": sid,
                "appOutcome": app_by_sid[sid].local_outcome,
            }
            for sid in app_only
        ],
        "providerOnly": [
            {
                "providerSid": m.sid,
                "providerStatus": m.status,
                "dateSent": m.date_sent.isoformat() if m.date_sent else None,
            }
            for m in (provider_by_sid[sid] for sid in provider_only)
        ],
    }


# --------------------------------------------------------------------------- #
# Message bodies
# --------------------------------------------------------------------------- #
def _placed_body(order) -> str:
    return "Thanks! Your order %s has been placed." % order.number


def _dispatched_body(order) -> str:
    return "Good news — your order %s is on its way!" % order.number


def _cancelled_body(order) -> str:
    return "Your order %s has been cancelled." % order.number


def _followup_body(order) -> str:
    return "How did the delivery of your order %s go? We'd love your feedback." % order.number


def _resend_body(source: OrderNotification) -> str:
    bodies = {
        OrderNotification.KIND_PLACED: _placed_body,
        OrderNotification.KIND_DISPATCHED: _dispatched_body,
        OrderNotification.KIND_CANCELLED: _cancelled_body,
        OrderNotification.KIND_DELIVERY_SURVEY: _followup_body,
        OrderNotification.KIND_RESEND: _dispatched_body,
    }
    origin = source.resend_of or source
    builder = bodies.get(origin.kind, _dispatched_body)
    return builder(source.order)
