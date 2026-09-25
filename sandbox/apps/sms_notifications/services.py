"""
Order notifications by SMS: what happens when an order moves, and what an
operator can do about the messages afterwards.

Rules held here:
* A message that cannot be sent never fails the order operation - ``notify``
  swallows every failure after recording it.
* Every send goes through ``safe_write.safe_send`` (claim before call).
* A number is never logged; logs carry notification/order ids only.
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import gateway
from .gateway import ProviderRejected, ProviderUnavailable
from .models import ContactNumber, SmsNotification
from .safe_write import SEND_WINDOW, complete, ref_token, safe_send, settle_by_lookup

logger = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
ShippingEventType = get_model("order", "ShippingEventType")
OrderCreator = get_class("order.utils", "OrderCreator")
EventHandler = get_class("order.processing", "EventHandler")
Selector = get_class("partner.strategy", "Selector")
Repository = get_class("shipping.repository", "Repository")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
InvalidOrderStatus = get_class("order.exceptions", "InvalidOrderStatus")

STATUS_DISPATCHED = "Dispatched"
STATUS_CANCELLED = "Cancelled"
MAX_REFRESH_PER_REQUEST = 20


class Conflict(Exception):
    """The request is valid but the resource is not in a state that allows it (409)."""


class Invalid(Exception):
    """The request itself is wrong (400/422)."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _install_id() -> str:
    return settings.SMS_NOTIFICATIONS_INSTALL_ID


def mask_number(number: str | None) -> str | None:
    if not number:
        return None
    return number[:2] + "*" * max(len(number) - 5, 0) + number[-3:]


# --------------------------------------------------------------------------
# Flow 1 - contact numbers
# --------------------------------------------------------------------------


def register_number(user, raw_number: str, country_code: str | None = None) -> tuple[ContactNumber, bool]:
    """Validate with the provider and store its canonical form. Returns (number, created)."""
    cleaned = "".join(ch for ch in raw_number if ch not in " ()-.")
    if not cleaned or len(cleaned) > 20 or not cleaned.lstrip("+").isdigit():
        raise Invalid("phoneNumber must be a phone number.", 422)
    if country_code is not None and (len(country_code) != 2 or not country_code.isalpha()):
        raise Invalid("countryCode must be a two-letter ISO country code.", 422)
    result = gateway.lookup_number(cleaned, country_code.upper() if country_code else None)
    if result is None:
        raise Invalid("The messaging provider does not recognise this as a usable phone number.", 422)
    with transaction.atomic():
        existing = ContactNumber.objects.filter(
            user=user, phone_number=result.phone_number, deleted_at__isnull=True).first()
        if existing:
            return existing, False
        number = ContactNumber.objects.create(
            user=user, phone_number=result.phone_number, country_code=result.country_code)
    logger.info("contact number %s registered for user %s", number.pk, user.pk)
    return number, True


def active_numbers(user):
    return ContactNumber.objects.filter(user=user, deleted_at__isnull=True)


def delete_number(user, number_id: int) -> ContactNumber | None:
    """Soft-delete one of the caller's numbers and call off anything still scheduled to it."""
    with transaction.atomic():
        number = active_numbers(user).filter(pk=number_id).first()
        if number is None:
            return None
        number.deleted_at = timezone.now()
        number.save(update_fields=["deleted_at"])
    still_sendable = [SmsNotification.SENDING, SmsNotification.PENDING, SmsNotification.UNKNOWN]
    for followup in SmsNotification.objects.filter(contact_number=number, kind=SmsNotification.KIND_FOLLOWUP,
                                                   outcome__in=still_sendable):
        call_off(followup)
    logger.info("contact number %s deleted by user %s", number.pk, user.pk)
    return number


# --------------------------------------------------------------------------
# Flow 2 - orders and the messages they trigger
# --------------------------------------------------------------------------

MESSAGE_TEXT = {
    SmsNotification.KIND_PLACED: "Thanks for your order {number} with {shop}. We'll text you when it ships.",
    SmsNotification.KIND_DISPATCHED: "Good news: your {shop} order {number} is on its way.",
    SmsNotification.KIND_FOLLOWUP: "How did the delivery of your {shop} order {number} go? Just reply to let us know.",
    SmsNotification.KIND_CANCELLED: "Your {shop} order {number} has been cancelled.",
}


