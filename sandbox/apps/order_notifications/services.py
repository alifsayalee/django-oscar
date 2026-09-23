"""
Order notification workflows.

Rules every function here keeps:

* A local row is committed *before* the provider is asked to do anything, so
  every provider effect has something pointing at it.
* Messaging never fails the order operation it accompanies: ``notify`` swallows
  (and logs) every failure and records it on the notification instead.
* Phone numbers are never logged.
"""

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.apps.order.exceptions import InvalidOrderStatus
from oscar.core.loading import get_class, get_model

from . import status as st
from . import twilio_gateway as gateway
from .models import ContactNumber, Notification, OrderTransitionClaim, ResendRequest, new_ref

logger = logging.getLogger("order_notifications")

Order = get_model("order", "Order")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Selector = get_class("partner.strategy", "Selector")
Applicator = get_class("offer.applicator", "Applicator")
Repository = get_class("shipping.repository", "Repository")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
EventHandler = get_class("order.processing", "EventHandler")

DISPATCHED_STATUS = "Dispatched"
CANCELLED_STATUS = "Cancelled"

#: A send still marked "sending" after this long lost its request; treat the
#: outcome as unknown and look it up rather than wait for it.
STALE_SENDING = timedelta(seconds=gateway.REQUEST_TIMEOUT * 3)

CANCEL_ATTEMPTS = 3


class ServiceError(Exception):
    """A request we refuse, with the HTTP status our API answers."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def follow_up_delay():
    return timedelta(hours=settings.ORDER_NOTIFICATIONS_FOLLOWUP_DELAY_HOURS)


# ---------------------------------------------------------------------------
# Contact numbers
# ---------------------------------------------------------------------------


def active_numbers(user):
    return ContactNumber.objects.filter(user=user, removed_at__isnull=True)


def primary_number(user):
    """The number a shopper is messaged at: the most recently registered one."""
    if user is None:
        return None
    return active_numbers(user).order_by("-created_at", "-id").first()


def register_number(user, raw_number, country_code=None):
    """Validate with the provider and store its canonical form. Returns (number, created)."""
    canonical = gateway.lookup_number(raw_number, country_code)
    try:
        with transaction.atomic():
            number = ContactNumber.objects.create(
                user=user,
                phone_number=canonical.phone_number,
                country_code=(canonical.country_code or "")[:2],
            )
    except IntegrityError:
        # Already on file (possibly registered concurrently): answer with it.
        return active_numbers(user).get(phone_number=canonical.phone_number), False
    logger.info("contact number %s registered for user %s", number.pk, user.pk)
    return number, True


def remove_number(number):
    """Stop using ``number``: unlisted, and anything still queued to it is called off."""
    with transaction.atomic():
        changed = ContactNumber.objects.filter(pk=number.pk, removed_at__isnull=True).update(
            removed_at=timezone.now()
        )
        pending = list(
            Notification.objects.filter(contact_number=number, cancel_requested_at__isnull=True)
            .exclude(outcome__in=st.TERMINAL)
            .filter(kind=Notification.FOLLOW_UP)
        )
        Notification.objects.filter(pk__in=[n.pk for n in pending]).update(
            cancel_requested_at=timezone.now()
        )
    for notification in pending:
        notification.refresh_from_db()
        _cancel_follow_up(notification)
    logger.info("contact number %s removed (changed=%s)", number.pk, bool(changed))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _settle(notification, message):
    """Record the provider's view of a message on our row."""
    notification.provider_sid = message.sid
    notification.provider_status = message.status or ""
    notification.outcome = st.outcome_from_provider(message.status)
    notification.error_code = message.error_code
    notification.error_message = (message.error_message or "")[:255]
    if message.date_created is not None:
        notification.provider_created_at = message.date_created
    if message.date_sent is not None:
        notification.provider_sent_at = message.date_sent
    notification.last_checked_at = timezone.now()
    notification.save()


def _mark(notification, outcome, error_message=""):
    notification.outcome = outcome
    if error_message:
        notification.error_message = error_message[:255]
    notification.last_checked_at = timezone.now()
    notification.save()


