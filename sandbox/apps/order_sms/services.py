"""
Order SMS notifications: what gets sent, when, and what became of it.

Every provider write that sends a message goes through ``_safe_send``: a
claim row is inserted first (its unique ``reference`` is what rejects a second
send of the same message, in any process), the provider is called, and a send
whose outcome is unknown is settled by looking the message up by the
reference it carries - never by sending it again.
"""

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.core.loading import get_model
from twilio_sdk.core import ApiError

from . import provider
from .models import ContactNumber, Notification

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")

STATUS_DISPATCHED = "Dispatched"
STATUS_CANCELLED = "Cancelled"

# A claim still "sending" after this long has lost its sender; the next
# request checks the provider instead of waiting on it.
SEND_WINDOW = timedelta(minutes=2)
# How often a read may re-ask the provider about an unsettled message.
REFRESH_INTERVAL = timedelta(seconds=15)


class NotificationConflict(Exception):
    """The request cannot be carried out in the notification's current state."""


# Contact numbers -----------------------------------------------------------


def active_numbers(user):
    return ContactNumber.objects.filter(user=user, deleted_at__isnull=True)


def current_number(user):
    """The number a shopper is messaged on: their most recently registered one."""
    return active_numbers(user).order_by("-created_at", "-id").first()


def register_number(user, raw):
    """
    Validate ``raw`` with the provider and store its canonical form. Raises
    ``provider.NumberRejected`` for a number the provider will not accept.
    """
    e164, country = provider.lookup_number(raw)
    try:
        with transaction.atomic():
            return ContactNumber.objects.create(user=user, e164=e164, country_code=country), True
    except IntegrityError:
        # Already registered by this shopper: registering it again is a no-op.
        return active_numbers(user).get(e164=e164), False


def remove_number(contact):
    """
    Remove a shopper's number. Anything still queued with the provider for it
    is called off, so nothing reaches it afterwards.
    """
    ContactNumber.objects.filter(pk=contact.pk, deleted_at__isnull=True).update(
        deleted_at=timezone.now())
    queued = Notification.objects.filter(
        contact_number=contact, kind=Notification.KIND_FOLLOWUP
    ).exclude(cancel_state=Notification.CANCEL_DONE)
    for notification in queued:
        _call_off_quietly(notification)


# Message text --------------------------------------------------------------


def _order_text(order, kind):
    shop = "Oscar Sandbox"
    if kind == Notification.KIND_PLACED:
        return "%s: thanks! Your order %s has been placed." % (shop, order.number)
    if kind == Notification.KIND_DISPATCHED:
        return "%s: your order %s is on its way." % (shop, order.number)
    if kind == Notification.KIND_FOLLOWUP:
        return "%s: how did the delivery of order %s go? Reply to let us know." % (
            shop, order.number)
    if kind == Notification.KIND_CANCELLED:
        return "%s: your order %s has been cancelled." % (shop, order.number)
    raise ValueError("unknown notification kind %r" % kind)


def _message_body(order, kind, reference):
    # The reference token is how a send with an unknown outcome is found again.
    return "%s Ref %s" % (_order_text(order, kind), provider.reference_token(reference))


def _event_reference(order, kind):
    return "%s:order:%s:%s" % (settings.ORDER_SMS_REFERENCE_PREFIX, order.number, kind)


def _resend_reference(original, idempotency_key):
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
    return "%s:resend:%s:%s" % (settings.ORDER_SMS_REFERENCE_PREFIX, original.pk, key_hash)


# The safe write ------------------------------------------------------------


def _claim(**fields):
    """
    Insert the claim row. Returns ``(notification, True)`` when this request
    holds the claim, or ``(existing, False)`` when another request already
    did - the database's unique constraint decides, not a prior read.
    """
    try:
        with transaction.atomic():
            return Notification.objects.create(
                outcome=Notification.OUTCOME_SENDING, claimed_at=timezone.now(), **fields
            ), True
    except IntegrityError:
        return Notification.objects.get(reference=fields["reference"]), False


def _take_over_released(notification):
    """
    A claim recorded ``failed`` with no provider message (never sent, or
    refused) is released: the next request may take it again - atomically.
    """
    taken = Notification.objects.filter(
        pk=notification.pk,
        outcome=Notification.OUTCOME_FAILED,
        provider_sid__isnull=True,
    ).update(outcome=Notification.OUTCOME_SENDING, claimed_at=timezone.now(), failure_reason="")
    return taken == 1