def _with_ref(text: str, token: str) -> str:
    return "%s Ref %s" % (text, token)


def _without_ref(body: str) -> str:
    return body.rsplit(" Ref ", 1)[0]


def notify(order, kind: str, send_at: datetime | None = None) -> SmsNotification | None:
    """Tell the order's shopper about ``kind``. Never raises; None if they have no number."""
    reference = "%s:order:%s:%s" % (_install_id(), order.pk, kind)
    try:
        number = active_numbers(order.user).first() if order.user_id else None
        if number is None:
            return None
        text = MESSAGE_TEXT[kind].format(number=order.number, shop=settings.OSCAR_SHOP_NAME)
        return safe_send(reference, {
            "order": order, "user": order.user, "contact_number": number, "kind": kind,
            "to_number": number.phone_number, "body": _with_ref(text, ref_token(reference)),
            "scheduled_for": send_at,
        })
    except Exception as exc:  # a message must never fail the order operation
        logger.error("order %s: %s notification could not be processed (%s)", order.pk, kind, type(exc).__name__)
        return SmsNotification.objects.filter(reference=reference).first()


def place_order(user, items: list[tuple[int, int]]):
    """Place an order for ``[(product_id, quantity), ...]`` through Oscar's own order pipeline."""
    if not items:
        raise Invalid("items must contain at least one product.")
    strategy = Selector().strategy(user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user, status=Basket.SAVED)  # SAVED: invisible to the site's open basket
        basket.strategy = strategy
        for product_id, quantity in items:
            product = Product.objects.filter(pk=product_id, is_public=True).first()
            if product is None or product.is_parent:
                raise Invalid("Product %s is not a purchasable catalogue item." % product_id, 422)
            info = strategy.fetch_for_product(product)
            allowed, reason = info.availability.is_purchase_permitted(quantity)
            if not allowed:
                raise Invalid("Product %s: %s" % (product_id, reason), 422)
            basket.add_product(product, quantity)
        shipping_method = Repository().get_default_shipping_method(basket=basket)
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user)
        basket.submit()
    logger.info("order %s placed by user %s via API", order.pk, user.pk)
    notify(order, SmsNotification.KIND_PLACED)
    return order


def _followup_time() -> datetime:
    return timezone.now() + timedelta(hours=settings.SMS_FOLLOWUP_DELAY_HOURS)


def dispatch_order(order_id: int):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None:
            return None
        if order.status != STATUS_DISPATCHED:
            try:
                EventHandler().handle_order_status_change(order, STATUS_DISPATCHED)
            except InvalidOrderStatus:
                raise Conflict("Order %s cannot be dispatched from status '%s'." % (order.number, order.status))
            event_type, __ = ShippingEventType.objects.get_or_create(name=STATUS_DISPATCHED)
            lines = list(order.lines.all())
            EventHandler().create_shipping_event(order, event_type, lines, [line.quantity for line in lines])
    notify(order, SmsNotification.KIND_DISPATCHED)
    # Re-read the committed status: a cancel may have landed meanwhile.
    if Order.objects.filter(pk=order.pk, status=STATUS_DISPATCHED).exists():
        followup = notify(order, SmsNotification.KIND_FOLLOWUP, send_at=_followup_time())
        if followup is not None and Order.objects.filter(pk=order.pk, status=STATUS_CANCELLED).exists():
            call_off(followup)  # the cancel ran while we scheduled: it may not have seen our sid
    order.refresh_from_db()
    return order


def cancel_order(order_id: int):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None:
            return None
        if order.status != STATUS_CANCELLED:
            try:
                EventHandler().handle_order_status_change(order, STATUS_CANCELLED)
            except InvalidOrderStatus:
                raise Conflict("Order %s cannot be cancelled from status '%s'." % (order.number, order.status))
    # Call off the follow-up first: it must never reach the shopper.
    for followup in order.sms_notifications.filter(kind=SmsNotification.KIND_FOLLOWUP):
        call_off(followup)
    notify(order, SmsNotification.KIND_CANCELLED)
    order.refresh_from_db()
    return order


