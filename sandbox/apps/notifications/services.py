"""Orchestration between Oscar's order flow and the Twilio gateway.

Two rules from the task shape this module:

* A message that cannot be sent must never fail the underlying operation. The order is placed,
  dispatched or cancelled and the request still succeeds; the failure is recorded on the notification
  row instead.
* Dispatch, cancel and resend must not fire their side effects twice. Each claims a durable row
  first (a unique insert), and the claim -- not a read-then-write -- decides the single winner.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.apps.partner.strategy import Selector
from oscar.core.loading import get_class, get_model

from . import twilio_gateway as tw
from .models import ContactNumber, OrderNotification, OrderTransition

log = logging.getLogger("apps.notifications")

Product = get_model("catalogue", "Product")
StockRecord = get_model("partner", "StockRecord")
Basket = get_model("basket", "Basket")
Order = get_model("order", "Order")
Free = get_class("shipping.methods", "Free")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")

# How far ahead the "how did delivery go?" follow-up is queued (within Twilio's 15min-7day window).
FOLLOWUP_DELAY = timedelta(days=3)

FOLLOWUP_PENDING_STATUSES = {"scheduled", "accepted", "queued"}


class OrderError(Exception):
    """A problem placing or moving an order (a caller-facing 4xx)."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# --- recipient -------------------------------------------------------------------------------------

def recipient_number(order) -> str | None:
    """The number to text for this order's shopper: their most recently registered one, or None."""
    if order.user_id is None:
        return None
    cn = (
        ContactNumber.objects.filter(user_id=order.user_id)
        .order_by("-created_at")
        .first()
    )
    return cn.canonical_number if cn else None


# --- notification recording (send-never-fails-the-operation) ---------------------------------------

def _apply_result(row: OrderNotification, result: tw.MessageResult) -> None:
    row.provider_sid = result.sid or ""
    row.provider_status = result.provider_status or ""
    row.outcome = result.outcome
    row.error_code = result.error_code
    row.provider_time = result.date_sent


def _record_send(order, user, kind, body, *, followup=False) -> OrderNotification:
    """Create a notification row and try to send it. Any provider failure is recorded, not raised."""
    to = recipient_number(order)
    if to is None:
        return OrderNotification.objects.create(
            order=order, user=user, kind=kind, body=body,
            outcome=OrderNotification.OUTCOME_SKIPPED, is_followup=followup,
        )
    row = OrderNotification.objects.create(
        order=order, user=user, kind=kind, to_number=to, body=body,
        outcome=OrderNotification.OUTCOME_SENDING, is_followup=followup,
    )
    try:
        if followup:
            send_at = timezone.now() + FOLLOWUP_DELAY
            row.scheduled_send_at = send_at
            result = tw.schedule_followup(to, body, send_at)
        else:
            result = tw.send_immediate(to, body)
        _apply_result(row, result)
    except tw.TwilioGatewayError as e:
        row.outcome = e.outcome
        log.warning("Notification %s for order %s could not be sent (%s)", kind, order.number, e.outcome)
    row.save()
    return row


# --- flow 2: place / dispatch / cancel -------------------------------------------------------------

