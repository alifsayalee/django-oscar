"""
Order SMS notifications.

Every provider write follows the same shape: a durable row is committed first
(the claim), then Twilio is called, then the row is settled from what Twilio
said. A message that cannot be sent never fails the order operation that
triggered it; its row records what happened instead.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import twilio_gateway as gateway
from .models import ContactNumber, Notification, OrderTransitionClaim

logger = logging.getLogger("apps.order_sms")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Selector = get_class("partner.strategy", "Selector")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
FreeShipping = get_class("shipping.methods", "Free")
InvalidOrderStatus = get_class("order.exceptions", "InvalidOrderStatus")

DISPATCHED = "Dispatched"
CANCELLED = "Cancelled"

# Don't ask Twilio about the same message more often than this.
SYNC_MIN_INTERVAL = timedelta(seconds=15)
# Bound on provider reads a single GET may trigger.
MAX_SYNCS_PER_REQUEST = 25
# How far before the claim to look when asking Twilio whether a send landed.
FIND_SLACK = timedelta(minutes=2)
# Reconciliation: how many app-only candidates to confirm one by one.
MAX_RECONCILE_LOOKUPS = 100

TEMPLATES = {
    Notification.ORDER_PLACED: "{shop}: thanks! Your order {number} has been placed. {ref}",
    Notification.ORDER_DISPATCHED: "{shop}: good news - your order {number} is on its way. {ref}",
    Notification.DELIVERY_FOLLOWUP: (
        "{shop}: how did the delivery of order {number} go? Reply and let us know. {ref}"
    ),
    Notification.ORDER_CANCELLED: "{shop}: your order {number} has been cancelled. {ref}",
}


class OrderRequestInvalid(Exception):
    pass


class ActionNotAllowed(Exception):
    """The request is well-formed but the resource's state forbids it (HTTP 409)."""


def followup_delay():
    return timedelta(seconds=int(getattr(settings, "ORDER_SMS_FOLLOWUP_DELAY_SECONDS", 3 * 24 * 3600)))


def _render(kind, order, notification):
    return TEMPLATES[kind].format(
        shop=getattr(settings, "OSCAR_SHOP_NAME", "Oscar"),
        number=order.number,
        ref=notification.ref_tag,
    )


# ---------------------------------------------------------------------------
# Contact numbers
# ---------------------------------------------------------------------------


def current_contact(user):
    """The number messages go to: the shopper's most recently registered one."""
    return ContactNumber.objects.filter(user=user).first()


def register_contact(user, raw_number, country_code=None):
    """
    Ask Twilio for the canonical form (raising ProviderRejected when it does not
    recognise a usable number) and store that, never the caller's spelling.
    """
    canonical, country = gateway.lookup_number(raw_number, country_code)
    contact, created = ContactNumber.objects.get_or_create(
        user=user, phone_number=canonical, defaults={"country_code": country or ""}
    )
    return contact, created


def delete_contact(contact):
    """Remove a number; any follow-up still queued at Twilio for it is called off first."""
    pending = Notification.objects.filter(contact=contact, kind=Notification.DELIVERY_FOLLOWUP)
    request_followup_cancel(pending)
    contact.delete()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _apply(notification, snap):
    """Settle a row from what Twilio said about the message."""
    notification.provider_sid = snap.sid
    notification.status = snap.status or Notification.UNKNOWN
    notification.error_code = snap.error_code
    notification.error_message = (snap.error_message or "")[:255]
    notification.provider_date_created = snap.date_created or notification.provider_date_created
    notification.provider_date_sent = snap.date_sent or notification.provider_date_sent
    notification.last_synced_at = timezone.now()
    notification.save(
        update_fields=[
            "provider_sid",
            "status",
            "error_code",
            "error_message",
            "provider_date_created",
            "provider_date_sent",
            "last_synced_at",
            "updated_at",
        ]
    )


def _mark(notification, status, error=None):
    notification.status = status
    if error is not None:
        notification.error_code = error.provider_code
        notification.error_message = error.message[:255]
    notification.save(update_fields=["status", "error_code", "error_message", "updated_at"])


def _find_landed(notification):
    """Look the message up at Twilio by the reference carried in its body."""
    if notification.contact is None:
        return None
    try:
        listing = gateway.list_messages(
            sent_after=notification.created_at - FIND_SLACK,
            sent_before=timezone.now() + timedelta(days=1),
            to=notification.contact.phone_number,
            page_size=50,
            max_pages=3,
        )
    except gateway.ProviderError:
        return None
    for snap in listing.messages:
        if snap.body and notification.ref_tag in snap.body:
            return snap
    return None