def call_off(followup: SmsNotification) -> SmsNotification:
    """Make sure a scheduled message is not sent. Never raises; the result is in ``cancel_state``."""
    try:
        if not followup.provider_sid:
            if followup.outcome == SmsNotification.FAILED:
                followup.cancel_state = gateway.DONE  # it was never created at the provider
                followup.save(update_fields=["cancel_state"])
                return followup
            if followup.outcome == SmsNotification.SENDING:
                # In flight in the dispatching request, which re-checks the order after it
                # records the sid; a stale claim is settled by lookup below.
                if followup.claimed_at > timezone.now() - SEND_WINDOW:
                    followup.cancel_state = gateway.PENDING
                    followup.save(update_fields=["cancel_state"])
                    return followup
            settle_by_lookup(followup)
            if not followup.provider_sid:
                followup.cancel_state = gateway.UNKNOWN
                followup.save(update_fields=["cancel_state"])
                logger.error("follow-up %s: cannot confirm it was called off (not found at provider yet)",
                             followup.pk)
                return followup
        if followup.provider_status == "canceled":
            followup.cancel_state = gateway.DONE
            followup.save(update_fields=["cancel_state"])
            return followup
        try:
            answer = gateway.cancel_message(followup.provider_sid)
        except ProviderRejected:
            # Typically: no longer cancellable. Read what it actually is.
            answer = gateway.fetch_message(followup.provider_sid)
        followup.cancel_state = gateway.cancel_outcome(answer.status)
        complete(followup, gateway.send_outcome(answer.status), answer)
        followup.save(update_fields=["cancel_state"])
        if followup.cancel_state != gateway.DONE:
            logger.error("follow-up %s: call-off outcome %s", followup.pk, followup.cancel_state)
    except Exception as exc:
        logger.error("follow-up %s: call-off failed (%s)", followup.pk, type(exc).__name__)
        followup.cancel_state = gateway.UNKNOWN
        followup.save(update_fields=["cancel_state"])
    return followup


# --------------------------------------------------------------------------
# Reading provider state back (no callback URL exists - we ask)
# --------------------------------------------------------------------------


def needs_refresh(row: SmsNotification) -> bool:
    if row.outcome in (SmsNotification.PENDING, SmsNotification.UNKNOWN):
        return True
    if row.outcome == SmsNotification.SENDING:
        return row.claimed_at <= timezone.now() - SEND_WINDOW
    return row.cancel_state in (gateway.PENDING, gateway.UNKNOWN)


def refresh(row: SmsNotification) -> SmsNotification:
    """Bring one notification up to date from the provider. Never raises."""
    try:
        if not row.provider_sid:
            if row.outcome in (SmsNotification.SENDING, SmsNotification.UNKNOWN) and needs_refresh(row):
                settle_by_lookup(row)
            return row
        answer = gateway.fetch_message(row.provider_sid)
        complete(row, gateway.send_outcome(answer.status), answer)
        if row.cancel_state:
            row.cancel_state = gateway.cancel_outcome(answer.status)
            row.save(update_fields=["cancel_state"])
    except Exception as exc:
        logger.warning("notification %s: refresh failed (%s)", row.pk, type(exc).__name__)
    return row


def refresh_all(rows, limit: int = MAX_REFRESH_PER_REQUEST) -> None:
    for row in [r for r in rows if needs_refresh(r)][:limit]:
        refresh(row)


# --------------------------------------------------------------------------
# Flow 3 - operator actions
# --------------------------------------------------------------------------


@dataclass
class ResendResult:
    notification: SmsNotification
    repeated: bool


