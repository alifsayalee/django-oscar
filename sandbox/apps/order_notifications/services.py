"""
Order SMS notifications: the business rules around the Twilio gateway.

Every provider write (send, schedule, call off, redact) goes through
:func:`run_safe_write`, which claims the write in the database *before* the
provider call and settles it from the provider's own answer. A message that
cannot be sent never raises out of here: the outcome is recorded on the
notification and the order operation carries on.
"""
import atexit
import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from oscar.core.loading import get_class, get_model

from . import twilio_gateway as tg
from .models import ContactNumber, Notification, Outcome, ProviderAction

logger = logging.getLogger('apps.order_notifications')

Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
Basket = get_model('basket', 'Basket')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Selector = get_class('partner.strategy', 'Selector')
ShippingRepository = get_class('shipping.repository', 'Repository')
InvalidOrderStatus = get_class('order.exceptions', 'InvalidOrderStatus')

DISPATCHED_STATUS = 'Dispatched'
CANCELLED_STATUS = 'Cancelled'

# A claim younger than this is still in flight: nobody else calls the provider for it.
# It must exceed one provider call (timeout) plus our own work around it.
SEND_WINDOW = timedelta(seconds=90)
MAX_REFRESHES_PER_REQUEST = 25
MAX_RECONCILE_LOOKUPS = 100