def _deliver(notification):
    try:
        snap = gateway.send_message(
            notification.contact.phone_number,
            notification.body,
            idempotency_key=str(notification.ref),
            send_at=notification.send_at,
        )
    except gateway.ProviderError as error:
        if not error.outcome_unknown:
            # Refused, or never left this process: nothing was sent.
            _mark(notification, Notification.REJECTED, error)
            logger.warning("notification %s not sent: %s", notification.pk, error.message)
            return
        snap = _find_landed(notification)
        if snap is None:
            # An empty lookup cannot prove it did not land.
            _mark(notification, Notification.UNKNOWN, error)
            logger.error("notification %s outcome unknown: %s", notification.pk, error.message)
            return
    _apply(notification, snap)
    if notification.kind == Notification.DELIVERY_FOLLOWUP:
        _honour_cancel_request(notification)


def notify(order, kind, *, contact=None, send_at=None, resend_of=None, idempotency_key=None):
    """
    Claim, send and settle one message. Returns (notification, created); the
    notification is None when the shopper has no number on file. When the claim
    already exists nothing is sent and the existing row is returned.
    """
    if contact is None:
        contact = current_contact(order.user)
        if contact is None:
            return None, False
    notification = Notification(
        order=order,
        user=order.user,
        contact=contact,
        kind=kind,
        send_at=send_at,
        resend_of=resend_of,
        idempotency_key=idempotency_key,
    )
    notification.body = _render(kind, order, notification)
    try:
        with transaction.atomic():
            notification.save()
    except IntegrityError:
        if resend_of is not None:
            existing = Notification.objects.get(resend_of=resend_of, idempotency_key=idempotency_key)
        else:
            existing = Notification.objects.get(order=order, kind=kind, resend_of__isnull=True)
        return existing, False
    try:
        _deliver(notification)
    except Exception:
        # Never let a messaging problem fail the order operation.
        logger.exception("notification %s: unexpected error while sending", notification.pk)
        _mark(notification, Notification.UNKNOWN)
    return notification, True


# ---------------------------------------------------------------------------
# Follow-up cancellation and status refresh
# ---------------------------------------------------------------------------


def _cancel_at_provider(notification):
    try:
        snap = gateway.cancel_message(notification.provider_sid)
    except gateway.ProviderRejected:
        # Twilio would not cancel it - most likely it is no longer scheduled. Find out.
        try:
            snap = gateway.fetch_message(notification.provider_sid)
        except gateway.ProviderError:
            logger.error("follow-up %s: cancel refused and status unreadable", notification.pk)
            return
    except gateway.ProviderError as error:
        logger.error(
            "follow-up %s: cancel failed (%s); retried on next sync", notification.pk, error.message
        )
        return
    _apply(notification, snap)
    if snap.outcome != gateway.CANCELED:
        logger.error(
            "follow-up %s could not be called off; Twilio status is %s", notification.pk, snap.status
        )


def _honour_cancel_request(notification):
    notification.refresh_from_db(fields=["cancel_requested_at"])
    if (
        notification.cancel_requested_at
        and notification.provider_sid
        and notification.outcome == gateway.SCHEDULED
    ):
        _cancel_at_provider(notification)


def request_followup_cancel(queryset):
    """
    Record, durably and before any provider call, that these follow-ups must not
    go out; then cancel those Twilio is holding. A send still in flight sees the
    flag when it settles, and a failed cancel is retried by every later sync.
    """
    now = timezone.now()
    queryset.filter(kind=Notification.DELIVERY_FOLLOWUP, cancel_requested_at__isnull=True).update(
        cancel_requested_at=now
    )
    for notification in queryset.filter(
        kind=Notification.DELIVERY_FOLLOWUP, provider_sid__isnull=False, status="scheduled"
    ):
        _cancel_at_provider(notification)


