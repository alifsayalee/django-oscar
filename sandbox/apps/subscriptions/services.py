"""
Subscription use cases: list plans, ensure a Maxio customer, subscribe, list mine.

Every write to Maxio follows one pattern: claim a row locally (a unique
constraint decides the single sender), send under a reference we generated,
reconcile by that reference when the outcome is unreadable, and settle the row
from what Maxio *said*. A request that loses the claim never sends.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from .maxio.errors import ProviderError, ProviderRejected, ProviderUnavailable
from .maxio.states import Bucket
from .models import MaxioCustomer, SubscriptionRequest

logger = logging.getLogger(__name__)

# How long a 'sending' claim is trusted to still be in flight before another request may
# take it over: comfortably above the worst case of one write plus two reads at 10 s each.
SEND_WINDOW = timedelta(seconds=60)
PLANS_CACHE_SECONDS = 60


class ServiceError(Exception):
    status_code = 400
    code = 'invalid_request'

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class PlanNotAvailable(ServiceError):
    code = 'plan_not_available'


class AccountIncomplete(ServiceError):
    code = 'account_incomplete'


class CustomerInProgress(ServiceError):
    status_code = 409
    code = 'customer_setup_in_progress'


@dataclass
class SubscribeResult:
    row: SubscriptionRequest
    http_status: int


# -- plans -------------------------------------------------------------------

def list_plans(gateway):
    key = f'maxio:plans:{gateway.product_family}'
    page = cache.get(key)
    if page is None:
        page = gateway.list_plans()
        cache.set(key, page, PLANS_CACHE_SECONDS)
    return page


def money(amount_in_cents, currency=None):
    if amount_in_cents is None:
        return None
    return {
        'amountInCents': amount_in_cents,
        'amount': str((Decimal(amount_in_cents) / 100).quantize(Decimal('0.01'))),
        'currency': currency or None,
    }


def plan_payload(plan):
    return {
        'planHandle': plan.handle,
        'name': plan.name,
        'description': plan.description,
        'price': money(plan.price_in_cents),
        'interval': plan.interval,
        'intervalUnit': plan.interval_unit,
    }


# -- shared claim helpers ------------------------------------------------------

def _take_over(model, row):
    """Atomically move a stale or unresolved claim to 'sending' for this request only."""
    changed = model.objects.filter(pk=row.pk, status=row.status, updated_at=row.updated_at).update(
        status=model.SENDING, updated_at=timezone.now())
    if changed:
        row.refresh_from_db()
    return bool(changed)


def _is_fresh(row):
    return row.status == row.SENDING and row.updated_at > timezone.now() - SEND_WINDOW


def _mark_sent(row):
    row.sent_at = timezone.now()
    row.save(update_fields=['sent_at', 'updated_at'])


def _set_status(row, status, error=''):
    row.status = status
    fields = ['status', 'updated_at']
    if hasattr(row, 'error'):
        row.error = error
        fields.append('error')
    row.save(update_fields=fields)


# -- customers -------------------------------------------------------------------

def _customer_fields(user):
    email = (user.email or '').strip()
    if not email:
        raise AccountIncomplete('Your account needs an email address before you can subscribe.')
    local_part = email.split('@', 1)[0]
    return {
        'first_name': (user.first_name or '').strip() or local_part,
        'last_name': (user.last_name or '').strip() or local_part,
        'email': email,
    }


def _settle_customer(row, customer_id):
    row.maxio_customer_id = customer_id
    row.status = MaxioCustomer.DONE
    row.save(update_fields=['maxio_customer_id', 'status', 'updated_at'])
    return customer_id


def _find_customer_or_unknown(gateway, row):
    try:
        return gateway.find_customer_id(row.reference)
    except ProviderError:
        _set_status(row, MaxioCustomer.UNKNOWN)
        raise


def _send_customer(gateway, row, user):
    if row.sent_at is not None:
        # An earlier attempt went out; it may have landed.
        found = _find_customer_or_unknown(gateway, row)
        if found is not None:
            return _settle_customer(row, found)
    try:
        fields = _customer_fields(user)
    except AccountIncomplete:
        _set_status(row, MaxioCustomer.FAILED)
        raise
    _mark_sent(row)
    try:
        customer_id = gateway.create_customer(reference=row.reference, **fields)
    except ProviderError as exc:
        # "Reference already taken" means an earlier attempt landed; an unreadable
        # outcome may have landed. Anything else definitely did not happen.
        landed_maybe = exc.outcome_unknown or (isinstance(exc, ProviderRejected) and exc.duplicate_reference)
        if not landed_maybe:
            _set_status(row, MaxioCustomer.FAILED)
            raise
        found = _find_customer_or_unknown(gateway, row)
        if found is None:
            # Not found *yet* is not proof it never landed.
            _set_status(row, MaxioCustomer.UNKNOWN)
            raise ProviderUnavailable(504, 'Billing account setup outcome unknown; try again shortly.',
                                      outcome_unknown=True) from exc
        return _settle_customer(row, found)
    return _settle_customer(row, customer_id)


def ensure_customer(gateway, user):
    """Return the Maxio customer id for this shopper, creating the customer at most once."""
    row = MaxioCustomer.objects.filter(user=user).first()
    if row is None:
        try:
            with transaction.atomic():
                row = MaxioCustomer.objects.create(
                    user=user, reference=f'oscar-user-{user.pk}-{uuid.uuid4().hex[:12]}',
                    status=MaxioCustomer.SENDING)
        except IntegrityError:
            row = MaxioCustomer.objects.get(user=user)
        else:
            return _send_customer(gateway, row, user)
    if row.status == MaxioCustomer.DONE:
        return row.maxio_customer_id
    if _is_fresh(row) or not _take_over(MaxioCustomer, row):
        raise CustomerInProgress('Your billing account is being set up; try again in a moment.')
    return _send_customer(gateway, row, user)


# -- subscriptions -----------------------------------------------------------------

STATUS_FOR_BUCKET = {
    Bucket.ACTIVE: SubscriptionRequest.DONE,
    Bucket.PROBLEM: SubscriptionRequest.DONE,
    Bucket.INACTIVE: SubscriptionRequest.DONE,
    Bucket.PENDING: SubscriptionRequest.PENDING,
    Bucket.ENDED: SubscriptionRequest.ENDED,
    Bucket.FAILED: SubscriptionRequest.FAILED,
    Bucket.UNKNOWN: SubscriptionRequest.NEEDS_REVIEW,
}


def _copy_info(row, info):
    row.maxio_subscription_id = info.id
    row.state = info.state
    row.currency = info.currency or ''
    row.next_billing_at = info.next_billing_at
    if info.plan_name:
        row.plan_name = info.plan_name


def apply_info(row, info):
    """Refresh a live row from Maxio's current view of its subscription."""
    _copy_info(row, info)
    status = STATUS_FOR_BUCKET[info.bucket]
    if row.status == SubscriptionRequest.NEEDS_REVIEW and status not in SubscriptionRequest.RELEASED:
        status = SubscriptionRequest.NEEDS_REVIEW  # stays visible until an operator resolves it
    row.status = status
    row.save()


