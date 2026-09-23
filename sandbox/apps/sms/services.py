"""Order placement and SMS-notification orchestration.

Sends are best-effort: a message that cannot go out never fails the underlying
order operation, and a shopper with no number on file is simply not messaged.
"""

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from oscar.apps.catalogue.models import Product
from oscar.apps.checkout.calculators import OrderTotalCalculator
from oscar.apps.order.utils import OrderCreator
from oscar.apps.partner.strategy import Selector
from oscar.apps.shipping.methods import Free

from .models import ContactNumber, OrderNotification
from . import twilio_gateway
from .twilio_gateway import TwilioError

logger = logging.getLogger("sms.services")

# Oscar status pipeline values from the sandbox settings.
STATUS_DISPATCHED = "Being processed"
STATUS_CANCELLED = "Cancelled"


class OrderPlacementError(Exception):
    """The order could not be built from the requested catalogue items."""


# --- Order placement -----------------------------------------------------

def place_order(user, items):
    """Create an Oscar order for ``user`` from ``items``.

    ``items`` is a list of ``{"product_id": int, "quantity": int}``. Reuses
    Oscar's basket/strategy/order pipeline rather than a parallel model.
    """
    if not items:
        raise OrderPlacementError("no items supplied")

    from oscar.apps.basket.models import Basket

    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(user=user)

    for item in items:
        try:
            product_id = int(item["product_id"])
            quantity = int(item["quantity"])
        except (KeyError, TypeError, ValueError):
            raise OrderPlacementError("each item needs product_id and quantity")
        if quantity < 1:
            raise OrderPlacementError("quantity must be at least 1")
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise OrderPlacementError(f"unknown product {product_id}")

        info = basket.strategy.fetch_for_product(product)
        if info.stockrecord is None or not info.availability.is_available_to_buy:
            raise OrderPlacementError(
                f"product {product_id} is not available to buy"
            )
        basket.add_product(product, quantity=quantity)

    if basket.is_empty:
        raise OrderPlacementError("basket is empty")

    shipping_method = Free()
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


# --- Notification helpers -----------------------------------------------

def _latest_contact(user):
    return (
        ContactNumber.objects.filter(owner=user).order_by("-created_at").first()
    )


def _apply_result(notif, result):
    notif.provider_sid = result.sid or ""
    notif.status = result.status or ""
    notif.error_code = result.error_code
    notif.error_message = result.error_message or ""
    if result.body is not None:
        notif.body = result.body


def _record_send_failure(notif, exc):
    notif.status = OrderNotification.LOCAL_STATUS_SEND_FAILED
    # Our own message only — never the provider's raw body or the number.
    notif.error_message = str(exc)


def _send_and_record(order, contact, kind, body, *, idempotency_key=None):
    """Send an immediate SMS and persist a notification. Best-effort."""
    notif = OrderNotification(
        order=order,
        recipient=contact,
        to_number=contact.e164,
        kind=kind,
        body=body,
        idempotency_key=idempotency_key,
    )
    try:
        result = twilio_gateway.send_sms(contact.e164, body)
        _apply_result(notif, result)
    except TwilioError as exc:
        _record_send_failure(notif, exc)
        logger.warning("SMS %s send failed for order %s: %s", kind, order.number, exc)
    notif.save()
    return notif


def notify_order_placed(order):
    contact = _latest_contact(order.user)
    if contact is None:
        return None
    body = (
        f"Thanks! Your order {order.number} has been placed. "
        f"We'll text you as it progresses."
    )
    return _send_and_record(order, contact, OrderNotification.KIND_ORDER_PLACED, body)


def dispatch_order(order):
    """Mark dispatched, tell the shopper, and queue a delivery follow-up."""
    order.set_status(STATUS_DISPATCHED)

    contact = _latest_contact(order.user)
    if contact is None:
        return {"dispatched": None, "followup": None}

    body = f"Good news — your order {order.number} is on its way!"
    dispatched = _send_and_record(
        order, contact, OrderNotification.KIND_ORDER_DISPATCHED, body
    )

    followup = _queue_followup(order, contact)
    return {"dispatched": dispatched, "followup": followup}


