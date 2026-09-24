"""
Order SMS notifications: the Django side of the integration.

Every provider write that sends a message goes through ``deliver`` (the safe
write): a claim row committed before the call, one call carrying our reference,
a lookup by that reference when the outcome is unknown, and a record completed
from what the provider said. Notification failures never propagate into the
order operation that triggered them.
"""
import atexit
import hashlib
import logging
import threading
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_class, get_model
from twilio_sdk.core import ApiError

from . import twilio_gateway as tg
from .models import ContactNumber, Installation, Notification

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")
Selector = get_class("partner.strategy", "Selector")
Repository = get_class("shipping.repository", "Repository")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
OrderCreator = get_class("order.utils", "OrderCreator")
InvalidOrderStatus = get_class("order.exceptions", "InvalidOrderStatus")

DISPATCHED_STATUS = "Dispatched"
CANCELLED_STATUS = "Cancelled"

#: A claim still 'sending' after this long is stale: its sender is gone, so it is checked, never re-sent.
SEND_WINDOW = timedelta(minutes=2)
#: How often a non-final notification is re-read from the provider when someone looks at it.
REFRESH_INTERVAL = timedelta(seconds=10)
#: Upper bound on provider pages one reconciliation report may walk (1000 messages each).
RECONCILIATION_MAX_PAGES = 100

FINAL_OUTCOMES = (Notification.DONE, Notification.FAILED, Notification.NEEDS_REVIEW)


