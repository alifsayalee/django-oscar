"""
Subscription billing use cases: list plans, ensure a Maxio customer, subscribe, read subscriptions.

Writes follow one pattern: claim a durable local row first (a unique constraint picks the single
winner), then call Maxio once with a reference we generated, reconcile by that reference when the
outcome could not be read, verify what Maxio echoed back, and settle the row from the state Maxio
reported.
"""
import datetime
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, TypeVar

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from maxio_advanced_billing.core import UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest, Customer, Product,
    Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from . import maxio
from .maxio import ProviderError
from .models import BillingCustomer, SubscriptionRequest

logger = logging.getLogger(__name__)

T = TypeVar('T')

PLANS_CACHE_SECONDS = 60
PLANS_PER_PAGE = 200            # the provider's maximum page size
PLANS_MAX_PAGES = 10            # backstop: never walk an unbounded listing
# How long an in-flight claim is trusted before another request may look at it:
# comfortably above one call's timeout (no retries on writes).
SEND_WINDOW = datetime.timedelta(seconds=60)


# --- Errors the views answer ---------------------------------------------------------------------

class BillingError(Exception):
    status_code = 400

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra


class UnknownPlan(BillingError):
    status_code = 400


class SubscriptionNotFound(BillingError):
    status_code = 404


class CustomerSetupInProgress(BillingError):
    status_code = 409


class OutcomeUnknown(BillingError):
    """The subscribe call may have landed at Maxio; the request row stays UNKNOWN and is reconciled."""
    status_code = 504


# --- Reading SDK values safely -------------------------------------------------------------------

def present(value: 'T | UnsetType | None') -> Optional[T]:
    """An SDK member as a plain value: UNSET (never sent by Maxio) becomes None."""
    if isinstance(value, UnsetType):
        return None
    return value


def wire(value: object) -> Optional[str]:
    """Open-enum members and unknown strings alike, as their wire value."""
    value = present(value)
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def iso(value: 'datetime.datetime | UnsetType | None') -> Optional[str]:
    moment = present(value)
    return moment.isoformat() if moment is not None else None


def money(cents: Optional[int]) -> Optional[str]:
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal('0.01')))


# --- Subscription state -> our outcome ------------------------------------------------------------