def _queue_followup(order, contact):
    delay_days = getattr(settings, "SMS_FOLLOWUP_DELAY_DAYS", 3)
    send_at = timezone.now() + timedelta(days=delay_days)
    body = (
        f"How did the delivery of your order {order.number} go? "
        f"We'd love your feedback."
    )
    notif = OrderNotification(
        order=order,
        recipient=contact,
        to_number=contact.e164,
        kind=OrderNotification.KIND_DELIVERY_FOLLOWUP,
        body=body,
        is_scheduled=True,
    )
    try:
        result = twilio_gateway.schedule_followup(contact.e164, body, send_at)
        _apply_result(notif, result)
    except TwilioError as exc:
        notif.is_scheduled = False
        _record_send_failure(notif, exc)
        logger.warning(
            "Follow-up scheduling failed for order %s: %s", order.number, exc
        )
    notif.save()
    return notif


def cancel_order(order):
    """Cancel the order, call off any pending follow-up, and tell the shopper."""
    order.set_status(STATUS_CANCELLED)

    cancelled_followups = _cancel_pending_followups(order)

    contact = _latest_contact(order.user)
    cancelled_notice = None
    if contact is not None:
        body = f"Your order {order.number} has been cancelled."
        cancelled_notice = _send_and_record(
            order, contact, OrderNotification.KIND_ORDER_CANCELLED, body
        )
    return {
        "cancelled_notice": cancelled_notice,
        "cancelled_followups": cancelled_followups,
    }


# How hard to try to call off a scheduled follow-up. The provider briefly
# rejects a cancel issued in the first moment after scheduling (before the
# scheduled message has propagated); retrying a few times closes that window so
# a cancelled order's follow-up can never slip out.
_CANCEL_ATTEMPTS = 4
_CANCEL_RETRY_DELAY = 2.0


def _cancel_scheduled_with_retry(sid):
    last_exc = None
    for attempt in range(_CANCEL_ATTEMPTS):
        try:
            return twilio_gateway.cancel_scheduled(sid)
        except TwilioError as exc:
            last_exc = exc
            # Only a provider *rejection* (a known 4xx, e.g. "not cancelable
            # yet") is worth retrying. An unknown outcome or a transport error
            # is not — retrying could double-handle it.
            if exc.status_code is None:
                break
            if attempt < _CANCEL_ATTEMPTS - 1:
                time.sleep(_CANCEL_RETRY_DELAY)
    raise last_exc


def _cancel_pending_followups(order):
    """Call off every still-scheduled follow-up so none reaches the customer."""
    pending = OrderNotification.objects.filter(
        order=order,
        kind=OrderNotification.KIND_DELIVERY_FOLLOWUP,
        is_scheduled=True,
    )
    cancelled = []
    for notif in pending:
        if notif.provider_sid:
            try:
                result = _cancel_scheduled_with_retry(notif.provider_sid)
                notif.status = result.status or "canceled"
            except TwilioError as exc:
                # Record but do not fail the cancellation of the order itself.
                notif.error_message = str(exc)
                logger.warning(
                    "Could not cancel scheduled follow-up for order %s: %s",
                    order.number,
                    exc,
                )
                # Leave is_scheduled True so it can be retried; still counts as
                # attempted.
                notif.save()
                continue
        else:
            notif.status = "canceled"
        notif.is_scheduled = False
        notif.save()
        cancelled.append(notif)
    return cancelled


# --- Operator: resend ----------------------------------------------------

