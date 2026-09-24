"""
Order notification logic: who gets messaged, when, and what is recorded.

Rules this module holds to:

* A message that cannot be sent never fails the order operation that caused
  it; the failure is recorded on the Notification instead.
* A Notification row is committed *before* its provider call, so a send whose
  outcome is unknown is always findable afterwards.
* Phone numbers are never logged.
"""

import atexit
import logging
import secrets
import threading
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import gateway as gw
from .models import ContactNumber, Notification, ResendRequest

logger = logging.getLogger(__name__)

Basket = get_model("basket", "Basket")
Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
Repository = get_class("shipping.repository", "Repository")
Selector = get_class("partner.strategy", "Selector")
Applicator = get_class("offer.applicator", "Applicator")
InvalidOrderStatus = get_class("order.exceptions", "InvalidOrderStatus")

STATUS_DISPATCHED = "Dispatched"
STATUS_CANCELLED = "Cancelled"

# Refresh a message's state from the provider at most this often, and at
# most this many messages per request.
REFRESH_INTERVAL = timedelta(seconds=5)
REFRESH_LIMIT = 25

# Cancel/redact are safe to repeat, so transient failures are retried.
IDEMPOTENT_ATTEMPTS = 3

RESENDABLE_OUTCOMES = frozenset({gw.OUTCOME_FAILED, "not_sent"})


class ServiceError(Exception):
    def __init__(self, http_status, message, **extra):
        super().__init__(message)
        self.http_status = http_status
        self.extra = extra


# -- The provider client ------------------------------------------------------

_gateway = None
_gateway_lock = threading.Lock()


def get_gateway():
    """
    The process-wide gateway, built lazily on first use (so it is created
    after any worker fork) and closed at interpreter exit.
    """
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                _gateway = gw.TwilioGateway(
                    account_sid=settings.TWILIO_ACCOUNT_SID,
                    auth_token=settings.TWILIO_AUTH_TOKEN,
                    from_number=settings.TWILIO_FROM_NUMBER,
                    messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
                    base_url=settings.TWILIO_BASE_URL,
                    timeout=settings.TWILIO_TIMEOUT_SECONDS,
                )
                atexit.register(_gateway.close)
    return _gateway


def set_gateway(gateway):
    """Swap the gateway (tests). Returns the previous one."""
    global _gateway
    with _gateway_lock:
        previous, _gateway = _gateway, gateway
    return previous


# -- Contact numbers -----------------------------------------------------------

def register_contact_number(user, raw_number, country_code=None):
    """
    Validate ``raw_number`` with the provider and store its canonical form.
    Returns (contact_number, created).
    """
    raw_number = (raw_number or "").strip()
    if not raw_number or len(raw_number) > 32:
        raise ServiceError(400, "phoneNumber is required.")
    try:
        looked_up = get_gateway().lookup_number(raw_number, country_code=country_code or None)
    except gw.InvalidDestination:
        raise ServiceError(422, "The messaging provider does not recognise this as a valid phone number.")
    except gw.ProviderError as e:
        raise ServiceError(e.http_status, str(e))
    try:
        with transaction.atomic():
            contact, created = ContactNumber.objects.get_or_create(
                user=user,
                phone_number=looked_up.phone_number,
                deleted_at__isnull=True,
                defaults={"country_code": looked_up.country_code or ""},
            )
    except IntegrityError:
        contact = ContactNumber.objects.get(
            user=user, phone_number=looked_up.phone_number, deleted_at__isnull=True
        )
        created = False
    logger.info("contact number %s registered for user %s (created=%s)", contact.pk, user.pk, created)
    return contact, created


def delete_contact_number(contact):
    """Remove a number; anything still queued for it is called off."""
    contact.deleted_at = timezone.now()
    contact.save(update_fields=["deleted_at"])
    pending = Notification.objects.filter(
        contact_number=contact, outcome__in=[gw.OUTCOME_SCHEDULED, gw.OUTCOME_UNKNOWN]
    )
    for notification in pending:
        _cancel_before_send(notification)
    logger.info("contact number %s removed", contact.pk)


