"""
Order notifications by SMS: contact numbers, the messages that go out as an
order moves, and the operator actions on them.

Messaging never fails the order operation that triggered it: the order change
is committed first, then messaging runs in a guarded block whose result is only
reported.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from twilio_sdk.models.enums import MessageEnumStatus

from . import gateway
from .models import ContactNumber, Notification
from .safe_write import TERMINAL, SendRequest, apply_provider_state, check_by_reference, install_id, safe_send, send_window

log = logging.getLogger("apps.sms_notifications")

Outcome = Notification.Outcome
Kind = Notification.Kind

Order = get_model("order", "Order")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
ShippingRepository = get_class("shipping.repository", "Repository")
Selector = get_class("partner.strategy", "Selector")
InvalidOrderStatus = get_class("order.exceptions", "InvalidOrderStatus")

STATUS_DISPATCHED = "Dispatched"
STATUS_CANCELLED = "Cancelled"


class ServiceError(Exception):
    """A request this module refuses, with the HTTP status the API should answer."""

    def __init__(self, status_code: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# Contact numbers
# ---------------------------------------------------------------------------


def register_contact_number(user: Any, raw_number: str, country_code: str | None) -> tuple[ContactNumber, bool]:
    """Validate with the provider and store its canonical form. Returns (number, created)."""
    try:
        found = gateway.lookup_number(raw_number, country_code)
    except gateway.NumberNotUsable:
        raise ServiceError(422, "The messaging provider does not recognise this as a usable phone number.")
    except gateway.ProviderError as e:
        raise ServiceError(e.status_code, e.message, outcomeUnknown=False)
    try:
        with transaction.atomic():
            number = ContactNumber.objects.create(
                user=user,
                phone_number=found.phone_number,
                country_code=found.country_code,
                national_format=found.national_format,
            )
        return number, True
    except IntegrityError:
        existing = ContactNumber.objects.get(user=user, phone_number=found.phone_number, deleted_at__isnull=True)
        return existing, False


def active_contact_for(user: Any) -> ContactNumber | None:
    if user is None:
        return None
    return ContactNumber.objects.filter(user=user, deleted_at__isnull=True).first()


def remove_contact_number(number: ContactNumber) -> None:
    """Remove a number: it is never messaged again, including anything already queued for it."""
    with transaction.atomic():
        ContactNumber.objects.filter(pk=number.pk, deleted_at__isnull=True).update(deleted_at=timezone.now())
    # Queued with the provider, or possibly on its way there.
    queued = Notification.objects.filter(contact_number=number).filter(
        Q(provider_status=MessageEnumStatus.SCHEDULED)
        | Q(provider_sid__isnull=True, outcome__in=[Outcome.SENDING, Outcome.UNKNOWN])
    )
    request_cancellation(queued)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _order_reference(order: Any, kind: str) -> str:
    return f"{install_id()}:order:{order.pk}:{kind}"


def _message_text(order: Any, kind: str) -> str:
    shop = getattr(settings, "OSCAR_SHOP_NAME", "Oscar")
    match kind:
        case Kind.PLACED:
            return f"{shop}: thanks for your order {order.number}. We'll text you when it is on its way."
        case Kind.DISPATCHED:
            return f"{shop}: good news, order {order.number} has been dispatched and is on its way."
        case Kind.FOLLOWUP:
            return f"{shop}: how did the delivery of order {order.number} go? We'd love to hear from you."
        case Kind.CANCELLED:
            return f"{shop}: order {order.number} has been cancelled. Contact us if this is unexpected."
    raise ValueError(kind)


def _order_not_cancelled(order_id: int) -> str | None:
    status = Order.objects.filter(pk=order_id).values_list("status", flat=True).first()
    return "order was cancelled before the follow-up was queued" if status == STATUS_CANCELLED else None


def notify(order: Any, kind: str) -> Notification | None:
    """Send (or, for the follow-up, queue) one order message. Never raises."""
    try:
        contact = active_contact_for(order.user)
        if contact is None:
            return None  # a shopper with no number on file is simply not messaged
        scheduled_for = None
        before_send = None
        if kind == Kind.FOLLOWUP:
            if not settings.TWILIO_MESSAGING_SERVICE_SID:
                log.error("Follow-up for order %s not queued: TWILIO_MESSAGING_SERVICE_SID is not set", order.pk)
                return None
            scheduled_for = timezone.now() + timedelta(hours=settings.SMS_NOTIFICATIONS_FOLLOWUP_DELAY_HOURS)
            order_id = order.pk

            def before_send() -> str | None:
                return _order_not_cancelled(order_id)

        n = safe_send(
            SendRequest(
                reference=_order_reference(order, kind),
                kind=kind,
                order_id=order.pk,
                contact_number=contact,
                text=_message_text(order, kind),
                scheduled_for=scheduled_for,
            ),
            before_send=before_send,
        )
        honour_cancel_request(n)
        return n
    except Exception:
        log.exception("SMS %s for order %s could not be processed", kind, order.pk)
        return None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderItem:
    product_id: int
    quantity: int


def place_order(user: Any, items: Iterable[OrderItem], request: HttpRequest) -> tuple[Any, Notification | None]:
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for item in items:
            product = Product.objects.filter(pk=item.product_id).first()
            if product is None:
                raise ServiceError(422, f"Catalogue item {item.product_id} does not exist.")
            info = basket.strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(item.quantity)
            if product.is_parent or not permitted:
                raise ServiceError(422, f"Catalogue item {item.product_id} cannot be bought: {reason or 'not purchasable'}")
            basket.add_product(product, item.quantity)
        shipping_method = ShippingRepository().get_default_shipping_method(basket=basket, user=user, request=request)
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            order_number=OrderNumberGenerator().order_number(basket),
            request=request,
        )
        basket.submit()
    return order, notify(order, Kind.PLACED)


def _change_status(order_id: int, new_status: str) -> tuple[Any, bool]:
    """Returns (order, changed). A repeat of the same transition is not an error."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.status == new_status:
            return order, False
        try:
            order.set_status(new_status)
        except InvalidOrderStatus:
            raise ServiceError(409, f"Order cannot move from '{order.status}' to '{new_status}'.")
    return order, True