def _http_status(row, created):
    if row.status == SubscriptionRequest.DONE:
        return 201 if created else 200
    if row.status == SubscriptionRequest.FAILED:
        return 502
    if row.status == SubscriptionRequest.UNKNOWN:
        return 504
    return 202  # sending, pending, needs_review: accepted, not confirmed


def _settle_subscription(row, info, plan, created):
    _copy_info(row, info)
    status = STATUS_FOR_BUCKET[info.bucket]
    error = ''
    if status in (SubscriptionRequest.DONE, SubscriptionRequest.PENDING) and (
            info.plan_handle != plan.handle or info.price_in_cents != plan.price_in_cents):
        # It exists, but not as asked: keep it visible rather than calling it done.
        status = SubscriptionRequest.NEEDS_REVIEW
        error = (f'Maxio echoed plan {info.plan_handle!r} at {info.price_in_cents} cents; '
                 f'asked for {plan.handle!r} at {plan.price_in_cents} cents.')
        logger.error('Subscription %s needs review: %s', row.reference, error)
    elif status == SubscriptionRequest.FAILED:
        error = f'Maxio reported state {info.state!r}.'
    row.status = status
    row.error = error
    row.save()
    return SubscribeResult(row, _http_status(row, created))


def _reconcile_subscription(gateway, row, plan):
    """Look the subscription up by the reference we sent; absence leaves it unknown."""
    try:
        info = gateway.find_subscription(row.reference)
    except ProviderError:
        info = None
    if info is None:
        _set_status(row, SubscriptionRequest.UNKNOWN, 'Outcome unknown; will be reconciled by reference.')
        return SubscribeResult(row, 504)
    return _settle_subscription(row, info, plan, created=True)


