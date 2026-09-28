"""
Order notifications: who gets told what, and what became of it.

Order operations never fail because a message could not be sent: every send
goes through ``safe_write`` and its failures are recorded on the notification,
not raised to the caller.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dt_time
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from twilio_sdk.core import UNSET, ApiError
from twilio_sdk.models import ApiV2010AccountMessage

from . import provider
from .models import ContactNumber, Notification, Outcome, ProviderAction
from .safe_write import (
    ClaimRecord,
    OutcomeUnknown,
    cancel_outcome,
    delivery_outcome,
    parse_provider_time,
    read_message,
    redact_outcome,
    safe_write,
    schedule_outcome,
    status_text,
)
from .twilio_client import mask_number

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
ShippingEventType = get_model("order", "ShippingEventType")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
EventHandler = get_class("order.processing", "EventHandler")
Selector = get_class("partner.strategy", "Selector")
FreeShipping = get_class("shipping.methods", "Free")

DISPATCHED_EVENT_CODE = "dispatched"
DISPATCHED_EVENT_NAME = "Dispatched"
DISPATCHED_ORDER_STATUS = "Being processed"
CANCELLED_ORDER_STATUS = "Cancelled"

# What a read from the provider may change (never the text, which a content
# disposal may clear concurrently).
PROVIDER_STATE_FIELDS = [
    "outcome",
    "provider_sid",
    "provider_status",
    "provider_error_code",
    "provider_error_message",
    "provider_date_created",
    "provider_date_sent",
    "provider_checked_at",
]
TERMINAL_STATUSES = {"delivered", "read", "failed", "undelivered", "canceled"}
REFRESH_INTERVAL = timedelta(seconds=30)


class ServiceError(Exception):
    """A request this app refuses, with the HTTP status to answer."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class ProviderUnavailable(ServiceError):
    """The provider could not answer a read (never a statement about the caller)."""


def reference(*parts: object) -> str:
    return ":".join([settings.SMS_REFERENCE_PREFIX, *(str(p) for p in parts)])


def reference_code(ref: str) -> str:
    """Short, stable code for a reference; appended to the SMS so it can be found."""
    return hashlib.sha256(ref.encode()).hexdigest()[:10]


def compose_body(text: str, ref: str) -> str:
    return "%s Ref %s" % (text, reference_code(ref))


def provider_error_code(e: ApiError[Any]) -> object:
    """Twilio's error code from a RawError body, without logging the body."""
    try:
        payload = e.error.json()
    except ValueError:
        return None
    return payload.get("code") if isinstance(payload, dict) else None


# --- claim stores -------------------------------------------------------------


def _apply_message(record: Notification, message: ApiV2010AccountMessage, status: object) -> None:
    got = read_message(message)
    if got is not None:
        record.provider_sid = got.provider_id
        record.provider_date_sent = got.provider_time
    record.provider_status = status_text(status)
    error_code = message.error_code
    record.provider_error_code = error_code if isinstance(error_code, int) else None
    error_message = message.error_message
    record.provider_error_message = (
        error_message[:255] if isinstance(error_message, str) else ""
    )
    record.provider_date_created = parse_provider_time(message.date_created)
    record.provider_checked_at = timezone.now()


class NotificationSendStore:
    """The claim for a send is the Notification row itself."""

    def __init__(self, ref: str, **fields: Any) -> None:
        self.reference = ref
        self._fields = fields

    def try_claim(self) -> bool:
        now = timezone.now()
        try:
            with transaction.atomic():
                Notification.objects.create(
                    reference=self.reference,
                    outcome=Outcome.SENDING,
                    claimed_at=now,
                    **self._fields,
                )
            return True
        except IntegrityError:
            # A failed send with no provider id never happened: it releases
            # the claim, and the next request may take it again.
            return bool(
                Notification.objects.filter(
                    reference=self.reference, outcome=Outcome.FAILED, provider_sid=""
                ).update(outcome=Outcome.SENDING, claimed_at=now, completed_at=None)
            )

    def load(self) -> Notification:
        return Notification.objects.get(reference=self.reference)

    def complete(
        self,
        outcome: str,
        message: ApiV2010AccountMessage | None = None,
        status: object = UNSET,
    ) -> Notification:
        with transaction.atomic():
            record = Notification.objects.select_for_update().get(reference=self.reference)
            record.outcome = outcome
            record.completed_at = timezone.now()
            if message is not None:
                _apply_message(record, message, status)
            record.save()
        return record


