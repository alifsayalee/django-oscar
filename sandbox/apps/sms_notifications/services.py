"""Order notifications by SMS: the Django side of the integration.

Order operations never fail because of a message: every send is attempted after the order change has
been committed, and whatever happens to it is recorded on a ``Notification`` row, not raised.
"""

import atexit
import hashlib
import logging
import re
import threading
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from twilio_sdk.core import ApiError

from . import provider
from .models import ContactNumber, Notification, NotificationAction
from .store import DjangoClaimStore

logger = logging.getLogger("apps.sms_notifications")

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
ShippingEventType = get_model("order", "ShippingEventType")
Selector = get_class("partner.strategy", "Selector")
Repository = get_class("shipping.repository", "Repository")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")
EventHandler = get_class("order.processing", "EventHandler")

STATUS_PENDING = "Pending"
STATUS_DISPATCHED = "Being processed"  # the sandbox pipeline's post-dispatch status
STATUS_CANCELLED = "Cancelled"
DISPATCHED_EVENT = "Dispatched"

MESSAGES = {
    Notification.PLACED: "Thanks for your order {number}! We'll text you when it's on its way.",
    Notification.DISPATCHED: "Good news: order {number} has been dispatched and is on its way.",
    Notification.FOLLOW_UP: "How did the delivery of order {number} go? Reply to let us know.",
    Notification.CANCELLED: "Your order {number} has been cancelled. Contact us if this is unexpected.",
}
REF_SUFFIX = re.compile(r" Ref [0-9A-F]{10}$")
REFRESH_LIMIT = 25