def status_from_state(state: object) -> str:
    """The one place a Maxio subscription state becomes one of our request statuses."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return SubscriptionRequest.ACTIVE
        case SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP:
            return SubscriptionRequest.PENDING
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID
              | SubscriptionState.PAUSED | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            return SubscriptionRequest.ATTENTION
        case SubscriptionState.FAILED_TO_CREATE:
            return SubscriptionRequest.FAILED
        case SubscriptionState.CANCELED | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED:
            return SubscriptionRequest.ENDED
        case _:
            return SubscriptionRequest.UNKNOWN   # unset, or a state newer than this SDK: neither done nor failed


# --- Plans ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    handle: str
    name: Optional[str]
    description: Optional[str]
    price_in_cents: Optional[int]
    interval: Optional[int]
    interval_unit: Optional[str]
    trial_price_in_cents: Optional[int]
    trial_interval: Optional[int]
    trial_interval_unit: Optional[str]
    initial_charge_in_cents: Optional[int]
    requires_payment_method: Optional[bool]
    maxio_product_id: Optional[int]

    def as_json(self) -> dict[str, Any]:
        return {
            'planHandle': self.handle,
            'name': self.name,
            'description': self.description,
            'priceInCents': self.price_in_cents,
            'price': money(self.price_in_cents),
            'interval': self.interval,
            'intervalUnit': self.interval_unit,
            'trialPriceInCents': self.trial_price_in_cents,
            'trialInterval': self.trial_interval,
            'trialIntervalUnit': self.trial_interval_unit,
            'setupFeeInCents': self.initial_charge_in_cents,
            'requiresPaymentMethod': self.requires_payment_method,
            'maxioProductId': self.maxio_product_id,
        }


@dataclass(frozen=True)
class PlanList:
    product_family: str
    plans: list[Plan]
    truncated: bool


def _plan_from_product(product: Product) -> Optional[Plan]:
    handle = present(product.handle)
    if not handle or present(product.archived_at) is not None:
        return None
    return Plan(
        handle=handle,
        name=present(product.name),
        description=present(product.description) or None,
        price_in_cents=present(product.price_in_cents),
        interval=present(product.interval),
        interval_unit=wire(product.interval_unit),
        trial_price_in_cents=present(product.trial_price_in_cents),
        trial_interval=present(product.trial_interval),
        trial_interval_unit=wire(product.trial_interval_unit),
        initial_charge_in_cents=present(product.initial_charge_in_cents),
        requires_payment_method=present(product.require_credit_card),
        maxio_product_id=present(product.id),
    )


def _fetch_plans(family: str) -> PlanList:
    plans: list[Plan] = []
    truncated = False
    for page in range(1, PLANS_MAX_PAGES + 1):
        batch = maxio.read('list_products_for_product_family', lambda c: c.product_families
                           .list_products_for_product_family('handle:' + family, page=page, per_page=PLANS_PER_PAGE))
        for item in batch:
            plan = _plan_from_product(item.product)
            if plan is not None:
                plans.append(plan)
        if len(batch) < PLANS_PER_PAGE:
            break
    else:
        truncated = True
        logger.warning('Plan listing for %s stopped at %d pages', family, PLANS_MAX_PAGES)
    return PlanList(product_family=family, plans=plans, truncated=truncated)


def list_plans(*, fresh: bool = False) -> PlanList:
    family = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    key = 'subscriptions:plans:%s' % family
    cached = None if fresh else cache.get(key)
    if isinstance(cached, PlanList):
        return cached
    try:
        result = _fetch_plans(family)
    except ProviderError as exc:
        if exc.provider_status == 404:
            # The configured product family does not exist on this Maxio site: our configuration.
            raise ProviderError(502, 'Configured product family was not found at the billing provider.',
                                provider_status=404) from exc
        raise
    cache.set(key, result, PLANS_CACHE_SECONDS)
    return result


def find_plan(handle: str) -> Plan:
    """A plan handle is only valid if the plan listing offers it (checked against a fresh list on a miss)."""
    for fresh in (False, True):
        for plan in list_plans(fresh=fresh).plans:
            if plan.handle == handle:
                return plan
    raise UnknownPlan('Unknown planHandle; choose one from GET /api/subscription-plans.', planHandle=handle)


# --- Customer --------------------------------------------------------------------------------------

def customer_reference(user: Any) -> str:
    """Deterministic per user, and distinct across database rebuilds that reuse primary keys."""
    joined = int(user.date_joined.timestamp()) if getattr(user, 'date_joined', None) else 0
    return 'oscar-user-%s-%s' % (user.pk, joined)


def _find_customer(reference: str) -> Optional[Customer]:
    """Maxio's customer for this reference; None only on Maxio's own 404."""
    try:
        return maxio.read('read_customer_by_reference',
                          lambda c: c.customers.read_customer_by_reference(reference)).customer
    except ProviderError as exc:
        if exc.provider_status == 404:
            return None
        raise


def _customer_body(user: Any, reference: str) -> CreateCustomerRequest:
    email = (user.email or '').strip()
    local_part = email.split('@')[0] if email else ''
    return CreateCustomerRequest(customer=CreateCustomer(
        first_name=(user.first_name or '').strip() or local_part or 'Customer',
        last_name=(user.last_name or '').strip() or 'Customer',
        email=email,
        reference=reference,
    ))


def _link(row: BillingCustomer, customer: Customer) -> int:
    customer_id = present(customer.id)
    if not isinstance(customer_id, int):
        raise ProviderError(502, 'Billing provider returned a customer without an id.', outcome_unknown=True)
    BillingCustomer.objects.filter(pk=row.pk).update(maxio_customer_id=customer_id, claimed_at=None,
                                                     updated_at=timezone.now())
    row.maxio_customer_id = customer_id
    return customer_id


def ensure_customer(user: Any) -> int:
    """The Maxio customer id for this user, creating the customer at most once."""
    if not user.email:
        raise BillingError('Your account needs an email address before subscribing.')
    row, _ = BillingCustomer.objects.get_or_create(user=user, defaults={'reference': customer_reference(user)})
    if row.maxio_customer_id:
        return row.maxio_customer_id

    # Claim the right to create: one conditional write decides, so only one request calls create_customer.
    now = timezone.now()
    claimed = BillingCustomer.objects.filter(
        Q(claimed_at__isnull=True) | Q(claimed_at__lt=now - SEND_WINDOW), pk=row.pk, maxio_customer_id__isnull=True,
    ).update(claimed_at=now)

    existing = _find_customer(row.reference)     # a previous attempt may already have created it
    if existing is not None:
        return _link(row, existing)
    if not claimed:
        raise CustomerSetupInProgress('Your billing account is being set up; retry in a moment.')

    try:
        created = maxio.call('create_customer',
                             lambda c: c.customers.create_customer(body=_customer_body(user, row.reference)))
        return _link(row, created.customer)
    except ProviderError as exc:
        if exc.outcome_unknown or exc.provider_status == 422:
            # It may have landed, or the reference is already taken by an earlier attempt: look, never re-create.
            found = _find_customer(row.reference)
            if found is not None:
                return _link(row, found)
        if not exc.outcome_unknown:
            BillingCustomer.objects.filter(pk=row.pk).update(claimed_at=None)   # nothing happened: release
        raise


def linked_customer_id(user: Any) -> Optional[int]:
    row = BillingCustomer.objects.filter(user=user).first()
    if row is None:
        return None
    if row.maxio_customer_id:
        return row.maxio_customer_id
    found = _find_customer(row.reference)
    return _link(row, found) if found is not None else None


# --- Subscriptions ---------------------------------------------------------------------------------

@dataclass
class SubscribeResult:
    request: SubscriptionRequest
    subscription: Optional[Subscription] = None
    created: bool = False                      # True only for the request that created it
    notes: list[str] = field(default_factory=list)


def subscription_json(sub: Subscription) -> dict[str, Any]:
    product = present(sub.product)
    customer = present(sub.customer)
    price = present(sub.product_price_in_cents)
    return {
        'subscriptionId': present(sub.id),
        'state': wire(sub.state),
        'status': status_from_state(present(sub.state)),
        'planHandle': present(product.handle) if product else None,
        'planName': present(product.name) if product else None,
        'priceInCents': price,
        'price': money(price),
        'interval': present(product.interval) if product else None,
        'intervalUnit': wire(product.interval_unit) if product else None,
        'nextBillingAt': iso(sub.current_period_ends_at),
        'nextAssessmentAt': iso(sub.next_assessment_at),
        'currentPeriodStartedAt': iso(sub.current_period_started_at),
        'activatedAt': iso(sub.activated_at),
        'createdAt': iso(sub.created_at),
        'canceledAt': iso(sub.canceled_at),
        'customerId': present(customer.id) if customer else None,
        'reference': present(sub.reference),
    }


def request_json(row: SubscriptionRequest) -> dict[str, Any]:
    return {
        'reference': row.reference,
        'planHandle': row.plan_handle,
        'status': row.status,
        'subscriptionId': row.maxio_subscription_id,
        'state': row.maxio_state or None,
        'requestedAt': row.created_at.isoformat() if row.created_at else None,
    }


def _subscription_of(response: SubscriptionResponse) -> Subscription:
    sub = present(response.subscription)
    if sub is None or not isinstance(present(sub.id), int):
        raise ProviderError(502, 'Billing provider returned no subscription.', outcome_unknown=True)
    return sub


def _find_by_reference(reference: str) -> Optional[Subscription]:
    try:
        response = maxio.read('find_subscription', lambda c: c.subscriptions.find_subscription(reference=reference))
    except ProviderError as exc:
        if exc.provider_status == 404:
            return None
        raise
    sub = _subscription_of(response)
    if present(sub.reference) not in (None, reference):
        raise ProviderError(502, 'Billing provider returned a different subscription for our reference.')
    return sub


def _save(row: SubscriptionRequest, **values: Any) -> None:
    for name, value in values.items():
        setattr(row, name, value)
    row.save(update_fields=list(values) + ['updated_at'])


def _settle(row: SubscriptionRequest, sub: Subscription, *, verify: bool) -> None:
    """Record what Maxio says about the subscription; verify it is what was asked for first."""
    state = present(sub.state)
    values: dict[str, Any] = {
        'maxio_subscription_id': present(sub.id),
        'maxio_state': wire(state) or '',
        'status': status_from_state(state),
    }
    if verify:
        product = present(sub.product)
        echoed_handle = present(product.handle) if product else None
        echoed_price = present(sub.product_price_in_cents)
        mismatches = []
        if echoed_handle != row.plan_handle:
            mismatches.append('plan %r != %r' % (echoed_handle, row.plan_handle))
        if row.expected_price_in_cents is not None and echoed_price != row.expected_price_in_cents:
            mismatches.append('price %r != %r' % (echoed_price, row.expected_price_in_cents))
        if mismatches:
            logger.error('Subscription %s created but not as asked: %s', row.reference, '; '.join(mismatches))
            values.update(status=SubscriptionRequest.NEEDS_REVIEW, detail='; '.join(mismatches))
    elif row.status == SubscriptionRequest.NEEDS_REVIEW and values['status'] != SubscriptionRequest.ENDED:
        values['status'] = SubscriptionRequest.NEEDS_REVIEW        # a refresh never clears a review flag
    _save(row, **values)


def _claim(user: Any, plan: Plan) -> Optional[SubscriptionRequest]:
    try:
        with transaction.atomic():
            return SubscriptionRequest.objects.create(
                user=user, plan_handle=plan.handle, reference='sub-' + uuid.uuid4().hex,
                status=SubscriptionRequest.SENDING, expected_price_in_cents=plan.price_in_cents)
    except IntegrityError:
        return None


def _reconcile(row: SubscriptionRequest) -> SubscribeResult:
    """Look the request up at Maxio by the reference it was sent with. Never creates."""
    try:
        found = _find_by_reference(row.reference)
    except ProviderError:
        _save(row, status=SubscriptionRequest.UNKNOWN)
        raise
    if found is None:
        # Not found (yet) cannot prove it never happened: a timed-out create can still land.
        _save(row, status=SubscriptionRequest.UNKNOWN)
        raise OutcomeUnknown('The subscription request could not be confirmed yet; retry the same request later.',
                             reference=row.reference, planHandle=row.plan_handle)
    _settle(row, found, verify=True)
    return SubscribeResult(request=row, subscription=found)


def _answer_held(user: Any, plan: Plan, held: SubscriptionRequest, *, may_reclaim: bool) -> SubscribeResult:
    """Another request already holds this (user, plan): answer from its row; never call create."""
    fresh = held.updated_at > timezone.now() - SEND_WINDOW
    if held.status == SubscriptionRequest.SENDING and fresh:
        return SubscribeResult(request=held, notes=['A subscribe request for this plan is already in progress.'])
    if held.status == SubscriptionRequest.SENDING and held.sent_at is None:
        # A stale claim that provably never reached Maxio: release it and start over.
        _save(held, status=SubscriptionRequest.FAILED, detail='abandoned before sending')
        return subscribe(user, plan.handle, _may_reclaim=False)
    held_id = held.maxio_subscription_id
    if held_id is None:                                     # stale SENDING (sent) or UNKNOWN
        return _reconcile(held)

    sub = _subscription_of(maxio.read('read_subscription',
                                      lambda c: c.subscriptions.read_subscription(held_id)))
    _settle(held, sub, verify=False)
    if held.status == SubscriptionRequest.ENDED and may_reclaim:
        return subscribe(user, plan.handle, _may_reclaim=False)   # the old one ended: a new one is allowed
    return SubscribeResult(request=held, subscription=sub,
                           notes=['You already have a subscription to this plan.'])


def subscribe(user: Any, plan_handle: str, *, _may_reclaim: bool = True) -> SubscribeResult:
    plan = find_plan(plan_handle)

    # 1. Claim first: the partial unique constraint lets exactly one live request per (user, plan) exist.
    row = _claim(user, plan)
    if row is None:
        held = SubscriptionRequest.objects.get(
            user=user, plan_handle=plan.handle, status__in=SubscriptionRequest.HOLDING)
        logger.info('Subscribe %s/%s: claim held by %s (%s)', user.pk, plan.handle, held.reference, held.status)
        return _answer_held(user, plan, held, may_reclaim=_may_reclaim)
    logger.info('Subscribe %s/%s: claimed as %s', user.pk, plan.handle, row.reference)

    # 2. The customer. Any failure here means the subscription was never sent: release the claim.
    try:
        customer_id = ensure_customer(user)
    except Exception as exc:
        _save(row, status=SubscriptionRequest.FAILED, detail='customer setup failed: %s' % type(exc).__name__)
        raise

    # 3. Create, once, under our reference.
    # This API never captures a payment method, so Maxio must collect by invoice (remittance): under
    # automatic collection the signup charge is refused with "No payment method was on file".
    body = CreateSubscriptionRequest(subscription=CreateSubscription(
        product_handle=plan.handle, customer_id=customer_id, reference=row.reference,
        payment_collection_method=CollectionMethod.REMITTANCE))
    _save(row, sent_at=timezone.now())
    try:
        created = _subscription_of(maxio.call('create_subscription',
                                              lambda c: c.subscriptions.create_subscription(body=body)))
    except ProviderError as exc:
        if not exc.outcome_unknown:
            # Rejected, refused or never sent: nothing was created. Release the claim.
            _save(row, status=SubscriptionRequest.FAILED, detail='; '.join(exc.details) or exc.message)
            raise
        # 4. It may have landed: reconcile by the reference we sent.
        return _reconcile(row)

    # 5. Verify, then settle from the state Maxio reported.
    _settle(row, created, verify=True)
    return SubscribeResult(request=row, subscription=created, created=True)


def my_subscriptions(user: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The caller's subscriptions as Maxio reports them, plus local requests not yet confirmed."""
    subscriptions: list[dict[str, Any]] = []
    customer_id = linked_customer_id(user)
    if customer_id is not None:
        responses = maxio.read('list_customer_subscriptions',
                               lambda c: c.customers.list_customer_subscriptions(customer_id))
        by_id = {r.maxio_subscription_id: r for r in SubscriptionRequest.objects.filter(
            user=user, status__in=SubscriptionRequest.HOLDING, maxio_subscription_id__isnull=False)}
        for response in responses:
            sub = present(response.subscription)
            if sub is None:
                continue
            row = by_id.get(present(sub.id))
            if row is not None:
                _settle(row, sub, verify=False)      # keep the claim in step (an ended one releases it)
            subscriptions.append(subscription_json(sub))
    unconfirmed = [request_json(r) for r in SubscriptionRequest.objects.filter(
        user=user, status__in=[SubscriptionRequest.SENDING, SubscriptionRequest.UNKNOWN,
                               SubscriptionRequest.NEEDS_REVIEW])]
    return subscriptions, unconfirmed


def get_subscription(user: Any, subscription_id: int) -> Subscription:
    customer_id = linked_customer_id(user)
    if customer_id is None:
        raise SubscriptionNotFound('Subscription not found.')
    try:
        sub = _subscription_of(maxio.read('read_subscription',
                                          lambda c: c.subscriptions.read_subscription(subscription_id)))
    except ProviderError as exc:
        if exc.provider_status == 404:
            raise SubscriptionNotFound('Subscription not found.') from exc
        raise
    customer = present(sub.customer)
    if customer is None or present(customer.id) != customer_id:
        raise SubscriptionNotFound('Subscription not found.')     # not the caller's: do not reveal it exists
    row = SubscriptionRequest.objects.filter(
        user=user, maxio_subscription_id=subscription_id, status__in=SubscriptionRequest.HOLDING).first()
    if row is not None:
        _settle(row, sub, verify=False)
    return sub