class CheckOnlyStore(NotificationSendStore):
    """A held send claim, checked by lookup: it can never be taken to send again."""

    def try_claim(self) -> bool:
        return False


class ActionStore:
    """The claim for a cancel or redact is a ProviderAction row."""

    def __init__(self, notification: Notification, action: str) -> None:
        self.notification = notification
        self.action = action
        self.reference = reference("notification", notification.pk, action)

    def try_claim(self) -> bool:
        now = timezone.now()
        try:
            with transaction.atomic():
                ProviderAction.objects.create(
                    reference=self.reference,
                    notification=self.notification,
                    action=self.action,
                    outcome=Outcome.SENDING,
                    claimed_at=now,
                )
            return True
        except IntegrityError:
            return bool(
                ProviderAction.objects.filter(
                    reference=self.reference, outcome=Outcome.FAILED, provider_sid=""
                ).update(outcome=Outcome.SENDING, claimed_at=now, completed_at=None)
            )

    def load(self) -> ProviderAction:
        return ProviderAction.objects.get(reference=self.reference)

    def complete(
        self,
        outcome: str,
        message: ApiV2010AccountMessage | None = None,
        status: object = UNSET,
    ) -> ProviderAction:
        with transaction.atomic():
            action = ProviderAction.objects.select_for_update().get(reference=self.reference)
            action.outcome = outcome
            action.completed_at = timezone.now()
            if message is not None:
                got = read_message(message)
                if got is not None:
                    action.provider_sid = got.provider_id
                action.provider_status = status_text(status)
                notification = Notification.objects.select_for_update().get(
                    pk=self.notification.pk
                )
                _apply_message(notification, message, status)
                notification.save()
            action.save()
        return action


# --- sending ------------------------------------------------------------------


def active_contact(user: Any) -> ContactNumber | None:
    return (
        ContactNumber.objects.filter(user=user, removed_at__isnull=True)
        .order_by("-created_at", "-id")
        .first()
    )


def _send(
    store: NotificationSendStore, to: str, body: str, send_at: datetime | None
) -> ClaimRecord:
    """Runs one send through the safe write; records, never raises, provider failures."""
    if send_at is None:

        def send(ref: str) -> ApiV2010AccountMessage:
            return provider.send_now(to, body)

        outcome_of = delivery_outcome
    else:
        scheduled_at = send_at

        def send(ref: str) -> ApiV2010AccountMessage:
            return provider.send_scheduled(to, body, scheduled_at)

        outcome_of = schedule_outcome

    def find(ref: str) -> ApiV2010AccountMessage | None:
        return provider.find_message_by_code(to, reference_code(ref))

    try:
        return safe_write(store, send, find, outcome_of, repeat_is_safe=False)
    except ApiError as e:
        logger.warning(
            "send %s refused by provider: HTTP %s code %s",
            store.reference,
            e.status_code,
            provider_error_code(e),
        )
    except OutcomeUnknown:
        logger.warning("send %s outcome unknown; left for a later check", store.reference)
    except httpx.RequestError as e:
        logger.warning("send %s never left: %s", store.reference, type(e).__name__)
    return store.load()


def notify(order: Any, kind: str, text: str, send_at: datetime | None = None) -> Notification | None:
    """Tell the order's shopper; None when they have no number on file."""
    contact = active_contact(order.user)
    if contact is None:
        return None
    ref = reference("order", order.number, kind)
    store = NotificationSendStore(
        ref,
        order=order,
        user=order.user,
        contact_number=contact,
        kind=kind,
        text=text,
        send_at=send_at,
    )
    try:
        # Content already chosen for this reference wins over a new rendering.
        existing = Notification.objects.get(reference=ref)
        body = compose_body(existing.text, ref)
        to = existing.contact_number.phone_number
    except Notification.DoesNotExist:
        body = compose_body(text, ref)
        to = contact.phone_number
    _send(store, to, body, send_at)
    notification = Notification.objects.get(reference=ref)
    if notification.kind == Notification.FOLLOW_UP and notification.cancel_requested_at:
        # The order was cancelled while this follow-up was being queued.
        cancel_follow_up(notification)
        notification.refresh_from_db()
    return notification


# --- cancelling a queued follow-up --------------------------------------------


