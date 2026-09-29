"""
Subscription billing use-cases, with Maxio Advanced Billing as system of record.

Each public function is one caller-visible action; views stay thin.
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.core import UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
    Customer,
    Product,
    Subscription,
    SubscriptionResponse,
)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from .maxio import MaxioError, call, get_client
from .models import MaxioSubscriptionEnrollment as Enrollment

logger = logging.getLogger('apps.subscriptions')

V = TypeVar('V')

#: Subscription states after which the subscription no longer bills.
ENDED_STATES = frozenset({SubscriptionState.CANCELED, SubscriptionState.EXPIRED, SubscriptionState.FAILED_TO_CREATE})

#: A pending claim older than this is assumed to belong to a request that died.
STALE_PENDING_AFTER = datetime.timedelta(minutes=5)


class SubscriptionConflict(Exception):
    """A subscribe for this user and plan is already in flight."""


class InvalidRequest(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _v(value: V | None | UnsetType) -> V | None:
    """Resolve the SDK's UNSET sentinel to None before a value leaves this module."""
    return None if isinstance(value, UnsetType) else value


def _iso(value: datetime.datetime | None | UnsetType) -> str | None:
    resolved = _v(value)
    return resolved.isoformat() if resolved is not None else None


def _money(cents: int | None) -> str | None:
    return None if cents is None else str((Decimal(cents) / 100).quantize(Decimal('0.01')))


def family_handle() -> str:
    handle = (getattr(settings, 'MAXIO_DEFAULT_PRODUCT_FAMILY', '') or '').strip()
    if not handle:
        raise ImproperlyConfigured('MAXIO_DEFAULT_PRODUCT_FAMILY must be set')
    return handle


def collection_method() -> CollectionMethod:
    """How Maxio collects payment for new subscriptions.

    The shop captures no card, so the default is ``remittance``: Maxio issues
    the invoice as open and does not attempt payment. Sites on the legacy
    Statements architecture use ``invoice`` instead.
    """
    raw = (getattr(settings, 'MAXIO_PAYMENT_COLLECTION_METHOD', '') or CollectionMethod.REMITTANCE.value)
    try:
        return CollectionMethod(raw.strip().lower())
    except ValueError:
        raise ImproperlyConfigured(
            'MAXIO_PAYMENT_COLLECTION_METHOD must be one of %s, got %r'
            % ([m.value for m in CollectionMethod], raw)) from None


def customer_reference(user: Any) -> str:
    prefix = getattr(settings, 'MAXIO_CUSTOMER_REFERENCE_PREFIX', 'oscar-sandbox-user-')
    return '%s%s' % (prefix, user.pk)


# ---------------------------------------------------------------------------
# Serialisation of Maxio objects into our API's shape
# ---------------------------------------------------------------------------

def plan_to_dict(product: Product) -> dict[str, Any]:
    price_in_cents = _v(product.price_in_cents)
    interval_unit = _v(product.interval_unit)
    return {
        'planHandle': _v(product.handle),
        'productId': _v(product.id),
        'name': _v(product.name),
        'description': _v(product.description) or '',
        'priceInCents': price_in_cents,
        'price': _money(price_in_cents),
        'interval': _v(product.interval),
        'intervalUnit': str(interval_unit) if interval_unit is not None else None,
        'requiresPaymentMethod': bool(_v(product.require_credit_card)),
    }


def customer_to_dict(customer: Customer) -> dict[str, Any]:
    return {
        'customerId': _v(customer.id),
        'reference': _v(customer.reference),
        'email': _v(customer.email),
        'firstName': _v(customer.first_name),
        'lastName': _v(customer.last_name),
    }


def subscription_to_dict(subscription: Subscription) -> dict[str, Any]:
    product = _v(subscription.product)
    customer = _v(subscription.customer)
    state = _v(subscription.state)
    price_in_cents = _v(subscription.product_price_in_cents)
    if price_in_cents is None and product is not None:
        price_in_cents = _v(product.price_in_cents)
    return {
        'subscriptionId': _v(subscription.id),
        'state': str(state) if state is not None else None,
        'planHandle': _v(product.handle) if product is not None else None,
        'planName': _v(product.name) if product is not None else None,
        'priceInCents': price_in_cents,
        'price': _money(price_in_cents),
        'currency': _v(subscription.currency),
        'interval': _v(product.interval) if product is not None else None,
        'intervalUnit': (str(_v(product.interval_unit)) if product is not None
                         and _v(product.interval_unit) is not None else None),
        'nextBillingAt': _iso(subscription.next_assessment_at),
        'currentPeriodEndsAt': _iso(subscription.current_period_ends_at),
        'activatedAt': _iso(subscription.activated_at),
        'createdAt': _iso(subscription.created_at),
        'canceledAt': _iso(subscription.canceled_at),
        'customerId': _v(customer.id) if customer is not None else None,
    }


