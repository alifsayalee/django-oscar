"""
What the SMS notifications app does, independent of HTTP.

Every provider write goes through :func:`.safe_write.safe_write` with a reference derived from the operation, and every
provider read goes through :func:`.provider.read`. Phone numbers are never logged: log lines name records by id.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from twilio_sdk.core import UNSET, ApiError
from twilio_sdk.models import ApiV2010AccountMessage

from . import provider
from .models import ContactNumber, Notification, ProviderAction
from .outcomes import (
    DONE, FAILED, PENDING, SENDING, SKIPPED, UNKNOWN, UNSETTLED, aggregate, cancel_outcome, redaction_outcome,
    status_from_provider)
from .safe_write import (
    SEND_WINDOW, Answer, ClaimStore, NotNeeded, OutcomeUnknown, Unreadable, WriteRefused, safe_write)

logger = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
SurchargeApplicator = get_class("checkout.applicator", "SurchargeApplicator")
Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")

DISPATCHED = "Dispatched"
CANCELLED = "Cancelled"

FOLLOW_UP_KINDS = (Notification.DELIVERY_FOLLOW_UP,)


class ServiceError(Exception):
    """A request this app refuses, with the status to answer it with."""

    def __init__(self, status_code: int, message: str, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------------------------------------------------
# References and message text
# ---------------------------------------------------------------------------------------------------------------------

def reference(*parts) -> str:
    """A reference unique to this install and to the operation - the same on every attempt and every repeat."""
    prefix = getattr(settings, "SMS_NOTIFICATIONS_REFERENCE_PREFIX", "oscar-sandbox")
    return ":".join([prefix] + [str(p) for p in parts])


def token_for(ref: str) -> str:
    """A short, searchable stand-in for ``ref`` that travels in the message body (the provider has no other field)."""
    return hashlib.sha256(ref.encode("utf-8")).hexdigest()[:10]


def message_text(kind: str, order, token: str) -> str:
    shop = getattr(settings, "OSCAR_SHOP_NAME", "Oscar")
    texts = {
        Notification.ORDER_PLACED: "%s: thanks for your order %s! We'll text you when it ships.",
        Notification.ORDER_DISPATCHED: "%s: good news - order %s is on its way.",
        Notification.DELIVERY_FOLLOW_UP: "%s: how did the delivery of order %s go? We'd love to hear from you.",
        Notification.ORDER_CANCELLED: "%s: your order %s has been cancelled. Contact us if you didn't ask for this.",
    }
    return (texts[kind] % (shop, order.number)) + " (ref %s)" % token


def follow_up_delay() -> timedelta:
    return timedelta(days=int(getattr(settings, "SMS_FOLLOW_UP_DELAY_DAYS", 3)))


# ---------------------------------------------------------------------------------------------------------------------
# Reading the provider's answer
# ---------------------------------------------------------------------------------------------------------------------

def message_fields(message: ApiV2010AccountMessage) -> dict:
    status = message.status
    return {
        "provider_sid": message.sid,
        "provider_status": str(status) if status is not UNSET and status is not None else "",
        "provider_error_code": message.error_code if isinstance(message.error_code, int) else None,
        "provider_created_at": provider.parse_provider_time(message.date_created),
        "provider_sent_at": provider.parse_provider_time(message.date_sent),
        "last_checked_at": timezone.now(),
    }


def read_send(message: ApiV2010AccountMessage) -> Answer:
    """A send's answer. A 2xx without a sid or a status names nothing we can act on: the outcome is unknown."""
    sid = message.sid
    if not isinstance(sid, str) or not sid or message.status is UNSET:
        raise Unreadable()
    fields = message_fields(message)
    return Answer(sid, message.status, fields["provider_sent_at"] or fields["provider_created_at"], fields)


GONE = object()  # the provider no longer holds the message: for a call-off, that is "off"


def gone_or(call):
    try:
        return call()
    except ApiError as e:
        if e.status_code == 404:
            return GONE
        raise


def read_call_off(notification: Notification):
    def read(result) -> Answer:
        if result is GONE:
            from twilio_sdk.models.enums import MessageEnumStatus

            return Answer(notification.provider_sid, MessageEnumStatus.CANCELED, timezone.now(),
                          {"provider_status": "gone", "provider_time": timezone.now()})
        if not provider.has_sid(result) or result.status is UNSET:
            raise Unreadable()
        when = provider.parse_provider_time(result.date_updated) or timezone.now()
        return Answer(result.sid, result.status, when, {"provider_status": str(result.status), "provider_time": when})
    return read