def _resolve_send(notification: Notification) -> Notification:
    """Settle a send whose outcome is unknown, by looking it up (never re-sending)."""
    if notification.outcome not in (Outcome.SENDING, Outcome.UNKNOWN):
        return notification
    store = CheckOnlyStore(notification.reference)
    to = notification.contact_number.phone_number
    outcome_of = schedule_outcome if notification.send_at else delivery_outcome

    def send(ref: str) -> ApiV2010AccountMessage:
        # Unreachable: a check-only claim is never taken, so safe_write only looks.
        raise RuntimeError("a held send claim is only ever checked")

    def find(ref: str) -> ApiV2010AccountMessage | None:
        return provider.find_message_by_code(to, reference_code(ref))

    try:
        safe_write(store, send, find, outcome_of, repeat_is_safe=False)
    except (ApiError, OutcomeUnknown, httpx.RequestError):
        pass
    notification.refresh_from_db()
    return notification


@dataclass
class CancelResult:
    notification_id: int
    outcome: str
    detail: str = ""


def cancel_follow_up(notification: Notification) -> CancelResult:
    Notification.objects.filter(pk=notification.pk, cancel_requested_at__isnull=True).update(
        cancel_requested_at=timezone.now()
    )
    notification = _resolve_send(notification)
    if not notification.provider_sid:
        if notification.outcome == Outcome.FAILED:
            # Never queued with the provider: there is nothing that could go out.
            return CancelResult(notification.pk, Outcome.DONE, "never queued")
        # Still unresolved: cancel_requested_at makes the next check call it off.
        return CancelResult(notification.pk, Outcome.UNKNOWN, "queue outcome not yet known")

    message_sid = notification.provider_sid
    store = ActionStore(notification, ProviderAction.CANCEL)

    def send(ref: str) -> ApiV2010AccountMessage:
        return provider.cancel_message(message_sid)

    def find(ref: str) -> ApiV2010AccountMessage | None:
        return provider.fetch_message(message_sid)

    try:
        action = safe_write(store, send, find, cancel_outcome, repeat_is_safe=True)
    except ApiError as e:
        # Refused: typically the message already left the schedule. Read what it is now.
        logger.warning(
            "cancel of notification %s refused: HTTP %s code %s",
            notification.pk,
            e.status_code,
            provider_error_code(e),
        )
        _fetch_and_apply(notification)
        status = notification.provider_status
        return CancelResult(
            notification.pk,
            cancel_outcome(status) if status else Outcome.FAILED,
            "provider refused the cancel",
        )
    except (OutcomeUnknown, httpx.RequestError):
        return CancelResult(notification.pk, Outcome.UNKNOWN, "cancel outcome not yet known")
    if action.outcome in (Outcome.PENDING, Outcome.UNKNOWN):
        # Re-read: the cancel may have taken effect since.
        _recheck_action(ActionStore(notification, ProviderAction.CANCEL), cancel_outcome)
        action = ActionStore(notification, ProviderAction.CANCEL).load()
    return CancelResult(notification.pk, action.outcome)


def _recheck_action(store: ActionStore, outcome_of: Any) -> None:
    try:
        message = provider.fetch_message(store.notification.provider_sid)
    except (ApiError, httpx.RequestError, ValueError):
        return
    got = read_message(message)
    if got is not None:
        store.complete(outcome_of(got.status), message, got.status)


# --- provider-owned state -----------------------------------------------------


def _refreshed_outcome(notification: Notification, status: object) -> str:
    """The send's outcome in the light of the provider's latest status."""
    if notification.outcome not in (Outcome.PENDING, Outcome.DONE, Outcome.FAILED):
        return notification.outcome  # sending/unknown are settled by lookup only
    if notification.send_at is None:
        # An immediate message is done once delivered.
        return delivery_outcome(status)
    if notification.outcome == Outcome.PENDING:
        # A follow-up is done once the provider holds it for later; after
        # that its delivery is reported separately.
        mapped = schedule_outcome(status)
        return mapped if mapped != Outcome.UNKNOWN else notification.outcome
    return notification.outcome


def refresh_notification(notification: Notification, *, force: bool = False) -> Notification:
    """Bring the provider's view of one message up to date (reads only)."""
    if notification.outcome in (Outcome.SENDING, Outcome.UNKNOWN):
        claimed_long_ago = notification.claimed_at < timezone.now() - timedelta(seconds=60)
        if notification.outcome == Outcome.UNKNOWN or claimed_long_ago:
            notification = _resolve_send(notification)
    due = force or (
        notification.provider_checked_at is None
        or notification.provider_checked_at < timezone.now() - REFRESH_INTERVAL
    )
    if notification.provider_sid and due and notification.provider_status not in TERMINAL_STATUSES:
        _fetch_and_apply(notification)
    if (
        notification.kind == Notification.FOLLOW_UP
        and notification.cancel_requested_at
        and (
            not notification.provider_sid
            # Still queued (or not yet read): calling it off can still work.
            or cancel_outcome(notification.provider_status) in (Outcome.PENDING, Outcome.UNKNOWN)
        )
        and notification.outcome != Outcome.FAILED
    ):
        cancel_follow_up(notification)
        notification.refresh_from_db()
    return notification