def _send_subscription(gateway, row, plan, customer_id):
    if row.sent_at is not None:
        # An earlier attempt went out under this reference; it may have landed.
        try:
            info = gateway.find_subscription(row.reference)
        except ProviderError:
            _set_status(row, SubscriptionRequest.UNKNOWN, 'Outcome unknown; will be reconciled by reference.')
            return SubscribeResult(row, 504)
        if info is not None:
            return _settle_subscription(row, info, plan, created=False)
        # Definitively absent: re-sending under the SAME reference is safe, because
        # Maxio rejects a reused subscription reference.
    _mark_sent(row)
    try:
        info = gateway.create_subscription(
            customer_id=customer_id, plan_handle=plan.handle, reference=row.reference)
    except ProviderRejected as exc:
        if exc.duplicate_reference:
            return _reconcile_subscription(gateway, row, plan)  # an earlier attempt landed
        _set_status(row, SubscriptionRequest.FAILED, exc.message)
        raise
    except ProviderError as exc:
        if exc.outcome_unknown:
            return _reconcile_subscription(gateway, row, plan)
        _set_status(row, SubscriptionRequest.FAILED, exc.message)
        raise
    return _settle_subscription(row, info, plan, created=True)


def _from_held(gateway, held, plan, customer_id):
    """Answer a request that lost the claim. Returns None when the held row turned out released."""
    if held.status in (SubscriptionRequest.SENDING, SubscriptionRequest.UNKNOWN):
        if _is_fresh(held) or not _take_over(SubscriptionRequest, held):
            return SubscribeResult(held, 202)  # someone else's call may still be in flight
        return _send_subscription(gateway, held, plan, customer_id)
    # done / pending / needs_review: report Maxio's current view of it.
    try:
        info = gateway.find_subscription(held.reference)
    except ProviderError:
        info = None  # cannot refresh now; the stored outcome is still ours to report
    if info is not None:
        apply_info(held, info)
        if held.status in SubscriptionRequest.RELEASED:
            return None
    return SubscribeResult(held, _http_status(held, created=False))


def subscribe(gateway, user, plan_handle):
    plan = gateway.get_plan(plan_handle)
    if plan is None:
        raise PlanNotAvailable(f'Plan {plan_handle!r} is not available.')
    customer_id = ensure_customer(gateway, user)
    for _ in range(3):
        try:
            with transaction.atomic():
                row = SubscriptionRequest.objects.create(
                    user=user, plan_handle=plan.handle, reference=f'oscar-sub-{uuid.uuid4().hex}',
                    status=SubscriptionRequest.SENDING, plan_name=plan.name,
                    price_in_cents=plan.price_in_cents)
        except IntegrityError:
            held = (SubscriptionRequest.objects.filter(user=user, plan_handle=plan.handle)
                    .exclude(status__in=SubscriptionRequest.RELEASED).first())
            if held is None:
                continue  # released in the meantime: claim again
            result = _from_held(gateway, held, plan, customer_id)
            if result is None:
                continue  # the held subscription has ended: the plan is free again
            return result
        return _send_subscription(gateway, row, plan, customer_id)
    raise CustomerInProgress('Subscription is being updated; try again in a moment.')


# -- reading back ---------------------------------------------------------------

def subscription_payload(info=None, row=None):
    if info is not None:
        status = info.bucket.value
        subscription_id, state = info.id, info.state
        plan_handle, plan_name = info.plan_handle, info.plan_name
        price, currency = info.price_in_cents, info.currency
        next_billing_at, reference = info.next_billing_at, info.reference
    else:
        status = row.status
        subscription_id, state = row.maxio_subscription_id, row.state or None
        plan_handle, plan_name = row.plan_handle, row.plan_name or None
        price, currency = row.price_in_cents, row.currency
        next_billing_at, reference = row.next_billing_at, row.reference
    return {
        'subscriptionId': subscription_id,
        'reference': reference,
        'status': status,
        'state': state,
        'reviewRequired': bool(row is not None and row.status == SubscriptionRequest.NEEDS_REVIEW),
        'planHandle': plan_handle,
        'planName': plan_name,
        'price': money(price, currency),
        'nextBillingAt': next_billing_at.isoformat() if next_billing_at else None,
    }


def my_subscriptions(gateway, user):
    rows = list(SubscriptionRequest.objects.filter(user=user))
    by_reference = {row.reference: row for row in rows}
    entries, matched = [], set()
    customer = MaxioCustomer.objects.filter(user=user, status=MaxioCustomer.DONE).first()
    if customer is not None:
        for info in gateway.list_customer_subscriptions(customer.maxio_customer_id):
            row = by_reference.get(info.reference)
            if row is not None:
                matched.add(row.pk)
                if row.status not in SubscriptionRequest.RELEASED:
                    apply_info(row, info)
            entries.append(subscription_payload(info, row))
    # Requests not settled yet (in flight, or outcome unknown) are shown too, never dropped.
    for row in rows:
        if row.pk not in matched and row.status in (
                SubscriptionRequest.SENDING, SubscriptionRequest.UNKNOWN, SubscriptionRequest.PENDING):
            entries.append(subscription_payload(row=row))
    return entries