class ServiceError(Exception):
    """A request this app refuses, with the HTTP status and a message we wrote."""

    def __init__(self, status_code, message, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.extra = extra


# ---------------------------------------------------------------------------
# The provider client: one per process, built lazily (after any fork), closed at exit
# ---------------------------------------------------------------------------

_messaging = None
_messaging_lock = threading.Lock()


def twilio_config():
    return provider.TwilioConfig(
        account_sid=settings.TWILIO_ACCOUNT_SID,
        auth_token=settings.TWILIO_AUTH_TOKEN,
        from_number=settings.TWILIO_FROM_NUMBER,
        messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
        base_url=settings.TWILIO_BASE_URL or None,
        lookups_base_url=getattr(settings, "TWILIO_LOOKUPS_BASE_URL", None) or None,
        timeout=float(getattr(settings, "TWILIO_TIMEOUT_SECONDS", 10.0)),
    )


def get_messaging():
    global _messaging
    if _messaging is None:
        with _messaging_lock:
            if _messaging is None:
                config = twilio_config()
                client = provider.build_client(config)
                atexit.register(client.close)
                _messaging = provider.Messaging(client, config)
    return _messaging


def set_messaging(messaging):
    """Swap the process-wide client (tests; credential rotation). Returns the previous one."""
    global _messaging
    with _messaging_lock:
        previous, _messaging = _messaging, messaging
    return previous


def install_prefix():
    return settings.SMS_INSTALL_ID


def order_reference(order, kind):
    return f"{install_prefix()}:order:{order.pk}:{kind}"


def action_reference(notification, kind):
    return f"{install_prefix()}:notification:{notification.pk}:{kind}"


def resend_reference(notification, idempotency_key):
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    return f"{install_prefix()}:notification:{notification.pk}:resend:{digest}"


# ---------------------------------------------------------------------------
# Contact numbers
# ---------------------------------------------------------------------------


def register_contact_number(user, raw_number):
    if not isinstance(raw_number, str) or not raw_number.strip() or len(raw_number) > 32:
        raise ServiceError(400, "phoneNumber must be a non-empty string of at most 32 characters.")
    try:
        result = get_messaging().lookup(raw_number.strip())
    except provider.NotConfigured as e:
        raise ServiceError(503, "SMS is not configured on this site.") from e
    except provider.ProviderError as e:
        raise ServiceError(e.status_code, str(e)) from e
    if not result.valid:
        raise ServiceError(
            422, "The messaging provider does not consider this a usable phone number.", problems=result.problems
        )
    try:
        with transaction.atomic():
            number = ContactNumber.objects.create(
                user=user, phone_number=result.canonical, country_code=result.country_code or ""
            )
        return number, True
    except IntegrityError:
        return ContactNumber.objects.get(user=user, phone_number=result.canonical), False


def current_contact_number(user):
    return ContactNumber.objects.filter(user=user).order_by("-created_at", "-id").first()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def _parse_lines(lines):
    if not isinstance(lines, list) or not lines or len(lines) > 50:
        raise ServiceError(400, "lines must be a non-empty list of {productId, quantity} (at most 50).")
    parsed = []
    for line in lines:
        if not isinstance(line, dict):
            raise ServiceError(400, "Each line must be an object with productId and quantity.")
        product_id, quantity = line.get("productId"), line.get("quantity", 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise ServiceError(400, "productId must be an integer.")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 100:
            raise ServiceError(400, "quantity must be an integer between 1 and 100.")
        parsed.append((product_id, quantity))
    return parsed


def _shipping_address(data):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ServiceError(400, "shippingAddress must be an object.")
    required = ("firstName", "lastName", "line1", "city", "postcode", "countryCode")
    missing = [name for name in required if not isinstance(data.get(name), str) or not data[name].strip()]
    if missing:
        raise ServiceError(400, "shippingAddress is missing: " + ", ".join(missing))
    try:
        country = Country.objects.get(iso_3166_1_a2=data["countryCode"].upper(), is_shipping_country=True)
    except Country.DoesNotExist as e:
        raise ServiceError(400, "shippingAddress.countryCode is not a country this shop ships to.") from e
    return ShippingAddress(
        first_name=data["firstName"],
        last_name=data["lastName"],
        line1=data["line1"],
        line4=data["city"],
        postcode=data["postcode"],
        country=country,
    )


def place_order(user, lines, shipping_address=None, request=None):
    """Place an order through Oscar's own basket/order machinery, then text the shopper."""
    parsed = _parse_lines(lines)
    address = _shipping_address(shipping_address)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for product_id, quantity in parsed:
            try:
                product = Product.objects.get(pk=product_id)
            except Product.DoesNotExist as e:
                raise ServiceError(400, f"Product {product_id} does not exist.") from e
            info = basket.strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted or info.price is None or not info.price.exists:
                raise ServiceError(409, f"Product {product_id} cannot be bought: {reason or 'no price'}".strip())
            basket.add_product(product, quantity)
        basket.reset_offer_applications()
        method = Repository().get_default_shipping_method(basket=basket, user=user, shipping_addr=address)
        charge = method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, charge)
        if address is not None:
            address.save()
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=method,
            shipping_charge=charge,
            user=user,
            shipping_address=address,
            order_number=OrderNumberGenerator().order_number(basket),
            request=request,
        )
        basket.submit()
    notifications = [n for n in [notify(order, Notification.PLACED)] if n is not None]
    return order, notifications


def _locked_order(order_id):
    return Order.objects.select_for_update().get(pk=order_id)


def _is_dispatched(order):
    return order.shipping_events.filter(event_type__name=DISPATCHED_EVENT).exists()


def dispatch_order(order_id, operator):
    with transaction.atomic():
        order = _locked_order(order_id)
        if order.status == STATUS_CANCELLED:
            raise ServiceError(409, "A cancelled order cannot be dispatched.")
        if _is_dispatched(order):
            raise ServiceError(409, "This order has already been dispatched.")
        handler = EventHandler(user=operator)
        event_type, _ = ShippingEventType.objects.get_or_create(name=DISPATCHED_EVENT)
        lines = list(order.lines.all())
        handler.handle_shipping_event(order, event_type, lines, [line.quantity for line in lines])
        handler.handle_order_status_change(order, STATUS_DISPATCHED, note_msg="Dispatched via API")
    notifications = [notify(order, Notification.DISPATCHED)]
    follow_up = notify(order, Notification.FOLLOW_UP)
    notifications.append(follow_up)
    # Closes the race with a cancel that committed while the follow-up was being queued.
    if follow_up is not None and Order.objects.filter(pk=order.pk, status=STATUS_CANCELLED).exists():
        call_off_follow_up(order)
    return order, [n for n in notifications if n is not None]