def _fetch_and_apply(notification: Notification) -> None:
    """Read one message's provider state onto the notification (a read; never raises)."""
    try:
        message = provider.fetch_message(notification.provider_sid)
    except ApiError as e:
        logger.warning("refresh of notification %s failed: HTTP %s", notification.pk, e.status_code)
        return
    except (httpx.RequestError, ValueError) as e:
        logger.warning("refresh of notification %s failed: %s", notification.pk, type(e).__name__)
        return
    if read_message(message) is not None:
        _apply_message(notification, message, message.status)
        notification.outcome = _refreshed_outcome(notification, message.status)
        notification.save(update_fields=PROVIDER_STATE_FIELDS)


def refresh_order_notifications(order: Any) -> list[Notification]:
    return [refresh_notification(n) for n in order.sms_notifications.select_related("contact_number")]


# --- orders -------------------------------------------------------------------


def shop_name() -> str:
    return str(getattr(settings, "OSCAR_SHOP_NAME", "our shop"))


@transaction.atomic
def _create_order(user: Any, lines: list[tuple[int, int]]) -> Any:
    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(user=user)
    for product_id, quantity in lines:
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise ServiceError(422, "Unknown catalogue item %s." % product_id)
        info = basket.strategy.fetch_for_product(product)
        if not info.availability.is_available_to_buy:
            raise ServiceError(422, "Catalogue item %s is not available to buy." % product_id)
        permitted, reason = info.availability.is_purchase_permitted(quantity)
        if not permitted:
            raise ServiceError(422, "Catalogue item %s: %s" % (product_id, reason))
        basket.add_product(product, quantity)
    shipping_method = FreeShipping()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)
    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        user=user,
    )
    basket.submit()
    return order


def place_order(user: Any, lines: list[tuple[int, int]]) -> tuple[Any, Notification | None]:
    order = _create_order(user, lines)
    text = "Thanks for your order %s at %s (%s %s). We'll text you when it ships." % (
        order.number,
        shop_name(),
        Decimal(order.total_incl_tax).quantize(Decimal("0.01")),
        order.currency,
    )
    return order, notify(order, Notification.PLACED, text)


def is_dispatched(order: Any) -> bool:
    return bool(order.shipping_events.filter(event_type__code=DISPATCHED_EVENT_CODE).exists())


@dataclass
class OrderStepResult:
    order: Any
    notifications: list[Notification] = field(default_factory=list)
    follow_up_cancellations: list[CancelResult] = field(default_factory=list)


def dispatch_order(order: Any) -> OrderStepResult:
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status == CANCELLED_ORDER_STATUS:
            raise ServiceError(409, "Order %s is cancelled." % order.number)
        if not is_dispatched(order):
            if DISPATCHED_ORDER_STATUS in order.available_statuses():
                order.set_status(DISPATCHED_ORDER_STATUS)
            event_type, _ = ShippingEventType.objects.get_or_create(
                code=DISPATCHED_EVENT_CODE, defaults={"name": DISPATCHED_EVENT_NAME}
            )
            lines = list(order.lines.all())
            EventHandler().handle_shipping_event(
                order, event_type, lines, [line.quantity for line in lines]
            )
    result = OrderStepResult(order)
    on_its_way = notify(
        order,
        Notification.DISPATCHED,
        "Good news: your %s order %s is on its way." % (shop_name(), order.number),
    )
    if on_its_way is not None:
        result.notifications.append(on_its_way)
    follow_up_at = (timezone.now() + settings.SMS_FOLLOW_UP_DELAY).replace(microsecond=0)
    follow_up = notify(
        order,
        Notification.FOLLOW_UP,
        "How did the delivery of your %s order %s go? Reply and let us know."
        % (shop_name(), order.number),
        send_at=follow_up_at,
    )
    if follow_up is not None:
        result.notifications.append(follow_up)
    return result