def _require_subscription(response: SubscriptionResponse, *, action: str, write: bool) -> Subscription:
    """A subscription response whose body lacks the subscription or its id is not a success."""
    subscription = _v(response.subscription)
    if subscription is None or _v(subscription.id) is None:
        logger.error('Maxio %s returned no subscription id', action)
        raise MaxioError(502, 'Billing provider returned an incomplete subscription.', outcome_unknown=write)
    return subscription


def _is_live(subscription: Subscription) -> bool:
    state = _v(subscription.state)
    # Unknown (newer) states are treated as live: never double-subscribe on a guess.
    return state not in ENDED_STATES


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def list_plans() -> list[dict[str, Any]]:
    client = get_client()
    family = family_handle()
    plans: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = call(
            lambda: client.product_families.list_products_for_product_family(
                'handle:%s' % family, page=page, per_page=200),
            action='list plans')
        for item in batch:
            product = item.product
            handle = _v(product.handle)
            if handle and _v(product.archived_at) is None:
                plans.append(plan_to_dict(product))
        if len(batch) < 200:
            return plans
        page += 1


def get_plan(plan_handle: str) -> Product:
    """The plan with ``plan_handle``, only if it is an active plan in the configured family."""
    client = get_client()
    try:
        product = call(lambda: client.products.read_product_by_handle(plan_handle), action='read plan').product
    except MaxioError as e:
        if e.status_code == 404:
            raise InvalidRequest(404, 'Unknown plan %r.' % plan_handle) from e
        raise
    family = _v(product.product_family)
    if family is None or _v(family.handle) != family_handle() or _v(product.archived_at) is not None:
        raise InvalidRequest(404, 'Unknown plan %r.' % plan_handle)
    return product


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def find_customer(user: Any) -> Customer | None:
    """The caller's Maxio customer, or None when there is none yet (a real 404, nothing else)."""
    client = get_client()
    reference = customer_reference(user)
    try:
        return call(lambda: client.customers.read_customer_by_reference(reference), action='lookup customer').customer
    except MaxioError as e:
        if e.status_code == 404:
            return None
        raise


def _customer_names(user: Any) -> tuple[str, str]:
    email = (user.email or '').strip()
    first = (getattr(user, 'first_name', '') or '').strip()
    last = (getattr(user, 'last_name', '') or '').strip()
    fallback = email.split('@', 1)[0] if email else (user.get_username() or 'Customer')
    return first or fallback, last or 'Customer'


def ensure_customer(user: Any) -> tuple[Customer, bool]:
    """Find or create the Maxio customer for ``user``. Returns (customer, created).

    Idempotent: the customer is keyed by a deterministic reference derived
    from the user, so repeated or concurrent calls converge on one customer.
    """
    existing = find_customer(user)
    if existing is not None:
        return existing, False
    email = (user.email or '').strip()
    if not email:
        raise InvalidRequest(400, 'Your account needs an email address before subscribing.')
    first_name, last_name = _customer_names(user)
    body = CreateCustomerRequest(customer=CreateCustomer(
        first_name=first_name, last_name=last_name, email=email, reference=customer_reference(user)))
    client = get_client()
    try:
        customer = call(lambda: client.customers.create_customer(body=body), action='create customer', write=True).customer
    except MaxioError as e:
        # A concurrent request may have created it first (reference taken), or the
        # outcome is unknown: either way, the reference lookup settles it.
        if e.status_code == 422 or e.outcome_unknown:
            again = find_customer(user)
            if again is not None:
                return again, False
        raise
    if _v(customer.id) is None:
        raise MaxioError(502, 'Billing provider returned an incomplete customer.', outcome_unknown=True)
    return customer, True


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def list_my_subscriptions(user: Any) -> list[dict[str, Any]]:
    customer = find_customer(user)
    if customer is None:
        return []
    customer_id = _v(customer.id)
    if customer_id is None:
        raise MaxioError(502, 'Billing provider returned an incomplete customer.')
    client = get_client()
    responses = call(lambda: client.customers.list_customer_subscriptions(customer_id),
                     action='list customer subscriptions')
    subscriptions = [s for s in (_v(r.subscription) for r in responses) if s is not None]
    return [subscription_to_dict(s) for s in subscriptions]


def get_my_subscription(user: Any, subscription_id: int) -> dict[str, Any]:
    client = get_client()
    try:
        response = call(lambda: client.subscriptions.read_subscription(subscription_id), action='read subscription')
    except MaxioError as e:
        if e.status_code == 404:
            raise InvalidRequest(404, 'Subscription not found.') from e
        raise
    subscription = _require_subscription(response, action='read subscription', write=False)
    customer = _v(subscription.customer)
    if customer is None or _v(customer.reference) != customer_reference(user):
        # Never reveal whether someone else's subscription exists.
        raise InvalidRequest(404, 'Subscription not found.')
    return subscription_to_dict(subscription)