def place_order(user, items):
    """Place an order from catalogue product ids + quantities, reusing Oscar's order model."""
    if not items:
        raise OrderError("No items supplied")

    basket = Basket()
    basket.strategy = Selector().strategy(user=user)
    basket.save()
    for item in items:
        try:
            product_id = int(item["productId"])
            quantity = int(item.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            raise OrderError("Each item needs a numeric productId and quantity")
        if quantity < 1:
            raise OrderError("Quantity must be at least 1")
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise OrderError(f"Unknown productId {product_id}", status_code=404)
        if not StockRecord.objects.filter(product=product).exists():
            raise OrderError(f"Product {product_id} is not purchasable")
        basket.add_product(product, quantity=quantity)

    if basket.is_empty:
        raise OrderError("Basket is empty")

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
    _record_send(order, user, OrderNotification.KIND_PLACED,
                 f"Thanks! Your order {order.number} has been placed.")
    return order


def _claim_transition(order, to_status) -> bool:
    """Insert the claim row for a transition. True if this call won it, False if already claimed."""
    try:
        with transaction.atomic():
            OrderTransition.objects.create(order=order, to_status=to_status)
        return True
    except IntegrityError:
        return False


def _set_order_status(order, new_status) -> None:
    try:
        order.set_status(new_status)
    except Exception as e:  # Oscar raises on an invalid pipeline move; the claim already gated us.
        log.warning("Could not set order %s status to %s: %s", order.number, new_status, e)


def dispatch_order(order, actor):
    """Operator marks the order dispatched: tell the shopper, and queue the delivery follow-up."""
    if not _claim_transition(order, OrderTransition.DISPATCHED):
        return False  # already dispatched -- no second message, no second follow-up
    _set_order_status(order, "Being processed")
    shopper = order.user or actor
    _record_send(order, shopper, OrderNotification.KIND_DISPATCHED,
                 f"Good news - your order {order.number} is on its way!")
    _record_send(order, shopper, OrderNotification.KIND_DISPATCHED_FOLLOWUP,
                 f"How did the delivery of your order {order.number} go? We'd love your feedback.",
                 followup=True)
    return True


def cancel_order(order, actor):
    """Operator cancels the order: tell the shopper, and call off any follow-up not yet sent."""
    if not _claim_transition(order, OrderTransition.CANCELLED):
        return False  # already cancelled -- no second message
    _set_order_status(order, "Cancelled")
    _call_off_followups(order)
    shopper = order.user or actor
    _record_send(order, shopper, OrderNotification.KIND_CANCELLED,
                 f"Your order {order.number} has been cancelled.")
    return True


def _call_off_followups(order) -> None:
    """Cancel every still-pending scheduled follow-up for this order at the provider."""
    pending = OrderNotification.objects.filter(
        order=order,
        is_followup=True,
        canceled=False,
    ).exclude(provider_sid="")
    for row in pending:
        if row.provider_status and row.provider_status not in FOLLOWUP_PENDING_STATUSES:
            continue  # already sent/delivered/failed -- nothing to call off
        try:
            result = tw.cancel_scheduled(row.provider_sid)
            row.provider_status = result.provider_status or row.provider_status
            row.outcome = result.outcome
            row.canceled = True
            row.save()
            log.info("Called off follow-up for order %s", order.number)
        except tw.TwilioGatewayError as e:
            log.warning("Could not call off follow-up for order %s (%s)", order.number, e.outcome)


# --- flow 3: resend / redact / reconcile -----------------------------------------------------------

def resend(original: OrderNotification, idempotency_key: str, actor) -> OrderNotification:
    """Re-send a message that did not reach the shopper, guarded by a caller idempotency key.

    Claim-first: the unique idempotency_key is the claim. A repeat under the same key returns the
    row the first request produced (no second message); a fresh key is a legitimate new attempt.
    """
    to = original.to_number or recipient_number(original.order)
    if not to:
        raise OrderError("No number on file to resend to", status_code=409)

    try:
        with transaction.atomic():
            row = OrderNotification.objects.create(
                order=original.order,
                user=original.user,
                kind=OrderNotification.KIND_RESEND,
                to_number=to,
                body=original.body,
                outcome=OrderNotification.OUTCOME_SENDING,
                idempotency_key=idempotency_key,
            )
    except IntegrityError:
        # Same key already used: return the message that first request produced. No second send.
        existing = OrderNotification.objects.get(idempotency_key=idempotency_key)
        return existing

    try:
        result = tw.send_immediate(to, row.body or f"An update about your order {original.order.number}.")
        _apply_result(row, result)
    except tw.TwilioGatewayError as e:
        row.outcome = e.outcome
        log.warning("Resend for order %s could not be sent (%s)", original.order.number, e.outcome)
    row.save()
    return row


def dispose_content(row: OrderNotification) -> None:
    """Dispose of a message's text at the provider; the fact it was sent and its outcome survive."""
    if row.provider_sid:
        tw.redact_content(row.provider_sid)  # raises TwilioGatewayError on failure -> surfaced
    row.content_redacted = True
    row.body = ""
    row.save(update_fields=["content_redacted", "body", "updated_at"])


def refresh_status(row: OrderNotification) -> OrderNotification:
    """Refresh a non-terminal row's delivery outcome from the provider (best effort)."""
    if not row.provider_sid or row.outcome in (OrderNotification.OUTCOME_DONE,):
        return row
    try:
        result = tw.fetch_status(row.provider_sid)
    except tw.TwilioGatewayError:
        return row
    row.provider_status = result.provider_status or row.provider_status
    row.outcome = result.outcome
    row.error_code = result.error_code
    if result.date_sent:
        row.provider_time = result.date_sent
    row.save(update_fields=["provider_status", "outcome", "error_code", "provider_time", "updated_at"])
    return row


def reconcile(from_dt, to_dt):
    """Line up the provider's record of our number's messages against what this app believes it sent.

    Both sides are filtered on the provider's own clock (date_sent), never on created_at. The
    provider filter is whole-day, so we widen the request and narrow to the exact window in code.
    """
    from datetime import datetime, time, timezone as _tz
    lo = from_dt.astimezone(_tz.utc)
    hi = to_dt.astimezone(_tz.utc)
    # The SDK types DateSent</> as RFC3339 datetimes; the provider filters at day granularity, so
    # widen to whole-day boundaries and narrow back to the caller's exact instants below.
    day_from = datetime.combine(lo.date(), time.min, tzinfo=_tz.utc)
    day_to = datetime.combine((hi + timedelta(days=1)).date(), time.min, tzinfo=_tz.utc)
    provider_msgs, truncated = tw.list_from_number(day_from, day_to)

    # Narrow the widened day window back to the caller's exact instants.
    in_window = [m for m in provider_msgs if m.date_sent and from_dt <= m.date_sent < to_dt]
    provider_by_sid = {m.sid: m for m in in_window if m.sid}

    local_rows = list(
        OrderNotification.objects.filter(
            provider_time__gte=from_dt, provider_time__lt=to_dt
        ).exclude(provider_sid="")
    )
    unsettled = list(
        OrderNotification.objects.filter(
            provider_time__isnull=True, created_at__gte=from_dt, created_at__lt=to_dt
        )
    )

    local_by_sid = {r.provider_sid: r for r in local_rows}
    matched, local_only = [], []
    remaining = dict(provider_by_sid)
    for sid, row in local_by_sid.items():
        if sid in remaining:
            matched.append((row, remaining.pop(sid)))
        else:
            local_only.append(row)
    provider_only = list(remaining.values())

    return {
        "matched": matched,
        "local_only": local_only,
        "provider_only": provider_only,
        "unsettled": unsettled,
        "truncated": truncated,
    }