class ServiceError(Exception):
    """A request this service refuses; ``status_code`` is what the API answers with."""

    def __init__(self, status_code, message, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


# -- the gateway -------------------------------------------------------------------------------

_gateway = None
_gateway_lock = threading.Lock()


def get_gateway():
    """The process-wide gateway, built lazily (after any fork) and closed at exit."""
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                try:
                    client = tg.build_client(
                        account_sid=settings.TWILIO_ACCOUNT_SID,
                        auth_token=settings.TWILIO_AUTH_TOKEN,
                        base_url=settings.TWILIO_BASE_URL or None,
                        timeout=settings.ORDER_NOTIFICATIONS_PROVIDER_TIMEOUT,
                    )
                except ValueError as e:
                    raise tg.WriteNotSent(502, 'SMS messaging is not configured.') from e
                atexit.register(client.close)
                _gateway = tg.TwilioGateway(
                    client,
                    account_sid=settings.TWILIO_ACCOUNT_SID,
                    from_number=settings.TWILIO_FROM_NUMBER,
                    messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
                )
    return _gateway


def set_gateway(gateway):
    """Replace the process-wide gateway (tests, or a credential rotation)."""
    global _gateway
    with _gateway_lock:
        _gateway = gateway


def _prefix():
    return settings.ORDER_NOTIFICATIONS_REFERENCE_PREFIX


def _idempotency_key(reference):
    return hashlib.sha256(reference.encode('utf-8')).hexdigest()[:40]


# -- the safe write ----------------------------------------------------------------------------

def claim(model, reference, **fields):
    """Insert-or-fail on the unique ``reference``: exactly one caller gets ``created=True``."""
    try:
        with transaction.atomic():
            row = model.objects.create(
                reference=reference, outcome=Outcome.SENDING, claimed_at=timezone.now(), **fields)
        return row, True
    except IntegrityError:
        return model.objects.get(reference=reference), False


def _take_over(row, expected_outcomes):
    """Conditionally re-take an existing claim: one caller wins, whatever process it runs in.

    The winner marks it ``sending`` again, so later arrivals see it in flight."""
    won = type(row).objects.filter(
        pk=row.pk, outcome__in=expected_outcomes, claimed_at=row.claimed_at,
    ).update(outcome=Outcome.SENDING, claimed_at=timezone.now())
    if won:
        row.refresh_from_db()
    return bool(won)


@dataclass
class WriteSpec:
    send: object          # () -> MessageRecord: makes the provider call carrying the reference
    find: object          # () -> MessageRecord | None: the record for this reference at the provider
    outcome_of: object    # MessageRecord -> outcome
    store: object         # (row, MessageRecord, outcome) -> None: copies the provider's answer onto the row
    verify: object = None  # MessageRecord -> str | None: why the answer is not what we asked for
    check_after_refusal: bool = False  # a refusal can mean "already so": look before calling it failed
    # The provider cannot make a second one for this write (an update by sid): an unresolved
    # attempt is checked by sending it again under the same reference.
    repeat_is_safe: bool = False


def _complete(row, outcome, reason=''):
    row.outcome = outcome
    if hasattr(row, 'failure_reason'):
        row.failure_reason = reason[:255]
    else:
        row.detail = reason[:255]
        if outcome != Outcome.SENDING:
            row.completed_at = timezone.now()
    row.save()
    return row


def run_safe_write(row, created, spec):
    """Claim, call, check, verify, complete - for the claimed ``row``. Never raises a provider error."""
    checking = False
    if not created:
        if row.outcome == Outcome.SENDING and row.claimed_at > timezone.now() - SEND_WINDOW:
            return row                          # in flight elsewhere: make no provider call
        if row.outcome == Outcome.FAILED and not row.provider_status:
            # Refused or never sent: nothing happened, so the claim can be taken again.
            if not _take_over(row, [Outcome.FAILED]):
                return type(row).objects.get(pk=row.pk)
        elif row.outcome in (Outcome.SENDING, Outcome.UNKNOWN):
            # A stale sender or an unresolved outcome: LOOK, never create again.
            if not _take_over(row, [Outcome.SENDING, Outcome.UNKNOWN]):
                return type(row).objects.get(pk=row.pk)
            checking = True
        else:
            return row                          # done / pending / failed / needs_review: answer from it

    resending = checking and spec.repeat_is_safe
    not_done = Outcome.UNKNOWN if resending else Outcome.FAILED   # a check never fails the write
    record = None
    if resending or not checking:
        try:
            record = spec.send()
        except tg.WriteNotSent as e:
            return _complete(row, not_done, e.message)
        except tg.WriteRejected as e:
            if not spec.check_after_refusal:
                return _complete(row, not_done, e.message)
            try:
                found = spec.find()
            except tg.ProviderError:
                return _complete(row, not_done, e.message)
            if found is None:
                return _complete(row, not_done, e.message)
            outcome = spec.outcome_of(found)
            if outcome in (Outcome.DONE, Outcome.FAILED):
                # Already so, or too late: either way the provider's record settles it.
                spec.store(row, found, outcome)
                return _complete(row, outcome, '' if outcome == Outcome.DONE else e.message)
            return _complete(row, not_done, e.message)
        except tg.WriteOutcomeUnknown:
            record = None                       # may have landed: fall through to the lookup

    if record is None:
        try:
            record = spec.find()
        except tg.ProviderError as e:
            return _complete(row, Outcome.UNKNOWN, 'Could not confirm with the provider: %s' % e.message)
        if record is None:
            # Not found YET: an empty lookup cannot prove the write did not happen.
            return _complete(row, Outcome.UNKNOWN, 'The provider has no record of it yet.')

    if spec.verify is not None:
        problem = spec.verify(record)
        if problem:
            spec.store(row, record, Outcome.NEEDS_REVIEW)
            return _complete(row, Outcome.NEEDS_REVIEW, problem)

    outcome = spec.outcome_of(record)
    spec.store(row, record, outcome)
    return _complete(row, outcome, '' if outcome != Outcome.FAILED else _failure_text(record))


def _failure_text(record):
    if record.error_code:
        return 'The provider reports error %s (status %s).' % (record.error_code, record.status)
    return 'The provider reports status %s.' % record.status


# -- sending one notification ------------------------------------------------------------------

def _store_message(row, record, outcome):
    row.provider_sid = record.sid
    row.provider_status = record.status or ''
    row.provider_error_code = record.error_code
    row.provider_time = record.date_sent
    row.last_checked_at = timezone.now()


def _send_spec(notification):
    """The safe-write spec for a notification's own send, rebuilt from the row alone."""
    def send():
        gateway = get_gateway()
        key = _idempotency_key(notification.reference)
        if notification.scheduled_for is not None:
            return gateway.schedule_message(
                notification.to_number, notification.body, notification.scheduled_for, idempotency_key=key)
        return gateway.send_message(notification.to_number, notification.body, idempotency_key=key)

    def find():
        return get_gateway().find_by_reference(notification.to_number, notification.reference)

    def verify(record):
        if record.to and record.to != notification.to_number:
            return 'The provider addressed it to a different number than we asked.'
        return None

    def outcome_of(record):
        return tg.status_from_provider(record.status)

    return WriteSpec(send=send, find=find, outcome_of=outcome_of, store=_store_message, verify=verify)


def message_text(order, kind):
    shop = getattr(settings, 'OSCAR_SHOP_NAME', 'Oscar')
    texts = {
        Notification.PLACED: 'Thanks for your %s order %s. We will text you when it ships.',
        Notification.DISPATCHED: 'Good news: your %s order %s is on its way.',
        Notification.FOLLOW_UP: 'How did the delivery of your %s order %s go? Reply to let us know.',
        Notification.CANCELLED: 'Your %s order %s has been cancelled. Contact us with any questions.',
    }
    return texts[kind] % (shop, order.number)


def _current_contact(user):
    return ContactNumber.objects.filter(user=user).order_by('-created_at', '-id').first()


def notify(order, kind, *, reference=None, contact=None, resend_of=None, send_at=None):
    """Send (or schedule) one message about ``order``. Returns the Notification, or None when the
    shopper has no number on file. Never raises for a provider failure."""
    if contact is None:
        if order.user_id is None:
            return None
        contact = _current_contact(order.user)
        if contact is None:
            return None
    reference = reference or '%s:order:%s:%s' % (_prefix(), order.pk, kind)
    row, created = claim(
        Notification, reference,
        order=order, contact_number=contact, to_number=contact.phone_number, kind=kind,
        resend_of=resend_of, scheduled_for=send_at,
        body=tg.body_with_reference(message_text(order, kind), reference),
    )
    if not created and row.contact_number_id is None:
        return row          # its number was deleted since: nothing may be sent to it again
    try:
        row = run_safe_write(row, created, _send_spec(row))
    except Exception:       # never fail the order operation over a message
        logger.exception('Notification %s: unexpected error while sending', row.pk)
        row = _complete(Notification.objects.get(pk=row.pk), Outcome.UNKNOWN, 'Unexpected error while sending.')
    logger.info('Notification %s (%s, order %s): %s', row.pk, kind, order.pk, row.outcome)
    return row


def refresh(notification):
    """Bring a notification's provider state up to date by asking the provider (no callbacks exist)."""
    if notification.provider_sid:
        if tg.is_final_send_status(notification.provider_status):
            return notification
        # How often a GET may ask the provider about a message that has not settled yet.
        interval = timedelta(seconds=settings.ORDER_NOTIFICATIONS_REFRESH_INTERVAL_SECONDS)
        if notification.last_checked_at and notification.last_checked_at > timezone.now() - interval:
            return notification
        try:
            record = get_gateway().fetch_message(notification.provider_sid)
        except tg.ProviderError as e:
            logger.warning('Notification %s: status refresh failed: %s', notification.pk, e.message)
            return notification
        outcome = tg.status_from_provider(record.status)
        _store_message(notification, record, outcome)
        notification.outcome = outcome
        if outcome == Outcome.FAILED:
            notification.failure_reason = _failure_text(record)
        notification.save()
    elif notification.outcome in (Outcome.SENDING, Outcome.UNKNOWN) and notification.contact_number_id:
        # Unsettled: re-enter the safe write, which only LOOKS (never sends a second one).
        notification = run_safe_write(notification, False, _send_spec(notification))
    _enforce_call_off(notification)
    return notification


def refresh_many(notifications):
    budget = MAX_REFRESHES_PER_REQUEST
    result = []
    for notification in notifications:
        if budget > 0 and notification.outcome in (Outcome.SENDING, Outcome.PENDING, Outcome.UNKNOWN):
            budget -= 1
            notification = refresh(notification)
        result.append(notification)
    return result


# -- calling a scheduled follow-up off ---------------------------------------------------------

def _store_action(action, record, outcome):
    action.provider_status = record.status or ''


def _follow_up_must_not_go(notification):
    if notification.kind != Notification.FOLLOW_UP:
        return False
    order_cancelled = Order.objects.filter(pk=notification.order_id, status=CANCELLED_STATUS).exists()
    return order_cancelled or notification.contact_number_id is None


def _enforce_call_off(notification):
    """A follow-up for a cancelled order, or to a deleted number, is called off whenever we see it."""
    if notification.outcome in (Outcome.PENDING, Outcome.UNKNOWN) and _follow_up_must_not_go(notification):
        call_off(notification)


def _sync_status_from_action(notification, action):
    """An update's answer carries the message's current status: keep the notification in step."""
    if action.provider_status and action.provider_status != notification.provider_status:
        notification.provider_status = action.provider_status
        notification.outcome = tg.status_from_provider(action.provider_status)
        notification.last_checked_at = timezone.now()
        notification.save(update_fields=['provider_status', 'outcome', 'last_checked_at', 'updated_at'])


def call_off(notification):
    """Make sure a scheduled message never reaches the shopper. Returns the ProviderAction."""
    reference = '%s:notification:%s:cancel' % (_prefix(), notification.pk)
    action, created = claim(ProviderAction, reference, notification=notification, kind=ProviderAction.CANCEL)
    if action.outcome == Outcome.DONE:
        return action
    if not created and action.outcome == Outcome.FAILED and action.provider_status:
        # Too late last time; nothing a second request can change.
        return action

    if not notification.provider_sid:
        if notification.outcome in (Outcome.SENDING, Outcome.UNKNOWN):
            notification = run_safe_write(notification, False, _send_spec(notification))
        if not notification.provider_sid:
            if notification.outcome == Outcome.FAILED:
                return _complete(action, Outcome.DONE, 'Nothing reached the provider, so nothing can go out.')
            return _complete(action, Outcome.UNKNOWN, 'The scheduled message cannot be found at the provider yet.')

    sid = notification.provider_sid
    if not created and action.outcome == Outcome.FAILED:
        created = _take_over(action, [Outcome.FAILED])
        if not created:
            return ProviderAction.objects.get(pk=action.pk)

    def outcome_of(record):
        return tg.cancel_outcome(record.status)

    spec = WriteSpec(
        send=lambda: get_gateway().cancel_message(sid, idempotency_key=_idempotency_key(reference)),
        find=lambda: get_gateway().fetch_message(sid),
        outcome_of=outcome_of,
        store=_store_action,
        check_after_refusal=True,   # "cannot cancel" may mean it is already off, or already sent: look
        repeat_is_safe=True,        # cancelling one message by its sid twice cancels it once
    )
    try:
        action = run_safe_write(action, created, spec)
    except Exception:
        logger.exception('Call-off %s: unexpected error', action.pk)
        return _complete(ProviderAction.objects.get(pk=action.pk), Outcome.UNKNOWN, 'Unexpected error.')

    _sync_status_from_action(notification, action)
    logger.info('Call-off of notification %s: %s', notification.pk, action.outcome)
    return action


# -- contact numbers ---------------------------------------------------------------------------

_ALLOWED_NUMBER_CHARS = set('+0123456789 -().')


def register_contact_number(user, raw_number):
    if not isinstance(raw_number, str) or not raw_number.strip():
        raise ServiceError(400, 'phoneNumber is required.')
    raw_number = raw_number.strip()
    if len(raw_number) > 32 or not set(raw_number) <= _ALLOWED_NUMBER_CHARS:
        raise ServiceError(422, 'That is not a usable mobile number.')
    compact = ''.join(ch for ch in raw_number if ch in '+0123456789')
    try:
        result = get_gateway().lookup_number(compact)
    except tg.InvalidNumber:
        raise ServiceError(422, 'That is not a usable mobile number.')
    except tg.ProviderError as e:
        raise ServiceError(e.status_code, 'Could not validate the number right now: %s' % e.message)
    try:
        with transaction.atomic():
            contact, created = ContactNumber.objects.get_or_create(
                user=user, phone_number=result.phone_number,
                defaults={'country_code': result.country_code})
    except IntegrityError:
        contact, created = ContactNumber.objects.get(user=user, phone_number=result.phone_number), False
    logger.info('User %s registered contact number %s', user.pk, contact.pk)
    return contact, created


def delete_contact_number(user, contact_id):
    contact = ContactNumber.objects.filter(user=user, pk=contact_id).first()
    if contact is None:
        raise ServiceError(404, 'No such contact number.')
    # Anything still waiting at the provider for this number must never go out.
    waiting = Notification.objects.filter(
        contact_number=contact, scheduled_for__isnull=False,
        outcome__in=[Outcome.SENDING, Outcome.PENDING, Outcome.UNKNOWN])
    call_offs = [call_off(notification) for notification in waiting]
    contact.delete()
    logger.info('User %s deleted contact number %s', user.pk, contact_id)
    return call_offs


# -- orders ------------------------------------------------------------------------------------

def _parse_items(items):
    if not isinstance(items, list) or not items:
        raise ServiceError(400, 'items must be a non-empty list of {productId, quantity}.')
    parsed: dict[int, int] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ServiceError(400, 'Each item must be an object with productId and quantity.')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            raise ServiceError(400, 'productId must be an integer.')
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
            raise ServiceError(400, 'quantity must be an integer between 1 and 100.')
        parsed[product_id] = parsed.get(product_id, 0) + quantity
    return parsed


def place_order(user, items):
    quantities = _parse_items(items)
    products = {p.pk: p for p in Product.objects.filter(pk__in=quantities.keys())}
    missing = sorted(set(quantities) - set(products))
    if missing:
        raise ServiceError(400, 'Unknown catalogue item(s): %s' % ', '.join(map(str, missing)))

    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(user=user)
        for product_id, quantity in quantities.items():
            product = products[product_id]
            info = basket.strategy.fetch_for_product(product)
            if product.is_parent or info.stockrecord is None:
                raise ServiceError(400, 'Catalogue item %s cannot be bought on its own.' % product_id)
            permitted, reason = info.availability.is_purchase_permitted(quantity)
            if not permitted:
                raise ServiceError(409, 'Catalogue item %s: %s' % (product_id, reason))
            basket.add_product(product, quantity)
        shipping_method = ShippingRepository().get_default_shipping_method(basket=basket)
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator().calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user,
            order_number=OrderNumberGenerator().order_number(basket))
        basket.submit()

    notification = notify(order, Notification.PLACED)
    return order, notification


