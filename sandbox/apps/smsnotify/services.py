"""Order + notification orchestration.

Reuses Oscar's own basket/order machinery to place orders, drives order status
through Oscar's configured pipeline, and sends the SMS notifications as an order
moves. A send that fails NEVER fails the underlying order operation -- the order
is still placed/dispatched/cancelled and the failure is recorded on the
Notification row instead.
"""

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from oscar.apps.order.exceptions import InvalidOrderStatus
from oscar.core.loading import get_class, get_model

from . import exceptions, gateway
from .models import (
    ContactNumber,
    Notification,
    NotificationCategory,
    NotificationStatus,
    NON_TERMINAL_STATUSES,
)

log = logging.getLogger("smsnotify.services")

Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Order = get_model("order", "Order")
Free = get_class("shipping.methods", "Free")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")
Selector = get_class("partner.strategy", "Selector")

# Order status values from the sandbox's OSCAR_ORDER_STATUS_PIPELINE.
STATUS_PENDING = "Pending"
STATUS_DISPATCHED = "Being processed"
STATUS_CANCELLED = "Cancelled"

FOLLOWUP_DELAY = timedelta(days=3)


class OrderPlacementError(Exception):
    """The caller's order request could not be turned into an order (a 4xx)."""


class RecipientNotRegistered(Exception):
    """The message's destination is no longer a registered number of the shopper.

    A deleted number must never be messaged again -- including by a resend.
    """


# --------------------------------------------------------------------------- #
# Message bodies (kept short -- these are real, billed SMS)
# --------------------------------------------------------------------------- #


def _body_for(order, category):
    number = order.number
    if category == NotificationCategory.PLACED:
        return f"Thanks! Your order {number} has been placed."
    if category == NotificationCategory.DISPATCHED:
        return f"Good news -- your order {number} is on its way."
    if category == NotificationCategory.CANCELLED:
        return f"Your order {number} has been cancelled."
    if category == NotificationCategory.DELIVERY_FOLLOWUP:
        return f"How did the delivery of order {number} go? We'd love your feedback."
    return f"Update about your order {number}."


# --------------------------------------------------------------------------- #
# Order placement
# --------------------------------------------------------------------------- #


def place_order(user, items):
    """Place an order for ``user`` from ``items`` (list of {product_id, quantity}).

    Reuses Oscar's OrderCreator so the resulting rows are ordinary Oscar
    Order/Line records. Raises OrderPlacementError for bad input (empty, unknown
    product, non-purchasable).
    """
    if not items:
        raise OrderPlacementError("No items supplied.")

    basket = Basket()
    basket.strategy = Selector().strategy(user=user)
    basket.owner = user
    basket.save()

    for item in items:
        pid = item.get("product_id")
        qty = item.get("quantity", 1)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise OrderPlacementError(f"Invalid quantity for product {pid}.")
        if qty < 1:
            raise OrderPlacementError(f"Invalid quantity for product {pid}.")
        try:
            product = Product.objects.get(pk=pid)
        except (Product.DoesNotExist, ValueError, TypeError):
            raise OrderPlacementError(f"Unknown product {pid!r}.")
        info = basket.strategy.fetch_for_product(product)
        if info.availability is None or not info.availability.is_available_to_buy:
            raise OrderPlacementError(f"Product {pid} is not available to buy.")
        basket.add_product(product, qty)

    if basket.is_empty:
        raise OrderPlacementError("No purchasable items supplied.")

    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)

    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        user=user,
        status=STATUS_PENDING,
    )
    basket.submit()

    notify_order(order, NotificationCategory.PLACED)
    return order


# --------------------------------------------------------------------------- #
# Sending notifications
# --------------------------------------------------------------------------- #


def _active_numbers(user):
    return list(ContactNumber.objects.filter(owner=user))


def _record_send_failure(notif, err):
    # A write whose outcome is unknown may have reached the handset -> UNKNOWN,
    # not FAILED (which would claim it definitely did not arrive).
    notif.status = (
        NotificationStatus.UNKNOWN if getattr(err, "outcome_unknown", False)
        else NotificationStatus.FAILED
    )
    notif.error_message = getattr(err, "message", str(err))
    notif.save(update_fields=["status", "error_message", "updated"])


def _apply_result(notif, result):
    notif.provider_sid = result.sid
    notif.twilio_status = result.raw_status
    notif.status = result.status
    notif.error_code = result.error_code
    notif.error_message = result.error_message
    notif.save(
        update_fields=[
            "provider_sid", "twilio_status", "status",
            "error_code", "error_message", "updated",
        ]
    )


