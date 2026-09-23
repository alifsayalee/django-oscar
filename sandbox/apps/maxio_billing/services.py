"""
Subscription billing use cases.

Both provider writes (create customer, create subscription) follow the same
shape: claim a durable row first, call Maxio with the row's reference, reconcile
by that reference when the outcome is unclear, verify what Maxio echoed, and only
then settle the row. Maxio rejects a reused customer or subscription reference,
so resending under the *same* reference can never create a duplicate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import gateway
from .gateway import BillingError, PlanView, SubscriptionView
from .models import BillingCustomer, SubscriptionEnrollment

logger = logging.getLogger('apps.maxio_billing')

PLAN_CACHE_SECONDS = 60


class InProgress(Exception):
    """Another request holds the claim and may still be talking to Maxio."""

    def __init__(self, what: str, reference: str) -> None:
        super().__init__(what)
        self.what = what
        self.reference = reference


class UnknownPlan(Exception):
    pass


class CustomerDetailsMissing(Exception):
    pass


class OutcomeUnknown(Exception):
    """Maxio may or may not have acted; the row stays claimed until reconciled."""

    def __init__(self, what: str, reference: str) -> None:
        super().__init__(what)
        self.what = what
        self.reference = reference


def _send_window() -> timedelta:
    # Longer than one provider call can take, so a fresh "sending" row is never
    # mistaken for an abandoned one.
    return timedelta(seconds=float(settings.MAXIO_TIMEOUT_SECONDS) * 2 + 15)


def _is_fresh(updated: datetime) -> bool:
    return updated > timezone.now() - _send_window()


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanCatalog:
    plans: list[PlanView]
    truncated: bool


def list_plans(*, refresh: bool = False) -> PlanCatalog:
    family = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    key = f'maxio_billing:plans:{family}'
    cached = None if refresh else cache.get(key)
    if isinstance(cached, PlanCatalog):
        return cached
    plans, truncated = gateway.list_plans(family)
    catalog = PlanCatalog(plans=plans, truncated=truncated)
    cache.set(key, catalog, PLAN_CACHE_SECONDS)
    return catalog


def get_plan(handle: str) -> PlanView:
    """The plan must be one the family listing returns (never trust the caller's handle)."""
    for refresh in (False, True):
        for plan in list_plans(refresh=refresh).plans:
            if plan.handle == handle:
                return plan
    raise UnknownPlan(handle)


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

def _customer_names(user, first_name: str, last_name: str) -> tuple[str, str, str]:
    email = (user.email or '').strip()
    if not email:
        raise CustomerDetailsMissing('Your account has no email address.')
    first = (first_name or user.first_name or '').strip() or email.split('@')[0]
    last = (last_name or user.last_name or '').strip() or 'Customer'
    return first, last, email


def _take_over(model: type[BillingCustomer] | type[SubscriptionEnrollment], pk: int,
               from_statuses: tuple[str, ...], stale_before: datetime) -> bool:
    """
    Atomically move a row back to 'sending'; True only for the one request that
    did. A 'sending' row still inside the send window is never taken over.
    """
    qs = model.objects.filter(pk=pk, status__in=from_statuses).exclude(
        status=model.SENDING, updated__gt=stale_before)
    changed: int = qs.update(status=model.SENDING, updated=timezone.now())
    return changed == 1


def ensure_customer(user, *, first_name: str = '', last_name: str = '') -> BillingCustomer:
    first, last, email = _customer_names(user, first_name, last_name)

    try:
        with transaction.atomic():
            row = BillingCustomer.objects.create(user=user)
    except IntegrityError:
        row = BillingCustomer.objects.get(user=user)
        if row.status == BillingCustomer.LINKED:
            return row
        if row.status == BillingCustomer.SENDING and _is_fresh(row.updated):
            raise InProgress('customer', row.reference)
        if row.status in (BillingCustomer.SENDING, BillingCustomer.UNKNOWN):
            # Look before sending again: an earlier attempt may have landed.
            found = gateway.find_customer_id(row.reference)
            if found is not None:
                return _link_customer(row, found)
        stale_before = timezone.now() - _send_window()
        if not _take_over(BillingCustomer, row.pk,
                          (BillingCustomer.SENDING, BillingCustomer.UNKNOWN, BillingCustomer.FAILED),
                          stale_before):
            raise InProgress('customer', row.reference)
        row.refresh_from_db()

    return _send_customer(row, first, last, email)


def _link_customer(row: BillingCustomer, customer_id: int) -> BillingCustomer:
    row.maxio_customer_id = customer_id
    row.status = BillingCustomer.LINKED
    row.save(update_fields=['maxio_customer_id', 'status', 'updated'])
    return row


def _mark(row, status: str) -> None:
    row.status = status
    row.save(update_fields=['status', 'updated'])


def _send_customer(row: BillingCustomer, first: str, last: str, email: str) -> BillingCustomer:
    try:
        customer_id = gateway.create_customer(
            reference=row.reference, first_name=first, last_name=last, email=email)
    except BillingError as e:
        if isinstance(e, gateway.ProviderRejected) and e.provider_status == 422:
            # "Reference already taken" means an earlier attempt landed.
            found = _lookup_customer_or_unknown(row)
            if found is not None:
                return _link_customer(row, found)
            _mark(row, BillingCustomer.FAILED)
            raise
        if not e.outcome_unknown:
            _mark(row, BillingCustomer.FAILED)     # nothing happened; release for a retry
            raise
        found = _lookup_customer_or_unknown(row)
        if found is None:
            _mark(row, BillingCustomer.UNKNOWN)
            raise OutcomeUnknown('customer', row.reference) from e
        return _link_customer(row, found)
    return _link_customer(row, customer_id)


def _lookup_customer_or_unknown(row: BillingCustomer) -> int | None:
    try:
        return gateway.find_customer_id(row.reference)
    except BillingError as e:
        _mark(row, BillingCustomer.UNKNOWN)
        raise OutcomeUnknown('customer', row.reference) from e


def get_customer(user) -> BillingCustomer | None:
    return BillingCustomer.objects.filter(user=user).first()


# ---------------------------------------------------------------------------
# Subscribe
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubscribeResult:
    enrollment: SubscriptionEnrollment
    subscription: SubscriptionView | None
    existing: bool


def subscribe(user, plan_handle: str, *, first_name: str = '', last_name: str = '') -> SubscribeResult:
    plan = get_plan(plan_handle)
    customer = ensure_customer(user, first_name=first_name, last_name=last_name)
    # Two passes at most: the second only when the first found the held
    # subscription has since ended in Maxio, which releases the plan again.
    for _ in range(2):
        result = _claim_and_subscribe(user, plan, customer)
        if result is not None:
            return result
    raise InProgress('subscription', '')


def _existing(row: SubscriptionEnrollment, customer: BillingCustomer) -> SubscribeResult | None:
    """Answer a repeat request from Maxio's current view; None if it has ended meanwhile."""
    view = None
    if row.maxio_subscription_id is not None:
        try:
            view = gateway.read_subscription(row.maxio_subscription_id)
        except BillingError:
            logger.warning('maxio read of %s failed; answering from the stored snapshot',
                           row.maxio_subscription_id)
        if view is not None and row.status != SubscriptionEnrollment.NEEDS_REVIEW:
            _settle(row, view, None, customer)
            if row.status in SubscriptionEnrollment.RELEASED:
                return None
    return SubscribeResult(row, view, existing=True)


def _claim_and_subscribe(user, plan: PlanView, customer: BillingCustomer) -> SubscribeResult | None:
    try:
        with transaction.atomic():
            row = SubscriptionEnrollment.objects.create(
                user=user, customer=customer, plan_handle=plan.handle,
                plan_name=plan.name, price_in_cents=plan.price_in_cents)
    except IntegrityError:
        # A live request for this plan already exists: answer from it, never create another.
        held = SubscriptionEnrollment.objects.exclude(
            status__in=SubscriptionEnrollment.RELEASED).filter(user=user, plan_handle=plan.handle).first()
        if held is None:    # the holder was released a moment ago; let the caller resubmit
            raise InProgress('subscription', '')
        row = held
        if row.status not in (SubscriptionEnrollment.SENDING, SubscriptionEnrollment.UNKNOWN):
            return _existing(row, customer)
        if row.status == SubscriptionEnrollment.SENDING and _is_fresh(row.updated):
            raise InProgress('subscription', row.reference)
        found = gateway.find_subscription(row.reference)
        if found is not None:
            return SubscribeResult(_settle(row, found, plan, customer), found, existing=True)
        stale_before = timezone.now() - _send_window()
        if not _take_over(SubscriptionEnrollment, row.pk,
                          (SubscriptionEnrollment.SENDING, SubscriptionEnrollment.UNKNOWN), stale_before):
            raise InProgress('subscription', row.reference)
        row.refresh_from_db()

    view = _send_subscription(row, plan, customer)
    return SubscribeResult(row, view, existing=False)


def _send_subscription(row: SubscriptionEnrollment, plan: PlanView,
                       customer: BillingCustomer) -> SubscriptionView:
    assert customer.maxio_customer_id is not None
    try:
        view = gateway.create_subscription(
            reference=row.reference, customer_id=customer.maxio_customer_id,
            plan_handle=plan.handle,
            payment_collection_method=settings.MAXIO_PAYMENT_COLLECTION_METHOD)
    except BillingError as e:
        if isinstance(e, gateway.ProviderRejected) and e.provider_status == 422:
            found = _lookup_subscription_or_unknown(row)   # "reference taken" = an earlier attempt landed
            if found is not None:
                _settle(row, found, plan, customer)
                return found
            row.failure_reason = '; '.join(e.details)[:1000]
            row.status = SubscriptionEnrollment.FAILED
            row.save(update_fields=['failure_reason', 'status', 'updated'])
            raise
        if not e.outcome_unknown:
            row.failure_reason = e.message
            row.status = SubscriptionEnrollment.FAILED
            row.save(update_fields=['failure_reason', 'status', 'updated'])
            raise
        found = _lookup_subscription_or_unknown(row)
        if found is None:
            _mark(row, SubscriptionEnrollment.UNKNOWN)
            raise OutcomeUnknown('subscription', row.reference) from e
        view = found
    _settle(row, view, plan, customer)
    return view


def _lookup_subscription_or_unknown(row: SubscriptionEnrollment) -> SubscriptionView | None:
    try:
        return gateway.find_subscription(row.reference)
    except BillingError as e:
        _mark(row, SubscriptionEnrollment.UNKNOWN)
        raise OutcomeUnknown('subscription', row.reference) from e


def _settle(row: SubscriptionEnrollment, view: SubscriptionView, plan: PlanView | None,
            customer: BillingCustomer) -> SubscriptionEnrollment:
    """
    Record what Maxio said. With ``plan`` (a create we just made or reconciled)
    the echo is verified first: a subscription that does not match the request is
    kept but flagged for review rather than reported as active. Without it (a
    later read-back) only the snapshot and status are refreshed.
    """
    mismatches = []
    if plan is not None:
        if view.plan_handle != row.plan_handle:
            mismatches.append(f'plan {view.plan_handle!r} != {row.plan_handle!r}')
        if view.customer_id != customer.maxio_customer_id:
            mismatches.append(f'customer {view.customer_id} != {customer.maxio_customer_id}')
        if plan.price_in_cents is not None and view.price_in_cents != plan.price_in_cents:
            mismatches.append(f'price {view.price_in_cents} != {plan.price_in_cents}')

    row.maxio_subscription_id = view.id
    row.state = view.state
    row.plan_name = view.plan_name or row.plan_name
    row.price_in_cents = view.price_in_cents
    row.currency = view.currency or ''
    row.next_billing_at = view.next_billing_at
    if mismatches:
        logger.error('maxio subscription %s does not match request %s: %s',
                     view.id, row.reference, '; '.join(mismatches))
        row.status = SubscriptionEnrollment.NEEDS_REVIEW
        row.failure_reason = '; '.join(mismatches)
    else:
        row.status = view.status
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        # Reviving a released row would give the user two live rows for one plan
        # (e.g. an old subscription reactivated in Maxio): keep the snapshot only.
        logger.warning('maxio subscription %s: status %s not recorded for %s (another live row)',
                       view.id, row.status, row.reference)
        row.status = SubscriptionEnrollment.objects.values_list('status', flat=True).get(pk=row.pk)
        row.save()
    return row


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MySubscriptions:
    subscriptions: list[tuple[SubscriptionView, SubscriptionEnrollment | None]]
    unsettled: list[SubscriptionEnrollment]


def my_subscriptions(user) -> MySubscriptions:
    customer = get_customer(user)
    rows = list(SubscriptionEnrollment.objects.filter(user=user))
    if customer is None or customer.status != BillingCustomer.LINKED or customer.maxio_customer_id is None:
        return MySubscriptions([], [r for r in rows if r.maxio_subscription_id is None
                                    and r.status not in SubscriptionEnrollment.RELEASED])

    views = gateway.list_customer_subscriptions(customer.maxio_customer_id)
    by_id = {r.maxio_subscription_id: r for r in rows if r.maxio_subscription_id is not None}
    by_ref = {r.reference: r for r in rows}
    result = []
    for view in views:
        row = by_id.get(view.id) or (by_ref.get(view.reference) if view.reference else None)
        if row is not None and (row.status != SubscriptionEnrollment.NEEDS_REVIEW):
            if row.maxio_subscription_id is None or row.state != view.state:
                _settle(row, view, None, customer)
        result.append((view, row))
    seen = {view.id for view in views}
    unsettled = [r for r in rows if r.maxio_subscription_id not in seen
                 and r.status in (SubscriptionEnrollment.SENDING, SubscriptionEnrollment.UNKNOWN)]
    return MySubscriptions(result, unsettled)


def read_subscription(user, subscription_id: int) -> tuple[SubscriptionView, SubscriptionEnrollment | None] | None:
    """One subscription, only if it belongs to the caller's Maxio customer."""
    customer = get_customer(user)
    if customer is None or customer.maxio_customer_id is None:
        return None
    view = gateway.read_subscription(subscription_id)
    if view is None or view.customer_id != customer.maxio_customer_id:
        return None
    row = SubscriptionEnrollment.objects.filter(user=user, maxio_subscription_id=view.id).first()
    if row is not None and row.status != SubscriptionEnrollment.NEEDS_REVIEW and row.state != view.state:
        _settle(row, view, None, customer)
    return view, row