def _look_up_by_ref(notification):
    """Resolve a send whose outcome is unknown. Leaves it UNKNOWN when nothing is found."""
    to = notification.contact_number.phone_number if notification.contact_number else None
    if not to:
        _mark(notification, st.UNKNOWN)
        return notification
    try:
        found = gateway.find_by_ref(to, notification.ref, notification.created_at)
    except gateway.ProviderError:
        found = None
    if found is not None:
        _settle(notification, found)
    else:
        # Not found *yet* is not "never happened": it stays unknown.
        _mark(notification, st.UNKNOWN)
    return notification


def _send(notification, send_at=None):
    """Hand a committed SENDING row to the provider and record what happened."""
    to = notification.contact_number.phone_number
    try:
        if send_at is not None:
            message = gateway.schedule_message(to, notification.body, send_at)
        else:
            message = gateway.send_message(to, notification.body)
    except gateway.ProviderError as e:
        if e.outcome_unknown:
            logger.warning("notification %s: send outcome unknown; looking it up", notification.pk)
            return _look_up_by_ref(notification)
        logger.warning("notification %s: not sent (%s)", notification.pk, e.message)
        _mark(notification, st.FAILED, e.message)
        return notification
    _settle(notification, message)
    logger.info(
        "notification %s (%s) -> %s [%s]",
        notification.pk,
        notification.kind,
        notification.outcome,
        notification.provider_sid,
    )
    return notification


def _body(text, ref):
    return "%s Ref %s" % (text, ref)


def _text_of(body):
    return body.rsplit(" Ref ", 1)[0]


def notify(order, kind, text, send_at=None):
    """Message the order's shopper. Never raises; returns the notification or None."""
    try:
        contact = primary_number(order.user)
        if contact is None:
            return None
        ref = new_ref()
        with transaction.atomic():
            notification = Notification.objects.create(
                order=order,
                user=order.user,
                contact_number=contact,
                kind=kind,
                ref=ref,
                body=_body(text, ref),
                scheduled_for=send_at,
            )
        return _send(notification, send_at)
    except Exception:
        logger.exception("order %s: %s notification could not be sent", order.pk, kind)
        return None


# ---------------------------------------------------------------------------
# Refreshing provider state
# ---------------------------------------------------------------------------


def refresh(notification):
    """Bring a non-final notification up to date with the provider. Never raises."""
    try:
        if notification.cancel_requested_at and not notification.is_terminal:
            return _cancel_follow_up(notification)
        if notification.is_terminal:
            return notification
        if not notification.provider_sid:
            if notification.outcome == st.UNKNOWN or (
                notification.outcome == st.SENDING
                and notification.created_at < timezone.now() - STALE_SENDING
            ):
                return _look_up_by_ref(notification)
            return notification
        message = gateway.fetch_message(notification.provider_sid)
        if message is None:
            _mark(notification, st.UNKNOWN, "Provider has no record of this message.")
        else:
            _settle(notification, message)
    except gateway.ProviderError as e:
        logger.warning("notification %s: refresh failed (%s)", notification.pk, e.message)
    except Exception:
        logger.exception("notification %s: refresh failed", notification.pk)
    return notification


