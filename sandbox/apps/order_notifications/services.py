"""Order SMS notifications: what happens as an order moves.

A message that cannot be sent never fails the order operation it belongs to:
its outcome is recorded on the notification and reported, nothing more.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from oscar.apps.order.signals import order_status_changed
from oscar.core.loading import get_class, get_model

from twilio_sdk.core import ApiError, UnsetType
from twilio_sdk.models import ApiV2010AccountMessage

from . import provider
from .models import ContactNumber, Notification, Outcome, ProviderWrite
from .outcomes import GONE, cancel_outcome, redact_outcome, status_from_provider, worst
from .provider import ProviderError, text_or_none
from .safe_write import Answer, OutcomeUnknown, WriteNeverSent, WriteRefused, safe_write, write_ref

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")
OrderStatusChange = get_model("order", "OrderStatusChange")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
ShippingAddress = get_model("order", "ShippingAddress")
Country = get_model("address", "Country")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Repository = get_class("shipping.repository", "Repository")
Selector = get_class("partner.strategy", "Selector")
Applicator = get_class("offer.applicator", "Applicator")

DISPATCHED = "Dispatched"
CANCELLED = "Cancelled"
DISPATCHABLE_FROM = ("Pending", "Being processed")
CANCELLABLE_FROM = ("Pending", "Being processed", DISPATCHED)

# Delivery outcomes a later request should still ask the provider about.
UNSETTLED = (Outcome.SENDING, Outcome.PENDING, Outcome.UNKNOWN)
# Refresh a notification from the provider at most this often on reads.
REFRESH_INTERVAL = timedelta(seconds=15)
REFRESH_LIMIT = 20


class ServiceError(Exception):
    def __init__(self, status_code: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# Contact numbers
# ---------------------------------------------------------------------------


def active_numbers(user: Any) -> Any:
    return ContactNumber.objects.filter(user=user, disabled_at__isnull=True).order_by("created_at", "pk")


def register_number(user: Any, raw_number: str, country_code: str | None) -> tuple[ContactNumber, bool]:
    """Validate with the provider and store its canonical form. Raises ServiceError."""
    check = provider.lookup_number(raw_number, country_code)
    if not check.valid or not check.phone_number:
        raise ServiceError(
            422,
            "The SMS provider does not consider this number a usable destination.",
            validationErrors=check.validation_errors,
        )
    try:
        with transaction.atomic():
            number = ContactNumber.objects.create(
                user=user,
                phone_number=check.phone_number,
                country_code=check.country_code,
                national_format=check.national_format,
            )
        return number, True
    except IntegrityError:
        return active_numbers(user).get(phone_number=check.phone_number), False


@dataclass
class RemovalResult:
    outcome: str
    erased: bool
    call_offs: list[Notification] = field(default_factory=list)


def remove_number(number: ContactNumber) -> RemovalResult:
    """Stop messaging a number, call off anything queued for it, then erase it."""
    if number.disabled_at is None:
        number.disabled_at = timezone.now()
        number.save(update_fields=["disabled_at"])
    follow_ups = list(
        Notification.objects.filter(contact_number=number, kind=Notification.FOLLOW_UP)
        .exclude(call_off_outcome__in=[Outcome.DONE, Outcome.FAILED])
    )
    outcomes = [call_off_follow_up(n) for n in follow_ups]
    overall = worst(outcomes)
    erased = overall in (Outcome.DONE, Outcome.FAILED)  # nothing left that could still go out
    if erased:
        number.delete()
    return RemovalResult(outcome=Outcome.DONE if erased else overall, erased=erased, call_offs=follow_ups)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def reference_token(ref: str) -> str:
    return hashlib.sha256(ref.encode("utf-8")).hexdigest()[:10].upper()


def outgoing_text(notification: Notification) -> str:
    return "%s Ref %s" % (notification.body, notification.reference_token)


def message_text(kind: str, order: Any) -> str:
    shop = getattr(settings, "OSCAR_SHOP_NAME", "Oscar")
    number = order.number
    if kind == Notification.PLACED:
        return "%s: thanks - your order %s (%s %s) has been placed." % (
            shop, number, order.currency, order.total_incl_tax)
    if kind == Notification.DISPATCHED:
        return "%s: good news - your order %s has been dispatched and is on its way." % (shop, number)
    if kind == Notification.FOLLOW_UP:
        return "%s: how did the delivery of your order %s go? Reply and let us know." % (shop, number)
    return "%s: your order %s has been cancelled." % (shop, number)


def _get_or_create_notification(
    trigger_key: str, **fields: Any
) -> tuple[Notification, bool]:
    """The trigger key is unique: the database rejects a second notification for one event."""
    token = reference_token(write_ref(trigger_key, ProviderWrite.SEND))
    try:
        with transaction.atomic():
            return Notification.objects.create(trigger_key=trigger_key, reference_token=token, **fields), True
    except IntegrityError:
        return Notification.objects.get(trigger_key=trigger_key), False


def read_message(message: ApiV2010AccountMessage) -> Answer:
    return Answer(
        provider_id=text_or_none(message.sid),
        status=message.status,
        provider_time=provider.parse_provider_time(message.date_sent)
        or provider.parse_provider_time(message.date_created),
    )


def _apply_message(notification: Notification, message: ApiV2010AccountMessage) -> None:
    """Record the provider's view of a message on the notification."""
    sid = text_or_none(message.sid)
    if sid:
        notification.message_sid = sid
    status = message.status
    if isinstance(status, UnsetType):
        notification.provider_status = ""
        notification.outcome = Outcome.UNKNOWN  # an absent status is never success
    else:
        notification.provider_status = str(status)[:32]
        notification.outcome = status_from_provider(status)
    error_code = message.error_code
    notification.error_code = error_code if isinstance(error_code, int) else None
    error_message = text_or_none(message.error_message)
    notification.error_message = provider.mask(error_message) if error_message else ""
    created = provider.parse_provider_time(message.date_created)
    if created:
        notification.provider_created_at = created
    sent = provider.parse_provider_time(message.date_sent)
    if sent:
        notification.provider_sent_at = sent
    notification.last_checked_at = timezone.now()


