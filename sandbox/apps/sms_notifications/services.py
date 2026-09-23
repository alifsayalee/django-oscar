"""
Order SMS notifications: the domain logic between the HTTP views, Oscar's
order models and the Twilio gateway.

Rules this module keeps:

* A notification that cannot be sent never fails the order operation that
  triggered it - the failure is recorded on the notification row instead.
* Every provider write is claimed by a durable row *before* the call and
  settled from what the provider said afterwards (claim -> call -> reconcile
  -> settle). An uncertain outcome is looked up by the reference embedded in
  the message, and stays ``unknown`` if it cannot be found.
* Phone numbers are never logged.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from oscar.apps.order.exceptions import InvalidOrderStatus
from oscar.core.loading import get_class, get_model

from . import gateway as gw
from .models import ContactNumber, Notification, OrderTransitionClaim

logger = logging.getLogger(__name__)

Order = get_model("order", "Order")
Basket = get_model("basket", "Basket")
Product = get_model("catalogue", "Product")
Country = get_model("address", "Country")
ShippingAddress = get_model("order", "ShippingAddress")
Selector = get_class("partner.strategy", "Selector")
Repository = get_class("shipping.repository", "Repository")
OrderTotalCalculator = get_class("checkout.calculators", "OrderTotalCalculator")
SurchargeApplicator = get_class("checkout.applicator", "SurchargeApplicator")
OrderCreator = get_class("order.utils", "OrderCreator")
OrderNumberGenerator = get_class("order.utils", "OrderNumberGenerator")

STATUS_DISPATCHED = "Dispatched"
STATUS_CANCELLED = "Cancelled"

# A claim still "sending" after this long is treated as abandoned and looked up.
# Worst case of one attempt (no retries on create) plus a margin.
SEND_WINDOW = dt.timedelta(seconds=60)
SYNC_LIMIT = 25


class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


class InvalidInput(ServiceError):
    status_code = 422


class Conflict(ServiceError):
    status_code = 409


class NotFound(ServiceError):
    status_code = 404


# --------------------------------------------------------------------------
# Message text
# --------------------------------------------------------------------------


def _shop() -> str:
    return getattr(settings, "OSCAR_SHOP_NAME", "Oscar")


def render_body(notification: Notification) -> str:
    order = notification.order
    ref = "Ref %s" % notification.short_ref
    shop = _shop()
    if notification.kind == Notification.KIND_PLACED:
        return "%s: thanks - order %s is placed (total %s %s). %s" % (
            shop, order.number, order.total_incl_tax, order.currency, ref,
        )
    if notification.kind == Notification.KIND_DISPATCHED:
        return "%s: order %s is on its way. %s" % (shop, order.number, ref)
    if notification.kind == Notification.KIND_FOLLOWUP:
        return "%s: how did the delivery of order %s go? Reply and let us know. %s" % (
            shop, order.number, ref,
        )
    if notification.kind == Notification.KIND_CANCELLED:
        return "%s: order %s has been cancelled. %s" % (shop, order.number, ref)
    raise ValueError("unknown notification kind %r" % notification.kind)


# --------------------------------------------------------------------------
# Settling rows from provider answers
# --------------------------------------------------------------------------

_SETTLE_FIELDS = [
    "provider_sid",
    "provider_status",
    "status",
    "error_code",
    "provider_date_created",
    "provider_date_sent",
    "last_checked_at",
    "failure_reason",
    "updated_at",
]


def _settle(row: Notification, message: gw.ProviderMessage) -> None:
    row.provider_sid = message.sid
    row.provider_status = message.status
    row.status = message.outcome
    row.error_code = message.error_code
    row.provider_date_created = message.date_created or row.provider_date_created
    row.provider_date_sent = message.date_sent or row.provider_date_sent
    row.last_checked_at = timezone.now()
    row.failure_reason = "" if message.outcome != Notification.UNKNOWN else "unmapped_provider_status"
    fields = list(_SETTLE_FIELDS)
    if message.outcome == Notification.CANCELED and row.cancel_state == Notification.CANCEL_REQUESTED:
        row.cancel_state = Notification.CANCEL_DONE
        fields.append("cancel_state")
    # update_fields: never overwrite a cancel request written concurrently.
    row.save(update_fields=fields)


def _mark(row: Notification, status: str, reason: str, error: gw.ProviderError | None = None) -> None:
    row.status = status
    row.failure_reason = reason
    if error is not None and error.twilio_code is not None:
        row.error_code = error.twilio_code
    row.last_checked_at = timezone.now()
    row.save(update_fields=["status", "failure_reason", "error_code", "last_checked_at", "updated_at"])


def _reason(error: gw.ProviderError) -> str:
    if isinstance(error, gw.ProviderConfigError):
        return "provider_configuration"
    if isinstance(error, gw.ProviderRejected):
        return "provider_rejected"
    if error.status_code == 503:
        return "provider_rate_limited"
    return "provider_unavailable"


# --------------------------------------------------------------------------
# Sending (claim -> call -> reconcile -> settle)
# --------------------------------------------------------------------------


def _deliver(row: Notification) -> None:
    """Send (or schedule) the message a freshly claimed row describes. Never raises."""
    contact = row.contact_number
    if contact.deleted_at is not None:
        _mark(row, Notification.FAILED, "contact_number_deleted")
        return
    try:
        gateway = gw.get_gateway()
    except gw.ProviderError as e:
        _mark(row, Notification.FAILED, _reason(e))
        return
    body = render_body(row)
    try:
        if row.scheduled_for is not None:
            message = gateway.schedule(contact.phone_number, body, row.scheduled_for)
        else:
            message = gateway.send(contact.phone_number, body)
    except gw.ProviderError as e:
        if not e.outcome_unknown:
            _mark(row, Notification.FAILED, _reason(e), e)
            return
        # It may have landed: look for it by the reference we sent.
        found = _find_by_ref(gateway, row)
        if found is None:
            _mark(row, Notification.UNKNOWN, "outcome_unknown", e)
            return
        message = found
    except Exception:  # never let a notification break the order flow
        logger.exception("notification %s: unexpected error while sending", row.pk)
        _mark(row, Notification.UNKNOWN, "unexpected_error")
        return
    _settle(row, message)
    logger.info(
        "notification %s (%s, order %s) -> %s/%s",
        row.pk, row.kind, row.order.number, row.status, row.provider_status,
    )


def _find_by_ref(gateway: gw.TwilioGateway, row: Notification) -> gw.ProviderMessage | None:
    try:
        return gateway.find_by_ref(row.contact_number.phone_number, row.short_ref)
    except gw.ProviderError:
        return None


def notify(order, kind: str, *, send_at: dt.datetime | None = None) -> list[Notification]:
    """
    Tell the order's shopper about ``kind`` on every active number they have.
    A shopper with no number on file is simply not messaged. Never raises.
    """
    rows: list[Notification] = []
    if order.user_id is None:
        return rows
    for contact in ContactNumber.objects.filter(user_id=order.user_id, deleted_at__isnull=True):
        try:
            with transaction.atomic():
                row = Notification.objects.create(
                    order=order,
                    contact_number=contact,
                    kind=kind,
                    scheduled_for=send_at,
                    status=Notification.SENDING,
                )
        except IntegrityError:
            # Already claimed by an earlier or concurrent request: theirs to send.
            continue
        _deliver(row)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Cancelling follow-ups
# --------------------------------------------------------------------------


def _live_followups(**filters):
    return (
        Notification.objects.filter(kind=Notification.KIND_FOLLOWUP, **filters)
        .exclude(status__in=[Notification.CANCELED, Notification.FAILED, Notification.DELIVERED])
        .exclude(cancel_state__in=[Notification.CANCEL_DONE, Notification.CANCEL_TOO_LATE])
    )


def request_followup_cancellation(**filters) -> list[Notification]:
    """Mark matching follow-ups as to-be-cancelled (durably), then try now."""
    _live_followups(**filters).update(cancel_state=Notification.CANCEL_REQUESTED)
    rows = list(Notification.objects.filter(
        kind=Notification.KIND_FOLLOWUP, cancel_state=Notification.CANCEL_REQUESTED, **filters
    ))
    for row in rows:
        attempt_cancel(row)
    return rows


def attempt_cancel(row: Notification) -> None:
    """Try to cancel one follow-up at the provider. Never raises."""
    if row.cancel_state != Notification.CANCEL_REQUESTED:
        return
    try:
        gateway = gw.get_gateway()
    except gw.ProviderError:
        return
    if not row.provider_sid:
        if row.status == Notification.SENDING and timezone.now() - row.created_at < SEND_WINDOW:
            return  # the sender is still in flight; it cancels after it settles
        found = _find_by_ref(gateway, row)
        if found is None:
            if row.status in (Notification.SENDING, Notification.UNKNOWN):
                _mark(row, Notification.UNKNOWN, "outcome_unknown")
            else:  # it never reached the provider: nothing to cancel
                _set_cancel_state(row, Notification.CANCEL_DONE)
            return
        _settle(row, found)
    if row.status in (Notification.CANCELED, Notification.FAILED) or not row.provider_sid:
        _set_cancel_state(row, Notification.CANCEL_DONE)
        return
    try:
        message = gateway.cancel(row.provider_sid)
    except gw.ProviderError as e:
        logger.warning(
            "notification %s: cancel not confirmed (%s); will retry", row.pk, type(e).__name__
        )
        return
    _settle(row, message)
    if message.outcome in (gw.CANCELED, gw.FAILED):
        _set_cancel_state(row, Notification.CANCEL_DONE)
    elif message.status != "scheduled":
        # It went out before the cancel reached it. Surface it loudly.
        logger.error("notification %s: follow-up already sent; cancel too late", row.pk)
        _set_cancel_state(row, Notification.CANCEL_TOO_LATE)


def _set_cancel_state(row: Notification, state: str) -> None:
    row.cancel_state = state
    row.save(update_fields=["cancel_state", "updated_at"])


# --------------------------------------------------------------------------
# Keeping rows current (no callbacks reach us: we ask the provider)
# --------------------------------------------------------------------------


@dataclass
class SyncResult:
    checked: int = 0
    errors: int = 0


def sync_notifications(queryset, limit: int = SYNC_LIMIT) -> SyncResult:
    """
    Bring non-final rows in ``queryset`` up to date with the provider: retry
    requested cancels, resolve abandoned claims by reference, and refresh
    statuses. Bounded by ``limit`` provider-facing rows. Never raises.
    """
    result = SyncResult()
    try:
        gateway = gw.get_gateway()
    except gw.ProviderError:
        return result
    stale_before = timezone.now() - SEND_WINDOW
    rows = list(
        queryset.filter(
            # Pending cancels first: they are the ones that must not wait.
            cancel_state=Notification.CANCEL_REQUESTED
        )[:limit]
    )
    rows += list(
        queryset.exclude(cancel_state=Notification.CANCEL_REQUESTED)
        .filter(status__in=[Notification.PENDING, Notification.UNKNOWN, Notification.SENDING])
        .order_by("last_checked_at", "pk")[: max(limit - len(rows), 0)]
    )
    for row in rows:
        result.checked += 1
        try:
            if row.cancel_state == Notification.CANCEL_REQUESTED:
                attempt_cancel(row)
                if row.cancel_state == Notification.CANCEL_REQUESTED:
                    result.errors += 1
                continue
            if not row.provider_sid:
                if row.status == Notification.SENDING and row.created_at > stale_before:
                    continue  # still inside its sender's window
                found = gateway.find_by_ref(row.contact_number.phone_number, row.short_ref)
                if found is None:
                    _mark(row, Notification.UNKNOWN, "outcome_unknown")
                else:
                    _settle(row, found)
                continue
            message = gateway.fetch(row.provider_sid)
            if message is None:
                _mark(row, Notification.UNKNOWN, "provider_record_missing")
            else:
                _settle(row, message)
        except gw.ProviderError:
            result.errors += 1
        except Exception:
            logger.exception("notification %s: unexpected error while syncing", row.pk)
            result.errors += 1
    return result


# --------------------------------------------------------------------------
# Contact numbers
# --------------------------------------------------------------------------


def register_contact_number(user, raw_number: str, country_code: str | None) -> tuple[ContactNumber, bool]:
    raw_number = (raw_number or "").strip()
    if not raw_number or len(raw_number) > 32:
        raise InvalidInput("A phone number is required.", field="phoneNumber")
    if country_code is not None and (len(country_code) != 2 or not country_code.isalpha()):
        raise InvalidInput("countryCode must be an ISO 3166-1 alpha-2 code.", field="countryCode")
    try:
        result = gw.get_gateway().lookup(raw_number, country_code.upper() if country_code else None)
    except gw.ProviderRejected as e:
        # Anything other than the lookup's own 404 is not the caller's input.
        raise gw.ProviderFailure(
            "The number lookup failed.", status_code=502, outcome_unknown=False,
            provider_status=e.provider_status, twilio_code=e.twilio_code,
        ) from e
    if result is None:
        raise InvalidInput("The messaging provider does not recognise this as a usable phone number.",
                           field="phoneNumber")
    existing = ContactNumber.objects.filter(
        user=user, phone_number=result.phone_number, deleted_at__isnull=True
    ).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            contact = ContactNumber.objects.create(
                user=user, phone_number=result.phone_number, country_code=result.country_code
            )
    except IntegrityError:
        # A concurrent registration of the same number won.
        return ContactNumber.objects.get(
            user=user, phone_number=result.phone_number, deleted_at__isnull=True
        ), False
    logger.info("user %s registered contact number %s", user.pk, contact.pk)
    return contact, True


def delete_contact_number(contact: ContactNumber) -> None:
    updated = ContactNumber.objects.filter(pk=contact.pk, deleted_at__isnull=True).update(
        deleted_at=timezone.now()
    )
    if updated:
        logger.info("contact number %s deleted", contact.pk)
    # Nothing may be sent to it again - including follow-ups already queued.
    request_followup_cancellation(contact_number=contact)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


@dataclass
class OrderLineRequest:
    product_id: int
    quantity: int


def place_order(user, lines: list[OrderLineRequest], address_data: dict):
    """Place an order through Oscar's own basket, shipping and order machinery."""
    if not lines:
        raise InvalidInput("At least one line is required.", field="lines")
    country_code = str(address_data.get("country") or "").upper()
    country = Country.objects.filter(iso_3166_1_a2=country_code).first()
    if country is None:
        raise InvalidInput("Unknown country %r." % country_code, field="shippingAddress.country")
    if not country.is_shipping_country:
        raise InvalidInput("We do not ship to %s." % country_code, field="shippingAddress.country")
    address = ShippingAddress(
        first_name=str(address_data.get("firstName") or "")[:255],
        last_name=str(address_data.get("lastName") or "")[:255],
        line1=str(address_data.get("line1") or "")[:255],
        line2=str(address_data.get("line2") or "")[:255],
        line4=str(address_data.get("city") or "")[:255],
        state=str(address_data.get("state") or "")[:255],
        postcode=str(address_data.get("postcode") or "")[:64],
        country=country,
    )
    if not address.line1 or not address.last_name:
        raise InvalidInput("shippingAddress needs at least lastName and line1.", field="shippingAddress")
    try:
        address.clean()
    except ValidationError as e:
        raise InvalidInput("; ".join(e.messages), field="shippingAddress") from e

    strategy = Selector().strategy(user=user)
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = strategy
        for line in lines:
            product = Product.objects.filter(pk=line.product_id).first()
            if product is None or not product.is_public:
                raise InvalidInput("Unknown product %s." % line.product_id, field="lines")
            if product.is_parent:
                raise InvalidInput("Product %s is a parent product; order one of its variants."
                                   % line.product_id, field="lines")
            info = strategy.fetch_for_product(product)
            if not info.availability.is_available_to_buy:
                raise InvalidInput("Product %s is not available." % line.product_id, field="lines")
            permitted, reason = info.availability.is_purchase_permitted(line.quantity)
            if not permitted:
                raise InvalidInput("Product %s: %s" % (line.product_id, reason), field="lines")
            basket.add_product(product, line.quantity)
        address.save()
        shipping_method = Repository().get_default_shipping_method(
            basket=basket, shipping_addr=address, user=user
        )
        shipping_charge = shipping_method.calculate(basket)
        surcharges = SurchargeApplicator().get_applicable_surcharges(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge, surcharges=surcharges)
        order = OrderCreator().place_order(
            basket=basket,
            total=total,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            user=user,
            shipping_address=address,
            order_number=OrderNumberGenerator().order_number(basket),
            surcharges=surcharges,
        )
        basket.submit()
    # The "order placed" message is sent by the order_placed signal handler
    # (signals.py) once this transaction commits - for API and storefront
    # orders alike.
    return order