def _order_for_operator(order_id):
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        raise ServiceError(404, 'No such order.')
    return order


def dispatch_order(order_id):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None:
            raise ServiceError(404, 'No such order.')
        if order.status == CANCELLED_STATUS:
            raise ServiceError(409, 'A cancelled order cannot be dispatched.')
        if order.status != DISPATCHED_STATUS:
            try:
                order.set_status(DISPATCHED_STATUS)
            except InvalidOrderStatus:
                raise ServiceError(409, 'Order in status %s cannot be dispatched.' % order.status)

    dispatched = notify(order, Notification.DISPATCHED)
    follow_up = None
    if dispatched is not None:
        send_at = timezone.now() + settings.ORDER_NOTIFICATIONS_FOLLOW_UP_DELAY
        existing = Notification.objects.filter(
            reference='%s:order:%s:%s' % (_prefix(), order.pk, Notification.FOLLOW_UP)).first()
        follow_up = notify(order, Notification.FOLLOW_UP,
                           send_at=existing.scheduled_for if existing else send_at)
        # A cancel racing this dispatch may have run before the follow-up existed: look again.
        if follow_up is not None and _follow_up_must_not_go(follow_up):
            call_off(follow_up)
            follow_up.refresh_from_db()
    order.refresh_from_db()
    return order, dispatched, follow_up