def read_redaction(message) -> Answer:
    if not provider.has_sid(message):
        raise Unreadable()
    when = provider.parse_provider_time(message.date_updated) or timezone.now()
    body = message.body
    return Answer(message.sid, body, when, {
        "provider_status": "redacted" if body == "" else "not redacted",
        "provider_time": when,
    })


def record_message(notification: Notification, message: ApiV2010AccountMessage) -> Notification:
    """Store what a *read* told us about a message we sent, keeping the send's outcome in step with it."""
    if not provider.has_sid(message):
        return notification
    for name, value in message_fields(message).items():
        setattr(notification, name, value)
    notification.outcome = status_from_provider(message.status)
    notification.save()
    return notification


# ---------------------------------------------------------------------------------------------------------------------
# Contact numbers
# ---------------------------------------------------------------------------------------------------------------------

def register_contact_number(user, raw: str) -> tuple[ContactNumber, bool]:
    """
    Ask the provider whether ``raw`` is a usable destination and store its canonical form.

    Returns the stored number and whether it was newly created (a repeat returns the existing one).
    """
    raw = (raw or "").strip()
    if not raw or len(raw) > 32:
        raise ServiceError(400, "phoneNumber is required and must be at most 32 characters.")
    try:
        result = provider.lookup_number(raw)
    except provider.ProviderError as e:
        if e.status_code in (400, 404):
            raise ServiceError(400, "The SMS provider does not recognise this as a phone number.") from e
        raise
    if result.valid is False:
        errors = result.validation_errors if isinstance(result.validation_errors, list) else []
        raise ServiceError(400, "The SMS provider does not consider this number a usable destination.",
                           validationErrors=[str(e) for e in errors])
    canonical = result.phone_number
    if result.valid is not True or not isinstance(canonical, str) or not canonical:
        raise provider.ProviderError(502, "The SMS provider's validation answer could not be read.")
    country = result.country_code if isinstance(result.country_code, str) else ""
    try:
        with transaction.atomic():
            return ContactNumber.objects.create(user=user, phone_number=canonical, country_code=country), True
    except IntegrityError:
        return ContactNumber.objects.get(user=user, phone_number=canonical), False


def current_number(user) -> ContactNumber | None:
    if user is None:
        return None
    return ContactNumber.objects.filter(user=user).order_by("-created_at", "-pk").first()


@dataclass
class RemovalResult:
    outcome: str
    call_offs: list = field(default_factory=list)


def remove_contact_number(contact: ContactNumber) -> RemovalResult:
    """
    Remove a number, then call off every follow-up still queued with the provider for it.

    The row goes first so that no new message can pick the number; the queued follow-ups are then called off one by
    one. Any call-off that cannot be settled now is retried whenever its notification is next read.
    """
    user, number = contact.user, contact.phone_number
    contact.delete()
    open_follow_ups = Notification.objects.filter(
        user=user, to_number=number, kind__in=FOLLOW_UP_KINDS, outcome__in=UNSETTLED + (FAILED,)
    )
    call_offs = []
    for notification in open_follow_ups:
        # A follow-up whose send was refused holds nothing at the provider; make sure nothing re-sends it.
        if _tombstone_released_claim(notification) or notification.outcome == FAILED:
            continue
        call_offs.append(call_off(notification))
    return RemovalResult(aggregate([c.outcome for c in call_offs]), call_offs)


# ---------------------------------------------------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------------------------------------------------

def _find_sent(notification: Notification):
    """Look up the message a notification's claim sent: by sid when we have it, else by the reference token."""
    if notification.provider_sid:
        return provider.fetch_sms(notification.provider_sid)
    if not notification.to_number:
        return None
    return provider.find_sms_by_token(notification.to_number, notification.ref_token, notification.claimed_at)


def _send_guarded(notification_ref: str, kind: str):
    """``send`` for a claimed notification: re-check, after the claim, everything that may forbid the send."""

    def send():
        record = Notification.objects.select_related("order").get(reference=notification_ref)
        if record.contact_number_id is None or not ContactNumber.objects.filter(pk=record.contact_number_id).exists():
            raise NotNeeded("contact number removed")
        if kind in FOLLOW_UP_KINDS and record.order.status == CANCELLED:
            raise NotNeeded("order cancelled")
        return provider.send_sms(record.to_number, record.body, send_at=record.send_at)

    return send