def refresh_all(notifications, limit=25):
    """Refresh the non-final ones among ``notifications`` (bounded)."""
    pending = [n for n in notifications if not n.is_terminal]
    for notification in pending[:limit]:
        refresh(notification)
    return notifications


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def place_order(user, items, request=None):
    """Place an order for ``items`` [(product_id, quantity)] with Oscar's own machinery."""
    if not items:
        raise ServiceError(400, "An order needs at least one line.")
    strategy = Selector().strategy(request=request, user=user)
    with transaction.atomic():
        # A SAVED basket is never merged into the shopper's open web basket.
        basket = Basket.objects.create(owner=user, status=Basket.SAVED)
        basket.strategy = strategy
        for product_id, quantity in items:
            product = Product.objects.filter(pk=product_id).first()
            if product is None or not product.is_public:
                raise ServiceError(400, "Unknown catalogue item %s." % product_id)
            if product.is_parent:
                raise ServiceError(400, "Catalogue item %s cannot be bought directly." % product_id)
            info = strategy.fetch_for_product(product)
            if not info.price.exists:
                raise ServiceError(400, "Catalogue item %s has no price." % product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ServiceError(409, "Catalogue item %s: %s" % (product_id, reason))
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, request)
        shipping_method = Repository().get_default_shipping_method(
            basket=basket, user=user, request=request
        )
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            order_number=OrderNumberGenerator().order_number(basket),
            request=request,
        )
        basket.submit()
    logger.info("order %s placed by user %s via API", order.number, user.pk)
    notify(
        order,
        Notification.PLACED,
        "Thanks! Your order %s has been placed (total %s %s)."
        % (order.number, order.total_incl_tax, order.currency),
    )
    return order


def _claim_transition(order, to_status, user, extra=None):
    """Perform Oscar's own status change, once. Returns True only for the request that did."""
    try:
        with transaction.atomic():
            OrderTransitionClaim.objects.create(order=order, to_status=to_status, performed_by=user)
            locked = Order.objects.get(pk=order.pk)
            EventHandler(user).handle_order_status_change(
                locked, to_status, note_msg="Order %s via API." % to_status.lower()
            )
            if extra is not None:
                extra(locked)
    except IntegrityError:
        return False
    except InvalidOrderStatus as e:
        raise ServiceError(409, str(e)) from e
    order.refresh_from_db()
    return True


def dispatch(order, operator):
    """Mark ``order`` dispatched; tell the shopper and queue the follow-up with the provider."""
    if not _claim_transition(order, DISPATCHED_STATUS, operator):
        return order, False
    notify(order, Notification.DISPATCHED, "Good news: your order %s is on its way." % order.number)
    follow_up = notify(
        order,
        Notification.FOLLOW_UP,
        "How did the delivery of your order %s go? Reply and let us know." % order.number,
        send_at=timezone.now() + follow_up_delay(),
    )
    if follow_up is not None:
        # The order may have been cancelled while the follow-up was being queued.
        # The follow-up's sid is written before these reads, and cancel() writes
        # its flag/status before reading sids, so one side always sees the other.
        follow_up.refresh_from_db()
        cancelled = Order.objects.filter(pk=order.pk, status=CANCELLED_STATUS).exists()
        if cancelled or follow_up.cancel_requested_at:
            if not follow_up.cancel_requested_at:
                follow_up.cancel_requested_at = timezone.now()
                follow_up.save(update_fields=["cancel_requested_at", "updated_at"])
            _cancel_follow_up(follow_up)
    return order, True


def _request_follow_up_cancellation(order):
    Notification.objects.filter(
        order=order, kind=Notification.FOLLOW_UP, cancel_requested_at__isnull=True
    ).update(cancel_requested_at=timezone.now())


def cancel(order, operator):
    """Cancel ``order``: call off any queued follow-up, then tell the shopper."""
    changed = _claim_transition(
        order, CANCELLED_STATUS, operator, extra=_request_follow_up_cancellation
    )
    if not changed:
        # Already cancelled. Still make sure nothing queued can go out.
        cancel_follow_ups(order)
        return order, False
    cancel_follow_ups(order)
    notify(order, Notification.CANCELLED, "Your order %s has been cancelled." % order.number)
    return order, True


def cancel_follow_ups(order):
    for notification in Notification.objects.filter(
        order=order, kind=Notification.FOLLOW_UP, cancel_requested_at__isnull=False
    ).exclude(outcome__in=st.TERMINAL):
        _cancel_follow_up(notification)


