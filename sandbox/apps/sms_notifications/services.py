"""
Order SMS notifications: what happens when an order moves, and what an
operator can do about a message afterwards.

Every provider write goes through ``safe_write``; this module decides which
writes to make and what to tell the caller about them.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.apps.order.exceptions import InvalidOrderStatus
from oscar.core.loading import get_class, get_model
from twilio_sdk.core import ApiError
from twilio_sdk.models.enums import MessageEnumStatus

from . import twilio_gateway as gw
from .models import ContactNumber, Notification, ProviderWrite
from .safe_write import NotSent, OutcomeUnknown, apply_record, complete, safe_write, settle

logger = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")
ShippingEvent = get_model("order", "ShippingEvent")
ShippingEventType = get_model("order", "ShippingEventType")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
SurchargeApplicator = get_class("checkout.applicator", "SurchargeApplicator")
Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")

STATUS_DISPATCHED = "Dispatched"
STATUS_CANCELLED = "Cancelled"

MESSAGE_TEMPLATES = {
    Notification.KIND_PLACED: "Thanks for your order %(number)s! We'll text you when it ships.",
    Notification.KIND_DISPATCHED: "Good news: your order %(number)s is on its way.",
    Notification.KIND_FOLLOW_UP: "How did the delivery of order %(number)s go? Reply and let us know.",
    Notification.KIND_CANCELLED: "Your order %(number)s has been cancelled.",
}


class ServiceError(Exception):
    """A request this app refuses, with the HTTP status to answer it with."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# --------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------


def reference(*parts: object) -> str:
    """A write's reference: this install's prefix, then what identifies the write."""
    prefix = getattr(settings, "SMS_REFERENCE_PREFIX", "oscar-sandbox")
    return ":".join([prefix, *(str(p) for p in parts)])


def reference_tag(ref: str) -> str:
    """The short form of a reference that travels in the message body."""
    return hashlib.sha256(ref.encode()).hexdigest()[:10]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def message_text(order: Any, kind: str, ref: str) -> str:
    return "%s Ref %s" % (MESSAGE_TEMPLATES[kind] % {"number": order.number}, reference_tag(ref))


# --------------------------------------------------------------------------
# Contact numbers
# --------------------------------------------------------------------------


def register_contact_number(user, raw_number: str) -> tuple[ContactNumber, bool]:
    raw_number = (raw_number or "").strip()
    if not raw_number or len(raw_number) > 32:
        raise ServiceError(400, "phoneNumber is required.")
    result = gw.lookup_number(raw_number)
    if not result.valid or not result.phone_number:
        raise ServiceError(422, "The messaging provider does not consider this a usable phone number.")
    try:
        with transaction.atomic():
            contact = ContactNumber.objects.create(
                user=user, phone_number=result.phone_number, country_code=result.country_code or ""
            )
        return contact, True
    except IntegrityError:
        # Already on file (the canonical forms match): the same registration.
        return ContactNumber.objects.get(user=user, phone_number=result.phone_number, deleted_at__isnull=True), False


def active_contact_number(user) -> ContactNumber | None:
    return ContactNumber.objects.filter(user=user, deleted_at__isnull=True).order_by("-created_at", "-id").first()


def remove_contact_number(user, contact_id: int) -> tuple[ContactNumber, list[CallOff]]:
    with transaction.atomic():
        try:
            contact = ContactNumber.objects.select_for_update().get(pk=contact_id, user=user, deleted_at__isnull=True)
        except ContactNumber.DoesNotExist:
            raise ServiceError(404, "Contact number not found.")
        contact.deleted_at = timezone.now()
        contact.save(update_fields=["deleted_at"])
    # Nothing may be sent to it again: call off anything still scheduled for it.
    follow_ups = Notification.objects.filter(contact_number=contact, kind=Notification.KIND_FOLLOW_UP)
    return contact, [call_off(n) for n in follow_ups]


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


def _send_write(notification: Notification) -> ProviderWrite | None:
    return notification.writes.filter(operation=ProviderWrite.OP_SEND).first()