def _contact_still_active(notification: Notification) -> bool:
    if notification.contact_number_id is None:
        return False
    return ContactNumber.objects.filter(pk=notification.contact_number_id, disabled_at__isnull=True).exists()


def send_notification(notification: Notification) -> Notification:
    """Send (or schedule) one notification through the safe write. Never raises for the provider."""
    ref = write_ref(notification.trigger_key, ProviderWrite.SEND)
    if notification.outcome == Outcome.SENDING and not ProviderWrite.objects.filter(ref=ref).exists():
        if not _contact_still_active(notification):
            notification.outcome = Outcome.FAILED
            notification.error_message = "The contact number was removed before the message was sent."
            notification.save()
            return notification
    to = notification.to_number
    text = outgoing_text(notification)
    send_at = notification.scheduled_for

    try:
        written = safe_write(
            ref,
            ProviderWrite.SEND,
            notification,
            send=lambda: provider.create_message(to, text, send_at=send_at),
            find=lambda: provider.find_message_by_token(to, notification.reference_token),
            read=read_message,
            outcome_of=status_from_provider,
            repeat_is_safe=False,
        )
    except OutcomeUnknown:
        notification.outcome = Outcome.UNKNOWN
        logger.warning("notification %s: send outcome unknown; will be checked again", notification.pk)
    except WriteRefused as e:
        notification.outcome = Outcome.FAILED
        notification.error_code = e.rejection.code
        notification.error_message = e.rejection.message or "Refused by the SMS provider."
        logger.warning("notification %s: refused by provider (HTTP %s, code %s)",
                       notification.pk, e.rejection.status_code, e.rejection.code)
    except WriteNeverSent as e:
        notification.outcome = Outcome.FAILED
        notification.error_message = "The SMS provider could not be reached; nothing was sent."
        logger.warning("notification %s: never sent (%s)", notification.pk, type(e.__cause__).__name__)
    else:
        if written.payload is not None:
            _apply_message(notification, written.payload)
        elif written.record.provider_id:
            notification.message_sid = written.record.provider_id
            if notification.outcome == Outcome.SENDING:
                notification.outcome = written.record.outcome
    notification.save()
    return notification