class ResendError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def resend_notification(notification, idempotency_key):
    """Re-send a message that did not reach the shopper.

    Idempotent on ``idempotency_key``: a repeat under the same key returns the
    existing resend without sending again; a fresh key sends a new message.
    Returns ``(notification, created)``.
    """
    if not idempotency_key:
        raise ResendError("an idempotency key is required")

    existing = OrderNotification.objects.filter(
        idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        return existing, False

    if notification.recipient is None:
        # The number was removed; nothing may be sent to it again.
        raise ResendError(
            "the recipient number has been removed; cannot resend", status=409
        )
    if not notification.body:
        raise ResendError(
            "this message has no content to resend", status=409
        )

    contact = notification.recipient
    body = notification.body

    # Claim the idempotency key first so a concurrent repeat cannot double-send.
    try:
        with transaction.atomic():
            resend = OrderNotification.objects.create(
                order=notification.order,
                recipient=contact,
                to_number=contact.e164,
                kind=OrderNotification.KIND_RESEND,
                body=body,
                idempotency_key=idempotency_key,
            )
    except IntegrityError:
        existing = OrderNotification.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return existing, False
        raise

    try:
        result = twilio_gateway.send_sms(contact.e164, body)
        _apply_result(resend, result)
    except TwilioError as exc:
        _record_send_failure(resend, exc)
        logger.warning(
            "Resend failed for order %s: %s", notification.order.number, exc
        )
    resend.save()
    return resend, True


# --- Shopper: content disposal ------------------------------------------

class ContentDisposalError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def dispose_content(notification):
    """Dispose of a message's content at the provider and locally.

    The provider redaction must succeed for the guarantee to hold, so a failure
    here is reported (and the local copy is kept for a retry) rather than
    silently claimed. The record of the send and its outcome survives.
    """
    if notification.content_redacted:
        return notification

    if notification.provider_sid:
        try:
            twilio_gateway.redact_content(notification.provider_sid)
        except TwilioError as exc:
            raise ContentDisposalError(
                f"could not dispose of the content at the provider: {exc}"
            )

    notification.body = ""
    notification.content_redacted = True
    notification.save(update_fields=["body", "content_redacted", "updated_at"])
    return notification


# --- Delivery-status refresh --------------------------------------------

# Delivery outcomes that will not change again — no point re-fetching them.
TERMINAL_STATUSES = frozenset(
    {
        "delivered",
        "read",
        "undelivered",
        "failed",
        "canceled",
        OrderNotification.LOCAL_STATUS_SEND_FAILED,
    }
)


def refresh_status(notification):
    """Best-effort refresh of a notification's cached delivery outcome.

    Skips messages with no provider SID and those already in a terminal state
    (so a list view does not re-query the provider for outcomes that are fixed).
    """
    if not notification.provider_sid:
        return notification
    if notification.status in TERMINAL_STATUSES:
        return notification
    try:
        result = twilio_gateway.fetch_status(notification.provider_sid)
    except TwilioError as exc:
        logger.info(
            "Status refresh failed for notification %s: %s", notification.pk, exc
        )
        return notification
    changed = False
    if result.status and result.status != notification.status:
        notification.status = result.status
        changed = True
    if result.error_code != notification.error_code:
        notification.error_code = result.error_code
        changed = True
    if (result.error_message or "") != notification.error_message:
        notification.error_message = result.error_message or ""
        changed = True
    if changed:
        notification.save(
            update_fields=["status", "error_code", "error_message", "updated_at"]
        )
    return notification


# --- Operator: reconciliation -------------------------------------------

def reconcile(date_from, date_to):
    """Line the provider's record up against what this app believes it sent.

    Returns provider messages from this app's sending number in the range, the
    app's notifications in the range, and the discrepancies in both directions.
    """
    provider_messages = twilio_gateway.list_sent_messages(date_from, date_to)
    provider_by_sid = {m.sid: m for m in provider_messages if m.sid}

    app_notifs = OrderNotification.objects.filter(
        created_at__gte=date_from, created_at__lte=date_to
    ).exclude(provider_sid="")
    app_by_sid = {n.provider_sid: n for n in app_notifs}

    provider_sids = set(provider_by_sid)
    app_sids = set(app_by_sid)

    only_provider = sorted(provider_sids - app_sids)
    only_app = sorted(app_sids - provider_sids)
    matched = sorted(provider_sids & app_sids)

    return {
        "provider_messages": provider_messages,
        "provider_by_sid": provider_by_sid,
        "app_by_sid": app_by_sid,
        "only_provider": only_provider,
        "only_app": only_app,
        "matched": matched,
    }