def active_number_for(user):
    return ContactNumber.objects.filter(user=user, deleted_at__isnull=True).first()


# -- Orders --------------------------------------------------------------------

def place_order(user, lines, request=None):
    """
    Place an order for ``user`` through Oscar's own basket and OrderCreator.
    ``lines`` is a list of (product_id, quantity).
    """
    strategy = Selector().strategy(request=request, user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for product_id, quantity in lines:
            try:
                product = Product.objects.get(pk=product_id)
            except Product.DoesNotExist:
                raise ServiceError(422, "Catalogue item %s does not exist." % product_id)
            if not product.is_public or product.is_parent:
                raise ServiceError(422, "Catalogue item %s cannot be ordered." % product_id)
            info = strategy.fetch_for_product(product)
            if info.stockrecord is None:
                raise ServiceError(422, "Catalogue item %s is not for sale." % product_id)
            already = basket.product_quantity(product)
            permitted, reason = info.availability.is_purchase_permitted(already + quantity)
            if not permitted:
                raise ServiceError(422, "Catalogue item %s: %s" % (product_id, reason))
            basket.add_product(product, quantity)
        Applicator().apply(basket, user, request)
        shipping_method = Repository().get_default_shipping_method(basket=basket, user=user, request=request)
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            request=request,
        )
        basket.submit()
    logger.info("order %s placed by user %s", order.pk, user.pk)
    notify(order, Notification.ORDER_PLACED)
    return order


def dispatch_order(order):
    try:
        order.set_status(STATUS_DISPATCHED)
    except InvalidOrderStatus:
        raise ServiceError(409, "Order %s cannot be dispatched from status '%s'." % (order.pk, order.status))
    logger.info("order %s dispatched", order.pk)
    created = [notify(order, Notification.DISPATCHED)]
    delay = timedelta(minutes=settings.TWILIO_FOLLOWUP_DELAY_MINUTES)
    created.append(notify(order, Notification.DELIVERY_FOLLOWUP, send_at=timezone.now() + delay))
    return [n for n in created if n is not None]


def cancel_order(order):
    try:
        order.set_status(STATUS_CANCELLED)
    except InvalidOrderStatus:
        raise ServiceError(409, "Order %s cannot be cancelled from status '%s'." % (order.pk, order.status))
    logger.info("order %s cancelled", order.pk)
    # First make sure no delivery follow-up can reach the shopper.
    followups = list(
        order.sms_notifications.filter(kind=Notification.DELIVERY_FOLLOWUP).exclude(
            outcome__in=[gw.OUTCOME_CANCELED, gw.OUTCOME_FAILED, "not_sent"]
        )
    )
    for followup in followups:
        _cancel_before_send(followup)
    notice = notify(order, Notification.CANCELLED)
    return followups, notice


# -- Sending -------------------------------------------------------------------

def _message_text(order, kind, reference):
    shop = getattr(settings, "OSCAR_SHOP_NAME", "Oscar")
    number = order.number
    if kind == Notification.ORDER_PLACED:
        text = "%s: thanks, your order %s has been placed (total %s %s)." % (
            shop, number, order.total_incl_tax, order.currency)
    elif kind == Notification.DISPATCHED:
        text = "%s: good news, your order %s is on its way." % (shop, number)
    elif kind == Notification.DELIVERY_FOLLOWUP:
        text = "%s: how did the delivery of your order %s go? Reply and let us know." % (shop, number)
    else:
        text = "%s: your order %s has been cancelled." % (shop, number)
    return "%s Ref %s" % (text, reference)


def _new_reference():
    return "N" + secrets.token_hex(5).upper()