def dispatch_order(order_id: int) -> tuple[Any, bool]:
    order, changed = _change_status(order_id, STATUS_DISPATCHED)
    # References are per order and kind, so a repeated dispatch never sends twice.
    notify(order, Kind.DISPATCHED)
    notify(order, Kind.FOLLOWUP)
    return order, changed


def cancel_order(order_id: int) -> tuple[Any, bool]:
    order, changed = _change_status(order_id, STATUS_CANCELLED)
    try:
        request_cancellation(Notification.objects.filter(order=order, kind=Kind.FOLLOWUP))
    except Exception:
        log.exception("Cancelling the follow-up for order %s failed; the refresh will retry", order.pk)
    notify(order, Kind.CANCELLED)
    return order, changed


# ---------------------------------------------------------------------------
# Cancelling queued messages
# ---------------------------------------------------------------------------


def request_cancellation(notifications: Any) -> None:
    """Flag messages that must never go out, then cancel each one at the provider."""
    ids = list(notifications.values_list("pk", flat=True))
    with transaction.atomic():
        Notification.objects.filter(pk__in=ids, cancel_requested_at__isnull=True).update(
            cancel_requested_at=timezone.now()
        )
    for n in Notification.objects.filter(pk__in=ids):
        honour_cancel_request(n)


def honour_cancel_request(n: Notification) -> Notification:
    """If a cancel was requested and not yet settled, cancel at the provider (repeatable: it sets a fixed value)."""
    n.refresh_from_db()
    if n.cancel_requested_at is None or n.cancel_outcome in TERMINAL:
        return n
    if not n.provider_sid:
        if n.outcome == Outcome.FAILED:
            return _set_cancel(n, Outcome.DONE)  # nothing ever reached the provider
        if n.outcome == Outcome.UNKNOWN or (
            n.outcome == Outcome.SENDING and n.claimed_at <= timezone.now() - send_window()
        ):
            n = check_by_reference(n)
        if not n.provider_sid:
            # Still being sent (the sender re-checks this flag when it completes) or not found yet.
            return _set_cancel(n, Outcome.PENDING if n.outcome != Outcome.FAILED else Outcome.DONE)
    sid = n.provider_sid
    try:
        msg = gateway.cancel_message(sid)
    except gateway.NEVER_SENT:
        return _set_cancel(n, Outcome.PENDING)  # not attempted; retried on refresh
    except gateway.SDK_FAILURES as e:
        log.warning("Cancel of notification %s: %s; checking the message state", n.pk, type(e).__name__)
        try:
            msg = gateway.fetch_message(sid)  # a refusal usually means it is no longer cancellable
        except gateway.SDK_FAILURES:
            return _set_cancel(n, Outcome.UNKNOWN)
    apply_provider_state(n, msg)
    return _set_cancel(n, gateway.outcome_of_cancel(msg.status))


def _set_cancel(n: Notification, outcome: str) -> Notification:
    n.cancel_outcome = outcome
    fields = ["cancel_outcome", "updated_at"]
    if outcome == Outcome.DONE and n.canceled_at is None:
        n.canceled_at = timezone.now()
        fields.append("canceled_at")
    n.save(update_fields=fields)
    if outcome == Outcome.FAILED:
        log.error("Notification %s could not be cancelled: the provider had already sent it", n.pk)
    return n