def sync(notification, *, force=False):
    """Refresh a message's state from Twilio. Returns False when Twilio could not be read."""
    cancel_pending = notification.cancel_requested_at and notification.outcome == gateway.SCHEDULED
    if notification.status == Notification.UNKNOWN and notification.provider_sid is None:
        snap = _find_landed(notification)
        if snap is not None:
            _apply(notification, snap)
        return True
    if not notification.provider_sid or (notification.is_final and not cancel_pending):
        return True
    recently = notification.last_synced_at and timezone.now() - notification.last_synced_at < SYNC_MIN_INTERVAL
    if recently and not force and not cancel_pending:
        return True
    try:
        snap = gateway.fetch_message(notification.provider_sid)
    except gateway.ProviderError:
        return False
    _apply(notification, snap)
    if notification.kind == Notification.DELIVERY_FOLLOWUP:
        _honour_cancel_request(notification)
    return True


def sync_many(notifications):
    """Refresh up to MAX_SYNCS_PER_REQUEST rows; returns the ids whose state may be stale."""
    stale = set()
    budget = MAX_SYNCS_PER_REQUEST
    for notification in notifications:
        needs_provider = notification.provider_sid and not notification.is_final
        if not needs_provider and notification.status != Notification.UNKNOWN:
            continue
        if budget <= 0:
            stale.add(notification.pk)
            continue
        budget -= 1
        if not sync(notification):
            stale.add(notification.pk)
    return stale


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def place_order(user, lines):
    """
    Place an order through Oscar's own basket and OrderCreator, then tell the
    shopper. ``lines`` is a list of (product_id, quantity).
    """
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(user=user)
        for product_id, quantity in lines:
            product = Product.objects.filter(pk=product_id).first()
            if product is None:
                raise OrderRequestInvalid(f"Catalogue item {product_id} does not exist.")
            if product.is_parent:
                raise OrderRequestInvalid(
                    f"Catalogue item {product_id} is a parent product; order one of its variants."
                )
            info = basket.strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise OrderRequestInvalid(f"Catalogue item {product_id} cannot be bought: {reason}")
            try:
                basket.add_product(product, quantity)
            except ValueError as exc:
                raise OrderRequestInvalid(f"Catalogue item {product_id} cannot be bought: {exc}") from exc
        shipping_method = FreeShipping()
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            order_number=OrderNumberGenerator().order_number(basket),
        )
        basket.submit()
    notification, _ = notify(order, Notification.ORDER_PLACED)
    return order, notification


def _transition(order, to_status):
    """
    Move the order with Oscar's own ``set_status`` and claim the transition in
    the same transaction. True only for the one request that performed it.
    """
    try:
        with transaction.atomic():
            OrderTransitionClaim.objects.create(order=order, to_status=to_status)
            order.set_status(to_status)
    except IntegrityError:
        order.refresh_from_db()
        return False
    except InvalidOrderStatus as exc:
        order.refresh_from_db()
        raise ActionNotAllowed(str(exc)) from exc
    return True


def dispatch_order(order):
    performed = _transition(order, DISPATCHED)
    if performed:
        notify(order, Notification.ORDER_DISPATCHED)
        notify(
            order,
            Notification.DELIVERY_FOLLOWUP,
            send_at=timezone.now() + followup_delay(),
        )
    return performed


def cancel_order(order):
    performed = _transition(order, CANCELLED)
    if performed:
        # Call the follow-up off before anything else.
        request_followup_cancel(Notification.objects.filter(order=order))
        notify(order, Notification.ORDER_CANCELLED)
    return performed


# ---------------------------------------------------------------------------
# Operator actions
# ---------------------------------------------------------------------------