def notify(order, kind, *, send_at=None, resend_of=None, contact=None):
    """
    Message the order's shopper about ``kind``. Never raises: a message that
    cannot be sent is recorded, not propagated. Returns the Notification, or
    None when the shopper has no number on file.
    """
    try:
        if contact is None:
            contact = active_number_for(order.user) if order.user_id else None
        if contact is None:
            logger.info("order %s: no contact number on file, %s not sent", order.pk, kind)
            return None
        reference = _new_reference()
        notification = Notification.objects.create(
            order=order,
            user=order.user,
            contact_number=contact,
            kind=kind,
            resend_of=resend_of,
            reference=reference,
            body=_message_text(order, kind, reference),
            scheduled_for=send_at,
            submit_state=Notification.SUBMIT_SENDING,
            outcome="pending",
        )
    except Exception:
        logger.exception("order %s: could not record %s notification", order.pk, kind)
        return None
    _submit(notification)
    return notification


def _submit(notification):
    """Hand a committed Notification to the provider and record the result."""
    try:
        gateway = get_gateway()
        to = notification.contact_number.phone_number
        if notification.scheduled_for:
            message = gateway.schedule(to, notification.body, notification.scheduled_for)
        else:
            message = gateway.send(to, notification.body)
    except gw.ProviderError as e:
        _record_failure(notification, e)
        return
    except Exception:
        # Not a failure kind we know: we cannot say it did not go out.
        logger.exception("notification %s: unexpected error while sending", notification.pk)
        notification.submit_state = Notification.SUBMIT_UNKNOWN
        notification.outcome = gw.OUTCOME_UNKNOWN
        notification.error_message = "Unexpected error while sending."
        notification.save()
        return
    notification.submitted_at = timezone.now()
    _apply_provider_message(notification, message)
    logger.info("notification %s accepted by provider as %s (%s)",
                notification.pk, message.sid, message.status)


def _record_failure(notification, error):
    if error.outcome_unknown:
        notification.submit_state = Notification.SUBMIT_UNKNOWN
        notification.outcome = gw.OUTCOME_UNKNOWN
    else:
        notification.submit_state = Notification.SUBMIT_NOT_SENT
        notification.outcome = "not_sent"
    notification.error_code = error.provider_code
    notification.error_message = str(error)[:255]
    notification.save()
    logger.warning("notification %s not accepted (%s, outcome_unknown=%s)",
                   notification.pk, error.http_status, error.outcome_unknown)


def _apply_provider_message(notification, message):
    notification.provider_sid = message.sid
    notification.provider_status = message.status or ""
    notification.outcome = message.outcome
    notification.submit_state = Notification.SUBMIT_ACCEPTED
    notification.error_code = message.error_code
    notification.error_message = (message.error_message or "")[:255]
    if message.date_sent:
        notification.sent_at = message.date_sent
    if notification.outcome in gw.TERMINAL_OUTCOMES:
        notification.cancel_requested = False
    notification.last_checked_at = timezone.now()
    notification.save()


def _resolve_unknown(notification):
    """Look for a send with unknown outcome at the provider by its reference."""
    if notification.contact_number is None:
        return False
    try:
        found = get_gateway().find_by_reference(notification.contact_number.phone_number, notification.reference)
    except gw.ProviderError as e:
        logger.warning("notification %s: lookup by reference failed (%s)", notification.pk, e.http_status)
        return False
    notification.last_checked_at = timezone.now()
    if found is None:
        # Not found is not proof it never landed: stays unknown.
        notification.save(update_fields=["last_checked_at", "updated_at"])
        return False
    _apply_provider_message(notification, found)
    return True