def cancel_order(order_id, operator):
    """Cancel the order (idempotent), tell the shopper, and call off a queued follow-up.

    Repeating the call on an already-cancelled order re-runs the notification and call-off steps; each
    is protected by its own claim, so nothing is sent twice.
    """
    with transaction.atomic():
        order = _locked_order(order_id)
        if order.status != STATUS_CANCELLED:
            if STATUS_CANCELLED not in order.available_statuses():
                raise ServiceError(409, f"An order in status '{order.status}' cannot be cancelled.")
            handler = EventHandler(user=operator)
            handler.handle_order_status_change(order, STATUS_CANCELLED, note_msg="Cancelled via API")
            handler.cancel_stock_allocations(order)
    follow_up = call_off_follow_up(order)
    notification = notify(order, Notification.CANCELLED)
    return order, [n for n in [notification] if n is not None], follow_up


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _kept(fn, what):
    """Run a notification step; a message problem is recorded and logged, never raised."""
    try:
        return fn()
    except provider.OutcomeUnknown:
        logger.warning("SMS %s: outcome unknown; left for the status check", what)
    except ApiError as e:
        logger.warning("SMS %s: provider refused the request (HTTP %s)", what, e.status_code)
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("SMS %s: provider call failed (%s)", what, type(e).__name__)
    except provider.NotConfigured:
        logger.error("SMS %s: Twilio settings are incomplete; nothing was sent", what)
    except Exception:  # noqa: BLE001 - a message must never break the order operation
        logger.exception("SMS %s: unexpected failure", what)
    return None


def notify(order, kind):
    """Send (or, for the follow-up, queue with the provider) one order message.

    Returns the Notification row, or None when the shopper has no number on file.
    """
    contact = current_contact_number(order.user) if order.user_id else None
    if contact is None:
        return None
    reference = order_reference(order, kind)
    body = provider.body_with_reference(MESSAGES[kind].format(number=order.number), reference)
    send_at = None
    if kind == Notification.FOLLOW_UP:
        send_at = timezone.now() + timedelta(minutes=settings.SMS_FOLLOWUP_DELAY_MINUTES)
    store = DjangoClaimStore(
        Notification, order=order, user=order.user, contact_number=contact, kind=kind, body=body, scheduled_for=send_at
    )
    _send(store, reference, contact.phone_number, body, send_at, what=f"{kind} for order {order.pk}")
    return Notification.objects.filter(reference=reference).first()


def _send(store, reference, to, body, send_at, what):
    try:
        messaging = get_messaging()
    except provider.NotConfigured:
        if store.try_claim(reference):
            store.complete(reference, provider.FAILED)  # nothing left: the claim is released
        logger.error("SMS %s: Twilio settings are incomplete; nothing was sent", what)
        return None
    captured = {}

    def read(message):
        captured["state"] = provider.message_state(message)
        return provider.send_answer(message)

    def send():
        if send_at is not None:
            return messaging.send_later(to, body, send_at)
        return messaging.send_now(to, body)

    record = _kept(
        lambda: provider.safe_write(
            store, reference, send=send, find=lambda: messaging.find_by_reference(to, reference), read=read
        ),
        what,
    )
    if "state" in captured:
        _apply_state(Notification.objects.get(reference=reference), captured["state"], set_outcome=False)
    return record


def _apply_state(notification, state, *, set_outcome=True):
    """Store what the provider currently says about a message on its Notification."""
    notification.provider_status = state.status or notification.provider_status
    if state.date_sent:
        notification.date_sent = state.date_sent
    notification.provider_time = state.provider_time or notification.provider_time
    if state.error_code is not None:
        notification.error_code = state.error_code
    if state.sid and not notification.provider_sid:
        notification.provider_sid = state.sid
    if set_outcome:
        notification.outcome = provider.status_from_provider(state.raw_status)
    notification.status_checked_at = timezone.now()
    notification.save(
        update_fields=[
            "provider_status",
            "date_sent",
            "provider_time",
            "error_code",
            "provider_sid",
            "outcome",
            "status_checked_at",
        ]
    )


# ---------------------------------------------------------------------------
# Status: reading back what the provider says (a later request can act on it)
# ---------------------------------------------------------------------------

UNSETTLED = (provider.PENDING, provider.UNKNOWN, provider.SENDING)