def refresh_notification(notification: Notification, *, force: bool = False) -> Notification:
    """Ask the provider what became of a message whose outcome is not settled (read)."""
    if notification.outcome not in UNSETTLED:
        return notification
    now = timezone.now()
    if not force and notification.last_checked_at and now - notification.last_checked_at < REFRESH_INTERVAL:
        return notification
    if not notification.message_sid:
        if notification.outcome == Outcome.SENDING:
            record = ProviderWrite.objects.filter(
                ref=write_ref(notification.trigger_key, ProviderWrite.SEND)).first()
            if record is None and notification.created_at > now - timedelta(minutes=5):
                return notification  # not claimed yet: the request that owns it is still running
        return send_notification(notification)  # the safe write's check path: lookup, never a second send
    try:
        message = provider.guarded_read(lambda: provider.fetch_message(notification.message_sid))
    except ProviderError as e:
        notification.last_checked_at = now
        notification.save(update_fields=["last_checked_at"])
        logger.info("notification %s: status check failed (%s)", notification.pk, e.status_code)
        return notification
    _apply_message(notification, message)
    notification.save()
    record = ProviderWrite.objects.filter(ref=write_ref(notification.trigger_key, ProviderWrite.SEND)).first()
    if record is not None and record.outcome != notification.outcome:
        record.outcome = notification.outcome
        record.provider_id = record.provider_id or notification.message_sid
        record.provider_time = notification.provider_sent_at or notification.provider_created_at
        record.completed_at = now
        record.save(update_fields=["outcome", "provider_id", "provider_time", "completed_at"])
    return notification


def refresh_many(notifications: list[Notification]) -> None:
    for n in [n for n in notifications if n.outcome in UNSETTLED][:REFRESH_LIMIT]:
        refresh_notification(n)


def call_off_follow_up(notification: Notification) -> str:
    """Make sure a queued follow-up never reaches the shopper. Returns the call-off outcome."""
    if notification.call_off_outcome in (Outcome.DONE, Outcome.FAILED):
        return notification.call_off_outcome
    if not notification.message_sid:
        if notification.outcome in UNSETTLED:
            refresh_notification(notification, force=True)  # settle a lost send first
        if not notification.message_sid:
            outcome: str
            if notification.outcome == Outcome.FAILED:
                outcome = Outcome.DONE  # never queued with the provider: nothing to call off
            else:
                outcome = Outcome.UNKNOWN
            notification.call_off_outcome = outcome
            notification.save(update_fields=["call_off_outcome", "updated_at"])
            return outcome

    sid = notification.message_sid

    def gone_or(get: Any) -> Any:
        try:
            return get()
        except ApiError as e:
            if e.status_code == 404:
                return GONE
            raise

    def read(result: Any) -> Answer:
        if result is GONE:
            return Answer(sid, GONE, timezone.now())
        return Answer(text_or_none(result.sid), result.status, None)

    try:
        written = safe_write(
            write_ref(notification.trigger_key, ProviderWrite.CANCEL),
            ProviderWrite.CANCEL,
            notification,
            send=lambda: gone_or(lambda: provider.cancel_message(sid)),
            find=lambda: gone_or(lambda: provider.fetch_message(sid)),
            read=read,
            outcome_of=cancel_outcome,
            repeat_is_safe=True,  # setting the same message to canceled again cannot make a second one
            check_on_refusal=True,  # refused because it already left? the message's own state says
        )
        outcome = written.record.outcome
        if written.payload is not None and written.payload is not GONE:
            _apply_message(notification, written.payload)
    except OutcomeUnknown:
        outcome = Outcome.UNKNOWN
    except WriteNeverSent:
        outcome = Outcome.UNKNOWN  # the claim was released; the next call-off tries again
    if outcome == Outcome.FAILED:
        logger.warning("notification %s: follow-up could not be called off; it already left the schedule",
                       notification.pk)
    notification.call_off_outcome = outcome
    notification.save()
    return outcome


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderItem:
    product_id: int
    quantity: int