def _cancel_follow_up(notification):
    """Make sure a follow-up whose cancellation was requested never goes out."""
    if notification.is_terminal:
        return notification
    if not notification.provider_sid:
        if notification.outcome == st.SENDING and (
            notification.created_at >= timezone.now() - STALE_SENDING
        ):
            # Its send is still in flight; that request re-checks the flag
            # once the provider answers and cancels it then.
            return notification
        _look_up_by_ref(notification)
        if not notification.provider_sid:
            if notification.outcome == st.FAILED:
                return notification  # never reached the provider: nothing to call off
            logger.error(
                "follow-up %s: cancellation pending, provider message not found yet",
                notification.pk,
            )
            return notification
        if notification.is_terminal:
            return notification

    for attempt in range(CANCEL_ATTEMPTS):
        try:
            message = gateway.cancel_message(notification.provider_sid)
        except gateway.ProviderRejected as e:
            # Typically: it is no longer cancellable. Record what it actually is.
            logger.warning("follow-up %s: cancel refused (%s)", notification.pk, e.message)
            try:
                message = gateway.fetch_message(notification.provider_sid)
            except gateway.ProviderError:
                message = None
            if message is not None:
                _settle(notification, message)
            break
        except gateway.ProviderError as e:
            logger.warning(
                "follow-up %s: cancel attempt %s failed (%s)",
                notification.pk,
                attempt + 1,
                e.message,
            )
            if attempt + 1 < CANCEL_ATTEMPTS:
                time.sleep(0.5 * (2**attempt))
            continue
        _settle(notification, message)
        if notification.outcome != st.CANCELED:
            # The update answered without the cancelled state: ask again.
            refreshed = gateway.fetch_message(notification.provider_sid)
            if refreshed is not None:
                _settle(notification, refreshed)
        break
    if notification.outcome != st.CANCELED:
        logger.error(
            "follow-up %s: NOT cancelled (outcome %s); the sweep command will retry",
            notification.pk,
            notification.outcome,
        )
    return notification


# ---------------------------------------------------------------------------
# Operator actions
# ---------------------------------------------------------------------------


def resend(original, idempotency_key, operator):
    """Re-send a message that did not reach the shopper. Returns (notification, created)."""
    existing = ResendRequest.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.original_id != original.pk:
            raise ServiceError(422, "This idempotency key was used for a different notification.")
        return existing.result, False

    refresh(original)
    if original.outcome != st.FAILED:
        raise ServiceError(
            409, "Only a message that did not reach the shopper can be re-sent "
            "(this one is '%s')." % original.outcome
        )
    if original.content_disposed_at:
        raise ServiceError(409, "The content of this message has been disposed of.")
    contact = original.contact_number
    if contact is None or not contact.is_active:
        raise ServiceError(409, "The shopper's number has been removed.")
    order = original.order
    if order.status == CANCELLED_STATUS and original.kind in (
        Notification.DISPATCHED,
        Notification.FOLLOW_UP,
    ):
        raise ServiceError(409, "The order has been cancelled.")

    ref = new_ref()
    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                order=order,
                user=original.user,
                contact_number=contact,
                kind=original.kind,
                ref=ref,
                body=_body(_text_of(original.body), ref),
                resend_of=original,
            )
            ResendRequest.objects.create(
                idempotency_key=idempotency_key,
                original=original,
                result=notification,
                requested_by=operator,
            )
    except IntegrityError:
        # A concurrent request claimed the key first: answer from its row.
        existing = ResendRequest.objects.get(idempotency_key=idempotency_key)
        if existing.original_id != original.pk:
            raise ServiceError(422, "This idempotency key was used for a different notification.")
        return existing.result, False
    return _send(notification), True