def _cancel_before_send(notification):
    """
    Call off a message that has not gone out yet. Safe to repeat. If the
    provider cannot confirm it, ``cancel_requested`` stays set and the
    cancellation is retried whenever the message is refreshed.
    """
    notification.cancel_requested = True
    notification.save(update_fields=["cancel_requested", "updated_at"])
    if not notification.provider_sid:
        if notification.submit_state in (Notification.SUBMIT_UNKNOWN, Notification.SUBMIT_SENDING):
            _resolve_unknown(notification)
        if not notification.provider_sid:
            if notification.submit_state == Notification.SUBMIT_NOT_SENT:
                notification.cancel_requested = False
                notification.save(update_fields=["cancel_requested", "updated_at"])
            return
    if notification.outcome in gw.TERMINAL_OUTCOMES:
        notification.cancel_requested = False
        notification.save(update_fields=["cancel_requested", "updated_at"])
        return
    gateway = get_gateway()
    for attempt in range(IDEMPOTENT_ATTEMPTS):
        try:
            message = gateway.cancel(notification.provider_sid)
        except gw.ProviderRejected:
            # Usually: it is no longer cancellable (already sent). Record the
            # provider's actual state rather than assuming.
            try:
                _apply_provider_message(notification, gateway.fetch(notification.provider_sid))
            except gw.ProviderError:
                pass
            logger.warning("notification %s: provider refused cancellation (now %s)",
                           notification.pk, notification.provider_status)
            return
        except gw.ProviderConfigError:
            return
        except gw.ProviderError as e:
            logger.warning("notification %s: cancellation attempt %s failed (%s)",
                           notification.pk, attempt + 1, e.http_status)
            continue
        _apply_provider_message(notification, message)
        logger.info("notification %s cancelled at provider (%s)", notification.pk, message.status)
        return


# -- Reading state back ----------------------------------------------------------

def refresh(notifications, force=False):
    """
    Bring non-final notifications up to date by asking the provider (no
    callbacks can reach this app). Failures leave the stored state as is.
    """
    now = timezone.now()
    checked = 0
    for notification in notifications:
        if checked >= REFRESH_LIMIT:
            break
        if notification.outcome in gw.TERMINAL_OUTCOMES or notification.outcome == "not_sent":
            continue
        if not force and notification.last_checked_at and now - notification.last_checked_at < REFRESH_INTERVAL:
            continue
        checked += 1
        if notification.cancel_requested:
            _cancel_before_send(notification)
            continue
        if not notification.provider_sid:
            if notification.submit_state in (Notification.SUBMIT_UNKNOWN, Notification.SUBMIT_SENDING):
                _resolve_unknown(notification)
            continue
        try:
            message = get_gateway().fetch(notification.provider_sid)
        except gw.ProviderError as e:
            logger.warning("notification %s: refresh failed (%s)", notification.pk, e.http_status)
            continue
        _apply_provider_message(notification, message)


# -- Operator actions ---------------------------------------------------------------

def resend(notification, key, operator):
    """
    Re-send a message that did not reach the shopper, at most once per
    idempotency ``key``. Returns (resend_request, replayed).
    """
    existing = ResendRequest.objects.filter(key=key).first()
    if existing is not None:
        if existing.notification_id != notification.pk:
            raise ServiceError(422, "This idempotency key was already used for another notification.")
        if existing.state in (ResendRequest.STATE_DONE, ResendRequest.STATE_SENDING, ResendRequest.STATE_UNKNOWN):
            if existing.result is not None and existing.state != ResendRequest.STATE_DONE:
                refresh([existing.result], force=True)
                _settle_resend(existing)
            return existing, True
        # STATE_FAILED: the earlier attempt definitely did not go out, so a
        # new attempt under the same key is safe.

    refresh([notification], force=True)
    notification.refresh_from_db()
    if notification.outcome not in RESENDABLE_OUTCOMES:
        raise ServiceError(
            409, "Only a message that did not reach the shopper can be re-sent (this one is '%s')." % notification.outcome
        )
    order = notification.order
    if order.status == STATUS_CANCELLED and notification.kind != Notification.CANCELLED:
        raise ServiceError(409, "The order was cancelled; only its cancellation notice can be re-sent.")
    contact = notification.contact_number
    if contact is None or contact.deleted_at is not None:
        contact = active_number_for(notification.user)
    if contact is None:
        raise ServiceError(409, "The shopper has no contact number on file.")

    if existing is None:
        try:
            with transaction.atomic():
                existing = ResendRequest.objects.create(key=key, notification=notification, requested_by=operator)
        except IntegrityError:
            # The same key arrived concurrently: answer from that attempt.
            return ResendRequest.objects.get(key=key), True
    else:
        existing.state = ResendRequest.STATE_SENDING
        existing.save(update_fields=["state", "updated_at"])

    # A resent follow-up goes now; the reason to resend is that it failed.
    result = notify(order, notification.kind, resend_of=notification, contact=contact)
    existing.result = result
    _settle_resend(existing)
    return existing, False


