"""Application services: order placement + the notification lifecycle.

Order placement reuses Oscar's own basket/order machinery (OrderCreator) rather than a parallel
model. Every notification interaction goes through `provider` and can never fail the underlying
order operation.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from oscar.apps.basket.models import Basket
from oscar.apps.catalogue.models import Product
from oscar.apps.checkout.calculators import OrderTotalCalculator
from oscar.apps.order.models import Order
from oscar.apps.order.utils import OrderCreator
from oscar.apps.partner.strategy import Selector
from oscar.apps.shipping.methods import NoShippingRequired

from . import provider
from .models import (
    ContactNumber,
    Notification,
    NotificationKind,
    OrderTransitionClaim,
    Outcome,
)

log = logging.getLogger("apps.sms")

# Oscar status the sandbox pipeline allows: Pending -> Being processed / Cancelled.
DISPATCH_STATUS = "Being processed"
CANCEL_STATUS = "Cancelled"
INITIAL_STATUS = settings.OSCAR_INITIAL_ORDER_STATUS  # 'Pending'

# A follow-up "how did delivery go?" is queued with the provider a few days out.
FOLLOWUP_DELAY = timedelta(days=3)


# --- domain errors (mapped to HTTP status at the boundary) ---------------------------------

class ServiceError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class InvalidRequest(ServiceError):
    def __init__(self, message):
        super().__init__(400, message)


class Conflict(ServiceError):
    def __init__(self, message):
        super().__init__(409, message)


# --- contact numbers -----------------------------------------------------------------------

def register_contact_number(user, raw_number):
    """Validate a number with the provider and store its canonical E.164 form for `user`.

    A number the provider does not consider a usable destination is rejected here (400), before
    any message is ever attempted.
    """
    raw_number = (raw_number or "").strip()
    if not raw_number:
        raise InvalidRequest("a phone number is required")
    canonical = provider.lookup_number(raw_number)  # ProviderError on provider/transport failure
    if not canonical:
        raise InvalidRequest("that number is not a usable messaging destination")
    obj, _created = ContactNumber.objects.get_or_create(owner=user, e164=canonical)
    return obj


def list_contact_numbers(user):
    return list(ContactNumber.objects.filter(owner=user))


def delete_contact_number(user, contact_id):
    """Remove one of the caller's own numbers. Afterwards nothing is sent to it again."""
    deleted, _ = ContactNumber.objects.filter(owner=user, pk=contact_id).delete()
    return deleted > 0


def current_contact(user):
    """The number the shop will message: the shopper's most recently registered one, or None."""
    return ContactNumber.objects.filter(owner=user).order_by("-created_at", "-pk").first()


# --- notification plumbing -----------------------------------------------------------------

def _apply_result(notification, result):
    notification.provider_sid = result.sid or ""
    notification.provider_status = result.status or ""
    notification.outcome = result.outcome
    notification.outcome_unknown = result.outcome_unknown
    notification.error_code = result.error_code
    notification.error_message = result.error_message or ""
    if result.date_sent:
        notification.provider_date_sent = result.date_sent
    if result.date_created:
        notification.provider_date_created = result.date_created
    notification.save()


def _notify_immediate(order, kind, body):
    """Send an immediate SMS about `order` to its owner, if a number is on file. Never raises."""
    contact = current_contact(order.user)
    if contact is None:
        return None  # no number on file -> simply not messaged
    notification = Notification.objects.create(
        order=order,
        owner=order.user,
        kind=kind,
        to_number=contact.e164,
        body=body,
        outcome=Outcome.PENDING,
    )
    result = provider.send_immediate(contact.e164, body)
    _apply_result(notification, result)
    return notification


def _schedule_followup(order):
    contact = current_contact(order.user)
    if contact is None:
        return None
    body = _body_followup(order)
    notification = Notification.objects.create(
        order=order,
        owner=order.user,
        kind=NotificationKind.DELIVERY_FOLLOWUP,
        to_number=contact.e164,
        body=body,
        outcome=Outcome.PENDING,
    )
    send_at = timezone.now() + FOLLOWUP_DELAY
    result = provider.schedule_followup(contact.e164, body, send_at)
    _apply_result(notification, result)
    return notification


def _cancel_pending_followups(order):
    """Call off any follow-up that has not yet gone out for this order (best-effort, recorded)."""
    pending = Notification.objects.filter(
        order=order,
        kind=NotificationKind.DELIVERY_FOLLOWUP,
        outcome__in=[Outcome.SCHEDULED, Outcome.PENDING, Outcome.UNKNOWN],
    ).exclude(provider_sid="")
    for followup in pending:
        result = provider.cancel_scheduled(followup.provider_sid)
        if result.sid is not None or result.outcome == Outcome.CANCELED:
            followup.outcome = Outcome.CANCELED
            followup.outcome_unknown = False
        else:
            # Could not confirm the follow-up was called off — flag it so an operator can act
            # before it might reach a customer whose order was cancelled.
            followup.error_message = result.error_message or followup.error_message
            followup.outcome_unknown = True
        if result.status:
            followup.provider_status = result.status
        followup.save()