class ApiProblem(Exception):
    """An error our API answers with a given status and a message we wrote."""

    def __init__(self, status_code, message, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


class OutcomeUnknown(tg.ProviderError):
    def __init__(self, notification):
        super().__init__(504, "The provider may or may not have sent this message; it is being checked.",
                         outcome_unknown=True)
        self.notification = notification


# --------------------------------------------------------------------------
# Gateway lifetime
# --------------------------------------------------------------------------

_gateway = None
_gateway_lock = threading.Lock()


def gateway_config():
    return tg.GatewayConfig(
        account_sid=settings.TWILIO_ACCOUNT_SID,
        auth_token=settings.TWILIO_AUTH_TOKEN,
        from_number=settings.TWILIO_FROM_NUMBER,
        messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
        base_url=settings.TWILIO_BASE_URL or None,
        timeout=settings.ORDER_SMS_PROVIDER_TIMEOUT,
    )


def get_gateway():
    """One long-lived client per process, built lazily (so after any fork) and closed at exit."""
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                config = gateway_config()
                client = tg.build_client(config)
                atexit.register(client.close)
                _gateway = tg.Gateway(client, config)
    return _gateway


def set_gateway(gateway):
    """Replace the process gateway (tests inject one built on a stub transport)."""
    global _gateway
    _gateway = gateway


# --------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------


def install_id():
    configured = getattr(settings, "ORDER_SMS_INSTALL_ID", "")
    if configured:
        return configured
    row = Installation.objects.order_by("id").first()
    if row is None:
        try:
            with transaction.atomic():
                row = Installation.objects.create()
        except IntegrityError:
            row = Installation.objects.order_by("id").first()
    return str(row.install_id)


def reference_for(*parts):
    return ":".join([install_id(), *[str(p) for p in parts]])


def token_for(reference):
    return hashlib.sha256(reference.encode()).hexdigest()[:12]


def compose_body(text, token):
    return "%s Ref %s" % (text, token)


# --------------------------------------------------------------------------
# The safe write
# --------------------------------------------------------------------------


def _try_claim(fields):
    """Insert-or-fail on the unique reference. Returns (row, won)."""
    reference = fields["reference"]
    try:
        with transaction.atomic():
            return Notification.objects.create(outcome=Notification.SENDING, claimed_at=timezone.now(),
                                               **fields), True
    except IntegrityError:
        pass
    # A 'failed' with no provider SID released its claim: nothing happened, so it may be taken again,
    # atomically, and sent under the same reference.
    retaken = Notification.objects.filter(
        reference=reference, outcome=Notification.FAILED, provider_sid__isnull=True
    ).update(outcome=Notification.SENDING, claimed_at=timezone.now(), failure_reason="")
    row = Notification.objects.get(reference=reference)
    return row, retaken == 1


def _apply_view(n, view):
    n.provider_sid = view.sid
    n.provider_status = view.status or ""
    n.outcome = view.outcome
    n.error_code = view.error_code
    n.error_message = (view.error_message or "")[:255]
    n.provider_date_created = view.date_created or n.provider_date_created
    n.provider_date_sent = view.date_sent or n.provider_date_sent
    n.provider_time = n.provider_date_sent or n.provider_date_created
    n.last_checked_at = timezone.now()


def _complete(n, outcome=None, view=None, reason=""):
    if view is not None:
        _apply_view(n, view)
    if outcome is not None:
        n.outcome = outcome
    if reason:
        n.failure_reason = reason[:255]
    n.save()
    return n


def _destination(n):
    contact = n.contact_number
    if contact is None or contact.removed_at is not None:
        return None
    return contact.phone_number


def _check(n, gateway):
    """Settle an unknown send by looking it up by the reference token we sent. Never re-sends."""
    to = n.contact_number.phone_number if n.contact_number else None
    if to is None:
        return _complete(n, Notification.UNKNOWN, reason="destination removed before the outcome was known")
    try:
        found = gateway.find_by_token(to, n.ref_token, not_before=n.claimed_at - timedelta(hours=1))
    except (ApiError, httpx.RequestError, ValueError, tg.ProviderError):
        return _complete(n, Notification.UNKNOWN)
    if found is None:
        # An empty lookup cannot prove it did not happen: stays unknown, never failed.
        return _complete(n, Notification.UNKNOWN)
    return _complete(n, view=found)


def deliver(fields, send_at=None):
    """
    The ONE path for every message this app sends. ``fields`` must carry reference, ref_token, kind,
    order, user, contact_number and text. Returns the Notification row in whatever state is known.
    """
    n, won = _try_claim(fields)
    checking = False
    if not won:
        if n.outcome == Notification.SENDING and n.claimed_at > timezone.now() - SEND_WINDOW:
            return n  # in flight elsewhere: answer "in progress", no provider call
        if n.outcome not in (Notification.SENDING, Notification.UNKNOWN):
            return n  # settled: answer from the record
        checking = True  # stale sender, or unresolved: look, never create

    try:
        gateway = get_gateway()
    except tg.TwilioNotConfigured as exc:
        if checking:
            return n
        return _complete(n, Notification.FAILED, reason=exc.message)

    if checking:
        return _check(n, gateway)

    to = _destination(n)
    if to is None:
        return _complete(n, Notification.FAILED, reason="no active contact number")
    try:
        view = gateway.create_message(to, compose_body(n.text, n.ref_token), n.ref_token, send_at=send_at)
    except tg.TwilioNotConfigured as exc:
        return _complete(n, Notification.FAILED, reason=exc.message)
    except tg.NEVER_SENT as exc:
        return _complete(n, Notification.FAILED, reason="never sent: %s" % type(exc).__name__)
    except ApiError as exc:
        code, message = tg.provider_error_details(exc)
        if exc.status_code < 500:
            # A verdict on the request itself: refused, nothing was created. Claim released (no SID).
            n.error_code = code
            n.error_message = message[:255]
            return _complete(n, Notification.FAILED, reason="provider refused (HTTP %s)" % exc.status_code)
        # A 5xx on a write may still have landed: check below.
    except (httpx.RequestError, ValueError):
        pass  # sent, and no readable answer: may have landed
    else:
        return _complete(n, view=view)
    return _check(n, gateway)


# --------------------------------------------------------------------------
# Contact numbers
# --------------------------------------------------------------------------


def active_numbers(user):
    return ContactNumber.objects.filter(user=user, removed_at__isnull=True)


def primary_number(user):
    return active_numbers(user).order_by("-created_at", "-id").first()


def register_number(user, raw_number, country_code=None):
    raw_number = (raw_number or "").strip()
    if not raw_number or len(raw_number) > 32:
        raise ApiProblem(400, "phoneNumber is required.")
    try:
        canonical, country = get_gateway().lookup_number(raw_number, country_code or None)
    except tg.NumberNotUsable:
        raise ApiProblem(422, "The SMS provider does not recognise this as a valid phone number.")
    except Exception as exc:  # noqa: BLE001 -- translated by the one ladder
        raise tg.translate(exc, is_write=False) from exc
    try:
        with transaction.atomic():
            return ContactNumber.objects.create(user=user, phone_number=canonical, country_code=country or ""), True
    except IntegrityError:
        return active_numbers(user).get(phone_number=canonical), False


def remove_number(user, contact_number_id):
    """Remove a number and call off anything still queued for it. Repeating it retries the call-offs."""
    contact = ContactNumber.objects.filter(user=user, pk=contact_number_id).first()
    if contact is None:
        raise ApiProblem(404, "Contact number not found.")
    already_removed = contact.removed_at is not None
    if not already_removed:
        contact.removed_at = timezone.now()
        contact.save(update_fields=["removed_at"])
    queued = Notification.objects.filter(contact_number=contact, kind=Notification.FOLLOWUP).exclude(
        cancel_outcome=Notification.DONE).exclude(outcome__in=FINAL_OUTCOMES)
    if already_removed and not queued.exists():
        raise ApiProblem(404, "Contact number not found.")
    unresolved = [n.pk for n in queued if call_off(n) != Notification.DONE]
    if unresolved:
        raise ApiProblem(502, "The number was removed, but a scheduled message to it could not be confirmed "
                              "as cancelled yet. Repeat the request to retry.", notificationIds=unresolved)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

TEXTS = {
    Notification.PLACED: "Thanks for your order %s! We'll text you when it ships.",
    Notification.DISPATCHED: "Good news: your order %s is on its way.",
    Notification.FOLLOWUP: "How did the delivery of your order %s go? Reply and let us know.",
    Notification.CANCELLED: "Your order %s has been cancelled.",
}


def notify(order, kind, send_at=None):
    """Send one order notification. Never raises: a message that cannot be sent never fails the order."""
    reference = None
    try:
        reference = reference_for(kind, order.pk)
        existing = Notification.objects.filter(reference=reference).select_related("contact_number").first()
        contact = existing.contact_number if existing else primary_number(order.user)
        if contact is None:
            return None  # no number on file: simply not messaged
        fields = dict(reference=reference, ref_token=token_for(reference), kind=kind, order=order,
                      user=order.user, contact_number=contact, text=TEXTS[kind] % order.number,
                      scheduled_for=send_at)
        return deliver(fields, send_at=send_at)
    except Exception as exc:  # noqa: BLE001 -- the order must still succeed
        logger.error("order %s: %s notification could not be sent (%s)", order.pk, kind, type(exc).__name__)
        return Notification.objects.filter(reference=reference).first() if reference else None


def place_order(user, request, lines):
    if not isinstance(lines, list) or not lines:
        raise ApiProblem(400, "lines must be a non-empty list of {productId, quantity}.")
    wanted = []
    for line in lines:
        if not isinstance(line, dict):
            raise ApiProblem(400, "Each line must be an object with productId and quantity.")
        product_id, quantity = line.get("productId"), line.get("quantity", 1)
        if not isinstance(product_id, int) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
            raise ApiProblem(400, "productId must be an integer and quantity an integer from 1 to 100.")
        wanted.append((product_id, quantity))

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for product_id, quantity in wanted:
            product = Product.objects.filter(pk=product_id).first()
            if product is None:
                raise ApiProblem(404, "Product %s not found." % product_id)
            info = basket.strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not product.is_public or not permitted:
                raise ApiProblem(409, "Product %s cannot be bought: %s" % (product_id, reason or "unavailable"))
            basket.add_product(product, quantity)
        shipping_method = Repository().get_default_shipping_method(basket=basket, user=user, request=request)
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        order = OrderCreator().place_order(basket=basket, total=total, shipping_method=shipping_method,
                                           shipping_charge=shipping_charge, user=user, request=request)
        basket.submit()
    # Committed: the order exists whatever happens to the message.
    return order, notify(order, Notification.PLACED)


def _operator_order(order_id):
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        raise ApiProblem(404, "Order not found.")
    return order


def _transition(order, new_status):
    if order.status == new_status:
        raise ApiProblem(409, "Order is already %s." % new_status)
    try:
        with transaction.atomic():
            order.set_status(new_status)
    except InvalidOrderStatus:
        raise ApiProblem(409, "An order in status '%s' cannot become '%s'." % (order.status, new_status))


def dispatch_order(order_id):
    order = _operator_order(order_id)
    _transition(order, DISPATCHED_STATUS)
    dispatched = notify(order, Notification.DISPATCHED)
    followup = notify(order, Notification.FOLLOWUP,
                      send_at=timezone.now() + timedelta(hours=settings.ORDER_SMS_FOLLOWUP_DELAY_HOURS))
    # A cancel may have committed while the follow-up was being queued: whichever side sees the
    # other calls it off, so a cancelled order never keeps a queued follow-up.
    order.refresh_from_db()
    if followup is not None and order.status == CANCELLED_STATUS:
        followup.refresh_from_db()
        call_off(followup)
    return order, dispatched, followup


def cancel_order(order_id):
    order = _operator_order(order_id)
    _transition(order, CANCELLED_STATUS)
    followup = Notification.objects.filter(order=order, kind=Notification.FOLLOWUP).first()
    if followup is not None:
        call_off(followup)
        followup.refresh_from_db()
    cancelled = notify(order, Notification.CANCELLED)
    return order, cancelled, followup


def _cancel_view_outcome(view):
    return tg.cancel_outcome_from_provider(view.status)


def call_off(n):
    """
    Call off a scheduled follow-up at the provider. Setting status=canceled is harmless to repeat,
    so it takes no claim; its outcome is read from the message status, and re-read when unclear.
    Returns the cancel outcome.
    """
    if n.cancel_outcome == Notification.DONE:
        return Notification.DONE
    if n.cancel_requested_at is None:
        n.cancel_requested_at = timezone.now()
        n.save(update_fields=["cancel_requested_at"])
    try:
        gateway = get_gateway()
    except tg.TwilioNotConfigured:
        return _set_cancel(n, Notification.UNKNOWN)

    if not n.provider_sid:
        if n.outcome == Notification.FAILED:
            return _set_cancel(n, Notification.DONE)  # never created at the provider: nothing to call off
        if n.outcome == Notification.SENDING and n.claimed_at > timezone.now() - SEND_WINDOW:
            return _set_cancel(n, Notification.UNKNOWN)  # the sender will see the cancelled order itself
        _check(n, gateway)
        if not n.provider_sid:
            return _set_cancel(n, Notification.UNKNOWN)

    try:
        view = gateway.cancel_message(n.provider_sid)
    except (ApiError, httpx.RequestError, ValueError):
        # Refused (e.g. already sent) or no readable answer: the message's own status settles it.
        try:
            view = gateway.fetch_message(n.provider_sid)
        except (ApiError, httpx.RequestError, ValueError):
            return _set_cancel(n, Notification.UNKNOWN)
    _apply_view(n, view)
    outcome = _cancel_view_outcome(view)
    if outcome == Notification.FAILED:
        logger.warning("notification %s: follow-up could not be called off (status %s)", n.pk, view.status)
    return _set_cancel(n, outcome)


def _set_cancel(n, outcome):
    n.cancel_outcome = outcome
    n.save()
    return outcome


# --------------------------------------------------------------------------
# Reading back what became of a message
# --------------------------------------------------------------------------


def refresh(n, force=False):
    """Re-read a non-final notification from the provider (a read: safe to repeat). Never raises."""
    now = timezone.now()
    followup_needs_call_off = (
        n.kind == Notification.FOLLOWUP and n.order.status == CANCELLED_STATUS
        and n.cancel_outcome != Notification.DONE and n.outcome not in FINAL_OUTCOMES
    )
    if n.outcome in FINAL_OUTCOMES and not followup_needs_call_off:
        return n
    if not force and n.last_checked_at and now - n.last_checked_at < REFRESH_INTERVAL:
        return n
    if n.outcome == Notification.SENDING and n.claimed_at > now - SEND_WINDOW:
        return n
    try:
        gateway = get_gateway()
        if followup_needs_call_off:
            call_off(n)
        elif n.provider_sid:
            _complete(n, view=gateway.fetch_message(n.provider_sid))
        else:
            _check(n, gateway)
    except Exception as exc:  # noqa: BLE001 -- a failed refresh leaves the stored state as it was
        logger.warning("notification %s: refresh failed (%s)", n.pk, type(exc).__name__)
    return n


# --------------------------------------------------------------------------
# Operator actions
# --------------------------------------------------------------------------


def resend(notification_id, idempotency_key):
    if not idempotency_key or len(idempotency_key) > 200:
        raise ApiProblem(400, "An Idempotency-Key header (1-200 characters) is required.")
    original = Notification.objects.select_related("order", "contact_number").filter(pk=notification_id).first()
    if original is None:
        raise ApiProblem(404, "Notification not found.")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
    reference = reference_for("resend", original.pk, key_hash)

    existing = Notification.objects.filter(reference=reference).first()
    if existing is None:
        refresh(original, force=True)
        if original.outcome != Notification.FAILED:
            raise ApiProblem(409, "Only a message that did not reach the shopper can be re-sent "
                                  "(this one is '%s')." % original.outcome)
        if original.provider_status == "canceled" or original.cancel_requested_at:
            raise ApiProblem(409, "This message was deliberately called off and must not be re-sent.")
        if original.order.status == CANCELLED_STATUS and original.kind in (
                Notification.DISPATCHED, Notification.FOLLOWUP):
            raise ApiProblem(409, "The order was cancelled; this message no longer applies.")
        if not original.text or original.content_disposed_at or original.content_disposal_requested_at:
            raise ApiProblem(409, "The content of this message was disposed of; it cannot be re-sent.")
        if _destination(original) is None:
            raise ApiProblem(409, "The shopper's number was removed; nothing may be sent to it.")

    fields = dict(reference=reference, ref_token=token_for(reference), kind=Notification.RESEND,
                  order=original.order, user=original.user, contact_number=original.contact_number,
                  resend_of=original, text=original.text)
    return deliver(fields), existing is not None


def dispose_content(notification_id):
    n = Notification.objects.filter(pk=notification_id).first()
    if n is None:
        raise ApiProblem(404, "Notification not found.")
    if n.content_disposed_at:
        return n
    if n.content_disposal_requested_at is None:
        n.content_disposal_requested_at = timezone.now()
        n.save(update_fields=["content_disposal_requested_at"])

    if not n.provider_sid and n.outcome not in (Notification.SENDING, Notification.UNKNOWN):
        # Never created at the provider (refused or never sent): only our own copy exists.
        n.text = ""
        n.content_disposed_at = timezone.now()
        n.save()
        return n
    try:
        gateway = get_gateway()
    except tg.TwilioNotConfigured as exc:
        raise ApiProblem(exc.status_code, exc.message)
    if not n.provider_sid:
        if n.outcome == Notification.SENDING and n.claimed_at > timezone.now() - SEND_WINDOW:
            raise ApiProblem(409, "This message is still being sent; try again shortly.")
        _check(n, gateway)
        if not n.provider_sid:
            raise ApiProblem(409, "It is not yet known whether the provider holds this message; the content "
                                  "is kept until that is settled. Try again later.")

    if n.provider_sid:  # always true here; kept explicit for readers
        try:
            view = gateway.redact_message(n.provider_sid)
        except ApiError as exc:
            if exc.status_code >= 500:
                view = _reread(gateway, n)
            else:
                view = _reread(gateway, n)
                if view is None or view.body != "":
                    _, message = tg.provider_error_details(exc)
                    raise ApiProblem(409, "The provider refused to redact this message%s." % (
                        ": " + message if message else ""))
        except (httpx.RequestError, ValueError):
            view = _reread(gateway, n)
        if view is None or view.body != "":
            raise ApiProblem(504 if view is None else 502,
                             "Redaction at the provider could not be confirmed; repeat the request.")
        _apply_view(n, view)

    n.text = ""
    n.content_disposed_at = timezone.now()
    n.save()
    return n


def _reread(gateway, n):
    try:
        return gateway.fetch_message(n.provider_sid)
    except (ApiError, httpx.RequestError, ValueError):
        return None


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def reconcile(start, end):
    if end <= start:
        raise ApiProblem(400, "'to' must be after 'from'.")
    if end - start > timedelta(days=31):
        raise ApiProblem(400, "The range may span at most 31 days.")
    try:
        gateway = get_gateway()
        # Ask the provider for our sending number's messages only, over a window widened by a day each
        # side, then narrow it back to the caller's instants on the provider's own event time.
        fetched = gateway.list_sent_between(start - timedelta(days=1), end + timedelta(days=1),
                                            max_pages=RECONCILIATION_MAX_PAGES)
    except tg.PageCapExceeded:
        raise ApiProblem(422, "The provider holds too many messages in this range; request a shorter one.")
    except Exception as exc:  # noqa: BLE001 -- translated by the one ladder
        raise tg.translate(exc, is_write=False) from exc

    # Only sends are reconciled: an inbound record From our number (a destination routing back into this
    # account) is not something we sent. It is counted, not matched.
    inbound_ignored = sum(1 for v in fetched if not v.outbound and v.provider_time and start <= v.provider_time < end)
    fetched = [v for v in fetched if v.outbound]
    provider = {v.sid: v for v in fetched if v.provider_time and start <= v.provider_time < end}
    by_sid_everywhere = {v.sid: v for v in fetched}

    candidates = Notification.objects.filter(provider_sid__isnull=False).filter(
        provider_time__gte=start - timedelta(days=1), provider_time__lt=end + timedelta(days=1)
    ).select_related("order")
    matched, local_only = [], []
    for n in candidates:
        seen = by_sid_everywhere.get(n.provider_sid)
        when = seen.provider_time if seen else n.provider_time
        if not (when and start <= when < end):
            continue
        if seen is not None:
            if seen.status != n.provider_status or seen.outcome != n.outcome:
                _complete(n, view=seen)  # the provider is authoritative for what happened
            if n.provider_sid in provider:
                del provider[n.provider_sid]
                matched.append((n, seen))
                continue
        local_only.append(n)

    unsettled = Notification.objects.filter(provider_sid__isnull=True, claimed_at__gte=start,
                                            claimed_at__lt=end).exclude(outcome=Notification.FAILED)
    not_sent = Notification.objects.filter(provider_sid__isnull=True, claimed_at__gte=start,
                                           claimed_at__lt=end, outcome=Notification.FAILED)
    return {
        "matched": matched,
        "providerOnly": sorted(provider.values(), key=lambda v: v.provider_time),
        "localOnly": local_only,
        "unsettled": list(unsettled),
        "notSent": list(not_sent),
        "inboundIgnored": inbound_ignored,
    }