def _send_outcome(record: gw.MessageRecord | str) -> str:
    if not isinstance(record, gw.MessageRecord) or not record.sid:
        return ProviderWrite.UNKNOWN  # an answer we cannot name
    return gw.status_from_provider(record.status)


def _find_send(write: ProviderWrite) -> gw.MessageRecord | None:
    """The provider's record of a send: by sid once known, else by the reference tag."""
    notification = write.notification
    if write.provider_sid or notification.provider_sid:
        found = gw.fetch_message(write.provider_sid or notification.provider_sid)
        return found if isinstance(found, gw.MessageRecord) else None
    return gw.find_message_by_tag(notification.contact_number.phone_number, reference_tag(write.reference))


def _perform_send(write: ProviderWrite) -> gw.MessageRecord:
    notification = write.notification
    contact = ContactNumber.objects.get(pk=notification.contact_number_id)
    if not contact.is_active:
        raise NotSent("contact number removed")
    if notification.send_at is not None and notification.kind == Notification.KIND_FOLLOW_UP:
        if Order.objects.filter(pk=notification.order_id, status=STATUS_CANCELLED).exists():
            raise NotSent("order cancelled")
    return gw.create_message(contact.phone_number, notification.body, send_at=notification.send_at)


def messaging_configured() -> bool:
    try:
        gw.get_client()
    except ImproperlyConfigured:
        logger.error("SMS notifications disabled: Twilio settings are incomplete.")
        return False
    return True


def send_notification(ref: str, notification_factory) -> Notification | None:
    """
    Send one notification through the safe write. Never raises: a message
    that cannot be sent must not fail the operation it is about.
    """
    try:
        write = safe_write(
            ref,
            operation=ProviderWrite.OP_SEND,
            notification_factory=notification_factory,
            send=_perform_send,
            find=_find_send,
            outcome_of=_send_outcome,
        )
        return write.notification
    except OutcomeUnknown as e:
        logger.warning("notification %s: outcome unknown", e.write.notification_id)
        return e.write.notification
    except (NotSent, ApiError, httpx.HTTPError, ValueError) as e:
        logger.warning("notification send not made (%s)", type(e).__name__)
        recorded = ProviderWrite.objects.filter(reference=ref).select_related("notification").first()
        return recorded.notification if recorded else None


def notify(order: Any, kind: str, *, send_at: datetime | None = None) -> Notification | None:
    """Tell the order's shopper about ``kind``. No number on file: no message."""
    if not messaging_configured():
        return None
    ref = reference("order", order.pk, kind)
    existing = ProviderWrite.objects.filter(reference=ref).select_related("notification").first()
    contact = active_contact_number(order.user) if order.user_id else None
    if existing is None and contact is None:
        return None

    def factory() -> Notification:
        assert contact is not None
        return Notification.objects.create(
            order=order,
            user=order.user,
            contact_number=contact,
            kind=kind,
            body=message_text(order, kind, ref),
            send_at=send_at,
        )

    return send_notification(ref, factory)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


@dataclass
class LineRequest:
    product_id: int
    quantity: int


def place_order(user, lines: list[LineRequest]) -> tuple[Any, Notification | None]:
    if not lines:
        raise ServiceError(400, "An order needs at least one line.")
    strategy = Selector().strategy(user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for line in lines:
            try:
                product = Product.objects.get(pk=line.product_id)
            except Product.DoesNotExist:
                raise ServiceError(400, "Unknown catalogue item %s." % line.product_id)
            info = strategy.fetch_for_product(product)
            if not product.is_public or info.price.excl_tax is None:
                raise ServiceError(400, "Catalogue item %s is not for sale." % line.product_id)
            permitted, reason = info.availability.is_purchase_permitted(line.quantity)
            if not permitted:
                raise ServiceError(409, "Catalogue item %s: %s" % (line.product_id, reason))
            basket.add_product(product, line.quantity)
        shipping_method = Free()
        shipping_charge = shipping_method.calculate(basket)
        surcharges = SurchargeApplicator().get_applicable_surcharges(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge, surcharges=surcharges)
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            surcharges=surcharges,
        )
        basket.set_as_submitted()
    # The order is committed before any message is attempted.
    return order, notify(order, Notification.KIND_PLACED)