def resend(original_id: int, idempotency_key: str) -> ResendResult | None:
    original = SmsNotification.objects.select_related("order", "contact_number").filter(pk=original_id).first()
    if original is None:
        return None
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    reference = "%s:resend:%s:%s" % (_install_id(), original.pk, key_hash)

    # The same key again is a repeat: answer from what that request did.
    previous = SmsNotification.objects.filter(reference=reference).first()
    released = previous is not None and previous.outcome == SmsNotification.FAILED and not previous.provider_sid
    if previous is not None and not released:
        if needs_refresh(previous):
            refresh(previous)
        return ResendResult(previous, repeated=True)

    refresh(original)
    if original.outcome != SmsNotification.FAILED:
        raise Conflict("Only a message that did not reach the shopper can be re-sent "
                       "(this one is '%s')." % original.outcome)
    if original.content_disposed_at:
        raise Conflict("The content of this message has been disposed of; it cannot be re-sent.")
    number = original.contact_number
    if number is None or number.deleted_at is not None:
        raise Conflict("The shopper's number for this message is no longer on file.")
    if original.kind == SmsNotification.KIND_FOLLOWUP and original.order.status == STATUS_CANCELLED:
        raise Conflict("The order was cancelled; its delivery follow-up must not be sent.")

    row = safe_send(reference, {
        "order": original.order, "user": original.user, "contact_number": number,
        "kind": original.kind, "resend_of": original, "to_number": number.phone_number,
        "body": _with_ref(_without_ref(original.body), ref_token(reference)),
    })
    logger.info("notification %s re-sent as %s (outcome %s)", original.pk, row.pk, row.outcome)
    return ResendResult(row, repeated=False)


def dispose_content(notification_id: int) -> SmsNotification | None:
    row = SmsNotification.objects.filter(pk=notification_id).first()
    if row is None:
        return None
    if row.content_disposed_at:
        return row
    refresh(row)
    if row.provider_sid:
        if gateway.send_outcome(row.provider_status or None) == gateway.PENDING:
            raise Conflict("The message has not finished sending (status '%s'); its content can be "
                           "disposed of once it has." % row.provider_status)
        answer = gateway.redact_message(row.provider_sid)
        if answer.body != "":
            # The provider did not echo an empty body: the text may still be there.
            raise ProviderUnavailable(502, "The provider did not confirm the content was erased.",
                                      outcome_unknown=True)
        complete(row, gateway.send_outcome(answer.status), answer)
    elif row.outcome in (SmsNotification.SENDING, SmsNotification.UNKNOWN):
        raise Conflict("It is not yet known whether this message reached the provider; "
                       "try again once its outcome is settled.")
    row.body = ""
    row.content_disposed_at = timezone.now()
    row.save(update_fields=["body", "content_disposed_at"])
    logger.info("notification %s: content disposed of", row.pk)
    return row


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


@dataclass
class Reconciliation:
    start: datetime
    end: datetime
    matched: list = field(default_factory=list)
    provider_only: list = field(default_factory=list)
    local_only: list = field(default_factory=list)
    not_sent: list = field(default_factory=list)
    unsettled: list = field(default_factory=list)


def reconcile(start: datetime, end: datetime) -> Reconciliation:
    """Line the provider's messages FROM our number, sent in [start, end), up against ours.

    Both sides are filtered on the provider's clock (``date_sent``). The provider
    filter may be day-granular, so it is widened by a day each side and narrowed
    back here.
    """
    report = Reconciliation(start=start, end=end)
    provider = [m for m in gateway.list_sent_from_our_number(start - timedelta(days=1), end + timedelta(days=1))
                if m.sid and m.date_sent and start <= m.date_sent < end]

    by_sid = defaultdict(list)
    for message in provider:
        by_sid[message.sid].append(message)
    local_by_sid = {n.provider_sid: n for n in SmsNotification.objects.filter(provider_sid__in=list(by_sid))}
    for sid, messages in by_sid.items():
        local = local_by_sid.get(sid)
        for message in messages:
            (report.matched if local else report.provider_only).append((message, local))

    in_window = SmsNotification.objects.filter(provider_sent_at__gte=start, provider_sent_at__lt=end)
    report.local_only = list(in_window.exclude(provider_sid__in=list(by_sid)))
    created_in_window = SmsNotification.objects.filter(created_at__gte=start, created_at__lt=end)
    # Accepted by the provider but never sent (scheduled, or called off): no date_sent.
    report.not_sent = list(created_in_window.filter(provider_sid__isnull=False, provider_sent_at__isnull=True)
                           .exclude(provider_sid__in=list(by_sid)))
    # No provider id: in flight, or may have been sent - an operator must look.
    report.unsettled = list(created_in_window.filter(
        provider_sid__isnull=True, outcome__in=[SmsNotification.SENDING, SmsNotification.UNKNOWN]))
    return report