def send_notification(order, kind: str, *, ref: str | None = None, send_at: datetime | None = None,
                      resend_of: Notification | None = None, contact: ContactNumber | None = None) -> Notification:
    """
    Send one message about ``order`` exactly once per reference, and return its record.

    Never raises for a messaging problem: the record's outcome says what happened, and a later request (a repeat of
    the same operation, or any read of the notification) settles anything left unknown.
    """
    ref = ref or reference("order", order.pk, kind)
    token = token_for(ref)
    contact = contact if contact is not None else current_number(order.user)
    defaults = {
        "ref_token": token,
        "order": order,
        "user": order.user,
        "kind": kind,
        "resend_of": resend_of,
        "contact_number": contact,
        "to_number": contact.phone_number if contact else "",
        "body": message_text(kind, order, token),
        "send_at": send_at,
    }
    if contact is None:
        # Nobody to tell: record that, and hold the reference so a later call does not send it either.
        return _record_skipped(ref, defaults, "no contact number on file")

    store = ClaimStore(Notification, defaults)
    try:
        return safe_write(
            store, ref,
            send=_send_guarded(ref, kind),
            find=_find_sent,
            read=read_send,
            outcome_of=status_from_provider,
        )
    except (OutcomeUnknown, WriteRefused) as e:
        logger.warning("notification %s: %s", e.record.pk, type(e).__name__)
        return e.record
    except provider.ProviderError as e:
        logger.warning("notification ref-token %s: provider error %s", token, e.status_code)
        return Notification.objects.get(reference=ref)


def _record_skipped(ref: str, defaults: dict, reason: str) -> Notification:
    try:
        with transaction.atomic():
            return Notification.objects.create(
                reference=ref, outcome=SKIPPED, skip_reason=reason, claimed_at=timezone.now(), **defaults
            )
    except IntegrityError:
        return Notification.objects.get(reference=ref)


def _tombstone_released_claim(notification: Notification) -> bool:
    """Turn a released (refused, nothing at the provider) follow-up claim into a held ``skipped`` one, atomically."""
    changed = Notification.objects.filter(pk=notification.pk, outcome=FAILED, provider_sid="").update(
        outcome=SKIPPED, skip_reason="called off before it was sent"
    )
    return changed == 1


# ---------------------------------------------------------------------------------------------------------------------
# Order lifecycle
# ---------------------------------------------------------------------------------------------------------------------