def cancel_order(order_id):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None:
            raise ServiceError(404, 'No such order.')
        if order.status != CANCELLED_STATUS:
            try:
                order.set_status(CANCELLED_STATUS)
            except InvalidOrderStatus:
                raise ServiceError(409, 'Order in status %s cannot be cancelled.' % order.status)

    # First make sure the delivery follow-up can never reach the shopper, then tell them.
    call_offs = [call_off(n) for n in order.sms_notifications.filter(kind=Notification.FOLLOW_UP)]
    cancelled = notify(order, Notification.CANCELLED)
    return order, cancelled, call_offs


# -- operator actions on one notification ------------------------------------------------------

def resend(notification_id, idempotency_key):
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 200:
        raise ServiceError(400, 'An Idempotency-Key of 1-200 characters is required.')
    original = Notification.objects.select_related('order').filter(pk=notification_id).first()
    if original is None:
        raise ServiceError(404, 'No such notification.')
    reference = '%s:resend:%s:%s' % (
        _prefix(), original.pk, hashlib.sha256(idempotency_key.strip().encode('utf-8')).hexdigest()[:32])

    existing = Notification.objects.filter(reference=reference).first()
    if existing is not None:
        # A repeat of the same request: answer from (or finish checking) the one it already made.
        return run_safe_write(existing, False, _send_spec(existing))

    original = refresh(original)
    if original.outcome != Outcome.FAILED:
        raise ServiceError(409, 'Only a message that did not reach the shopper can be re-sent '
                                '(this one is %s).' % original.outcome)
    order = original.order
    if original.kind == Notification.FOLLOW_UP and order.status == CANCELLED_STATUS:
        raise ServiceError(409, 'The order was cancelled; its delivery follow-up must not be sent.')
    contact = original.contact_number or (order.user and _current_contact(order.user))
    if contact is None or contact.user_id != order.user_id:
        raise ServiceError(409, 'The shopper has no mobile number on file.')
    return notify(order, original.kind, reference=reference, contact=contact, resend_of=original)