# --- message bodies (order number only; no personal data) ----------------------------------

def _body_placed(order):
    return f"Thanks! Your order {order.number} has been placed. We'll keep you posted."


def _body_dispatched(order):
    return f"Good news — your order {order.number} is on its way!"


def _body_followup(order):
    return f"How did the delivery of your order {order.number} go? We'd love your feedback."


def _body_cancelled(order):
    return f"Your order {order.number} has been cancelled. Contact us with any questions."


# --- order placement -----------------------------------------------------------------------

def place_order(user, items):
    """Place an order for `user` from (product_id, quantity) pairs, reusing Oscar's OrderCreator.

    Then tell the shopper their order was placed. Returns (order, placed_notification).
    """
    if not items:
        raise InvalidRequest("at least one line item is required")

    basket = Basket()
    basket.strategy = Selector().strategy(user=user)
    basket.save()
    basket.owner = user
    basket.save()

    for item in items:
        product_id = item.get("product_id") or item.get("productId")
        quantity = item.get("quantity", 1)
        if product_id is None:
            raise InvalidRequest("each item needs a product_id")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise InvalidRequest("quantity must be an integer")
        if quantity < 1:
            raise InvalidRequest("quantity must be at least 1")
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise InvalidRequest(f"unknown product {product_id}")

        info = basket.strategy.fetch_for_product(product)
        if not info.availability.is_available_to_buy:
            raise InvalidRequest(f"product {product_id} is not available to buy")
        basket.add_product(product, quantity=quantity)

    if basket.is_empty:
        raise InvalidRequest("the order has no purchasable lines")

    shipping_method = NoShippingRequired()
    shipping_charge = shipping_method.calculate(basket)
    total = OrderTotalCalculator().calculate(basket, shipping_charge)

    order = OrderCreator().place_order(
        basket=basket,
        total=total,
        shipping_method=shipping_method,
        shipping_charge=shipping_charge,
        user=user,
        status=INITIAL_STATUS,
    )
    notification = _notify_immediate(order, NotificationKind.ORDER_PLACED, _body_placed(order))
    return order, notification


# --- operator transitions ------------------------------------------------------------------

def dispatch_order(order):
    """Mark an order dispatched (once). Tell the shopper; queue a delivery follow-up.

    Returns (order, changed). `changed` is False if it was already dispatched (a clean no-op).
    """
    try:
        with transaction.atomic():
            OrderTransitionClaim.objects.create(order=order, to_status=DISPATCH_STATUS)
            fresh = Order.objects.select_for_update().get(pk=order.pk)
            if fresh.status != INITIAL_STATUS:
                raise Conflict(
                    f"order cannot be dispatched from status '{fresh.status}'"
                )
            fresh.set_status(DISPATCH_STATUS)  # host method: validation, history, cascade, signal
    except IntegrityError:
        return order, False  # already dispatched: no re-notify, no second follow-up

    _notify_immediate(fresh, NotificationKind.ORDER_DISPATCHED, _body_dispatched(fresh))
    _schedule_followup(fresh)
    return fresh, True


def cancel_order(order):
    """Cancel an order (once). Tell the shopper; call off any not-yet-sent delivery follow-up.

    Returns (order, changed). `changed` is False if it was already cancelled.
    """
    try:
        with transaction.atomic():
            OrderTransitionClaim.objects.create(order=order, to_status=CANCEL_STATUS)
            fresh = Order.objects.select_for_update().get(pk=order.pk)
            if fresh.status == CANCEL_STATUS:
                raise IntegrityError("already cancelled")
            if CANCEL_STATUS not in fresh.available_statuses():
                raise Conflict(
                    f"order cannot be cancelled from status '{fresh.status}'"
                )
            fresh.set_status(CANCEL_STATUS)
    except IntegrityError:
        return order, False

    # Prevent the incident: call off the follow-up BEFORE announcing the cancellation.
    _cancel_pending_followups(fresh)
    _notify_immediate(fresh, NotificationKind.ORDER_CANCELLED, _body_cancelled(fresh))
    return fresh, True


# --- operator notification actions ---------------------------------------------------------