def refresh(notification):
    """Ask the provider what became of a message. Returns True when the provider answered."""
    if notification.outcome not in UNSETTLED:
        return True
    try:
        messaging = get_messaging()
    except provider.NotConfigured:
        return False
    if notification.provider_sid:
        try:
            message = provider.guarded_read(lambda: messaging.fetch(notification.provider_sid))
        except provider.ProviderError:
            return False
        _apply_state(notification, provider.message_state(message))
        return True
    # No provider id: the send may or may not have landed. Settle it the only way that is safe --
    # by looking it up under the reference it was sent with.
    contact = notification.contact_number
    if contact is None:
        return False
    store = DjangoClaimStore(Notification)
    captured = {}

    def read(message):
        captured["state"] = provider.message_state(message)
        return provider.send_answer(message)

    def never_resend():  # a lookup-only check; this path must not create a message
        raise AssertionError("refresh must not send")

    _kept(
        lambda: provider.safe_write(
            store,
            notification.reference,
            send=never_resend,
            find=lambda: messaging.find_by_reference(contact.phone_number, notification.reference),
            read=read,
        ),
        f"status check for notification {notification.pk}",
    )
    notification.refresh_from_db()
    if "state" in captured:
        _apply_state(notification, captured["state"], set_outcome=False)
        return True
    return False


def refresh_many(notifications):
    unsettled = [n for n in notifications if n.outcome in UNSETTLED][:REFRESH_LIMIT]
    answered = {n.pk: refresh(n) for n in unsettled}
    return {pk for pk, ok in answered.items() if not ok}


# ---------------------------------------------------------------------------
# Follow-up call-off
# ---------------------------------------------------------------------------


def call_off_follow_up(order):
    """Make sure a queued delivery follow-up for this order never reaches the shopper.

    Returns a dict describing the call-off, or None when no follow-up was ever queued.
    """
    follow_up = order.sms_notifications.filter(kind=Notification.FOLLOW_UP, resend_of__isnull=True).first()
    if follow_up is None:
        return None
    if not follow_up.provider_sid:
        refresh(follow_up)
        follow_up.refresh_from_db()
    if not follow_up.provider_sid:
        if follow_up.outcome == provider.FAILED:
            return _call_off_result(follow_up, provider.DONE, "The follow-up was never created at the provider.")
        logger.error("SMS follow-up for order %s could not be located to call it off", order.pk)
        return _call_off_result(
            follow_up,
            provider.UNKNOWN,
            "The follow-up could not be located at the provider yet; repeat the cancel to check again.",
        )
    try:
        messaging = get_messaging()
    except provider.NotConfigured:
        return _call_off_result(follow_up, provider.UNKNOWN, "SMS is not configured on this site.")
    sid = follow_up.provider_sid
    reference = action_reference(follow_up, NotificationAction.CANCEL)
    store = DjangoClaimStore(
        NotificationAction, notification=follow_up, kind=NotificationAction.CANCEL, target_sid=sid
    )
    _kept(
        lambda: provider.safe_write(
            store,
            reference,
            send=lambda: messaging.cancel(sid),
            find=lambda: messaging.fetch(sid),
            read=provider.cancel_answer,
        ),
        f"follow-up call-off for order {order.pk}",
    )
    action = NotificationAction.objects.get(reference=reference)
    # Whatever the call-off said, read the message's own state back.
    try:
        _apply_state(follow_up, provider.message_state(provider.guarded_read(lambda: messaging.fetch(sid))))
    except provider.ProviderError:
        pass
    follow_up.refresh_from_db()
    outcome = action.outcome
    if outcome == provider.FAILED and follow_up.provider_status == "canceled":
        outcome = provider.DONE  # it was already called off by an earlier attempt
    detail = {
        provider.DONE: "The follow-up was called off before it went out.",
        provider.PENDING: "The call-off was accepted but is not reflected yet.",
        provider.FAILED: "Too late: the follow-up had already gone out or ended.",
    }.get(outcome, "The call-off outcome is not known yet; repeat the cancel to check again.")
    return _call_off_result(follow_up, outcome, detail)