def dispose_content(notification_id):
    notification = Notification.objects.filter(pk=notification_id).first()
    if notification is None:
        raise ServiceError(404, 'No such notification.')
    if notification.content_disposed_at is not None:
        return notification, Outcome.DONE

    if not notification.provider_sid and notification.outcome in (Outcome.SENDING, Outcome.UNKNOWN):
        notification = run_safe_write(notification, False, _send_spec(notification))
    if not notification.provider_sid:
        if notification.outcome != Outcome.FAILED:
            return notification, Outcome.UNKNOWN
        # It never reached the provider: only our own copy exists.
        _clear_content(notification)
        return notification, Outcome.DONE

    sid = notification.provider_sid
    reference = '%s:notification:%s:redact' % (_prefix(), notification.pk)
    action, created = claim(ProviderAction, reference, notification=notification, kind=ProviderAction.REDACT)
    if not created and action.outcome == Outcome.FAILED:
        created = _take_over(action, [Outcome.FAILED])
        if not created:
            action = ProviderAction.objects.get(pk=action.pk)
            return notification, action.outcome

    def outcome_of(record):
        return tg.redact_outcome(record.body)

    spec = WriteSpec(
        send=lambda: get_gateway().redact_message(sid, idempotency_key=_idempotency_key(reference)),
        find=lambda: get_gateway().fetch_message(sid),
        outcome_of=outcome_of,
        store=_store_action,
        check_after_refusal=True,   # a refusal may mean it is already redacted: look
        repeat_is_safe=True,        # blanking one message's body twice leaves it blank once
    )
    action = run_safe_write(action, created, spec)
    _sync_status_from_action(notification, action)
    if action.outcome == Outcome.DONE:
        _clear_content(notification)
    logger.info('Redaction of notification %s: %s', notification.pk, action.outcome)
    return notification, action.outcome