def _followup_delay() -> timedelta:
    return timedelta(seconds=int(getattr(settings, "SMS_FOLLOWUP_DELAY_SECONDS", 3 * 24 * 3600)))


def dispatch_order(order_id: int) -> tuple[Any, list[Notification]]:
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(pk=order_id)
        except Order.DoesNotExist:
            raise ServiceError(404, "Order not found.")
        if order.status != STATUS_DISPATCHED:
            try:
                order.set_status(STATUS_DISPATCHED)
            except InvalidOrderStatus:
                raise ServiceError(409, "Order %s cannot be dispatched from status %s." % (order.number, order.status))
            event_type, _ = ShippingEventType.objects.get_or_create(
                code="dispatched", defaults={"name": "Dispatched"}
            )
            event = ShippingEvent.objects.create(order=order, event_type=event_type, notes="Dispatched via API")
            for line in order.lines.all():
                event.line_quantities.create(line=line, quantity=line.quantity)

    notifications = [n for n in [notify(order, Notification.KIND_DISPATCHED)] if n]
    # The follow-up is queued with the provider, not held here.
    follow_up = notify(order, Notification.KIND_FOLLOW_UP, send_at=timezone.now() + _followup_delay())
    if follow_up:
        notifications.append(follow_up)
        # A cancel that ran while this was being scheduled must still win.
        order.refresh_from_db(fields=["status"])
        if order.status == STATUS_CANCELLED:
            call_off(follow_up)
    return order, notifications


@dataclass
class CallOff:
    notification: Notification
    outcome: str
    detail: str = ""


def call_off(notification: Notification) -> CallOff:
    """Make sure a scheduled follow-up never goes out. Never raises."""
    first_write = _send_write(notification)
    if first_write is None:
        return CallOff(notification, ProviderWrite.DONE, "never sent")
    send_write = settle(first_write, find=_find_send, outcome_of=_send_outcome)
    sid = send_write.provider_sid or notification.provider_sid
    if not sid:
        if send_write.outcome == ProviderWrite.FAILED:
            return CallOff(notification, ProviderWrite.DONE, "the provider never accepted it")
        # We cannot name what may be queued; the next request looks again.
        return CallOff(notification, ProviderWrite.UNKNOWN, "the follow-up's send is unsettled")
    if send_write.provider_status in (
        MessageEnumStatus.FAILED.value,
        MessageEnumStatus.UNDELIVERED.value,
        MessageEnumStatus.CANCELED.value,
    ):
        return CallOff(notification, ProviderWrite.DONE, "it will not be delivered")

    def cancel_outcome(record: gw.MessageRecord | str) -> str:
        if record == gw.GONE:
            return ProviderWrite.DONE
        assert isinstance(record, gw.MessageRecord)
        return gw.cancel_outcome(record.status)

    try:
        write = safe_write(
            reference("notification", notification.pk, "cancel"),
            operation=ProviderWrite.OP_CANCEL,
            notification_factory=lambda: notification,
            send=lambda w: gw.cancel_message(sid),
            find=lambda w: gw.fetch_message(sid),
            outcome_of=cancel_outcome,
            lookup_on_refusal=True,
        )
    except OutcomeUnknown as e:
        return CallOff(notification, e.write.outcome, "the provider did not confirm the call-off")
    except (ApiError, httpx.HTTPError, ValueError) as e:
        logger.warning("follow-up %s call-off not made (%s)", notification.pk, type(e).__name__)
        return CallOff(notification, ProviderWrite.FAILED, "the call-off could not be made")
    if write.outcome == ProviderWrite.SENDING:
        return CallOff(notification, write.outcome, "a call-off is in progress")
    notification.refresh_from_db()
    return CallOff(notification, write.outcome)