def _call_off_result(follow_up, outcome, detail):
    return {
        "notificationId": follow_up.pk,
        "outcome": outcome,
        "providerStatus": follow_up.provider_status or None,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Operator actions on a message
# ---------------------------------------------------------------------------


def resend(notification_id, idempotency_key):
    """Re-send a message that did not reach the shopper, at most once per caller key."""
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 200:
        raise ServiceError(400, "An idempotency key (1-200 characters) is required.")
    original = _get_notification(notification_id)
    reference = resend_reference(original, idempotency_key.strip())
    existing = Notification.objects.filter(reference=reference).first()
    if existing is None:
        _check_resendable(original)
        body = REF_SUFFIX.sub("", original.body)
        body = provider.body_with_reference(body, reference)
        contact = original.contact_number
        store = DjangoClaimStore(
            Notification,
            order=original.order,
            user=original.user,
            contact_number=contact,
            kind=original.kind,
            body=body,
            resend_of=original,
        )
        record = _send(store, reference, contact.phone_number, body, None, what=f"resend of {original.pk}")
    else:
        # A repeat under the same key: answered from the stored record (settling it if it was unknown).
        if existing.contact_number is None and existing.outcome == provider.FAILED:
            raise ServiceError(409, "The shopper removed the number this message was for.")
        store = DjangoClaimStore(Notification)
        record = _send(
            store,
            reference,
            existing.contact_number.phone_number if existing.contact_number else "",
            existing.body,
            None,
            what=f"repeat resend of {original.pk}",
        )
    notification = Notification.objects.get(reference=reference)
    return notification, (record.outcome if record is not None else notification.outcome)


def _check_resendable(original):
    if original.content_disposed_at is not None or not REF_SUFFIX.sub("", original.body).strip():
        raise ServiceError(409, "The content of this message was disposed of; it cannot be re-sent.")
    if original.contact_number is None:
        raise ServiceError(409, "The shopper removed the number this message was for.")
    if original.kind == Notification.FOLLOW_UP and original.order.status == STATUS_CANCELLED:
        raise ServiceError(409, "The order was cancelled; its delivery follow-up must not be sent.")
    if original.outcome in UNSETTLED:
        refresh(original)
        original.refresh_from_db()
    if original.outcome != provider.FAILED:
        raise ServiceError(
            409,
            "Only a message that did not reach the shopper can be re-sent.",
            outcome=original.outcome,
            providerStatus=original.provider_status or None,
        )


def dispose_content(notification_id):
    """Have the provider forget a message's text, keeping the fact it was sent and its outcome."""
    notification = _get_notification(notification_id)
    if notification.content_disposed_at is not None:
        return notification, provider.DONE
    # Stop holding the text ourselves straight away; nothing needs it after this request.
    Notification.objects.filter(pk=notification.pk).update(body="")
    if not notification.provider_sid:
        refresh(notification)
        notification.refresh_from_db()
    if not notification.provider_sid:
        if notification.outcome == provider.FAILED:
            # The provider never created the message, so it holds no text.
            _mark_disposed(notification)
            return notification, provider.DONE
        return notification, provider.UNKNOWN
    if notification.outcome in (provider.PENDING, provider.SENDING, provider.UNKNOWN):
        refresh(notification)
        notification.refresh_from_db()
        if notification.outcome == provider.PENDING:
            raise ServiceError(
                409,
                "The message has not finished sending; its content can be disposed of once it has.",
                providerStatus=notification.provider_status or None,
            )
    try:
        messaging = get_messaging()
    except provider.NotConfigured as e:
        raise ServiceError(503, "SMS is not configured on this site.") from e
    sid = notification.provider_sid
    reference = action_reference(notification, NotificationAction.REDACT)
    store = DjangoClaimStore(
        NotificationAction, notification=notification, kind=NotificationAction.REDACT, target_sid=sid
    )
    record = _kept(
        lambda: provider.safe_write(
            store,
            reference,
            send=lambda: messaging.redact(sid),
            find=lambda: messaging.fetch(sid),
            read=provider.redact_answer,
            repeat_is_safe=True,  # blanking the body by id twice has the same single effect
        ),
        f"content disposal for notification {notification.pk}",
    )
    outcome = record.outcome if record is not None else NotificationAction.objects.get(reference=reference).outcome
    if outcome == provider.DONE:
        _mark_disposed(notification)
    notification.refresh_from_db()
    return notification, outcome


def _mark_disposed(notification):
    Notification.objects.filter(pk=notification.pk).update(body="", content_disposed_at=timezone.now())
    notification.refresh_from_db()


def _get_notification(notification_id):
    try:
        return Notification.objects.select_related("order", "contact_number").get(pk=notification_id)
    except Notification.DoesNotExist as e:
        raise ServiceError(404, "No such notification.") from e


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile(start, end):
    """Line the provider's record of our number's messages up against ours, over [start, end)."""
    try:
        messaging = get_messaging()
    except provider.NotConfigured as e:
        raise ServiceError(503, "SMS is not configured on this site.") from e
    try:
        sent, complete = provider.guarded_read(lambda: messaging.list_sent(start, end))
    except provider.ProviderError as e:
        raise ServiceError(e.status_code, str(e)) from e

    provider_by_sid = {m.sid: m for m in sent if m.sid}
    known = {n.provider_sid: n for n in Notification.objects.filter(provider_sid__in=list(provider_by_sid))}

    matched, provider_only = [], []
    for sid, state in provider_by_sid.items():
        notification = known.get(sid)
        if notification is None:
            provider_only.append(
                {"providerSid": sid, "providerStatus": state.status, "dateSent": _iso(state.date_sent)}
            )
            continue
        before = notification.provider_status
        _apply_state(notification, state)  # the list answer is the provider's current word
        matched.append(
            {
                **notification_summary(notification),
                "statusChanged": before != notification.provider_status,
            }
        )

    local_in_window = Notification.objects.filter(date_sent__gte=start, date_sent__lt=end).exclude(
        provider_sid__in=list(provider_by_sid)
    )
    local_only = [notification_summary(n) for n in local_in_window]
    # Ours, created in the window, that the provider never reported sending (queued, called off,
    # refused, or not settled): a different finding from a discrepancy, so reported separately.
    not_sent = Notification.objects.filter(created_at__gte=start, created_at__lt=end, date_sent__isnull=True).exclude(
        provider_sid__in=list(provider_by_sid)
    )
    return {
        "from": _iso(start),
        "to": _iso(end),
        "fromNumber": messaging.from_number,
        "complete": complete,
        "counts": {
            "provider": len(provider_by_sid),
            "matched": len(matched),
            "providerOnly": len(provider_only),
            "localOnly": len(local_only),
            "notSentByProvider": not_sent.count(),
        },
        "matched": matched,
        "providerOnly": provider_only,
        "localOnly": local_only,
        "notSentByProvider": [notification_summary(n) for n in not_sent],
    }


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def _iso(value):
    return value.isoformat() if value else None


def notification_summary(n):
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "outcome": n.outcome,
        "providerSid": n.provider_sid,
        "providerStatus": n.provider_status or None,
        "dateSent": _iso(n.date_sent),
    }