def place_order(user: Any, items: list[OrderItem], shipping_address: dict[str, str] | None) -> Any:
    """Place an order through Oscar's own OrderCreator, then tell the shopper."""
    strategy = Selector().strategy(user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for item in items:
            try:
                product = Product.objects.get(pk=item.product_id)
            except Product.DoesNotExist:
                raise ServiceError(422, "Catalogue item %s does not exist." % item.product_id)
            info = strategy.fetch_for_product(product)
            if not product.is_public or not info.availability.is_available_to_buy:
                raise ServiceError(422, "Catalogue item %s is not available to buy." % item.product_id)
            allowed, reason = info.availability.is_purchase_permitted(item.quantity)
            if not allowed:
                raise ServiceError(422, "Catalogue item %s: %s" % (item.product_id, reason))
            basket.add_product(product, item.quantity)
        Applicator().apply(basket, user)

        address = None
        if shipping_address is not None:
            try:
                country = Country.objects.get(iso_3166_1_a2=shipping_address.get("country", "").upper())
            except Country.DoesNotExist:
                raise ServiceError(422, "Unknown shipping country.")
            address = ShippingAddress(
                first_name=shipping_address.get("firstName", ""),
                last_name=shipping_address.get("lastName", ""),
                line1=shipping_address.get("line1", ""),
                line4=shipping_address.get("city", ""),
                postcode=shipping_address.get("postcode", ""),
                country=country,
            )
            if not address.line1 or not address.last_name:
                raise ServiceError(422, "A shipping address needs lastName and line1.")
            address.save()

        shipping_method = Repository().get_default_shipping_method(basket=basket, shipping_addr=address)
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=address,
            order_number=OrderNumberGenerator().order_number(basket),
        )
        basket.submit()

    notify_order(order, Notification.PLACED)
    return order


def _transition(order: Any, allowed_from: tuple[str, ...], new_status: str) -> bool:
    """Compare-and-set the order status; exactly one concurrent caller wins."""
    with transaction.atomic():
        old_status = order.status
        if old_status not in allowed_from:
            return False
        won = Order.objects.filter(pk=order.pk, status=old_status).update(status=new_status)
        if not won:
            order.refresh_from_db()
            return False
        order.status = new_status
        line_status = order.cascade.get(new_status)
        if line_status:
            for line in order.lines.all():
                if line_status in line.available_statuses():
                    line.status = line_status
                    line.save(update_fields=["status"])
        OrderStatusChange.objects.create(order=order, old_status=old_status, new_status=new_status)
    order_status_changed.send(sender=order, order=order, old_status=old_status, new_status=new_status)
    return True


def notify_order(order: Any, kind: str, send_at: datetime | None = None) -> list[Notification]:
    """Create (once per event and number) and send the notifications for one order event."""
    sent = []
    for number in active_numbers(order.user):
        notification, created = _get_or_create_notification(
            "order:%s:%s:contact:%s" % (order.pk, kind, number.pk),
            order=order,
            user=order.user,
            contact_number=number,
            to_number=number.phone_number,
            kind=kind,
            body=message_text(kind, order),
            scheduled_for=send_at,
        )
        if created or notification.outcome in (Outcome.SENDING, Outcome.UNKNOWN):
            send_notification(notification)
        sent.append(notification)
    return sent


def dispatch_order(order: Any) -> list[Notification]:
    if not _transition(order, DISPATCHABLE_FROM, DISPATCHED) and order.status != DISPATCHED:
        raise ServiceError(409, "Order in status '%s' cannot be dispatched." % order.status)
    notify_order(order, Notification.DISPATCHED)
    delay = timedelta(hours=float(settings.ORDER_SMS_FOLLOWUP_DELAY_HOURS))
    order.refresh_from_db()
    if order.status == DISPATCHED:
        existing = Notification.objects.filter(order=order, kind=Notification.FOLLOW_UP).first()
        send_at = existing.scheduled_for if existing and existing.scheduled_for else timezone.now() + delay
        follow_ups = notify_order(order, Notification.FOLLOW_UP, send_at=send_at)
        # A cancel may have landed while the follow-up was being queued.
        order.refresh_from_db()
        if order.status == CANCELLED:
            for n in follow_ups:
                call_off_follow_up(n)
    return list(order.sms_notifications.all())