def _record_state(notification, state, **extra):
    """Store what the provider said about the message."""
    notification.provider_sid = state.sid
    notification.provider_status = state.status
    notification.provider_error_code = state.error_code
    notification.provider_time = state.provider_time
    notification.outcome = state.outcome
    notification.last_checked_at = timezone.now()
    for name, value in extra.items():
        setattr(notification, name, value)
    if notification.outcome == Notification.OUTCOME_FAILED and not notification.failure_reason:
        if state.error_message:
            notification.failure_reason = state.error_message[:255]
        elif state.error_code:
            notification.failure_reason = "Not delivered (provider error %s)." % state.error_code
        elif state.status == "canceled":
            notification.failure_reason = "Called off before it was sent."
    notification.save()
    return notification


def _complete(notification, outcome, reason=""):
    notification.outcome = outcome
    notification.failure_reason = reason[:255]
    notification.save(update_fields=["outcome", "failure_reason"])
    return notification


def _settle_by_lookup(notification):
    """
    The send may have landed: ask the provider for the message carrying this
    notification's reference. Only the provider's answer settles it; anything
    else leaves it unknown under the same reference.
    """
    try:
        state = provider.find_by_reference(
            notification.destination,
            provider.reference_token(notification.reference),
            notification.claimed_at - timedelta(days=1),
        )
    except (ApiError, httpx.RequestError, ValueError, provider.ProviderError):
        logger.warning("SMS notification %s: lookup failed; outcome unknown", notification.pk)
        return _complete(notification, Notification.OUTCOME_UNKNOWN,
                         "Outcome unknown: the provider could not be checked.")
    if state is None:
        return _complete(notification, Notification.OUTCOME_UNKNOWN,
                         "Outcome unknown: the provider has no record of it yet.")
    return _record_state(notification, state, failure_reason="")


def _send_claimed(notification):
    """Make the provider call for a claim this request holds."""
    try:
        state = provider.create_message(
            notification.destination, notification.body, send_at=notification.scheduled_for)
    except provider.NEVER_SENT:
        # Never left: nothing happened. Recording failed with no provider
        # message releases the claim for a later attempt.
        return _complete(notification, Notification.OUTCOME_FAILED,
                         "The provider could not be reached; not sent.")
    except provider.ProviderConfigError as exc:
        return _complete(notification, Notification.OUTCOME_FAILED, exc.message)
    except ApiError as exc:
        if exc.status_code < 500:
            code = provider.provider_error_code(exc.error)
            reason = "Refused by the provider (HTTP %s%s)." % (
                exc.status_code, ", code %s" % code if code else "")
            return _complete(notification, Notification.OUTCOME_FAILED, reason)
        # A 5xx on a create may still have landed: check.
        return _settle_by_lookup(notification)
    except (httpx.RequestError, ValueError, provider.ProviderUnreadable):
        # Sent, and no readable answer: may have landed.
        return _settle_by_lookup(notification)
    return _record_state(notification, state, failure_reason="")


def _safe_send(claim_fields):
    """
    Send one message at most once. Returns ``(notification, sent_now)``; the
    notification's outcome says what is known: sending, pending, done,
    failed or unknown.
    """
    notification, claimed = _claim(**claim_fields)
    if claimed:
        return _send_claimed(notification), True
    return _answer_repeat(notification), False


def _answer_repeat(notification):
    """
    Another request holds (or held) this claim. Answer from the stored record;
    the only provider calls are checks - never a fresh send, except re-taking
    a claim that was released because nothing was sent.
    """
    if notification.outcome == Notification.OUTCOME_SENDING:
        if notification.claimed_at > timezone.now() - SEND_WINDOW:
            return notification  # in flight elsewhere: answer "in progress"
        return _settle_by_lookup(notification)
    if notification.outcome == Notification.OUTCOME_UNKNOWN and not notification.provider_sid:
        return _settle_by_lookup(notification)
    if (notification.outcome == Notification.OUTCOME_FAILED
            and not notification.provider_sid
            and _take_over_released(notification)):
        notification.refresh_from_db()
        return _send_claimed(notification)
    return refresh(notification)