def notification_json(n, *, refresh_failed=False):
    actions = {a.kind: a for a in n.actions.all()}
    cancel = actions.get(NotificationAction.CANCEL)
    return {
        "notificationId": n.pk,
        "orderId": n.order_id,
        "kind": n.kind,
        "resendOf": n.resend_of_id,
        "destination": n.contact_number.masked if n.contact_number else None,
        "outcome": n.outcome,
        "providerSid": n.provider_sid,
        "providerStatus": n.provider_status or None,
        "errorCode": n.error_code,
        "scheduledFor": _iso(n.scheduled_for),
        "dateSent": _iso(n.date_sent),
        "createdAt": _iso(n.created_at),
        "statusCheckedAt": _iso(n.status_checked_at),
        "statusCheckFailed": refresh_failed,
        "calledOff": bool(cancel and (cancel.outcome == provider.DONE or n.provider_status == "canceled")),
        "content": None if n.content_disposed_at else (n.body or None),
        "contentDisposedAt": _iso(n.content_disposed_at),
    }


def order_json(order, notifications, *, refresh_failed=()):
    return {
        "orderId": order.pk,
        "number": str(order.number),
        "status": order.status,
        "dispatched": _is_dispatched(order),
        "total": str(order.total_incl_tax),
        "currency": order.currency,
        "placedAt": _iso(order.date_placed),
        "notifications": [notification_json(n, refresh_failed=n.pk in refresh_failed) for n in notifications],
    }