# ---------------------------------------------------------------------------
# Refreshing from the provider (there is no callback URL: we ask)
# ---------------------------------------------------------------------------


def needs_refresh(n: Notification) -> bool:
    if n.outcome not in TERMINAL:
        return True
    return n.cancel_requested_at is not None and n.cancel_outcome not in TERMINAL


def refresh(n: Notification) -> Notification:
    """Bring one notification up to date with the provider. Never raises for a provider failure."""
    if n.outcome == Outcome.SENDING and n.claimed_at > timezone.now() - send_window():
        return n  # its sender is still at work
    if not n.provider_sid:
        if n.outcome in (Outcome.SENDING, Outcome.UNKNOWN):
            n = check_by_reference(n)
    elif n.outcome not in TERMINAL:
        try:
            apply_provider_state(n, gateway.fetch_message(n.provider_sid))
        except gateway.SDK_FAILURES as e:
            log.warning("Refreshing notification %s failed: %s", n.pk, type(e).__name__)
    return honour_cancel_request(n)


def refresh_many(notifications: Iterable[Notification], limit: int) -> None:
    done = 0
    for n in notifications:
        if done >= limit:
            return
        if needs_refresh(n):
            refresh(n)
            done += 1


# ---------------------------------------------------------------------------
# Operator actions
# ---------------------------------------------------------------------------


def _strip_token(n: Notification) -> str:
    suffix = f" (ref {n.ref_token})"
    return n.body[: -len(suffix)] if n.body.endswith(suffix) else n.body


def resend(original: Notification, idempotency_key: str, operator: Any) -> tuple[Notification, bool]:
    """Re-send a message that did not reach the shopper. Returns (notification, is_repeat)."""
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    reference = f"{install_id()}:resend:{original.pk}:{key_hash[:24]}"
    existing = Notification.objects.filter(reference=reference).first()
    if existing is None:
        # Guards apply to a new resend only; a repeat is answered from what was recorded.
        original = refresh(original)
        if original.content_disposed_at is not None:
            raise ServiceError(409, "The content of this message was disposed of; it cannot be re-sent.")
        if original.kind == Kind.FOLLOWUP and (
            original.cancel_requested_at is not None or original.order.status == STATUS_CANCELLED
        ):
            raise ServiceError(409, "This follow-up belongs to a cancelled order and must not be sent.")
        if original.outcome != Outcome.FAILED:
            raise ServiceError(
                409,
                f"Only a message that did not reach the shopper can be re-sent (outcome: {original.outcome}).",
            )
        if original.contact_number.deleted_at is not None:
            raise ServiceError(409, "The shopper removed this number; nothing may be sent to it.")
    n = safe_send(
        SendRequest(
            reference=reference,
            kind=Kind.RESEND,
            order_id=original.order_id,
            contact_number=original.contact_number,
            text=_strip_token(original),
            resend_of=original,
            idempotency_key_hash=key_hash,
            requested_by_id=getattr(operator, "pk", None),
        )
    )
    return n, existing is not None


def dispose_content(n: Notification) -> Notification:
    """Redact the message text at the provider, then locally; status and history are kept."""
    if n.content_disposed_at is not None:
        return n
    n = refresh(n)
    if not n.provider_sid:
        if n.outcome == Outcome.FAILED:
            return _mark_disposed(n)  # the text never reached the provider
        raise ServiceError(409, "The provider has not confirmed this message yet; try again shortly.")
    if n.outcome not in TERMINAL:
        raise ServiceError(
            409, "The message is still in flight (or queued); its content can be disposed of once it has finished."
        )
    with transaction.atomic():
        Notification.objects.filter(pk=n.pk, content_disposal_requested_at__isnull=True).update(
            content_disposal_requested_at=timezone.now()
        )
    sid = n.provider_sid
    try:
        gateway.redact_message(sid)
    except gateway.SDK_FAILURES as e:
        err = gateway.translate(e)
        if not err.outcome_unknown:
            if err.provider_http_status in (400, 404, 409, 422):
                raise ServiceError(409, "The provider refused to redact this message.", providerCode=err.provider_code)
            raise ServiceError(err.status_code, err.message, outcomeUnknown=False)
        # It may have landed: the fetch below is what decides.
    try:
        confirmed = gateway.fetch_message(sid)
    except gateway.SDK_FAILURES:
        raise ServiceError(504, "The redaction could not be confirmed with the provider.", outcomeUnknown=True)
    if confirmed.body not in ("", None):
        raise ServiceError(502, "The provider still returns the message content.", outcomeUnknown=False)
    apply_provider_state(n, confirmed)
    return _mark_disposed(n)


def _mark_disposed(n: Notification) -> Notification:
    n.body = ""
    n.content_disposed_at = timezone.now()
    n.save(update_fields=["body", "content_disposed_at", "updated_at"])
    return n