def _read_subscription(subscription_id: int) -> Subscription | None:
    client = get_client()
    try:
        response = call(lambda: client.subscriptions.read_subscription(subscription_id), action='read subscription')
    except MaxioError as e:
        if e.status_code == 404:
            return None
        raise
    return _require_subscription(response, action='read subscription', write=False)


def _find_live_subscription(customer_id: int, plan_handle: str) -> Subscription | None:
    """An existing live subscription of this customer to this plan (reconciliation before create)."""
    client = get_client()
    responses = call(lambda: client.customers.list_customer_subscriptions(customer_id),
                     action='list customer subscriptions')
    for response in responses:
        subscription = _v(response.subscription)
        if subscription is None or _v(subscription.id) is None:
            continue
        product = _v(subscription.product)
        if product is not None and _v(product.handle) == plan_handle and _is_live(subscription):
            return subscription
    return None


@dataclass
class SubscribeResult:
    subscription: Subscription
    created: bool


def _claim(user: Any, plan_handle: str) -> tuple[Enrollment | None, Enrollment | None]:
    """Claim (user, plan). Returns (new_claim, existing_open_enrollment); exactly one is set.

    Insert first and let the partial unique constraint arbitrate: no read
    precedes the write in the same transaction, which is what lets SQLite wait
    on a concurrent writer instead of failing with "database is locked".
    """
    try:
        with transaction.atomic():
            return Enrollment.objects.create(user=user, plan_handle=plan_handle), None
    except IntegrityError:
        # An open enrollment already exists (possibly a concurrent request's).
        pass
    existing = (Enrollment.objects
                .filter(user=user, plan_handle=plan_handle, status__in=Enrollment.CLAIMING_STATUSES)
                .first())
    if existing is None:
        raise SubscriptionConflict()
    return None, existing


def _release(enrollment: Enrollment, status: str, error: str = '') -> None:
    enrollment.status = status
    enrollment.last_error = error
    enrollment.save(update_fields=['status', 'last_error', 'date_updated'])


def subscribe(user: Any, plan_handle: str) -> SubscribeResult:
    """Subscribe ``user`` to ``plan_handle``. Idempotent per (user, plan).

    A repeat call while a subscription to the plan is live returns that
    subscription (``created=False``); a call racing an in-flight subscribe for
    the same plan raises :class:`SubscriptionConflict`.
    """
    product = get_plan(plan_handle)

    for _attempt in range(2):
        claim, existing = _claim(user, plan_handle)
        if claim is not None:
            break
        assert existing is not None
        if existing.status == Enrollment.ACTIVE and existing.maxio_subscription_id:
            current = _read_subscription(existing.maxio_subscription_id)
            if current is not None and _is_live(current):
                return SubscribeResult(current, created=False)
            _release(existing, Enrollment.ENDED)
            continue
        if existing.status == Enrollment.PENDING and timezone.now() - existing.date_updated > STALE_PENDING_AFTER:
            # The request that claimed this died mid-flight; reconciliation below covers it.
            _release(existing, Enrollment.FAILED, 'Abandoned pending enrollment.')
            continue
        raise SubscriptionConflict()
    else:
        raise SubscriptionConflict()

    try:
        customer, _ = ensure_customer(user)
        customer_id = _v(customer.id)
        assert customer_id is not None  # ensure_customer guarantees it
        claim.maxio_customer_id = customer_id
        claim.save(update_fields=['maxio_customer_id', 'date_updated'])

        # Adopt a live subscription left by an earlier attempt whose outcome was unknown.
        subscription = _find_live_subscription(customer_id, plan_handle)
        created = subscription is None
        if subscription is None:
            body = CreateSubscriptionRequest(subscription=CreateSubscription(
                product_handle=_v(product.handle) or plan_handle, customer_id=customer_id,
                payment_collection_method=collection_method()))
            client = get_client()
            response = call(lambda: client.subscriptions.create_subscription(body=body),
                            action='create subscription', write=True)
            subscription = _require_subscription(response, action='create subscription', write=True)
    except (MaxioError, InvalidRequest) as e:
        _release(claim, Enrollment.FAILED, str(e))
        raise
    except Exception as e:
        _release(claim, Enrollment.FAILED, 'Unexpected error: %s' % type(e).__name__)
        raise

    claim.status = Enrollment.ACTIVE
    claim.maxio_subscription_id = _v(subscription.id)
    claim.last_error = ''
    claim.save(update_fields=['status', 'maxio_subscription_id', 'last_error', 'date_updated'])
    logger.info('User %s subscribed to %s (Maxio subscription %s, created=%s)',
                user.pk, plan_handle, claim.maxio_subscription_id, created)
    return SubscribeResult(subscription, created=created)