def cancel_order(order_id: int) -> tuple[Any, list[CallOff], Notification | None]:
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(pk=order_id)
        except Order.DoesNotExist:
            raise ServiceError(404, "Order not found.")
        if order.status != STATUS_CANCELLED:
            try:
                order.set_status(STATUS_CANCELLED)
            except InvalidOrderStatus:
                raise ServiceError(409, "Order %s cannot be cancelled from status %s." % (order.number, order.status))
    # Call off the follow-up first: that is the message that must never arrive.
    call_offs = []
    if messaging_configured():
        call_offs = [call_off(n) for n in order.sms_notifications.filter(kind=Notification.KIND_FOLLOW_UP)]
    return order, call_offs, notify(order, Notification.KIND_CANCELLED)


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------


def refresh_notifications(notifications) -> None:
    """Ask the provider about anything it has not finished with. Never sends."""
    for notification in notifications:
        write = _send_write(notification)
        if write is not None:
            try:
                settle(write, find=_find_send, outcome_of=_send_outcome)
            except ImproperlyConfigured:
                return
            notification.refresh_from_db()


def notification_outcome(notification: Notification) -> str:
    write = _send_write(notification)
    return write.outcome if write else ProviderWrite.UNKNOWN


# --------------------------------------------------------------------------
# Operator actions
# --------------------------------------------------------------------------


def resend(notification_id: int, idempotency_key: str) -> tuple[Notification, ProviderWrite]:
    key = (idempotency_key or "").strip()
    if not key or len(key) > 255:
        raise ServiceError(400, "An idempotency key (Idempotency-Key header or idempotencyKey) is required.")
    try:
        original = Notification.objects.select_related("order", "contact_number").get(pk=notification_id)
    except Notification.DoesNotExist:
        raise ServiceError(404, "Notification not found.")
    if not messaging_configured():
        raise ServiceError(503, "Messaging is not configured.")
    key_hash = hash_key(key)
    ref = reference("notification", original.pk, "resend", key_hash[:32])

    if not ProviderWrite.objects.filter(reference=ref).exists():
        # A first request under this key: it must be a genuine re-send.
        refresh_notifications([original])
        original.refresh_from_db()
        if original.content_redacted_at:
            raise ServiceError(409, "This message's content was disposed of; it cannot be re-sent.")
        if not original.contact_number.is_active:
            raise ServiceError(409, "The shopper removed this number; nothing may be sent to it.")
        if original.order.status == STATUS_CANCELLED and original.kind != Notification.KIND_CANCELLED:
            raise ServiceError(409, "The order was cancelled; only its cancellation notice may be re-sent.")
        if original.provider_status not in (
            MessageEnumStatus.FAILED.value,
            MessageEnumStatus.UNDELIVERED.value,
        ) and notification_outcome(original) != ProviderWrite.FAILED:
            raise ServiceError(
                409, "Only a message that did not reach the shopper can be re-sent (status: %s)."
                % (original.provider_status or notification_outcome(original)),
            )

    def factory() -> Notification:
        return Notification.objects.create(
            order=original.order,
            user=original.user,
            contact_number=original.contact_number,
            kind=original.kind,
            body=message_text(original.order, original.kind, ref),
            resend_of=original,
        )

    try:
        write = safe_write(
            ref,
            operation=ProviderWrite.OP_SEND,
            notification_factory=factory,
            send=_perform_send,
            find=_find_send,
            outcome_of=_send_outcome,
            idempotency_key_hash=key_hash,
        )
    except OutcomeUnknown as e:
        write = e.write
    except NotSent:
        raise ServiceError(409, "The re-send was not made: the number or order changed.")
    except ApiError as e:
        raise gw.provider_error(e.status_code, e.error) from e
    except gw.NEVER_SENT as e:
        raise gw.ProviderError(502, "Could not reach the messaging provider; nothing was sent.") from e
    return write.notification, write