def place_order(user, lines: list[dict]):
    """Place an order from catalogue items with Oscar's own basket and order machinery."""
    if not isinstance(lines, list) or not lines:
        raise ServiceError(400, "lines must be a non-empty list of {productId, quantity}.")
    wanted = []
    for line in lines:
        if not isinstance(line, dict):
            raise ServiceError(400, "each line must be an object with productId and quantity.")
        product_id, quantity = line.get("productId"), line.get("quantity", 1)
        if not isinstance(product_id, int) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
            raise ServiceError(400, "productId must be an integer and quantity an integer from 1 to 100.")
        wanted.append((product_id, quantity))

    strategy = Selector().strategy(user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in wanted:
            product = Product.objects.filter(pk=product_id).first()
            if product is None:
                raise ServiceError(400, "Product %s does not exist." % product_id, productId=product_id)
            info = strategy.fetch_for_product(product)
            if not product.is_public or info.price is None or not info.price.exists:
                raise ServiceError(400, "Product %s is not for sale." % product_id, productId=product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ServiceError(400, "Product %s: %s" % (product_id, reason), productId=product_id)
            basket.add_product(product, quantity)
        shipping_method = Free()
        shipping_charge = shipping_method.calculate(basket)
        surcharges = SurchargeApplicator().get_applicable_surcharges(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge, surcharges)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method, shipping_charge=shipping_charge,
            user=user, surcharges=surcharges,
        )
        basket.set_as_submitted()
    return order


def notify_order_placed(order) -> Notification:
    return send_notification(order, Notification.ORDER_PLACED)


def dispatch_order_state(order):
    """Mark the order dispatched (idempotent). Refuses a transition the order pipeline does not allow."""
    order.refresh_from_db()
    if order.status != DISPATCHED:
        if DISPATCHED not in order.available_statuses():
            raise ServiceError(409, "An order with status %r cannot be dispatched." % order.status)
        order.set_status(DISPATCHED)
    return order


def dispatch_notifications(order) -> list[Notification]:
    """Tell the shopper the order is on its way, and queue the delivery follow-up with the provider."""
    notice = send_notification(order, Notification.ORDER_DISPATCHED)
    follow_up = send_notification(order, Notification.DELIVERY_FOLLOW_UP, send_at=_follow_up_time(order))
    return [notice, follow_up]


def _follow_up_time(order) -> datetime:
    # Fixed per order, so a repeated dispatch asks for the same time.
    existing = Notification.objects.filter(reference=reference("order", order.pk, Notification.DELIVERY_FOLLOW_UP))
    record = existing.first()
    if record is not None and record.send_at is not None:
        return record.send_at
    return timezone.now() + follow_up_delay()


def cancel_order_state(order):
    """Mark the order cancelled (idempotent). Refuses a transition the order pipeline does not allow."""
    order.refresh_from_db()
    if order.status != CANCELLED:
        if CANCELLED not in order.available_statuses():
            raise ServiceError(409, "An order with status %r cannot be cancelled." % order.status)
        order.set_status(CANCELLED)
    return order


def cancel_notifications(order) -> dict:
    """Call off a follow-up that has not gone out, then tell the shopper the order is cancelled."""
    follow_up_call_off = call_off_follow_up(order)
    notice = send_notification(order, Notification.ORDER_CANCELLED)
    return {"notifications": [notice], "follow_up_call_off": follow_up_call_off}


@dataclass
class CallOff:
    notification: Notification | None
    outcome: str
    detail: str = ""
    action: ProviderAction | None = None


def call_off_follow_up(order) -> CallOff:
    """
    Make sure the order's delivery follow-up never reaches the shopper.

    If no follow-up was ever claimed, a ``skipped`` record takes its reference first, so a dispatch racing this
    cancel loses the claim. Otherwise the queued message is called off at the provider.
    """
    ref = reference("order", order.pk, Notification.DELIVERY_FOLLOW_UP)
    placeholder = {
        "ref_token": token_for(ref), "order": order, "user": order.user, "kind": Notification.DELIVERY_FOLLOW_UP,
        "to_number": "", "body": "",
    }
    record = _record_skipped(ref, placeholder, "order cancelled before dispatch")
    if record.outcome == SKIPPED:
        return CallOff(record, DONE, "nothing was queued")
    if _tombstone_released_claim(record):
        return CallOff(record, DONE, "nothing was queued")
    return call_off(record)


def call_off(notification: Notification) -> CallOff:
    """Cancel a queued (scheduled) message at the provider through the safe write."""
    if not notification.provider_sid:
        # We claimed the send but do not know yet what the provider holds: settle that first.
        refresh_notification(notification)
        notification.refresh_from_db()
        if notification.outcome == SKIPPED or _tombstone_released_claim(notification):
            return CallOff(notification, DONE, "nothing was queued")
        if not notification.provider_sid:
            outcome = PENDING if notification.outcome == SENDING else UNKNOWN
            return CallOff(notification, outcome, "the follow-up's send is not settled yet; will retry")

    if notification.provider_status == "canceled":
        return CallOff(notification, DONE, "already called off")
    if notification.outcome not in UNSETTLED:
        # Delivered, or the provider already tried and failed: there is nothing left to call off.
        return CallOff(notification, FAILED, "too late: the provider already attempted delivery")

    sid = notification.provider_sid
    store = ClaimStore(ProviderAction, {"notification": notification, "action": ProviderAction.CANCEL})
    try:
        action = safe_write(
            store, reference("cancel", notification.pk),
            send=lambda: gone_or(lambda: provider.cancel_scheduled_sms(sid)),
            find=lambda _record: gone_or(lambda: provider.fetch_sms(sid)),
            read=read_call_off(notification),
            outcome_of=cancel_outcome,
        )
    except (OutcomeUnknown, WriteRefused) as e:
        action = e.record
    except provider.ProviderError as e:
        return CallOff(notification, UNKNOWN, e.message)
    # The message's own state changed too: re-read it so its send outcome reflects the call-off.
    _refresh_quietly(notification)
    return CallOff(notification, action.outcome, action.detail, action)


# ---------------------------------------------------------------------------------------------------------------------
# Operator actions on a notification
# ---------------------------------------------------------------------------------------------------------------------

def resend(original: Notification, idempotency_key: str) -> Notification:
    """Send a message that did not reach the shopper again - once per caller-supplied idempotency key."""
    key = (idempotency_key or "").strip()
    if not key or len(key) > 255:
        raise ServiceError(400, "An Idempotency-Key (1-255 characters) is required.")
    ref = reference("resend", original.pk, hashlib.sha256(key.encode("utf-8")).hexdigest()[:32])

    # A repeat under the same key is answered from its own record (the claim still guards the send); only a first
    # request is checked for eligibility.
    repeat = Notification.objects.filter(reference=ref).first()
    if repeat is not None:
        if repeat.outcome in UNSETTLED and repeat.contact_number is not None:
            return send_notification(original.order, original.kind, ref=ref, resend_of=original,
                                     contact=repeat.contact_number)
        return _settle(repeat)
    _check_resendable(original)
    return send_notification(original.order, original.kind, ref=ref, resend_of=original,
                             contact=original.contact_number)


def _settle(notification: Notification) -> Notification:
    refresh_notification(notification)
    notification.refresh_from_db()
    return notification


def _check_resendable(original: Notification) -> None:
    refresh_notification(original)
    original.refresh_from_db()
    if original.outcome == SKIPPED:
        raise ServiceError(409, "Nothing was sent for this notification, so there is nothing to re-send.")
    if original.content_disposed_at is not None:
        raise ServiceError(409, "This notification's content was disposed of; it cannot be re-sent.")
    if original.kind in FOLLOW_UP_KINDS and (
        original.order.status == CANCELLED or original.provider_status == "canceled"
    ):
        raise ServiceError(409, "This follow-up was called off; re-sending it is not allowed.")
    if original.outcome != FAILED:
        # Delivered, still in flight, or not yet known: a second message could reach the shopper twice.
        raise ServiceError(409, "Only a message that failed can be re-sent (this one is %r)." % original.outcome,
                           outcome=original.outcome)
    contact = original.contact_number
    if contact is None or contact.user_id != original.order.user_id:
        raise ServiceError(409, "The number this message was sent to is no longer registered.")


def dispose_content(notification: Notification) -> ProviderAction | Notification:
    """Have the provider redact the message's text; the record of the send and its outcome survive."""
    if not notification.provider_sid and notification.outcome in UNSETTLED:
        _settle(notification)
    if not notification.provider_sid:
        if notification.outcome in UNSETTLED:
            raise OutcomeUnknown(notification)
        # Nothing ever reached the provider: only our own copy exists.
        _clear_local_content(notification)
        return notification

    sid = notification.provider_sid
    store = ClaimStore(ProviderAction, {"notification": notification, "action": ProviderAction.REDACT})
    try:
        action = safe_write(
            store, reference("redact", notification.pk),
            send=lambda: provider.redact_sms(sid),
            find=lambda _record: provider.fetch_sms(sid),
            read=read_redaction,
            outcome_of=redaction_outcome,
        )
    except WriteRefused as e:
        return e.record
    if action.outcome == DONE:
        _clear_local_content(notification)
    # The fact of the send and what became of it survive the redaction: keep them current.
    _refresh_quietly(notification)
    return action


def _clear_local_content(notification: Notification) -> None:
    notification.body = ""
    notification.content_disposed_at = notification.content_disposed_at or timezone.now()
    notification.save(update_fields=["body", "content_disposed_at", "updated_at"])


# ---------------------------------------------------------------------------------------------------------------------
# Refreshing from the provider (reads) - how a later request learns what became of a message
# ---------------------------------------------------------------------------------------------------------------------

def refresh_notification(notification: Notification) -> Notification:
    """Ask the provider about a message whose outcome is not settled. Never raises for a provider problem."""
    in_flight = notification.outcome == SENDING and notification.claimed_at > timezone.now() - SEND_WINDOW
    if notification.outcome not in UNSETTLED or in_flight:
        return notification
    try:
        found = provider.read(lambda: _find_sent(notification))
        if found is not None:
            record_message(notification, found)
    except (provider.ProviderError, provider.NotConfigured) as e:
        logger.info("notification %s: refresh failed (%s)", notification.pk, type(e).__name__)
    return notification


def _refresh_quietly(notification: Notification) -> None:
    if notification.provider_sid:
        try:
            record_message(notification, provider.fetch_sms_checked(notification.provider_sid))
        except (provider.ProviderError, provider.NotConfigured) as e:
            logger.info("notification %s: refresh failed (%s)", notification.pk, type(e).__name__)


def refresh_order(order) -> None:
    """Bring an order's notifications up to date, and push any call-off that is still unsettled."""
    for notification in order.sms_notifications.all():
        if notification.outcome in UNSETTLED:
            refresh_notification(notification)
        if notification.actions.filter(action=ProviderAction.CANCEL).exclude(outcome__in=(DONE, FAILED)).exists():
            call_off(notification)
    if order.status == CANCELLED:
        follow_up = order.sms_notifications.filter(kind=Notification.DELIVERY_FOLLOW_UP, resend_of=None).first()
        if follow_up is not None and follow_up.outcome in UNSETTLED and follow_up.provider_status != "canceled":
            call_off(follow_up)


# ---------------------------------------------------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------------------------------------------------

def reconcile(start: datetime, end: datetime) -> dict:
    """
    Line the provider's record of messages sent from our number in [start, end] up against ours.

    Both sides are selected on the provider's clock (when the provider sent the message). Records of ours that the
    provider has not given a sent time yet are refreshed first; those still without one are reported as unsettled.
    """
    if end < start:
        raise ServiceError(400, "'to' must not be before 'from'.")

    for notification in _unsettled_in(start, end):
        refresh_notification(notification)

    sent = provider.list_sent_sms(start, end)
    provider_by_sid: dict[str, list[ApiV2010AccountMessage]] = {}
    for message in sent.messages:
        provider_by_sid.setdefault(message.sid or "", []).append(message)

    local = list(Notification.objects.filter(provider_sent_at__gte=start, provider_sent_at__lte=end))
    matched, local_only = [], []
    for notification in local:
        records = provider_by_sid.pop(notification.provider_sid, [])
        entry = _local_entry(notification)
        if records:
            entry["providerStatus"] = str(records[0].status) if records[0].status is not UNSET else ""
            matched.append(entry)
        else:
            local_only.append(entry)
    provider_only = [_provider_entry(m) for records in provider_by_sid.values() for m in records]

    unsettled = [_local_entry(n) for n in _unsettled_in(start, end)]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "fromNumber": "configured sending number",
        "providerCount": len(sent.messages),
        "localCount": len(local),
        "matched": matched,
        "localOnly": local_only,
        "providerOnly": provider_only,
        "unsettled": unsettled,
        "truncated": sent.truncated,
        "consistent": not (local_only or provider_only or unsettled or sent.truncated),
    }


