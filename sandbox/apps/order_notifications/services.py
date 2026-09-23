"""
Order-notification workflows: contact numbers, order placement and status
changes, the messages they trigger, and the operator actions on those messages.

Views call these; only ``provider`` talks to Twilio. Messaging failures are
recorded on the Notification row and never propagate out of an order action.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import provider
from .models import ContactNumber, Notification, OrderTransition
from .provider import MessageState, ProviderError, ProviderRejected
from .twilio_client import TwilioNotConfigured

logger = logging.getLogger("apps.order_notifications")

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
SurchargeApplicator = get_class("checkout.applicator", "SurchargeApplicator")
Selector = get_class("partner.strategy", "Selector")
Free = get_class("shipping.methods", "Free")
InvalidOrderStatus = get_class("order.exceptions", "InvalidOrderStatus")

STATUS_DISPATCHED = "Dispatched"
STATUS_CANCELLED = "Cancelled"

MAX_REFRESH_PER_REQUEST = 20


class ServiceError(Exception):
    """A request we refuse, with the HTTP status and a caller-safe message."""

    def __init__(self, http_status, message, *, code=None, extra=None):
        super().__init__(message)
        self.http_status = http_status
        self.message = message
        self.code = code
        self.extra = extra or {}


# --------------------------------------------------------------------------
# Contact numbers
# --------------------------------------------------------------------------

def register_contact_number(user, raw_number):
    raw_number = (raw_number or "").strip()
    if not raw_number or len(raw_number) > 32:
        raise ServiceError(400, "phoneNumber is required.", code="invalid_request")
    try:
        result = provider.lookup_number(raw_number)
    except ProviderRejected as e:
        if e.provider_status == 404:
            raise ServiceError(422, "The messaging provider does not recognise this as a usable phone number.",
                               code="number_not_usable") from e
        raise ServiceError(422, "The messaging provider rejected this phone number.",
                           code="number_not_usable") from e
    except ProviderError as e:
        raise ServiceError(e.http_status, "Could not validate the number with the messaging provider.",
                           code="provider_unavailable") from e
    except TwilioNotConfigured as e:
        raise ServiceError(503, "Messaging is not configured.", code="not_configured") from e
    try:
        with transaction.atomic():
            number = ContactNumber.objects.create(
                user=user, e164=result.e164, country_code=result.country_code or "")
        created = True
    except IntegrityError:
        number = ContactNumber.objects.get(user=user, e164=result.e164)
        created = False
    logger.info("contact number %s registered for user %s (created=%s)", number.pk, user.pk, created)
    return number, created


def current_contact_number(user):
    if user is None:
        return None
    return ContactNumber.objects.filter(user=user).order_by("-created_at", "-id").first()


def delete_contact_number(number):
    """Delete a number only once nothing queued with the provider can still reach it."""
    outcomes = cancel_scheduled_notifications(
        Notification.objects.filter(contact_number=number, status__in=_CANCELLABLE))
    blocking = [o for o in outcomes if not o.settled]
    if blocking:
        raise ServiceError(
            502, "A queued message to this number could not be called off; the number was kept. Retry later.",
            code="cancel_unconfirmed", extra={"unresolved": [o.as_dict() for o in blocking]})
    pk = number.pk
    number.delete()
    logger.info("contact number %s deleted", pk)


# --------------------------------------------------------------------------
# Sending (the durable claim)
# --------------------------------------------------------------------------

def _apply_state(notification, state: MessageState):
    notification.provider_sid = state.sid
    notification.provider_status = state.provider_status
    notification.status = state.outcome
    notification.error_code = state.error_code
    notification.error_detail = (state.error_message or "")[:255]
    if state.date_sent is not None:
        notification.provider_date_sent = state.date_sent
    notification.last_checked_at = timezone.now()


def _save(notification):
    with transaction.atomic():
        notification.save()


def _message_body(kind, order):
    shop = getattr(settings, "OSCAR_SHOP_NAME", "Oscar")
    return {
        Notification.KIND_PLACED: f"{shop}: thanks! Your order {order.number} has been placed.",
        Notification.KIND_DISPATCHED: f"{shop}: good news, your order {order.number} is on its way.",
        Notification.KIND_FOLLOWUP: (f"{shop}: how did the delivery of order {order.number} go? "
                                     "Reply to let us know."),
        Notification.KIND_CANCELLED: f"{shop}: your order {order.number} has been cancelled.",
    }[kind]


def notify(order, kind, *, send_at=None, contact=None, resend_of=None, idempotency_key=None):
    """Claim, send, settle. Returns (notification, claimed_now). Never raises
    for a messaging failure: the outcome is written on the row instead.
    Returns (None, False) when the shopper has no number on file."""
    if contact is None:
        contact = current_contact_number(order.user)
        if contact is None:
            return None, False
    body = resend_of.body if resend_of is not None else _message_body(kind, order)

    # 1. Claim first: the unique constraints decide the winner.
    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                order=order, kind=kind, contact_number=contact, body=body, resend_of=resend_of,
                idempotency_key=idempotency_key, status=Notification.SENDING, scheduled_for=send_at)
    except IntegrityError:
        if idempotency_key is not None:
            return Notification.objects.get(idempotency_key=idempotency_key), False
        return Notification.objects.get(order=order, kind=kind, resend_of__isnull=True), False

    # 2. Only now call the provider.
    try:
        state = provider.send_sms(contact.e164, body, send_at=send_at)
    except ProviderError as e:
        notification.error_code = e.provider_code
        notification.error_detail = e.message[:255]
        if e.outcome_unknown:
            notification.status = provider.UNKNOWN
            _save(notification)
            _try_resolve_unknown(notification, contact)
        else:
            notification.status = Notification.NOT_SENT
            _save(notification)
        logger.warning("notification %s (%s, order %s) not confirmed: %s",
                       notification.pk, kind, order.pk, notification.status)
        return notification, True
    except TwilioNotConfigured:
        notification.status = Notification.NOT_SENT
        notification.error_detail = "Messaging is not configured."
        _save(notification)
        logger.error("notification %s not sent: Twilio settings missing", notification.pk)
        return notification, True
    except Exception:
        # A defect of ours after the claim: the provider may or may not have been called.
        notification.status = provider.UNKNOWN
        notification.error_detail = "Internal error while sending."
        _save(notification)
        logger.exception("notification %s: unexpected error while sending", notification.pk)
        return notification, True

    # 3. Settle from what the provider said.
    _apply_state(notification, state)
    _save(notification)
    logger.info("notification %s (%s, order %s) -> %s", notification.pk, kind, order.pk, notification.status)
    return notification, True


def _try_resolve_unknown(notification, contact):
    """After a send whose outcome is unknown, look for the message by what we
    sent. Adopt it only if exactly one unclaimed candidate matches. A miss
    leaves it unknown: an empty lookup cannot prove nothing landed."""
    if notification.scheduled_for is not None:
        return  # scheduled messages carry no send date, so the list cannot find them yet
    window_start = notification.created_at - timedelta(minutes=1)
    try:
        page = provider.list_sent_from(settings.TWILIO_FROM_NUMBER, window_start,
                                       timezone.now() + timedelta(minutes=1), max_pages=3)
    except (ProviderError, TwilioNotConfigured):
        return
    known = set(Notification.objects.exclude(provider_sid=None).values_list("provider_sid", flat=True))
    candidates = [m for m in page.messages
                  if m.is_outbound and m.to == contact.e164 and m.body == notification.body and m.sid not in known]
    if len(candidates) == 1:
        _apply_state(notification, candidates[0])
        try:
            _save(notification)
        except IntegrityError:
            pass


# --------------------------------------------------------------------------
# Cancelling queued (scheduled) messages
# --------------------------------------------------------------------------

_CANCELLABLE = (provider.SCHEDULED, Notification.SENDING, provider.UNKNOWN, provider.PENDING)


@dataclass
class CancelOutcome:
    notification: Notification
    settled: bool          # True when we know it will not go out, or know it already did
    detail: str = ""
    already_sent: bool = False

    def as_dict(self):
        return {"notificationId": self.notification.pk, "status": self.notification.status,
                "settled": self.settled, "alreadySent": self.already_sent, "detail": self.detail}


def cancel_scheduled_notifications(queryset):
    outcomes = []
    for notification in queryset.select_related("contact_number"):
        outcomes.append(_cancel_one(notification))
    return outcomes


def _cancel_one(notification):
    if notification.scheduled_for is None and notification.status != provider.SCHEDULED:
        # An immediate message: nothing queued to call off.
        return CancelOutcome(notification, settled=True, detail="not a queued message")
    if not notification.provider_sid:
        if notification.status == Notification.SENDING:
            # Another request is creating it right now; that request re-checks and cancels.
            return CancelOutcome(notification, settled=False, detail="creation in flight")
        return CancelOutcome(notification, settled=False, detail="no provider id recorded")
    try:
        state = provider.cancel_scheduled(notification.provider_sid)
    except ProviderRejected:
        # Refused: typically because it is no longer scheduled. Ask what it is now.
        state = _fetch_or_none(notification)
        if state is None:
            return CancelOutcome(notification, settled=False, detail="cancel refused; state unreadable")
    except ProviderError:
        state = _fetch_or_none(notification)
        if state is None:
            return CancelOutcome(notification, settled=False, detail="cancel not confirmed")
    _apply_state(notification, state)
    _save(notification)
    if notification.status == provider.CANCELED:
        return CancelOutcome(notification, settled=True, detail="canceled at provider")
    if notification.status == provider.SCHEDULED:
        return CancelOutcome(notification, settled=False, detail="still scheduled at provider")
    already = notification.status in (provider.SENT, provider.DELIVERED, provider.PENDING, provider.FAILED)
    return CancelOutcome(notification, settled=already, already_sent=already,
                         detail=f"provider status {notification.provider_status}")


def _fetch_or_none(notification):
    try:
        return provider.fetch_message(notification.provider_sid)
    except (ProviderError, TwilioNotConfigured):
        return None


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

def place_order(request, user, items):
    """Place an order from (product, quantity) pairs through Oscar's own
    basket/strategy/OrderCreator machinery, then tell the shopper."""
    basket = Basket.objects.create(owner=user)
    basket.strategy = Selector().strategy(request=request, user=user)
    for product, quantity in items:
        info = basket.strategy.fetch_for_product(product)
        permitted, reason = info.availability.is_purchase_permitted(quantity)
        if not permitted or info.price is None or not info.price.exists:
            raise ServiceError(409, f"Product {product.pk} cannot be purchased: {reason or 'unavailable'}",
                               code="not_purchasable", extra={"productId": product.pk})
        basket.add_product(product, quantity)
    shipping_method = Free()
    shipping_charge = shipping_method.calculate(basket)
    surcharges = SurchargeApplicator(request).get_applicable_surcharges(basket)
    total = OrderTotalCalculator(request).calculate(basket, shipping_charge, surcharges)
    order = OrderCreator().place_order(
        basket=basket, total=total, shipping_method=shipping_method, shipping_charge=shipping_charge,
        user=user, request=request, surcharges=surcharges)
    basket.set_as_submitted()
    notification, _ = notify(order, Notification.KIND_PLACED)
    return order, notification


def _claim_transition(order, to_status, user):
    """Insert the claim row and apply Oscar's own status change atomically.
    Returns True for the one request that performed the change."""
    try:
        with transaction.atomic():
            OrderTransition.objects.create(order=order, to_status=to_status, performed_by=user)
            locked = Order.objects.get(pk=order.pk)
            locked.set_status(to_status)
    except IntegrityError:
        return False
    except InvalidOrderStatus as e:
        raise ServiceError(409, f"Order {order.number} cannot move from '{order.status}' to '{to_status}'.",
                           code="invalid_transition") from e
    order.refresh_from_db()
    return True


@dataclass
class TransitionResult:
    order: object
    changed: bool
    notifications: list = field(default_factory=list)
    followup_cancellations: list = field(default_factory=list)


def dispatch_order(order, user):
    if not _claim_transition(order, STATUS_DISPATCHED, user):
        return TransitionResult(order, changed=False)
    result = TransitionResult(order, changed=True)
    on_its_way, _ = notify(order, Notification.KIND_DISPATCHED)
    delay = timedelta(seconds=int(settings.TWILIO_FOLLOWUP_DELAY_SECONDS))
    followup, _ = notify(order, Notification.KIND_FOLLOWUP, send_at=timezone.now() + delay)
    result.notifications = [n for n in (on_its_way, followup) if n is not None]
    if followup is not None:
        # A cancel may have committed while we were scheduling: it cannot see a
        # message we had not recorded yet, so we re-check and call it off ourselves.
        order.refresh_from_db()
        if order.status == STATUS_CANCELLED:
            result.followup_cancellations = [o.as_dict() for o in cancel_scheduled_notifications(
                Notification.objects.filter(pk=followup.pk))]
            followup.refresh_from_db()
    return result


def cancel_order(order, user):
    if not _claim_transition(order, STATUS_CANCELLED, user):
        return TransitionResult(order, changed=False)
    result = TransitionResult(order, changed=True)
    outcomes = cancel_scheduled_notifications(
        Notification.objects.filter(order=order, kind=Notification.KIND_FOLLOWUP, status__in=_CANCELLABLE))
    result.followup_cancellations = [o.as_dict() for o in outcomes]
    for o in outcomes:
        if not o.settled:
            logger.error("order %s cancelled but follow-up notification %s not confirmed cancelled: %s",
                         order.pk, o.notification.pk, o.detail)
    cancelled_msg, _ = notify(order, Notification.KIND_CANCELLED)
    if cancelled_msg is not None:
        result.notifications.append(cancelled_msg)
    return result


# --------------------------------------------------------------------------
# Reading state back from the provider
# --------------------------------------------------------------------------

def refresh_notifications(notifications):
    """Ask the provider about messages whose outcome is not final yet.
    Returns the set of notification ids whose refresh failed (stale)."""
    stale = set()
    budget = MAX_REFRESH_PER_REQUEST
    for n in notifications:
        if n.is_terminal or not n.provider_sid or n.content_disposed_at:
            continue
        if budget <= 0:
            stale.add(n.pk)
            continue
        budget -= 1
        state = _fetch_or_none(n)
        if state is None:
            stale.add(n.pk)
            continue
        _apply_state(n, state)
        _save(n)
    return stale


# --------------------------------------------------------------------------
# Operator actions
# --------------------------------------------------------------------------

RESENDABLE = (provider.FAILED, Notification.NOT_SENT)


def resend(source, idempotency_key):
    """Re-send a message that did not reach the shopper. The idempotency key
    is the claim: a repeat under the same key returns the first attempt."""
    existing = Notification.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.resend_of_id != source.pk:
            raise ServiceError(409, "This idempotency key was already used for a different message.",
                               code="idempotency_key_reused")
        return existing, False
    refresh_notifications([source])
    if source.status not in RESENDABLE:
        raise ServiceError(409, f"Only messages that did not reach the shopper can be re-sent "
                                f"(this one is '{source.status}').", code="not_resendable")
    if source.content_disposed_at:
        raise ServiceError(409, "This message's content was disposed of.", code="content_disposed")
    if source.kind == Notification.KIND_FOLLOWUP and source.order.status == STATUS_CANCELLED:
        raise ServiceError(409, "The order was cancelled; its delivery follow-up must not be sent.",
                           code="order_cancelled")
    contact = source.contact_number
    if contact is None or contact.user_id != source.order.user_id:
        raise ServiceError(409, "The shopper's number for this message is no longer on file.",
                           code="number_removed")
    notification, created = notify(source.order, source.kind, contact=contact, resend_of=source,
                                   idempotency_key=idempotency_key)
    if not created and notification.resend_of_id != source.pk:
        raise ServiceError(409, "This idempotency key was already used for a different message.",
                           code="idempotency_key_reused")
    return notification, created


DISPOSABLE = (provider.DELIVERED, provider.SENT, provider.FAILED, provider.CANCELED, Notification.NOT_SENT)


def dispose_content(notification):
    """Remove the message text at the provider (redaction) and here, keeping
    the record that it was sent and what became of it."""
    if notification.content_disposed_at:
        return notification
    refresh_notifications([notification])
    if notification.status not in DISPOSABLE:
        raise ServiceError(409, f"The message is '{notification.status}'; its content can be disposed of "
                                "once it has finished (or after it is cancelled).", code="not_final")
    if notification.provider_sid:
        try:
            state = provider.redact_body(notification.provider_sid)
        except ProviderRejected as e:
            raise ServiceError(409, "The messaging provider refused to redact this message.",
                               code="redaction_refused", extra={"providerCode": e.provider_code}) from e
        except ProviderError as e:
            raise ServiceError(e.http_status, "Could not reach the messaging provider to redact the message.",
                               code="provider_unavailable") from e
        if state.body:
            # Trust but verify: read it back once before declaring it gone.
            state = _fetch_or_none(notification) or state
            if state.body:
                raise ServiceError(502, "The messaging provider still returns the message text.",
                                   code="redaction_unconfirmed")
        # Keep the delivery outcome the provider reports; the text is what goes.
        _apply_state(notification, state)
    notification.body = ""
    notification.content_disposed_at = timezone.now()
    _save(notification)
    logger.info("notification %s content disposed", notification.pk)
    return notification


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def reconcile(start: datetime, end: datetime):
    """Line up the provider's record of messages sent from our number in
    [start, end) against ours, on the provider's own send-time clock."""
    from_number = settings.TWILIO_FROM_NUMBER
    # The provider's DateSent filter may be day-granular: ask a day wider, narrow in code.
    page = provider.list_sent_from(from_number, start - timedelta(days=1), end + timedelta(days=1))
    in_window = [m for m in page.messages if m.date_sent is not None and start <= m.date_sent < end]
    provider_msgs = [m for m in in_window if m.is_outbound]
    excluded_inbound = len(in_window) - len(provider_msgs)
    by_sid = {m.sid: m for m in provider_msgs}

    max_deferral = timedelta(seconds=int(settings.TWILIO_FOLLOWUP_DELAY_SECONDS)) + timedelta(days=1)
    local_rows = list(
        Notification.objects.filter(provider_date_sent__gte=start, provider_date_sent__lt=end)
        | Notification.objects.filter(provider_date_sent__isnull=True,
                                      created_at__gte=start - max_deferral, created_at__lt=end)
    )

    matched, local_only, unsettled = [], [], []
    for row in local_rows:
        state = by_sid.pop(row.provider_sid, None) if row.provider_sid else None
        if state is not None:
            changed = row.provider_date_sent != state.date_sent or row.status != state.outcome
            if changed and not row.content_disposed_at:
                _apply_state(row, state)
                _save(row)
            matched.append(_recon_local(row, state))
        elif row.provider_date_sent is not None and start <= row.provider_date_sent < end:
            local_only.append(_recon_local(row, None))  # we recorded a send in the window; provider has none
        else:
            # Never sent in the window as far as we know: scheduled, canceled,
            # not_sent, unknown, or still in flight. Not a discrepancy by itself.
            unsettled.append(_recon_local(row, None))
    provider_only = [_recon_provider(m) for m in by_sid.values()]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "sendingNumber": "***" + from_number[-4:],
        "complete": not page.truncated,
        "truncated": page.truncated,
        "providerPagesRead": page.pages,
        "counts": {
            "provider": len(provider_msgs),
            "matched": len(matched),
            "providerOnly": len(provider_only),
            "localOnly": len(local_only),
            "unsettledLocal": len(unsettled),
            "excludedInboundLegs": excluded_inbound,
        },
        "matched": matched,
        "providerOnly": provider_only,
        "localOnly": local_only,
        "unsettledLocal": unsettled,
    }


def _recon_local(row, state):
    data = {
        "notificationId": row.pk,
        "orderId": row.order_id,
        "kind": row.kind,
        "localStatus": row.status,
        "providerSid": row.provider_sid,
        "providerDateSent": row.provider_date_sent.isoformat() if row.provider_date_sent else None,
        "createdAt": row.created_at.isoformat(),
    }
    if state is not None:
        data["providerStatus"] = state.provider_status
    return data


def _recon_provider(m: MessageState):
    return {
        "providerSid": m.sid,
        "providerStatus": m.provider_status,
        "outcome": m.outcome,
        "dateSent": m.date_sent.isoformat() if m.date_sent else None,
        "errorCode": m.error_code,
    }