def dispose_content(notification):
    """Erase a message's text here and at the provider; its delivery record survives."""
    if notification.provider_sid:
        try:
            message = gateway.redact_message(notification.provider_sid)
        except gateway.ProviderRejected as e:
            raise ServiceError(
                409, "The provider would not erase this message yet (%s)." % e.message
            ) from e
        if message.body:
            message = gateway.fetch_message(notification.provider_sid)
            if message is None or message.body:
                raise ServiceError(502, "The provider still holds the message text.")
        _settle(notification, message)
    elif notification.outcome in (st.SENDING, st.UNKNOWN):
        # It may exist at the provider under our ref; find it before claiming it is gone.
        _look_up_by_ref(notification)
        if notification.provider_sid:
            return dispose_content(notification)
        if notification.outcome != st.FAILED:
            raise ServiceError(
                409, "Cannot confirm whether the provider holds this message yet; retry later."
            )
    notification.body = ""
    notification.content_disposed_at = notification.content_disposed_at or timezone.now()
    notification.save()
    logger.info("notification %s: content disposed", notification.pk)
    return notification


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _provider_row(message):
    return {
        "providerSid": message.sid,
        "providerStatus": message.status,
        "to": gateway.mask_number(message.to),
        "dateCreated": message.date_created.isoformat() if message.date_created else None,
        "dateSent": message.date_sent.isoformat() if message.date_sent else None,
    }


def reconcile(start, end, app_only_lookups=50):
    """Line the provider's messages from our number in [start, end) up against ours.

    Clock: the provider's ``date_created`` on both sides (recorded on our rows
    as ``provider_created_at``). The provider can only filter on the send
    date, by whole days, so the query is widened — by a day before, and by a
    day plus the longest scheduling deferral after — and narrowed back here.
    """
    fetched = gateway.list_messages(
        sent_after=start - timedelta(days=1),
        sent_before=end + follow_up_delay() + timedelta(days=1),
    )
    provider = {
        m.sid: m
        for m in fetched.messages
        if m.date_created is not None and start <= m.date_created < end
    }

    local = list(
        Notification.objects.filter(
            provider_created_at__gte=start, provider_created_at__lt=end
        ).select_related("order")
    )
    local_sids = {n.provider_sid for n in local}
    unsettled = list(
        Notification.objects.filter(
            provider_sid__isnull=True, created_at__gte=start, created_at__lt=end
        ).select_related("order")
    )

    matched, app_only = [], []
    for notification in local:
        message = provider.get(notification.provider_sid)
        if message is None:
            app_only.append(notification)
            continue
        previous = notification.outcome
        _settle(notification, message)
        matched.append(
            dict(
                _notification_row(notification),
                provider=_provider_row(message),
                outcomeChangedFrom=previous if previous != notification.outcome else None,
            )
        )

    app_only_rows = []
    for index, notification in enumerate(app_only):
        row = _notification_row(notification)
        if index < app_only_lookups:
            try:
                message = gateway.fetch_message(notification.provider_sid)
            except gateway.ProviderError:
                row["providerLookup"] = "failed"
            else:
                if message is None:
                    row["providerLookup"] = "not_found"
                else:
                    # The provider has it, but its list query did not return it.
                    row["providerLookup"] = "found_outside_list"
                    row["provider"] = _provider_row(message)
        else:
            row["providerLookup"] = "skipped"
        app_only_rows.append(row)

    provider_only = [
        _provider_row(m) for sid, m in sorted(provider.items()) if sid not in local_sids
    ]
    unsettled_rows = [_notification_row(n) for n in unsettled]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "sendingNumber": gateway.mask_number(settings.TWILIO_FROM_NUMBER),
        "providerTruncated": fetched.truncated,
        "counts": {
            "provider": len(provider),
            "app": len(local),
            "matched": len(matched),
            "appOnly": len(app_only_rows),
            "providerOnly": len(provider_only),
            "unsettled": len(unsettled_rows),
        },
        "matched": matched,
        "appOnly": app_only_rows,
        "providerOnly": provider_only,
        "unsettled": unsettled_rows,
    }


def _notification_row(notification):
    return {
        "notificationId": notification.pk,
        "orderId": notification.order_id,
        "orderNumber": notification.order.number,
        "kind": notification.kind,
        "outcome": notification.outcome,
        "providerSid": notification.provider_sid,
        "providerStatus": notification.provider_status or None,
        "providerCreatedAt": _iso(notification.provider_created_at),
        "createdAt": _iso(notification.created_at),
    }


def _iso(value):
    return value.isoformat() if value else None