def resend_notification(source, idempotency_key):
    """Re-send a message under a caller-supplied idempotency key.

    The new resend row is the durable claim: a repeat under the same key returns the earlier
    result without sending again; a fresh key sends legitimately. Returns (notification, sent).
    """
    idempotency_key = (idempotency_key or "").strip()
    if not idempotency_key:
        raise InvalidRequest("an idempotency key is required")

    notification = Notification(
        order=source.order,
        owner=source.owner,
        kind=NotificationKind.RESEND,
        to_number=source.to_number,
        body=source.body,
        source_notification=source,
        idempotency_key=idempotency_key,
        outcome=Outcome.PENDING,
    )
    try:
        with transaction.atomic():
            notification.save()
    except IntegrityError:
        existing = Notification.objects.get(
            source_notification=source, idempotency_key=idempotency_key
        )
        return existing, False  # same key -> no second message

    result = provider.send_immediate(notification.to_number, notification.body or source.body)
    _apply_result(notification, result)
    return notification, True


def dispose_content(notification):
    """Dispose of a message's text at the provider and here; the record and outcome survive."""
    if notification.provider_sid:
        result = provider.redact_message(notification.provider_sid)
        if result.sid is None:
            # Could not confirm the provider copy was removed — do not claim success.
            raise ServiceError(502, "content disposal at the provider could not be confirmed")
        if result.status:
            notification.provider_status = result.status
    notification.body = ""
    notification.content_disposed = True
    notification.save()
    return notification


def refresh_outcome(notification):
    """Re-read a notification's delivery state from the provider (best-effort)."""
    if not notification.provider_sid:
        return notification
    try:
        result = provider.fetch_status(notification.provider_sid)
    except provider.ProviderError:
        return notification
    if result is None:
        return notification
    _apply_result(notification, result)
    return notification


# --- reconciliation ------------------------------------------------------------------------

def _local_clock(notification):
    return (
        notification.provider_date_sent
        or notification.provider_date_created
        or notification.created_at
    )


def reconcile(start_dt, end_dt):
    """Line up the provider's record of FROM-number messages against ours over [start, end).

    Provider side is filtered by the provider's own DateSent (day-granular, so widened then
    narrowed to instants here) and only for TWILIO_FROM_NUMBER — the account carries other
    traffic. Matching is by message SID against the whole set.
    """
    from_number = settings.TWILIO_FROM_NUMBER

    # Refresh non-terminal local rows so their provider clock is populated for the line-up.
    stale = Notification.objects.filter(
        created_at__gte=start_dt - timedelta(days=1),
        created_at__lt=end_dt + timedelta(days=1),
        provider_date_sent__isnull=True,
    ).exclude(provider_sid="")
    for notification in stale:
        refresh_outcome(notification)

    # Widen the provider query to whole days (DateSent filter granularity), narrow in code.
    after = start_dt - timedelta(days=1)
    before = end_dt + timedelta(days=1)
    messages, truncated = provider.list_messages_from(from_number, after, before)

    provider_by_sid = {}
    for message in messages:
        sid = provider._val(message.sid)
        sent = provider._parse_dt(message.date_sent) or provider._parse_dt(message.date_created)
        if not sid or sent is None:
            continue
        if start_dt <= sent < end_dt:
            provider_by_sid[sid] = (message, sent)

    # Local rows we believe we sent from TWILIO_FROM_NUMBER (immediate sends), on the same clock,
    # in the window. Delivery follow-ups are excluded: they are sent via the Messaging Service, so
    # their `from` is chosen from the sender pool and need not be TWILIO_FROM_NUMBER — counting them
    # against a FROM-number-filtered provider answer would invent local-only discrepancies.
    local_candidates = Notification.objects.exclude(provider_sid="").exclude(
        kind=NotificationKind.DELIVERY_FOLLOWUP
    )
    local_by_sid = {}
    for notification in local_candidates:
        clock = _local_clock(notification)
        if clock is not None and start_dt <= clock < end_dt:
            local_by_sid[notification.provider_sid] = notification

    matched = []
    local_only = []
    for sid, notification in local_by_sid.items():
        if sid in provider_by_sid:
            message, sent = provider_by_sid[sid]
            matched.append((notification, message, sent))
        else:
            local_only.append(notification)

    provider_only = [
        (sid, message, sent)
        for sid, (message, sent) in provider_by_sid.items()
        if sid not in local_by_sid
    ]

    # Immediate-send rows in window we never got a sid for (send failed before the provider
    # assigned one) — the app tried but the provider has no record.
    unsettled = list(
        Notification.objects.filter(
            created_at__gte=start_dt, created_at__lt=end_dt, provider_sid=""
        ).exclude(kind=NotificationKind.DELIVERY_FOLLOWUP)
    )

    return {
        "from_number": from_number,
        "window": {"from": start_dt, "to": end_dt},
        "truncated": truncated,
        "matched": matched,
        "provider_only": provider_only,
        "local_only": local_only,
        "unsettled": unsettled,
    }