def cancel_order(order: Any) -> OrderStepResult:
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status != CANCELLED_ORDER_STATUS:
            if CANCELLED_ORDER_STATUS not in order.available_statuses():
                raise ServiceError(
                    409, "Order %s cannot be cancelled from status %s." % (order.number, order.status)
                )
            order.set_status(CANCELLED_ORDER_STATUS)
        # Flag every follow-up first, so one still being queued is called off
        # by the request that is queuing it.
        order.sms_notifications.filter(
            kind=Notification.FOLLOW_UP, cancel_requested_at__isnull=True
        ).update(cancel_requested_at=timezone.now())
    result = OrderStepResult(order)
    for follow_up in order.sms_notifications.filter(kind=Notification.FOLLOW_UP):
        result.follow_up_cancellations.append(cancel_follow_up(follow_up))
    cancelled = notify(
        order,
        Notification.CANCELLED,
        "Your %s order %s has been cancelled." % (shop_name(), order.number),
    )
    if cancelled is not None:
        result.notifications.append(cancelled)
    return result


# --- contact numbers ----------------------------------------------------------


def register_contact_number(user: Any, raw_number: str, country_code: str | None) -> tuple[ContactNumber, bool]:
    try:
        result = provider.lookup_number(raw_number, country_code)
    except ApiError as e:
        if e.status_code == 404:
            raise ServiceError(422, "That is not a phone number the provider recognises.")
        if e.status_code in (401, 403):
            raise ProviderUnavailable(502, "The messaging provider refused our credentials.")
        if e.status_code == 429:
            raise ProviderUnavailable(503, "The messaging provider is rate-limiting us.")
        if 400 <= e.status_code < 500:
            raise ServiceError(422, "The provider rejected that phone number.")
        raise ProviderUnavailable(502, "The messaging provider is unavailable.")
    except (httpx.RequestError, ValueError):
        raise ProviderUnavailable(502, "The messaging provider could not be reached.")

    canonical = result.phone_number
    if result.valid is not True or not isinstance(canonical, str) or not canonical:
        raise ServiceError(422, "That number is not a usable mobile destination.")
    country = result.country_code if isinstance(result.country_code, str) else ""
    try:
        with transaction.atomic():
            contact = ContactNumber.objects.create(
                user=user, phone_number=canonical, country_code=country[:2]
            )
        return contact, True
    except IntegrityError:
        return (
            ContactNumber.objects.get(user=user, phone_number=canonical, removed_at__isnull=True),
            False,
        )


def remove_contact_number(contact: ContactNumber) -> list[CancelResult]:
    ContactNumber.objects.filter(pk=contact.pk, removed_at__isnull=True).update(
        removed_at=timezone.now()
    )
    # Nothing may reach a removed number again: call off anything still queued.
    results = []
    for follow_up in Notification.objects.filter(
        contact_number=contact, kind=Notification.FOLLOW_UP
    ).exclude(provider_status="canceled"):
        results.append(cancel_follow_up(follow_up))
    return results


# --- operator actions ---------------------------------------------------------


def resend(original: Notification, idempotency_key: str) -> Notification:
    if original.content_disposed_at is not None or not original.text:
        raise ServiceError(409, "The content of this message has been disposed of.")
    if original.contact_number.removed_at is not None:
        raise ServiceError(409, "The shopper has removed that number.")
    if (
        original.kind == Notification.FOLLOW_UP
        and original.order.status == CANCELLED_ORDER_STATUS
    ):
        raise ServiceError(409, "The order was cancelled; its follow-up must not be sent.")
    original = refresh_notification(original, force=True)
    if original.outcome in (Outcome.SENDING, Outcome.UNKNOWN):
        raise ServiceError(409, "The original message's outcome is not settled yet.")
    never_sent = original.outcome == Outcome.FAILED and not original.provider_sid
    status = original.provider_status
    reached_or_in_flight = delivery_outcome(status) != Outcome.FAILED if status else True
    if not never_sent and reached_or_in_flight:
        raise ServiceError(409, "The original message has not failed to reach the shopper.")

    key_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    ref = reference("resend", original.pk, key_digest)
    store = NotificationSendStore(
        ref,
        order=original.order,
        user=original.user,
        contact_number=original.contact_number,
        kind=original.kind,
        resend_of=original,
        text=original.text,
    )
    _send(store, original.contact_number.phone_number, compose_body(original.text, ref), None)
    return Notification.objects.get(reference=ref)


