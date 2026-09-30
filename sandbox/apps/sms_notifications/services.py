"""
Order notifications by SMS.

Every provider write follows one order: claim (a row or a conditional update
the database refuses to repeat) -> SDK call -> record the result. Sending
never raises into the order operation that triggered it.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import gateway
from .gateway import ProviderError
from .models import ContactNumber, Notification

logger = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
SurchargeApplicator = get_class("checkout.applicator", "SurchargeApplicator")
Free = get_class("shipping.methods", "Free")
Selector = get_class("partner.strategy", "Selector")
InvalidOrderStatus = get_class("order.exceptions", "InvalidOrderStatus")

STATUS_DISPATCHED = "Dispatched"
STATUS_CANCELLED = "Cancelled"

# A claim older than this whose request never finished is treated as abandoned.
STALE_CLAIM = timedelta(minutes=2)
# Allowance for clock skew when matching a lost create against the provider's list.
CREATE_MATCH_SKEW = timedelta(seconds=60)
# Bound on provider re-reads per API request.
MAX_REFRESH_PER_REQUEST = 50


class ServiceError(Exception):
    def __init__(self, status_code, message, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


def mask_number(number):
    if not number:
        return ""
    return number[:3] + "*" * max(len(number) - 5, 0) + number[-2:]


def _shop_name():
    return getattr(settings, "OSCAR_SHOP_NAME", "Oscar")


def _message_text(kind, order):
    shop = _shop_name()
    texts = {
        Notification.Kind.ORDER_PLACED:
            "%s: thanks for your order %s. We'll text you when it ships." % (shop, order.number),
        Notification.Kind.DISPATCHED:
            "%s: good news, order %s is on its way." % (shop, order.number),
        Notification.Kind.DELIVERY_FOLLOWUP:
            "%s: how did the delivery of order %s go? Reply to let us know." % (shop, order.number),
        Notification.Kind.CANCELLED:
            "%s: order %s has been cancelled. If you did not expect this, please contact us."
            % (shop, order.number),
    }
    return texts[kind]


# ---------------------------------------------------------------------------
# Contact numbers
# ---------------------------------------------------------------------------

def _primary_number(user):
    if user is None:
        return None
    return ContactNumber.objects.filter(user=user).first()


def register_contact_number(user, raw_number, country_code=None):
    raw_number = (raw_number or "").strip()
    allowed = set("+0123456789 ()-.")
    digits = [c for c in raw_number if c.isdigit()]
    if not raw_number or len(raw_number) > 32 or not set(raw_number) <= allowed or not 6 <= len(digits) <= 15:
        raise ServiceError(400, "phoneNumber must be a phone number.")
    if country_code is not None:
        country_code = str(country_code).strip().upper()
        if len(country_code) != 2 or not country_code.isalpha():
            raise ServiceError(400, "countryCode must be a two-letter ISO country code.")
    compact = "".join(c for c in raw_number if c == "+" or c.isdigit())
    try:
        result = gateway.lookup_number(compact, country_code)
    except ProviderError as e:
        if e.rejected:
            # The provider could not make sense of the number at all.
            raise ServiceError(422, "The number is not a usable mobile destination.") from e
        raise ServiceError(e.status_code, e.message) from e
    if not result.valid:
        raise ServiceError(422, "The number is not a usable mobile destination.")
    try:
        with transaction.atomic():
            contact = ContactNumber.objects.create(
                user=user,
                phone_number=result.phone_number,
                country_code=result.country_code or "",
                national_format=result.national_format or "",
            )
    except IntegrityError:
        existing = ContactNumber.objects.get(user=user, phone_number=result.phone_number)
        raise ServiceError(409, "This number is already registered.", contactNumberId=existing.pk)
    logger.info("contact number %s registered for user %s", contact.pk, user.pk)
    return contact


def delete_contact_number(user, contact_id):
    try:
        contact = ContactNumber.objects.get(pk=contact_id, user=user)
    except ContactNumber.DoesNotExist:
        raise ServiceError(404, "Contact number not found.")
    # Nothing may be sent to it again: drop unsent claims and call off queued messages.
    Notification.objects.filter(contact_number=contact, state=Notification.State.PENDING,
                                attempt_started_at__isnull=True).update(
        state=Notification.State.SKIPPED, failure_reason="Contact number removed before sending.")
    queued = list(Notification.objects.filter(contact_number=contact, provider_status="scheduled"))
    contact.delete()
    for notification in queued:
        _cancel_scheduled(notification)
    logger.info("contact number %s removed by user %s", contact_id, user.pk)


# ---------------------------------------------------------------------------
# Claims and sends
# ---------------------------------------------------------------------------

def _claim_notification(order, kind, **fields):
    """Insert the claim row for (order, kind). Raises IntegrityError if already claimed."""
    contact = _primary_number(order.user)
    values = dict(order=order, kind=kind, body=_message_text(kind, order), **fields)
    if contact is None:
        values.update(state=Notification.State.SKIPPED, failure_reason="No mobile number on file.")
    else:
        values.update(state=Notification.State.PENDING, contact_number=contact, to_number=contact.phone_number)
    with transaction.atomic():
        return Notification.objects.create(**values)


def _claim_attempt(notification):
    """Atomically take the right to call the provider for this claim (at most once)."""
    now = timezone.now()
    taken = Notification.objects.filter(
        pk=notification.pk, state=Notification.State.PENDING, attempt_started_at__isnull=True,
    ).update(attempt_started_at=now)
    if taken:
        notification.attempt_started_at = now
    return bool(taken)


def _apply_snapshot(notification, snapshot):
    notification.provider_sid = snapshot.sid
    if snapshot.status:
        notification.provider_status = snapshot.status
    notification.provider_error_code = snapshot.error_code
    if snapshot.date_created:
        notification.provider_date_created = snapshot.date_created
    if snapshot.date_sent:
        notification.provider_date_sent = snapshot.date_sent
    notification.provider_checked_at = timezone.now()
    notification.state = Notification.State.SUBMITTED


def _record_send_failure(notification, error):
    if error.outcome_unknown:
        notification.state = Notification.State.UNKNOWN
        notification.failure_reason = "Provider answer lost: %s" % error.message
    else:
        notification.state = Notification.State.SEND_FAILED
        notification.failure_reason = error.message
        if error.provider_code:
            notification.provider_error_code = error.provider_code


def _contact_still_registered(notification):
    return notification.contact_number_id is not None and ContactNumber.objects.filter(
        pk=notification.contact_number_id).exists()


def _submit(notification, send):
    """Claim the attempt, call the provider via ``send``, record the result. Never raises."""
    try:
        if not _claim_attempt(notification):
            return notification
        if not _contact_still_registered(notification):
            notification.state = Notification.State.SKIPPED
            notification.failure_reason = "Contact number removed before sending."
            notification.save()
            return notification
        try:
            snapshot = send()
        except ProviderError as e:
            _record_send_failure(notification, e)
            notification.save()
            if notification.state == Notification.State.UNKNOWN:
                _settle_unknown(notification)
        else:
            _apply_snapshot(notification, snapshot)
            notification.save()
        logger.info("notification %s (%s) -> state=%s provider_status=%s", notification.pk,
                    notification.kind, notification.state, notification.provider_status or "-")
    except Exception:
        # A bug after the provider may have been called: never fail the order,
        # and do not claim the message was not sent.
        logger.exception("notification %s: unexpected error while sending", notification.pk)
        Notification.objects.filter(pk=notification.pk, state=Notification.State.PENDING).update(
            state=Notification.State.UNKNOWN, failure_reason="Internal error while sending.")
        notification.refresh_from_db()
    return notification


def _send_now(notification):
    if notification.state != Notification.State.PENDING:
        return notification
    return _submit(notification, lambda: gateway.send_message(notification.to_number, notification.body))


def _schedule_followup(notification):
    if notification.state != Notification.State.PENDING:
        return notification
    order = Order.objects.get(pk=notification.order_id)
    if order.status == STATUS_CANCELLED:
        Notification.objects.filter(pk=notification.pk, state=Notification.State.PENDING).update(
            state=Notification.State.SKIPPED, cancel_state=Notification.ActionState.NOT_NEEDED,
            failure_reason="Order cancelled before the follow-up was queued.")
        notification.refresh_from_db()
        return notification
    notification = _submit(notification, lambda: gateway.schedule_message(
        notification.to_number, notification.body, notification.scheduled_for))
    # The order may have been cancelled while we were queueing it.
    if Order.objects.filter(pk=notification.order_id, status=STATUS_CANCELLED).exists():
        _ensure_followup_cancelled(notification.order)
    return notification


def _settle_unknown(notification):
    """Find a create whose answer was lost, by what it carried (to, from, body, time)."""
    if notification.state != Notification.State.UNKNOWN or notification.provider_sid:
        return notification
    since = (notification.attempt_started_at or notification.created_at) - CREATE_MATCH_SKEW
    try:
        candidates = gateway.find_created_message(notification.to_number, notification.body, since)
    except ProviderError:
        logger.info("notification %s: outcome still unknown (provider unreachable)", notification.pk)
        return notification
    known = set(Notification.objects.filter(
        provider_sid__in=[c.sid for c in candidates]).values_list("provider_sid", flat=True))
    fresh = [c for c in candidates if c.sid not in known]
    if not fresh:
        logger.info("notification %s: outcome still unknown (no matching message yet)", notification.pk)
        return notification
    # Oldest matching message is the one this attempt created.
    fresh.sort(key=lambda c: c.date_created)
    _apply_snapshot(notification, fresh[0])
    notification.failure_reason = ""
    try:
        with transaction.atomic():
            notification.save()
    except IntegrityError:
        notification.refresh_from_db()
    logger.info("notification %s: lost create settled at the provider", notification.pk)
    return notification


def refresh_from_provider(notification):
    if not notification.provider_sid or notification.is_final:
        return notification
    try:
        snapshot = gateway.fetch_message(notification.provider_sid)
    except ProviderError:
        return notification
    _apply_snapshot(notification, snapshot)
    notification.save()
    return notification


def settle_order(order, refresh_budget=None):
    """
    Bring an order's notifications up to date with the provider: finish
    abandoned claims, settle lost creates, re-read delivery status, and make
    sure a cancelled order's follow-up is called off.
    """
    budget = [MAX_REFRESH_PER_REQUEST if refresh_budget is None else refresh_budget]
    stale_before = timezone.now() - STALE_CLAIM
    for notification in Notification.objects.filter(order=order):
        if budget[0] <= 0:
            break
        if notification.state == Notification.State.PENDING and notification.created_at < stale_before:
            if notification.attempt_started_at is None:
                # Claimed but never sent (the request died first): send it now.
                budget[0] -= 1
                if notification.kind == Notification.Kind.DELIVERY_FOLLOWUP:
                    _schedule_followup(notification)
                else:
                    _send_now(notification)
                continue
            if notification.attempt_started_at < stale_before:
                notification.state = Notification.State.UNKNOWN
                notification.failure_reason = "Send attempt interrupted."
                notification.save()
        if notification.state == Notification.State.UNKNOWN:
            budget[0] -= 1
            _settle_unknown(notification)
        elif notification.provider_sid and not notification.is_final:
            budget[0] -= 1
            refresh_from_provider(notification)
    if order.status == STATUS_CANCELLED:
        _ensure_followup_cancelled(order)


# ---------------------------------------------------------------------------
# Order flows
# ---------------------------------------------------------------------------

def place_order(user, lines):
    if not isinstance(lines, list) or not lines:
        raise ServiceError(400, "lines must be a non-empty list of {productId, quantity}.")
    wanted = []
    for line in lines:
        if not isinstance(line, dict):
            raise ServiceError(400, "Each line must be an object.")
        product_id, quantity = line.get("productId"), line.get("quantity", 1)
        if not isinstance(product_id, int) or not isinstance(quantity, int) or not 1 <= quantity <= 99:
            raise ServiceError(400, "productId must be an integer and quantity an integer from 1 to 99.")
        wanted.append((product_id, quantity))
    products = Product.objects.in_bulk([p for p, _ in wanted])
    strategy = Selector().strategy(user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in wanted:
            product = products.get(product_id)
            if product is None:
                raise ServiceError(400, "Unknown productId %s." % product_id)
            info = strategy.fetch_for_product(product)
            if not product.is_public or not info.availability.is_available_to_buy:
                raise ServiceError(400, "Product %s is not available to buy." % product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ServiceError(400, "Product %s: %s" % (product_id, reason))
            basket.add_product(product, quantity)
        shipping_method = Free()
        shipping_charge = shipping_method.calculate(basket)
        surcharges = SurchargeApplicator().get_applicable_surcharges(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge, surcharges)
        order = OrderCreator().place_order(
            user=user, basket=basket, shipping_method=shipping_method,
            shipping_charge=shipping_charge, total=total, surcharges=surcharges)
        basket.set_as_submitted()
        notification = _claim_notification(order, Notification.Kind.ORDER_PLACED)
    logger.info("order %s placed by user %s", order.pk, user.pk)
    _send_now(notification)
    return order


def _get_order_for_update(order_id):
    try:
        return Order.objects.select_for_update().get(pk=order_id)
    except Order.DoesNotExist:
        raise ServiceError(404, "Order not found.")


def dispatch_order(order_id, operator):
    try:
        with transaction.atomic():
            order = _get_order_for_update(order_id)
            if order.status == STATUS_DISPATCHED:
                raise ServiceError(409, "Order is already dispatched.")
            try:
                order.set_status(STATUS_DISPATCHED)
            except InvalidOrderStatus:
                raise ServiceError(409, "An order in status '%s' cannot be dispatched." % order.status)
            dispatched = _claim_notification(order, Notification.Kind.DISPATCHED, requested_by=operator)
            delay = timedelta(hours=float(getattr(settings, "SMS_FOLLOWUP_DELAY_HOURS", 72)))
            followup = _claim_notification(order, Notification.Kind.DELIVERY_FOLLOWUP, requested_by=operator,
                                           scheduled_for=timezone.now() + delay)
    except IntegrityError:
        raise ServiceError(409, "Order is already dispatched.")
    logger.info("order %s dispatched by operator %s", order.pk, operator.pk)
    _send_now(dispatched)
    _schedule_followup(followup)
    order.refresh_from_db()
    return order


def cancel_order(order_id, operator):
    """Cancel an order. Repeating it retries calling off any follow-up still queued."""
    already_cancelled = False
    notice = None
    try:
        with transaction.atomic():
            order = _get_order_for_update(order_id)
            if order.status == STATUS_CANCELLED:
                already_cancelled = True
            else:
                try:
                    order.set_status(STATUS_CANCELLED)
                except InvalidOrderStatus:
                    raise ServiceError(409, "An order in status '%s' cannot be cancelled." % order.status)
                notice = _claim_notification(order, Notification.Kind.CANCELLED, requested_by=operator)
    except IntegrityError:
        already_cancelled = True
        order = Order.objects.get(pk=order_id)
    # The follow-up is called off first: it is the message that must never go out.
    _ensure_followup_cancelled(order)
    if notice is not None:
        logger.info("order %s cancelled by operator %s", order.pk, operator.pk)
        _send_now(notice)
    order.refresh_from_db()
    return order, already_cancelled


def _followup_of(order):
    return Notification.objects.filter(
        order=order, kind=Notification.Kind.DELIVERY_FOLLOWUP, resend_of__isnull=True).first()


def _ensure_followup_cancelled(order):
    followup = _followup_of(order)
    if followup is None:
        return None
    if followup.cancel_state in (Notification.ActionState.DONE, Notification.ActionState.TOO_LATE,
                                 Notification.ActionState.NOT_NEEDED):
        return followup
    if followup.state in (Notification.State.SKIPPED, Notification.State.SEND_FAILED):
        Notification.objects.filter(pk=followup.pk).update(cancel_state=Notification.ActionState.NOT_NEEDED)
        followup.refresh_from_db()
        return followup
    if followup.state == Notification.State.PENDING:
        if followup.attempt_started_at is None:
            # Not handed to the provider yet; _schedule_followup re-checks the order and skips it.
            skipped = Notification.objects.filter(
                pk=followup.pk, state=Notification.State.PENDING, attempt_started_at__isnull=True,
            ).update(state=Notification.State.SKIPPED, cancel_state=Notification.ActionState.NOT_NEEDED,
                     failure_reason="Order cancelled before the follow-up was queued.")
            followup.refresh_from_db()
            if skipped:
                return followup
        else:
            # Being queued right now by another request, which re-checks the order afterwards.
            Notification.objects.filter(pk=followup.pk, cancel_state="").update(
                cancel_state=Notification.ActionState.UNKNOWN, cancel_requested_at=timezone.now())
            followup.refresh_from_db()
            return followup
    if followup.state == Notification.State.UNKNOWN:
        _settle_unknown(followup)
        if not followup.provider_sid:
            Notification.objects.filter(pk=followup.pk).update(
                cancel_state=Notification.ActionState.UNKNOWN, cancel_requested_at=timezone.now())
            followup.refresh_from_db()
            logger.warning("notification %s: follow-up could not be located yet; will re-check", followup.pk)
            return followup
    return _cancel_scheduled(followup)


def _cancel_scheduled(notification):
    """Call off a message queued with the provider. Claim -> update_message -> record."""
    if not notification.provider_sid:
        return notification
    now = timezone.now()
    claimable = Q(cancel_state__in=["", Notification.ActionState.UNKNOWN, Notification.ActionState.FAILED]) | Q(
        cancel_state=Notification.ActionState.REQUESTED, cancel_requested_at__lt=now - STALE_CLAIM)
    taken = Notification.objects.filter(claimable, pk=notification.pk).update(
        cancel_state=Notification.ActionState.REQUESTED, cancel_requested_at=now)
    notification.refresh_from_db()
    if not taken:
        return notification
    try:
        snapshot = gateway.cancel_message(notification.provider_sid)
    except ProviderError as e:
        # Whatever the failure, re-read the message to learn what is true now.
        try:
            snapshot = gateway.fetch_message(notification.provider_sid)
        except ProviderError:
            notification.cancel_state = (Notification.ActionState.UNKNOWN if e.outcome_unknown
                                         else Notification.ActionState.FAILED)
            notification.save()
            logger.warning("notification %s: cancel failed (%s); will re-check", notification.pk, e.message)
            return notification
    _apply_snapshot(notification, snapshot)
    if snapshot.status == "canceled":
        notification.cancel_state = Notification.ActionState.DONE
    elif snapshot.status in gateway.FINAL_STATUSES or snapshot.status in ("sending", "sent", "queued"):
        notification.cancel_state = Notification.ActionState.TOO_LATE
    else:
        notification.cancel_state = Notification.ActionState.FAILED
    notification.save()
    logger.info("notification %s: cancel -> %s (provider status %s)", notification.pk,
                notification.cancel_state, notification.provider_status)
    return notification


# ---------------------------------------------------------------------------
# Operator actions on a notification
# ---------------------------------------------------------------------------

RESENDABLE_KINDS = (Notification.Kind.ORDER_PLACED, Notification.Kind.DISPATCHED, Notification.Kind.CANCELLED)


def _validate_idempotency_key(key):
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or not key.isprintable() or key.strip() != key:
        raise ServiceError(400, "An idempotency key of 1-128 printable characters is required.")


def _replay(existing, original_id):
    if existing.resend_of_id != int(original_id):
        raise ServiceError(409, "This idempotency key was already used for a different notification.")
    return existing, True


def resend_notification(original_id, idempotency_key, operator):
    _validate_idempotency_key(idempotency_key)
    existing = Notification.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return _replay(existing, original_id)
    try:
        original = Notification.objects.select_related("order").get(pk=original_id)
    except Notification.DoesNotExist:
        raise ServiceError(404, "Notification not found.")
    if original.kind not in RESENDABLE_KINDS:
        raise ServiceError(409, "A scheduled follow-up cannot be re-sent.")
    if original.content_disposed_at is not None:
        raise ServiceError(409, "The content of this message has been disposed of.")
    order = original.order
    if order.status == STATUS_CANCELLED and original.kind != Notification.Kind.CANCELLED:
        raise ServiceError(409, "The order has been cancelled; this message is no longer relevant.")
    _settle_unknown(original)
    refresh_from_provider(original)
    if original.state in (Notification.State.PENDING, Notification.State.UNKNOWN):
        raise ServiceError(409, "The original message's outcome is not known yet; re-sending could duplicate it.")
    reached = original.state == Notification.State.SUBMITTED and original.provider_status not in gateway.FAILED_STATUSES
    if reached:
        raise ServiceError(409, "The original message did not fail (provider status '%s')."
                           % (original.provider_status or "unknown"))
    contact = _primary_number(order.user)
    if contact is None:
        raise ServiceError(409, "The shopper has no mobile number on file.")
    try:
        with transaction.atomic():
            resend = Notification.objects.create(
                order=order, kind=original.kind, resend_of=original, idempotency_key=idempotency_key,
                body=original.body, contact_number=contact, to_number=contact.phone_number,
                state=Notification.State.PENDING, requested_by=operator)
    except IntegrityError:
        existing = Notification.objects.get(idempotency_key=idempotency_key)
        return _replay(existing, original_id)
    logger.info("notification %s: re-send %s requested by operator %s", original.pk, resend.pk, operator.pk)
    return _send_now(resend), False


def dispose_content(notification_id, operator):
    try:
        notification = Notification.objects.get(pk=notification_id)
    except Notification.DoesNotExist:
        raise ServiceError(404, "Notification not found.")
    if notification.content_disposed_at is not None:
        return notification
    _settle_unknown(notification)
    if notification.state in (Notification.State.PENDING, Notification.State.UNKNOWN):
        raise ServiceError(409, "The message's provider copy cannot be located yet; try again shortly.")
    now = timezone.now()
    claimable = Q(disposal_state__in=["", Notification.ActionState.UNKNOWN, Notification.ActionState.FAILED]) | Q(
        disposal_state=Notification.ActionState.REQUESTED, disposal_requested_at__lt=now - STALE_CLAIM)
    taken = Notification.objects.filter(claimable, pk=notification.pk).update(
        disposal_state=Notification.ActionState.REQUESTED, disposal_requested_at=now)
    notification.refresh_from_db()
    if not taken:
        if notification.content_disposed_at is not None:
            return notification
        raise ServiceError(409, "Disposal is already in progress.")

    if notification.provider_sid:
        if notification.provider_status == "scheduled":
            # Not sent yet: call it off first, then redact what the provider holds.
            _cancel_scheduled(notification)
        try:
            snapshot = gateway.redact_message(notification.provider_sid)
        except ProviderError as e:
            try:
                snapshot = gateway.fetch_message(notification.provider_sid)
            except ProviderError:
                snapshot = None
            if snapshot is None or snapshot.body:
                notification.disposal_state = (Notification.ActionState.UNKNOWN if e.outcome_unknown
                                               else Notification.ActionState.FAILED)
                notification.save()
                logger.warning("notification %s: content disposal failed (%s)", notification.pk, e.message)
                status = 409 if e.rejected else e.status_code
                raise ServiceError(status, "The provider did not dispose of the message content: %s" % e.message)
        if snapshot.body:
            notification.disposal_state = Notification.ActionState.FAILED
            notification.save()
            raise ServiceError(502, "The provider still returns the message content.")
        _apply_snapshot(notification, snapshot)
    notification.body = ""
    notification.content_disposed_at = timezone.now()
    notification.disposal_state = Notification.ActionState.DONE
    notification.save()
    logger.info("notification %s: content disposed by operator %s", notification.pk, operator.pk)
    return notification


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _provider_entry(snapshot):
    return {
        "providerSid": snapshot.sid,
        "providerStatus": snapshot.status,
        "providerErrorCode": snapshot.error_code,
        "dateSent": snapshot.date_sent.isoformat() if snapshot.date_sent else None,
        "to": mask_number(snapshot.to),
    }


def _app_entry(notification):
    return {
        "notificationId": notification.pk,
        "orderId": notification.order_id,
        "kind": notification.kind,
        "state": notification.state,
        "providerSid": notification.provider_sid,
        "providerStatus": notification.provider_status or None,
        "dateSent": notification.provider_date_sent.isoformat() if notification.provider_date_sent else None,
        "to": mask_number(notification.to_number),
    }


def reconcile(start, end):
    if start >= end:
        raise ServiceError(400, "'from' must be before 'to'.")
    if end - start > timedelta(days=366):
        raise ServiceError(400, "The range may not exceed 366 days.")

    # Settle our own side first so it is compared on the provider's clock.
    unsettled_q = Notification.objects.filter(created_at__gte=start - timedelta(days=1), created_at__lte=end)
    for notification in unsettled_q.filter(state=Notification.State.UNKNOWN)[:MAX_REFRESH_PER_REQUEST]:
        _settle_unknown(notification)
    missing_time = unsettled_q.filter(provider_sid__isnull=False, provider_date_sent__isnull=True).exclude(
        provider_status__in=list(gateway.FINAL_STATUSES))
    for notification in missing_time[:MAX_REFRESH_PER_REQUEST]:
        refresh_from_provider(notification)

    try:
        provider_messages = gateway.list_messages_sent(start, end)
    except ProviderError as e:
        raise ServiceError(e.status_code, e.message)
    provider_by_sid = {m.sid: m for m in provider_messages}
    app_in_range = {n.provider_sid: n for n in Notification.objects.filter(
        provider_sid__isnull=False, provider_date_sent__gte=start, provider_date_sent__lte=end)}

    matched, provider_only = [], []
    known_elsewhere = {n.provider_sid: n for n in Notification.objects.filter(
        provider_sid__in=[sid for sid in provider_by_sid if sid not in app_in_range])}
    for sid, message in provider_by_sid.items():
        notification = app_in_range.get(sid) or known_elsewhere.get(sid)
        if notification is None:
            provider_only.append(_provider_entry(message))
            continue
        app_status_before = notification.provider_status or None
        _apply_snapshot(notification, message)
        notification.save()
        entry = _app_entry(notification)
        entry["appStatusBefore"] = app_status_before
        entry["statusAgreed"] = app_status_before == message.status
        matched.append(entry)
    app_only = [_app_entry(n) for sid, n in app_in_range.items() if sid not in provider_by_sid]
    unsettled = [_app_entry(n) for n in unsettled_q.filter(
        state__in=[Notification.State.PENDING, Notification.State.UNKNOWN], created_at__gte=start)]
    not_sent = [_app_entry(n) for n in unsettled_q.filter(
        provider_sid__isnull=False, provider_date_sent__isnull=True, created_at__gte=start)]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "sendingNumber": mask_number(getattr(settings, "TWILIO_FROM_NUMBER", "")),
        "summary": {
            "providerMessages": len(provider_by_sid),
            "matched": len(matched),
            "statusMismatches": sum(1 for m in matched if not m["statusAgreed"]),
            "providerOnly": len(provider_only),
            "appOnly": len(app_only),
            "unsettled": len(unsettled),
            "scheduledOrCanceled": len(not_sent),
        },
        "matched": matched,
        "providerOnly": provider_only,
        "appOnly": app_only,
        "unsettled": unsettled,
        "scheduledOrCanceled": not_sent,
    }