def resend(source, idempotency_key):
    """Returns (notification, created). A repeated key returns the first result and sends nothing."""
    existing = Notification.objects.filter(resend_of=source, idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, False
    sync(source, force=True)
    if source.content_redacted_at:
        raise ActionNotAllowed("This message's content was disposed of; it cannot be re-sent.")
    if source.outcome != gateway.FAILED:
        raise ActionNotAllowed(
            f"Only a message that did not reach the shopper can be re-sent (this one is {source.outcome})."
        )
    contact = source.contact
    if contact is None or contact.user_id != source.order.user_id:
        raise ActionNotAllowed("The number this message was for is no longer on file.")
    if source.order.status == CANCELLED and source.kind != Notification.ORDER_CANCELLED:
        raise ActionNotAllowed("The order was cancelled; only the cancellation message can be re-sent.")
    notification, created = notify(
        source.order, source.kind, contact=contact, resend_of=source, idempotency_key=idempotency_key
    )
    return notification, created


def dispose_content(notification):
    """
    Blank the message text at Twilio and here. The record of the message and
    its delivery outcome survive.
    """
    if notification.content_redacted_at:
        return notification
    if notification.provider_sid is None and notification.status == Notification.UNKNOWN:
        sync(notification, force=True)  # it may have landed after all
    if notification.provider_sid:
        sync(notification, force=True)
        if notification.outcome in (gateway.PENDING, gateway.SCHEDULED):
            raise ActionNotAllowed("The message has not reached a final state yet; try again later.")
        snap = gateway.redact_message(notification.provider_sid)
        if snap.body:
            logger.error("notification %s: Twilio did not confirm the redaction", notification.pk)
            raise gateway.ProviderFailure(
                "Twilio did not confirm the redaction.", http_status=502, outcome_unknown=True
            )
        _apply(notification, snap)
    elif notification.status != Notification.REJECTED:
        raise ActionNotAllowed("Twilio's copy of this message cannot be identified yet; try again later.")
    notification.body = ""
    notification.content_redacted_at = timezone.now()
    notification.save(update_fields=["body", "content_redacted_at", "updated_at"])
    return notification


@dataclass
class Reconciliation:
    start: datetime
    end: datetime
    truncated: bool
    pages: int
    inbound_excluded: int
    matched: list = field(default_factory=list)
    provider_only: list = field(default_factory=list)
    app_only: list = field(default_factory=list)
    out_of_window: list = field(default_factory=list)
    unverified: list = field(default_factory=list)
    unsettled: list = field(default_factory=list)


def reconcile(start, end):
    """
    Line up Twilio's record of messages from TWILIO_FROM_NUMBER against ours.

    Twilio's DateSent filter works in whole days, so the query is widened to
    whole UTC days and narrowed back here on the provider's clock (sent, else
    created). Our rows are selected on that same clock, from the provider
    timestamps stored on them.
    """
    utc = dt_timezone.utc
    day_start = datetime.combine(start.astimezone(utc).date(), time.min, tzinfo=utc)
    day_end = datetime.combine(end.astimezone(utc).date() + timedelta(days=1), time.min, tzinfo=utc)
    listing = gateway.list_messages(sent_after=day_start, sent_before=day_end)

    def within(snap):
        return snap.provider_time is not None and start <= snap.provider_time < end

    outbound = [m for m in listing.messages if m.is_outbound]
    fetched = {m.sid: m for m in outbound}
    in_window = [m for m in outbound if within(m)]
    report = Reconciliation(
        start=start,
        end=end,
        truncated=listing.truncated,
        pages=listing.pages,
        # The receiving leg of a message to a number on this same account.
        inbound_excluded=sum(1 for m in listing.messages if not m.is_outbound and within(m)),
    )

    known = {
        n.provider_sid: n
        for n in Notification.objects.filter(provider_sid__in=[m.sid for m in in_window]).select_related("order")
    }
    for snap in in_window:
        notification = known.get(snap.sid)
        if notification is None:
            report.provider_only.append((None, snap))
            continue
        previous = notification.status
        _apply(notification, snap)
        report.matched.append((notification, snap, previous))

    matched_sids = {n.provider_sid for n, _, _ in report.matched}
    local = (
        Notification.objects.filter(provider_sid__isnull=False)
        .filter(
            Q(provider_date_sent__gte=start, provider_date_sent__lt=end)
            | Q(provider_date_sent__isnull=True, provider_date_created__gte=start, provider_date_created__lt=end)
        )
        .exclude(provider_sid__in=matched_sids)
        .select_related("order")
    )
    lookups = 0
    for notification in local:
        snap = fetched.get(notification.provider_sid)
        if snap is None and lookups < MAX_RECONCILE_LOOKUPS:
            lookups += 1
            try:
                snap = gateway.fetch_message(notification.provider_sid)
            except gateway.ProviderRejected:
                snap = None  # Twilio has no such message
            except gateway.ProviderError:
                report.unverified.append((notification, None))
                continue
        elif snap is None:
            report.unverified.append((notification, None))
            continue
        if snap is None:
            report.app_only.append((notification, None))
            continue
        previous = notification.status
        _apply(notification, snap)
        if snap.provider_time and start <= snap.provider_time < end:
            # Twilio knows it and it belongs in the window; the day filter just missed it.
            report.matched.append((notification, snap, previous))
        else:
            report.out_of_window.append((notification, snap))

    report.unsettled = list(
        Notification.objects.filter(provider_sid__isnull=True, created_at__gte=start, created_at__lt=end)
        .select_related("order")
    )
    return report