def dispose_content(notification: Notification) -> ProviderAction | None:
    """Clear the text here and at the provider; the record of the message stays."""
    Notification.objects.filter(pk=notification.pk).update(
        text="", content_disposed_at=timezone.now()
    )
    notification.refresh_from_db()
    notification = _resolve_send(notification)
    if not notification.provider_sid:
        return None
    message_sid = notification.provider_sid
    store = ActionStore(notification, ProviderAction.REDACT)

    def send(ref: str) -> ApiV2010AccountMessage:
        return provider.redact_message(message_sid)

    def find(ref: str) -> ApiV2010AccountMessage | None:
        return provider.fetch_message(message_sid)

    def echoed_body(message: ApiV2010AccountMessage) -> object:
        return message.body

    try:
        safe_write(
            store, send, find, redact_outcome, repeat_is_safe=True, status_of=echoed_body
        )
    except ApiError as e:
        logger.warning(
            "redaction of notification %s refused: HTTP %s code %s",
            notification.pk,
            e.status_code,
            provider_error_code(e),
        )
    except (OutcomeUnknown, httpx.RequestError):
        logger.warning("redaction of notification %s outcome unknown", notification.pk)
    return store.load()


# --- reconciliation -----------------------------------------------------------


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    """Line the provider's messages from our number up against ours, on the provider's clock."""
    start, end = start.astimezone(dt_timezone.utc), end.astimezone(dt_timezone.utc)
    # The provider filters by whole days: widen by a day each side, then
    # narrow back to the exact window on the provider's send time.
    provider_messages: list[ApiV2010AccountMessage] = []
    for message in provider.messages_sent_from_us(
        datetime.combine(start.date() - timedelta(days=1), dt_time.min, tzinfo=dt_timezone.utc),
        datetime.combine(end.date() + timedelta(days=1), dt_time.min, tzinfo=dt_timezone.utc),
    ):
        sent = parse_provider_time(message.date_sent)
        if sent is not None and start <= sent <= end:
            provider_messages.append(message)

    # Local messages that may have been sent in the window but have no
    # provider send time recorded yet: ask the provider first.
    for notification in Notification.objects.filter(
        provider_date_sent__isnull=True, created_at__lte=end
    ).exclude(provider_sid=""):
        refresh_notification(notification, force=True)

    by_sid: dict[str, ApiV2010AccountMessage | None] = {}
    for message in provider_messages:
        if isinstance(message.sid, str):
            by_sid[message.sid] = message

    local = list(
        Notification.objects.filter(provider_date_sent__gte=start, provider_date_sent__lte=end)
        .select_related("order")
        .order_by("provider_date_sent")
    )
    matched, local_only, status_mismatches = [], [], []
    for notification in local:
        found = by_sid.pop(notification.provider_sid, None)
        if found is None:
            local_only.append(_local_entry(notification))
            continue
        provider_status = status_text(found.status)
        entry = _local_entry(notification) | {"providerStatus": provider_status}
        matched.append(entry)
        if provider_status != notification.provider_status:
            status_mismatches.append(entry | {"recordedStatus": notification.provider_status})
    provider_only = [_provider_entry(m) for m in by_sid.values() if m is not None]

    unsettled = [
        _local_entry(n)
        for n in Notification.objects.filter(
            provider_date_sent__isnull=True, created_at__gte=start, created_at__lte=end
        ).select_related("order")
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "sendingNumber": settings.TWILIO_FROM_NUMBER,
        "providerCount": len(provider_messages),
        "localCount": len(local),
        "matched": matched,
        "providerOnly": provider_only,
        "localOnly": local_only,
        "statusMismatches": status_mismatches,
        "unsettled": unsettled,
    }


def _local_entry(notification: Notification) -> dict[str, Any]:
    return {
        "notificationId": notification.pk,
        "orderId": str(notification.order.number),
        "kind": notification.kind,
        "messageSid": notification.provider_sid or None,
        "outcome": notification.outcome,
        "recordedStatus": notification.provider_status or None,
        "dateSent": notification.provider_date_sent.isoformat()
        if notification.provider_date_sent
        else None,
    }


def _provider_entry(message: ApiV2010AccountMessage) -> dict[str, Any]:
    sent = parse_provider_time(message.date_sent)
    to = message.to if isinstance(message.to, str) else ""
    return {
        "messageSid": message.sid if isinstance(message.sid, str) else None,
        "providerStatus": status_text(message.status),
        "to": mask_number(to) if to else None,
        "dateSent": sent.isoformat() if sent else None,
    }