def notify_order_event(order, kind, *, send_at=None):
    """
    Tell the order's shopper about an order event. Never raises: a message
    that cannot be sent must not fail the order operation. Returns the
    notification, or None when the shopper has no number on file.
    """
    try:
        contact = current_number(order.user) if order.user_id else None
        if contact is None:
            return None
        reference = _event_reference(order, kind)
        notification, _ = _safe_send({
            "order": order,
            "user": order.user,
            "contact_number": contact,
            "kind": kind,
            "reference": reference,
            "destination": contact.e164,
            "body": _message_body(order, kind, reference),
            "scheduled_for": send_at,
        })
        return notification
    except Exception:  # noqa: BLE001 - the order operation must still succeed
        logger.exception("SMS %s notification for order %s could not be sent", kind, order.pk)
        Notification.objects.filter(
            reference=_event_reference(order, kind), outcome=Notification.OUTCOME_SENDING,
        ).update(outcome=Notification.OUTCOME_UNKNOWN,
                 failure_reason="Outcome unknown: an internal error interrupted the send.")
        return Notification.objects.filter(reference=_event_reference(order, kind)).first()


# Order transitions ---------------------------------------------------------


class OrderStateConflict(Exception):
    pass


def dispatch_order(order):
    """Mark an order dispatched, tell the shopper, queue the follow-up."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status == STATUS_CANCELLED:
            raise OrderStateConflict("Order %s is cancelled." % order.number)
        if order.status != STATUS_DISPATCHED:
            if STATUS_DISPATCHED not in order.available_statuses():
                raise OrderStateConflict(
                    "Order %s cannot be dispatched from status '%s'." % (order.number, order.status))
            order.set_status(STATUS_DISPATCHED)

    notify_order_event(order, Notification.KIND_DISPATCHED)

    send_at = timezone.now() + timedelta(hours=settings.ORDER_SMS_FOLLOWUP_DELAY_HOURS)
    followup = notify_order_event(order, Notification.KIND_FOLLOWUP, send_at=send_at)

    # A cancel may have landed while the follow-up was being queued; it could
    # not call off a message whose id it did not know yet, so do it here.
    order.refresh_from_db()
    if followup is not None:
        followup.refresh_from_db()
        if order.status == STATUS_CANCELLED or followup.cancel_requested_at:
            _call_off_quietly(followup)
    return order


def cancel_order(order):
    """
    Cancel an order: call off any queued follow-up first, then tell the
    shopper. Repeating it retries a call-off that has not been confirmed.
    """
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status != STATUS_CANCELLED:
            if STATUS_CANCELLED not in order.available_statuses():
                raise OrderStateConflict(
                    "Order %s cannot be cancelled from status '%s'." % (order.number, order.status))
            order.set_status(STATUS_CANCELLED)
        Notification.objects.filter(
            order=order, kind=Notification.KIND_FOLLOWUP, cancel_requested_at__isnull=True
        ).update(cancel_requested_at=timezone.now())

    for followup in Notification.objects.filter(order=order, kind=Notification.KIND_FOLLOWUP):
        _call_off_quietly(followup)

    notify_order_event(order, Notification.KIND_CANCELLED)
    return order


def _call_off_quietly(notification):
    try:
        call_off(notification)
    except Exception:  # noqa: BLE001 - recorded on the notification, retried on the next cancel
        logger.exception("SMS notification %s: call-off failed", notification.pk)
        Notification.objects.filter(pk=notification.pk).exclude(
            cancel_state=Notification.CANCEL_DONE
        ).update(cancel_state=Notification.CANCEL_PENDING)


def call_off(notification):
    """Make sure a queued message never goes out, and record whether it worked."""
    if notification.cancel_state == Notification.CANCEL_DONE:
        return notification
    if not notification.cancel_requested_at:
        notification.cancel_requested_at = timezone.now()
        notification.save(update_fields=["cancel_requested_at"])

    if not notification.provider_sid:
        if notification.outcome == Notification.OUTCOME_SENDING and (
                notification.claimed_at > timezone.now() - SEND_WINDOW):
            # Still being queued by another request, which checks
            # cancel_requested_at once it has the message id.
            notification.cancel_state = Notification.CANCEL_PENDING
            notification.save(update_fields=["cancel_state"])
            return notification
        if notification.outcome in (Notification.OUTCOME_SENDING, Notification.OUTCOME_UNKNOWN):
            _settle_by_lookup(notification)
        if not notification.provider_sid:
            if notification.outcome == Notification.OUTCOME_FAILED:
                # Never queued with the provider: nothing can go out.
                notification.cancel_state = Notification.CANCEL_DONE
            else:
                notification.cancel_state = Notification.CANCEL_PENDING
            notification.save(update_fields=["cancel_state"])
            return notification

    try:
        state = provider.cancel_message(notification.provider_sid)
    except ApiError as exc:
        if exc.status_code >= 500 or exc.status_code == 429:
            state = None
        else:
            # Typically "not cancelable": read where it got to.
            state = provider.fetch_message(notification.provider_sid)
    except (httpx.RequestError, ValueError, provider.ProviderUnreadable):
        state = None
    if state is None:
        # Unknown whether the cancel landed: re-read the message.
        try:
            state = provider.fetch_message(notification.provider_sid)
        except (ApiError, httpx.RequestError, ValueError, provider.ProviderError):
            state = None
    if state is None:
        notification.cancel_state = Notification.CANCEL_PENDING
        notification.save(update_fields=["cancel_state"])
        return notification

    result = provider.cancel_outcome(state.status)
    cancel_state = {
        provider.DONE: Notification.CANCEL_DONE,
        provider.FAILED: Notification.CANCEL_FAILED,
    }.get(result, Notification.CANCEL_PENDING)
    if cancel_state == Notification.CANCEL_FAILED:
        logger.error("SMS notification %s: too late to call off (status %s)",
                     notification.pk, state.status)
    return _record_state(notification, state, cancel_state=cancel_state)


# Operator actions ----------------------------------------------------------

RESENDABLE_OUTCOMES = (Notification.OUTCOME_FAILED, Notification.OUTCOME_UNKNOWN)


def resend(original, idempotency_key):
    """
    Re-send a message that did not reach the shopper. The same key always
    answers with the same resend and never sends twice; a new key is a
    deliberate new attempt. Returns ``(notification, sent_now)``.
    """
    reference = _resend_reference(original, idempotency_key)
    existing = Notification.objects.filter(reference=reference).first()
    if existing is not None:
        return _answer_repeat(existing), False

    refresh(original)
    if original.outcome not in RESENDABLE_OUTCOMES:
        raise NotificationConflict(
            "Only a message that did not reach the shopper can be re-sent "
            "(this one is '%s')." % original.outcome)
    if original.kind == Notification.KIND_FOLLOWUP and original.order.status == STATUS_CANCELLED:
        raise NotificationConflict("The order was cancelled; its follow-up is not re-sent.")
    contact = current_number(original.user)
    if contact is None:
        raise NotificationConflict("The shopper has no mobile number on file.")
    return _safe_send({
        "order": original.order,
        "user": original.user,
        "contact_number": contact,
        "kind": original.kind,
        "resend_of": original,
        "idempotency_key": idempotency_key,
        "reference": reference,
        "destination": contact.e164,
        "body": _message_body(original.order, original.kind, reference),
    })


def dispose_content(notification):
    """
    Erase a message's text here and at the provider, keeping the record that
    it was sent and what became of it.
    """
    if notification.content_disposed_at:
        return notification
    if not notification.provider_sid and notification.outcome in (
            Notification.OUTCOME_SENDING, Notification.OUTCOME_UNKNOWN):
        _settle_by_lookup(notification)
    if not notification.provider_sid:
        if notification.outcome != Notification.OUTCOME_FAILED:
            raise NotificationConflict(
                "The provider's copy of this message cannot be confirmed yet; try again later.")
        # The provider never took this message: only our copy exists.
        return _clear_local_content(notification)

    notification.content_disposal_requested_at = timezone.now()
    notification.save(update_fields=["content_disposal_requested_at"])
    try:
        state = provider.redact_message(notification.provider_sid)
    except ApiError as exc:
        if 400 <= exc.status_code < 500 and exc.status_code != 429:
            current = provider.fetch_message(notification.provider_sid)
            if current is not None and current.body == "":
                return _clear_local_content(notification, current)
            raise NotificationConflict(
                "The provider cannot erase this message yet (status '%s'); a message "
                "is erasable once it has finished sending." % (
                    current.status if current else "unknown")) from exc
        state = None
    except (httpx.RequestError, ValueError, provider.ProviderUnreadable):
        state = None
    if state is None:
        state = provider.fetch_message(notification.provider_sid)
    if state is not None and state.body == "":
        return _clear_local_content(notification, state)  # done: the provider echoes no text
    if state is not None and state.body:
        # failed: the provider answered and still holds the text.
        raise provider.ProviderUnavailable(
            502, "The provider did not erase the message text.", outcome_unknown=False)
    # unknown: no readable answer about the text; ours is kept until confirmed.
    raise provider.ProviderUnavailable(
        502, "The provider did not confirm the message text was erased.", outcome_unknown=True)


def _clear_local_content(notification, state=None):
    notification.body = ""
    notification.content_disposed_at = timezone.now()
    if state is not None:
        return _record_state(notification, state)
    notification.save(update_fields=["body", "content_disposed_at"])
    return notification


# Reading back --------------------------------------------------------------

SETTLED = (Notification.OUTCOME_DONE, Notification.OUTCOME_FAILED)


def refresh(notification, *, force=False):
    """
    Ask the provider where an unsettled message got to (there is no webhook
    to tell us). Failures leave the stored state as it was.
    """
    if not notification.provider_sid:
        if notification.outcome == Notification.OUTCOME_SENDING and (
                notification.claimed_at <= timezone.now() - SEND_WINDOW):
            return _settle_by_lookup(notification)
        return notification
    if notification.outcome in SETTLED and not force:
        return notification
    if (not force and notification.last_checked_at
            and notification.last_checked_at > timezone.now() - REFRESH_INTERVAL):
        return notification
    try:
        state = provider.fetch_message(notification.provider_sid)
    except (ApiError, httpx.RequestError, ValueError, provider.ProviderError):
        logger.warning("SMS notification %s: status refresh failed", notification.pk)
        return notification
    if state is None:
        return notification
    return _record_state(notification, state)


def refresh_all(notifications):
    return [refresh(n) for n in notifications]


# Reconciliation ------------------------------------------------------------


def reconcile(start: datetime, end: datetime):
    """
    Line up the provider's record of messages sent from our number in
    [start, end) against what this app recorded, on the provider's clock.
    """
    provider_records: dict[str, provider.MessageState] = {}
    inbound_ignored = 0
    state: provider.MessageState | None
    for state in provider.list_sent_from_our_number(start, end):
        when = state.provider_time
        if when is None or not (start <= when < end):
            continue  # the day-granular filter over-fetches: narrow back
        if state.inbound:
            inbound_ignored += 1  # a receiving copy, not something we sent
            continue
        provider_records[state.sid] = state

    local = list(Notification.objects.filter(
        provider_time__gte=start, provider_time__lt=end, provider_sid__isnull=False))
    unsettled = list(Notification.objects.filter(
        provider_sid__isnull=True, claimed_at__gte=start, claimed_at__lt=end))

    matched: list[dict[str, Any]] = []
    app_only: list[dict[str, Any]] = []
    for notification in local:
        state = provider_records.pop(
            str(notification.provider_sid), None)
        if state is None:
            # Not listed for these dates: a message cancelled before sending
            # has no sent date. Ask for it directly before calling it missing.
            try:
                state = provider.fetch_message(str(notification.provider_sid))
            except (ApiError, httpx.RequestError, ValueError, provider.ProviderError):
                state = None
            if state is not None:
                _record_state(notification, state)
            app_only.append({
                "notification": notification,
                "providerStatus": state.status if state else None,
                "reason": "not in the provider's sent list for this range" if state
                else "the provider has no record of this message",
            })
            continue
        agrees = state.status == notification.provider_status
        _record_state(notification, state)
        matched.append({"notification": notification, "statusAgreed": agrees,
                        "providerStatus": state.status})

    provider_only = list(provider_records.values())
    return {
        "matched": matched,
        "appOnly": app_only,
        "providerOnly": provider_only,
        "unsettled": unsettled,
        "inboundIgnored": inbound_ignored,
    }