def _unsettled_in(start: datetime, end: datetime):
    """Our records with no provider sent time yet, whose outcome is still open, claimed or due in the window."""
    in_window = Q(claimed_at__gte=start, claimed_at__lte=end) | Q(send_at__gte=start, send_at__lte=end)
    return Notification.objects.filter(in_window, outcome__in=UNSETTLED, provider_sent_at__isnull=True)


def _local_entry(notification: Notification) -> dict:
    return {
        "notificationId": notification.pk,
        "orderId": notification.order_id,
        "kind": notification.kind,
        "providerSid": notification.provider_sid or None,
        "outcome": notification.outcome,
        "providerStatus": notification.provider_status or None,
        "sentAt": notification.provider_sent_at.isoformat() if notification.provider_sent_at else None,
    }


def _provider_entry(message: ApiV2010AccountMessage) -> dict:
    sent = provider.parse_provider_time(message.date_sent)
    return {
        "providerSid": message.sid,
        "providerStatus": str(message.status) if message.status is not UNSET else None,
        "sentAt": sent.isoformat() if sent else None,
        "direction": str(message.direction) if message.direction is not UNSET else None,
    }


__all__ = [
    "ServiceError", "register_contact_number", "remove_contact_number", "place_order", "notify_order_placed",
    "dispatch_order_state", "dispatch_notifications",
    "cancel_order_state", "cancel_notifications", "resend", "dispose_content", "refresh_order", "refresh_notification",
    "reconcile",
]