def notify_order(order, category):
    """Send an order-event SMS to each of the shopper's registered numbers.

    Best-effort: a provider failure is recorded on the Notification and swallowed.
    A shopper with no number on file is simply not messaged.
    """
    numbers = _active_numbers(order.user) if order.user_id else []
    created = []
    for cn in numbers:
        body = _body_for(order, category)
        notif = Notification.objects.create(
            order=order,
            category=category,
            recipient=cn.phone_number,
            contact_number=cn,
            body=body,
            status=NotificationStatus.PENDING,
        )
        try:
            result = gateway.send_sms(cn.phone_number, body)
            _apply_result(notif, result)
        except exceptions.ProviderError as e:
            log.warning("send failed for notification %s: %s", notif.pk, e.message)
            _record_send_failure(notif, e)
        created.append(notif)
    return created


def schedule_followups(order):
    """Queue a 'how did delivery go' follow-up with the provider for each number."""
    numbers = _active_numbers(order.user) if order.user_id else []
    send_at = timezone.now() + FOLLOWUP_DELAY
    created = []
    for cn in numbers:
        body = _body_for(order, NotificationCategory.DELIVERY_FOLLOWUP)
        notif = Notification.objects.create(
            order=order,
            category=NotificationCategory.DELIVERY_FOLLOWUP,
            recipient=cn.phone_number,
            contact_number=cn,
            body=body,
            is_scheduled=True,
            send_at=send_at,
            status=NotificationStatus.PENDING,
        )
        try:
            result = gateway.schedule_sms(cn.phone_number, body, send_at)
            _apply_result(notif, result)
        except exceptions.ProviderError as e:
            log.warning("schedule failed for notification %s: %s", notif.pk, e.message)
            _record_send_failure(notif, e)
        created.append(notif)
    return created


def _cancel_followup(notif):
    """Cancel a not-yet-sent follow-up at the provider. Best-effort."""
    if notif.canceled or not notif.provider_sid:
        return
    try:
        result = gateway.cancel_scheduled(notif.provider_sid)
        notif.twilio_status = result.raw_status
        notif.status = NotificationStatus.CANCELED
        notif.canceled = True
        notif.save(update_fields=["twilio_status", "status", "canceled", "updated"])
    except exceptions.ProviderError as e:
        # Too late (already sent) or provider error -> leave record, refresh status.
        log.info("could not cancel follow-up %s: %s", notif.pk, e.message)


# --------------------------------------------------------------------------- #
# Order transitions (operator)
# --------------------------------------------------------------------------- #


def dispatch_order(order):
    """Mark an order dispatched: tell the shopper and queue the delivery follow-up.

    Idempotent: dispatching an already-dispatched order re-fires nothing. Raises
    InvalidOrderStatus for an illegal transition (e.g. a cancelled order).
    """
    if order.status == STATUS_DISPATCHED:
        return order  # already dispatched -- no re-fire
    order.set_status(STATUS_DISPATCHED)  # raises InvalidOrderStatus if not allowed
    notify_order(order, NotificationCategory.DISPATCHED)
    schedule_followups(order)
    return order


def cancel_order(order):
    """Cancel an order: tell the shopper and call off any pending follow-up.

    Idempotent: cancelling an already-cancelled order re-fires nothing. Raises
    InvalidOrderStatus for an illegal transition.
    """
    if order.status == STATUS_CANCELLED:
        return order  # already cancelled -- no re-fire
    order.set_status(STATUS_CANCELLED)
    # Call off any follow-up that has not yet gone out, BEFORE telling the shopper.
    pending = order.sms_notifications.filter(
        category=NotificationCategory.DELIVERY_FOLLOWUP,
        canceled=False,
        provider_sid__isnull=False,
    )
    for notif in pending:
        _cancel_followup(notif)
    notify_order(order, NotificationCategory.CANCELLED)
    return order


# --------------------------------------------------------------------------- #
# Status refresh, resend, content disposal
# --------------------------------------------------------------------------- #


def refresh_notification(notif):
    """Refresh a non-terminal notification's delivery outcome from the provider."""
    if not notif.provider_sid or notif.canceled:
        return notif
    if notif.status not in NON_TERMINAL_STATUSES and notif.status != NotificationStatus.SENT:
        return notif
    try:
        result = gateway.fetch_status(notif.provider_sid)
    except exceptions.ProviderError as e:
        log.info("status refresh failed for %s: %s", notif.pk, e.message)
        return notif
    notif.twilio_status = result.raw_status
    notif.status = result.status
    notif.error_code = result.error_code
    notif.error_message = result.error_message
    notif.save(
        update_fields=["twilio_status", "status", "error_code", "error_message", "updated"]
    )
    return notif