def cancel_order(order: Any) -> list[Notification]:
    if not _transition(order, CANCELLABLE_FROM, CANCELLED) and order.status != CANCELLED:
        raise ServiceError(409, "Order in status '%s' cannot be cancelled." % order.status)
    # First make sure no delivery follow-up can reach the shopper, then tell them.
    for n in order.sms_notifications.filter(kind=Notification.FOLLOW_UP):
        call_off_follow_up(n)
    notify_order(order, Notification.CANCELLED)
    return list(order.sms_notifications.all())


def settle_cancelled_orders_follow_ups(notifications: list[Notification]) -> None:
    """On reads: a cancelled order's follow-up whose call-off is not settled is tried again."""
    for n in notifications:
        if (n.kind == Notification.FOLLOW_UP and n.order.status == CANCELLED
                and n.call_off_outcome not in (Outcome.DONE, Outcome.FAILED)):
            call_off_follow_up(n)


# ---------------------------------------------------------------------------
# Operator actions
# ---------------------------------------------------------------------------


def resend(original: Notification, idempotency_key: str) -> Notification:
    """Re-send a message that did not reach the shopper, at most once per idempotency key."""
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:40]
    trigger_key = "resend:%s:%s" % (original.pk, key_hash)
    existing = Notification.objects.filter(trigger_key=trigger_key).first()
    if existing is not None:
        # The same request again: answer from it, checking an unsettled send (never a second send).
        if existing.outcome in UNSETTLED:
            refresh_notification(existing, force=True)
        return existing

    refresh_notification(original, force=True)
    if original.outcome != Outcome.FAILED:
        raise ServiceError(409, "Only a message that did not reach the shopper can be re-sent "
                                "(its outcome is '%s')." % original.outcome)
    if original.content_disposed_at is not None:
        raise ServiceError(409, "The content of this message has been disposed of.")
    if original.contact_number_id is None or not ContactNumber.objects.filter(
            pk=original.contact_number_id, disabled_at__isnull=True).exists():
        raise ServiceError(409, "The shopper's number has been removed; nothing may be sent to it.")
    if original.order.status == CANCELLED and original.kind != Notification.CANCELLED:
        raise ServiceError(409, "The order has been cancelled; only the cancellation message can be re-sent.")

    notification, created = _get_or_create_notification(
        trigger_key,
        order=original.order,
        user=original.user,
        contact_number=original.contact_number,
        to_number=original.to_number,
        kind=original.kind,
        body=original.body,
        resend_of=original,
    )
    if created:
        send_notification(notification)
    return notification


def dispose_content(notification: Notification) -> str:
    """Erase a message's text at the provider (and here), keeping the record of it."""
    if notification.content_disposed_at is not None:
        return Outcome.DONE
    if not notification.message_sid and notification.outcome in UNSETTLED:
        refresh_notification(notification, force=True)
    if not notification.message_sid:
        if notification.outcome == Outcome.FAILED:
            _clear_content(notification)  # the provider never held it
            return Outcome.DONE
        notification.disposal_outcome = Outcome.UNKNOWN
        notification.save(update_fields=["disposal_outcome", "updated_at"])
        return Outcome.UNKNOWN

    sid = notification.message_sid

    def gone_or(get: Any) -> Any:
        try:
            return get()
        except ApiError as e:
            if e.status_code == 404:
                return GONE
            raise

    def read(result: Any) -> Answer:
        if result is GONE:
            return Answer(sid, GONE, timezone.now())
        return Answer(text_or_none(result.sid), result.body, None)

    try:
        written = safe_write(
            write_ref(notification.trigger_key, ProviderWrite.REDACT),
            ProviderWrite.REDACT,
            notification,
            send=lambda: provider.redact_message(sid),
            find=lambda: gone_or(lambda: provider.fetch_message(sid)),
            read=read,
            outcome_of=redact_outcome,
            repeat_is_safe=True,  # setting the text to empty again is the same request
        )
        outcome = written.record.outcome
        if written.payload is not None and written.payload is not GONE:
            _apply_message(notification, written.payload)  # the delivery state it reports
    except OutcomeUnknown:
        outcome = Outcome.UNKNOWN
    except WriteNeverSent:
        outcome = Outcome.UNKNOWN
    except WriteRefused as e:
        outcome = Outcome.FAILED
        notification.disposal_outcome = outcome
        notification.save(update_fields=["disposal_outcome", "updated_at"])
        raise ServiceError(409, e.rejection.message or "The SMS provider refused to erase the text.",
                           outcome=outcome, providerCode=e.rejection.code)
    notification.disposal_outcome = outcome
    notification.save()
    if outcome == Outcome.DONE:
        _clear_content(notification)
    return outcome