def _settle_resend(resend_request):
    result = resend_request.result
    if result is None:
        resend_request.state = ResendRequest.STATE_FAILED
    elif result.submit_state == Notification.SUBMIT_ACCEPTED:
        resend_request.state = ResendRequest.STATE_DONE
    elif result.submit_state == Notification.SUBMIT_NOT_SENT:
        resend_request.state = ResendRequest.STATE_FAILED
    else:
        resend_request.state = ResendRequest.STATE_UNKNOWN
    resend_request.save(update_fields=["state", "result", "updated_at"])


def redact_content(notification):
    """
    Dispose of a message's text at the provider and here. The record of the
    message (identifier, status, timestamps) is kept.
    """
    if notification.content_redacted_at is not None:
        return notification
    if notification.provider_sid:
        refresh([notification], force=True)
        notification.refresh_from_db()
        if notification.outcome in (gw.OUTCOME_SCHEDULED, gw.OUTCOME_PENDING):
            raise ServiceError(
                409, "The message has not finished sending; cancel or wait for it before disposing of its content."
            )
        gateway = get_gateway()
        message = None
        last_error = None
        for _ in range(IDEMPOTENT_ATTEMPTS):
            try:
                message = gateway.redact(notification.provider_sid)
                break
            except (gw.ProviderRejected, gw.ProviderConfigError) as e:
                raise ServiceError(e.http_status if e.http_status != 404 else 409,
                                   "The messaging provider refused to remove the content: %s" % e)
            except gw.ProviderError as e:
                last_error = e
        if message is None:
            raise ServiceError(last_error.http_status, str(last_error))
        if message.body:
            raise ServiceError(502, "The messaging provider still returns the message text.")
        _apply_provider_message(notification, message)
    elif notification.submit_state in (Notification.SUBMIT_UNKNOWN, Notification.SUBMIT_SENDING):
        if _resolve_unknown(notification):
            return redact_content(notification)
        raise ServiceError(409, "The message's fate at the provider is not known yet; try again later.")
    notification.body = ""
    notification.content_redacted_at = timezone.now()
    notification.save(update_fields=["body", "content_redacted_at", "updated_at"])
    logger.info("notification %s content disposed of", notification.pk)
    return notification


def reconcile(start, end):
    """
    Line the provider's record of messages sent from TWILIO_FROM_NUMBER in
    [start, end] up against the notifications this app believes it sent.
    """
    provider = {m.sid: m for m in get_gateway().list_sent(start, end)}

    ours = {}
    candidates = Notification.objects.filter(provider_sid__isnull=False).filter(
        sent_at__gte=start, sent_at__lte=end
    ) | Notification.objects.filter(
        provider_sid__isnull=False, sent_at__isnull=True, submitted_at__gte=start, submitted_at__lte=end
    ).exclude(outcome__in=[gw.OUTCOME_SCHEDULED, gw.OUTCOME_CANCELED])
    for notification in candidates.select_related("order"):
        ours[notification.provider_sid] = notification
    # Anything the provider reports that we hold, whatever our dates say.
    for notification in Notification.objects.filter(provider_sid__in=list(provider)).select_related("order"):
        ours.setdefault(notification.provider_sid, notification)

    unresolved = Notification.objects.filter(
        provider_sid__isnull=True,
        submit_state__in=[Notification.SUBMIT_UNKNOWN, Notification.SUBMIT_SENDING],
        created_at__gte=start, created_at__lte=end,
    )

    matched, app_only, provider_only = [], [], []
    for sid, notification in ours.items():
        message = provider.get(sid)
        if message is None:
            app_only.append({"notification": notification})
        else:
            matched.append({"notification": notification, "message": message})
    for sid, message in provider.items():
        if sid not in ours:
            provider_only.append({"message": message})
    return {
        "matched": matched,
        "app_only": app_only,
        "provider_only": provider_only,
        "unresolved": list(unresolved),
        "provider_count": len(provider),
    }