def resend_notification(original, idempotency_key):
    """Re-send a message that did not reach the shopper, under an idempotency key.

    Claim-first: the unique ``idempotency_key`` is the claim. A repeat under the
    same key returns the existing resend without sending again; a fresh key sends
    a new message. Returns (notification, created_now).

    Refuses if the recipient is no longer a registered number of the shopper: a
    deleted number must never be messaged again, resends included.
    """
    still_registered = ContactNumber.objects.filter(
        owner=original.order.user, phone_number=original.recipient
    ).exists()
    if not still_registered:
        raise RecipientNotRegistered(
            "The recipient number is no longer registered and cannot be messaged."
        )

    body = original.body or _body_for(original.order, original.category)
    try:
        with transaction.atomic():
            resend = Notification.objects.create(
                order=original.order,
                category=NotificationCategory.RESEND,
                recipient=original.recipient,
                contact_number=original.contact_number,
                body=body,
                status=NotificationStatus.PENDING,
                idempotency_key=idempotency_key,
                resent_from=original,
            )
    except IntegrityError:
        # Key already used -> return the message that claim produced. No second send.
        existing = Notification.objects.get(idempotency_key=idempotency_key)
        return existing, False

    # We won the claim -> send exactly once.
    try:
        result = gateway.send_sms(original.recipient, body)
        _apply_result(resend, result)
    except exceptions.ProviderError as e:
        log.warning("resend %s failed: %s", resend.pk, e.message)
        _record_send_failure(resend, e)
    return resend, True


def dispose_content(notif):
    """Redact a notification's content provider-side and locally; keep the record."""
    if notif.provider_sid and not notif.content_redacted:
        # Redact at the provider (empty body). If it fails we do NOT null locally,
        # so a retry can still reach the provider.
        gateway.redact_content(notif.provider_sid)
    notif.body = None
    notif.content_redacted = True
    notif.save(update_fields=["body", "content_redacted", "updated"])
    return notif


# --------------------------------------------------------------------------- #
# Contact-number deletion
# --------------------------------------------------------------------------- #


def delete_contact_number(cn):
    """Remove a number, first calling off any not-yet-sent follow-up addressed to it."""
    pending = Notification.objects.filter(
        order__user=cn.owner,
        recipient=cn.phone_number,
        category=NotificationCategory.DELIVERY_FOLLOWUP,
        canceled=False,
        provider_sid__isnull=False,
    )
    for notif in pending:
        _cancel_followup(notif)
    cn.delete()


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


def _app_event_time(notif):
    # Same-clock filtering: a scheduled follow-up's provider send time is its
    # send_at, not when we created the row.
    if notif.is_scheduled and notif.send_at:
        return notif.send_at
    return notif.created


def reconcile(date_from, date_to, from_number):
    """Line up the provider's messages against what this app believes it sent.

    Only counts messages sent FROM this app's configured number (asked of the
    provider, not filtered after the fact). Covers the whole [date_from, date_to]
    range: the provider filter is day-granular, so we widen to whole days and
    narrow by the real instants here.
    """
    from email.utils import parsedate_to_datetime

    # Widen to whole-day boundaries for the day-granular provider filter.
    widened_from = date_from.replace(hour=0, minute=0, second=0, microsecond=0)
    widened_to = (date_to + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    messages, truncated = gateway.list_sent_messages(from_number, widened_from, widened_to)

    # Narrow provider messages back to the caller's real instants.
    provider_in_range = []
    for m in messages:
        sent = None
        if m.date_sent:
            try:
                sent = parsedate_to_datetime(m.date_sent)
            except (TypeError, ValueError):
                sent = None
        if sent is not None and not (date_from <= sent <= date_to):
            continue
        provider_in_range.append(m)

    provider_by_sid = {m.sid: m for m in provider_in_range if m.sid}

    # App side, filtered on the same (provider) clock, restricted to our sends.
    app_notifs = [
        n
        for n in Notification.objects.filter(provider_sid__isnull=False).select_related("order")
        if date_from <= _app_event_time(n) <= date_to
    ]
    app_by_sid = {n.provider_sid: n for n in app_notifs}

    matched, app_only, provider_only = [], [], []
    for sid, notif in app_by_sid.items():
        if sid in provider_by_sid:
            m = provider_by_sid[sid]
            matched.append(
                {
                    "notificationId": notif.pk,
                    "providerSid": sid,
                    "appStatus": notif.status,
                    "providerStatus": m.status,
                    "orderNumber": notif.order.number,
                }
            )
        else:
            app_only.append(
                {
                    "notificationId": notif.pk,
                    "providerSid": sid,
                    "appStatus": notif.status,
                    "orderNumber": notif.order.number,
                }
            )
    for sid, m in provider_by_sid.items():
        if sid not in app_by_sid:
            provider_only.append({"providerSid": sid, "providerStatus": m.status})

    return {
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "sendingNumber": from_number,
        "providerMessageCount": len(provider_in_range),
        "appMessageCount": len(app_notifs),
        "matched": matched,
        "appOnly": app_only,
        "providerOnly": provider_only,
        "truncated": truncated,
    }