def _clear_content(notification: Notification) -> None:
    notification.body = ""
    notification.content_disposed_at = timezone.now()
    notification.disposal_outcome = Outcome.DONE
    notification.save(update_fields=["body", "content_disposed_at", "disposal_outcome", "updated_at"])


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile(start: datetime, end: datetime) -> dict[str, Any]:
    """Line the provider's record of messages from our number up against ours, on the provider's clock."""
    from_number = settings.TWILIO_FROM_NUMBER
    if not from_number:
        raise ServiceError(503, "TWILIO_FROM_NUMBER is not configured.")
    listing = provider.list_messages_sent_from(from_number, start.date(), end.date())

    provider_in_window: dict[str, ApiV2010AccountMessage] = {}
    for message in listing.messages:
        sid = text_or_none(message.sid)
        sent_at = provider.parse_provider_time(message.date_sent)
        if sid and sent_at is not None and start <= sent_at < end:
            provider_in_window[sid] = message

    local = list(
        Notification.objects.filter(provider_sent_at__gte=start, provider_sent_at__lt=end)
        .exclude(message_sid="")
        .select_related("order")
    )
    local_sids = {n.message_sid for n in local}
    # A provider message may belong to a notification whose stored send time differs.
    known_elsewhere = {
        n.message_sid: n
        for n in Notification.objects.filter(message_sid__in=list(set(provider_in_window) - local_sids))
    }
    unsettled = list(
        Notification.objects.filter(provider_sent_at__isnull=True, outcome__in=UNSETTLED)
        .filter(Q(provider_created_at__gte=start, provider_created_at__lt=end)
                | Q(provider_created_at__isnull=True, created_at__gte=start, created_at__lt=end))
    )

    def provider_entry(message: ApiV2010AccountMessage) -> dict[str, Any]:
        return {
            "messageSid": text_or_none(message.sid),
            "status": None if isinstance(message.status, UnsetType) else str(message.status),
            "to": provider.mask_number(text_or_none(message.to) or ""),
            "dateSent": _iso(provider.parse_provider_time(message.date_sent)),
            "errorCode": message.error_code if isinstance(message.error_code, int) else None,
        }

    matched: list[dict[str, Any]] = []
    local_only: list[dict[str, Any]] = []
    for n in local + list(known_elsewhere.values()):
        found = provider_in_window.get(n.message_sid)
        if found is None:
            local_only.append(_local_entry(n))
            continue
        message = found
        provider_status = "" if isinstance(message.status, UnsetType) else str(message.status)
        matched.append({
            **_local_entry(n),
            "provider": provider_entry(message),
            "statusAgrees": provider_status == n.provider_status,
        })
    all_local_sids = local_sids | set(known_elsewhere)
    provider_only = [provider_entry(m) for sid, m in provider_in_window.items() if sid not in all_local_sids]

    return {
        "from": _iso(start),
        "to": _iso(end),
        "sendingNumber": provider.mask_number(from_number),
        "providerListingComplete": listing.complete,
        "counts": {
            "provider": len(provider_in_window),
            "local": len(local) + len(known_elsewhere),
            "matched": len(matched),
            "providerOnly": len(provider_only),
            "localOnly": len(local_only),
            "unsettled": len(unsettled),
        },
        "matched": matched,
        "providerOnly": provider_only,
        "localOnly": local_only,
        "unsettled": [_local_entry(n) for n in unsettled],
    }


def _local_entry(n: Notification) -> dict[str, Any]:
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "messageSid": n.message_sid or None,
        "outcome": n.outcome,
        "providerStatus": n.provider_status or None,
        "providerSentAt": _iso(n.provider_sent_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