def transition(order, to_status: str) -> bool:
    """
    Move ``order`` to ``to_status`` through the host's own ``set_status``.
    Exactly one request wins; it alone fires the transition's messages (via
    the order_status_changed signal). Returns False for a no-op repeat.
    """
    try:
        with transaction.atomic():
            OrderTransitionClaim.objects.create(order=order, to_status=to_status)
            locked = Order.objects.select_for_update().get(pk=order.pk)
            locked.set_status(to_status)
    except IntegrityError:
        return False
    except InvalidOrderStatus as e:
        raise Conflict(
            "Order %s cannot move from %r to %r." % (order.number, order.status, to_status)
        ) from e
    order.refresh_from_db()
    return True


def handle_status_change(order, new_status: str) -> None:
    """Messages for an order status change, from any path (API or dashboard). Never raises."""
    try:
        if new_status == STATUS_DISPATCHED:
            notify(order, Notification.KIND_DISPATCHED)
            delay = dt.timedelta(hours=int(getattr(settings, "SMS_FOLLOWUP_DELAY_HOURS", 72)))
            send_at = (timezone.now() + delay).replace(microsecond=0)
            for row in notify(order, Notification.KIND_FOLLOWUP, send_at=send_at):
                row.refresh_from_db()
                if row.cancel_state == Notification.CANCEL_REQUESTED:
                    attempt_cancel(row)  # a cancel raced this dispatch
        elif new_status == STATUS_CANCELLED:
            # Stop the follow-up first: it is the message that must never arrive.
            request_followup_cancellation(order=order)
            notify(order, Notification.KIND_CANCELLED)
    except Exception:
        logger.exception("order %s: failed to handle status change to %r", order.number, new_status)