def _clear_content(notification):
    notification.body = ''
    notification.content_disposed_at = timezone.now()
    notification.save(update_fields=['body', 'content_disposed_at', 'updated_at'])


# -- reconciliation ----------------------------------------------------------------------------

def reconcile(start, end):
    """Line the provider's record of messages sent from our number in [start, end) up against ours.

    Both sides are placed on the provider's clock (its ``date_sent``): a message we created in the
    window but the provider has not reported sending is *unsettled*, never silently matched or missed.
    """
    if start >= end:
        raise ServiceError(400, '"from" must be earlier than "to".')
    try:
        provider_records = get_gateway().list_sent_messages(start, end)
    except tg.ProviderError as e:
        raise ServiceError(e.status_code, e.message)

    by_sid: dict[str, list[tg.MessageRecord]] = {}
    for record in provider_records:
        by_sid.setdefault(record.sid, []).append(record)
    ours = {n.provider_sid: n for n in Notification.objects.filter(provider_sid__in=list(by_sid))}

    matched, provider_only, status_changed = [], [], []
    for sid, records in by_sid.items():
        notification = ours.get(sid)
        if notification is None:
            provider_only.extend(records)
            continue
        record = records[0]
        if (record.status or '') != notification.provider_status or record.date_sent != notification.provider_time:
            outcome = tg.status_from_provider(record.status)
            _store_message(notification, record, outcome)
            notification.outcome = outcome
            notification.save()
            status_changed.append(notification.pk)
        matched.append((notification, record))
    matched_ids = {n.pk for n, _ in matched}

    in_window = Q(provider_time__gte=start, provider_time__lt=end)
    created_in_window = Q(created_at__gte=start, created_at__lt=end, provider_time__isnull=True)
    candidates = [n for n in Notification.objects.filter(in_window | created_in_window) if n.pk not in matched_ids]
    # The provider told us it sent these in the window, and its list does not have them.
    app_only = [n for n in candidates if n.provider_sid and n.provider_time is not None]
    # Ours, with a provider id but no send time: ask about each one (bounded) rather than guess.
    for n in [n for n in candidates if n.provider_sid and n.provider_time is None][:MAX_RECONCILE_LOOKUPS]:
        try:
            record = get_gateway().fetch_message(n.provider_sid)
        except tg.ProviderError as e:
            if e.provider_status == 404:
                app_only.append(n)          # the provider has no record of it at all
            continue
        outcome = tg.status_from_provider(record.status)
        _store_message(n, record, outcome)
        n.outcome = outcome
        n.save()
        status_changed.append(n.pk)
    # Certainly never reached the provider (refused, or never sent).
    never_sent = [n for n in candidates if not n.provider_sid and n.outcome == Outcome.FAILED]
    # Scheduled, called off, still in flight or unresolved: no provider send time yet.
    unsettled = [n for n in candidates if n not in app_only and n not in never_sent]
    return {
        'provider_records': provider_records,
        'matched': matched,
        'local_only': app_only,
        'provider_only': provider_only,
        'unsettled': unsettled,
        'never_sent': never_sent,
        'status_changed': status_changed,
    }