def dispose_content(notification_id: int) -> tuple[Notification, str]:
    """Have the provider dispose of a message's text; keep that it was sent."""
    try:
        notification = Notification.objects.select_related("contact_number").get(pk=notification_id)
    except Notification.DoesNotExist:
        raise ServiceError(404, "Notification not found.")
    if notification.content_redacted_at:
        return notification, ProviderWrite.DONE
    if not messaging_configured():
        raise ServiceError(503, "Messaging is not configured.")

    send_write = _send_write(notification)
    if send_write is not None:
        send_write = settle(send_write, find=_find_send, outcome_of=_send_outcome)
    sid = (send_write.provider_sid if send_write else "") or notification.provider_sid
    if not sid:
        if send_write is None or send_write.outcome == ProviderWrite.FAILED:
            # The provider never held it: disposing of our copy is all there is.
            _wipe_local_content(notification)
            return notification, ProviderWrite.DONE
        return notification, ProviderWrite.UNKNOWN

    try:
        write = safe_write(
            reference("notification", notification.pk, "redact"),
            operation=ProviderWrite.OP_REDACT,
            notification_factory=lambda: notification,
            send=lambda w: gw.redact_message(sid),
            find=lambda w: gw.fetch_message(sid),
            outcome_of=gw.redact_outcome,
            lookup_on_refusal=True,
        )
    except OutcomeUnknown as e:
        write = e.write
    except ApiError as e:
        raise gw.provider_error(e.status_code, e.error) from e
    except gw.NEVER_SENT as e:
        raise gw.ProviderError(502, "Could not reach the messaging provider.") from e
    notification.refresh_from_db()
    if write.outcome == ProviderWrite.DONE:
        _wipe_local_content(notification)
    return notification, write.outcome


def _wipe_local_content(notification: Notification) -> None:
    notification.body = ""
    notification.content_redacted_at = timezone.now()
    notification.save(update_fields=["body", "content_redacted_at", "updated_at"])


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


@dataclass
class Reconciliation:
    start: datetime
    end: datetime
    matched: list[tuple[Notification, gw.MessageRecord]] = field(default_factory=list)
    provider_only: list[gw.MessageRecord] = field(default_factory=list)
    local_only: list[Notification] = field(default_factory=list)
    unsettled: list[Notification] = field(default_factory=list)


def reconcile(start: datetime, end: datetime) -> Reconciliation:
    if not messaging_configured():
        raise ServiceError(503, "Messaging is not configured.")
    # The provider filters on whole GMT days: widen, then narrow back.
    start_day = datetime.combine(start.astimezone(dt_timezone.utc).date(), datetime.min.time(), dt_timezone.utc)
    end_day = datetime.combine(
        end.astimezone(dt_timezone.utc).date() + timedelta(days=1), datetime.min.time(), dt_timezone.utc
    )
    try:
        fetched = gw.list_messages_sent_from_us(start_day, end_day)
    except ApiError as e:
        raise gw.provider_error(e.status_code, e.error) from e
    except gw.NEVER_SENT as e:
        raise gw.ProviderError(502, "Could not reach the messaging provider.") from e
    except httpx.RequestError as e:
        raise gw.ProviderError(504, "The messaging provider did not answer in time.") from e
    except ValueError as e:
        raise gw.ProviderError(502, "Unreadable response from the messaging provider.") from e

    by_sid: dict[str, gw.MessageRecord] = {
        r.sid: r for r in fetched if r.sid and r.date_sent and start <= r.date_sent < end
    }

    # The provider's word updates what we hold, so both sides use its clock.
    known = {n.provider_sid: n for n in Notification.objects.filter(provider_sid__in=list(by_sid))}
    for sid, notification in known.items():
        record = by_sid[sid]
        write = _send_write(notification)
        if write is not None and write.outcome in (ProviderWrite.PENDING, ProviderWrite.UNKNOWN, ProviderWrite.SENDING):
            complete(write, _send_outcome(record), record)
        else:
            apply_record(notification, record)

    report = Reconciliation(start=start, end=end)
    local = Notification.objects.filter(provider_date_sent__gte=start, provider_date_sent__lt=end).select_related("order")
    for notification in local:
        found = by_sid.pop(notification.provider_sid, None)
        if found is None:
            report.local_only.append(notification)
        else:
            report.matched.append((notification, found))
    # Known to the app but its stored send time fell outside the window.
    for sid in list(by_sid):
        if sid in known:
            report.matched.append((known[sid], by_sid.pop(sid)))
    report.provider_only = list(by_sid.values())
    report.unsettled = list(
        Notification.objects.filter(
            provider_date_sent__isnull=True, created_at__gte=start, created_at__lt=end
        ).select_related("order")
    )
    return report