# --------------------------------------------------------------------------
# Operator actions
# --------------------------------------------------------------------------


def resend(notification: Notification, idempotency_key: str) -> tuple[Notification, bool]:
    """
    Re-send a message that did not reach the shopper. The same key always
    answers with the same resend (never a second message); a fresh key is a
    genuine new attempt. Returns (row, replayed).
    """
    existing = Notification.objects.filter(
        resend_of=notification, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        return existing, True
    sync_notifications(Notification.objects.filter(pk=notification.pk), limit=1)
    notification.refresh_from_db()
    order = notification.order
    if notification.status != Notification.FAILED:
        raise Conflict(
            "Only a message that did not reach the shopper can be re-sent (status is %r)."
            % notification.status
        )
    if notification.content_disposed_at is not None:
        raise Conflict("This message's content was disposed of; it cannot be re-sent.")
    if notification.contact_number.deleted_at is not None:
        raise Conflict("The shopper removed this number; nothing may be sent to it.")
    if order.status == STATUS_CANCELLED and notification.kind != Notification.KIND_CANCELLED:
        raise Conflict("The order is cancelled; only the cancellation notice may be re-sent.")
    try:
        with transaction.atomic():
            row = Notification.objects.create(
                order=order,
                contact_number=notification.contact_number,
                kind=notification.kind,
                resend_of=notification,
                idempotency_key=idempotency_key,
                status=Notification.SENDING,
            )
    except IntegrityError:
        return Notification.objects.get(resend_of=notification, idempotency_key=idempotency_key), True
    _deliver(row)
    return row, False


def dispose_content(notification: Notification) -> Notification:
    """
    Erase the text of a message at the provider (not merely hide it here).
    The row - that it was sent, and what became of it - survives.
    """
    if notification.content_disposed_at is not None:
        return notification
    gateway = gw.get_gateway()
    if not notification.provider_sid:
        found = _find_by_ref(gateway, notification)
        if found is not None:
            _settle(notification, found)
        elif notification.status == Notification.FAILED:
            pass  # never reached the provider: there is no copy to erase
        else:
            raise Conflict("Cannot yet confirm whether the provider holds this message; try again later.")
    if notification.provider_sid:
        if notification.provider_status == "scheduled":
            # Not sent yet: stop it before erasing it, or an empty text would go out.
            if notification.cancel_state != Notification.CANCEL_REQUESTED:
                _set_cancel_state(notification, Notification.CANCEL_REQUESTED)
            attempt_cancel(notification)
            if notification.cancel_state == Notification.CANCEL_REQUESTED:
                raise Conflict("The scheduled message could not be cancelled yet; try again later.")
        try:
            message = gateway.redact(notification.provider_sid)
        except gw.ProviderRejected as e:
            raise Conflict(
                "The provider cannot erase this message yet (it may still be in flight); try again later."
            ) from e
        _settle(notification, message)
    notification.content_disposed_at = timezone.now()
    notification.save(update_fields=["content_disposed_at", "updated_at"])
    logger.info("notification %s: content disposed", notification.pk)
    return notification


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

MAX_RECONCILIATION_RANGE = dt.timedelta(days=93)


def reconcile(start: dt.datetime, end: dt.datetime) -> dict:
    if end <= start:
        raise InvalidInput("'to' must be after 'from'.", field="to")
    if end - start > MAX_RECONCILIATION_RANGE:
        raise InvalidInput("The range may span at most %s days." % MAX_RECONCILIATION_RANGE.days, field="to")

    # Local side on the provider's clock: first give rows sent around the
    # window a send time if they lack one.
    unsettled_qs = Notification.objects.filter(
        provider_date_sent__isnull=True, created_at__lt=end
    ).exclude(status__in=[Notification.CANCELED, Notification.FAILED])
    sync = sync_notifications(unsettled_qs, limit=50)

    sent = gw.get_gateway().list_sent(start, end)  # ProviderError -> the view's ladder
    provider_by_sid: dict[str, gw.ProviderMessage] = {m.sid: m for m in sent.messages}

    local = list(
        Notification.objects.filter(provider_date_sent__gte=start, provider_date_sent__lt=end)
        .select_related("order", "contact_number")
    )
    matched, local_only = [], []
    for row in local:
        message = provider_by_sid.pop(row.provider_sid or "", None)
        if message is None:
            local_only.append(_local_entry(row))
            continue
        agrees = message.status == row.provider_status
        if not agrees:
            _settle(row, message)  # the provider is authoritative about what happened
        matched.append({**_local_entry(row), "providerStatus": message.status, "statusAgreed": agrees})
    provider_only = [
        {
            "providerMessageSid": m.sid,
            "providerStatus": m.status,
            "sentAt": _iso(m.date_sent),
            "to": _mask(m.to),
            "errorCode": m.error_code,
        }
        for m in provider_by_sid.values()
    ]
    unsettled = [
        _local_entry(row)
        for row in Notification.objects.filter(
            provider_date_sent__isnull=True, created_at__gte=start, created_at__lt=end
        ).exclude(status__in=[Notification.CANCELED, Notification.FAILED])
    ]
    never_sent = Notification.objects.filter(
        provider_date_sent__isnull=True, created_at__gte=start, created_at__lt=end,
        status__in=[Notification.CANCELED, Notification.FAILED],
    ).count()
    return {
        "from": _iso(start),
        "to": _iso(end),
        "sendingNumber": _mask(gw.get_gateway().from_number),
        "complete": not sent.truncated,
        "truncated": sent.truncated,
        "providerPagesRead": sent.pages,
        "summary": {
            "providerMessages": len(sent.messages),
            "localMessages": len(local),
            "matched": len(matched),
            "statusMismatches": sum(1 for m in matched if not m["statusAgreed"]),
            "providerOnly": len(provider_only),
            "localOnly": len(local_only),
            "unsettled": len(unsettled),
            "neverSent": never_sent,
            "localRefreshErrors": sync.errors,
        },
        "matched": matched,
        "providerOnly": provider_only,
        "localOnly": local_only,
        "unsettled": unsettled,
    }


def _local_entry(row: Notification) -> dict:
    return {
        "notificationId": row.pk,
        "orderId": row.order_id,
        "kind": row.kind,
        "status": row.status,
        "localProviderStatus": row.provider_status,
        "providerMessageSid": row.provider_sid,
        "sentAt": _iso(row.provider_date_sent),
        "createdAt": _iso(row.created_at),
    }


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _mask(number: str | None) -> str | None:
    if not number:
        return None
    return number[:2] + "*" * max(len(number) - 4, 0) + number[-2:] if len(number) > 4 else "****"


def money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
