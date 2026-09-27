"""
Subscription billing operations. Maxio is the system of record for plans,
customers and subscriptions; the only local state is the claim on each write.
"""
import hashlib
import uuid
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest,
    CustomerResponse, Product, Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from .errors import BillingError, guarded_read
from .models import BillingInstall, BillingWrite
from .safe_write import Answer, SafeWrite, WriteResult

V = TypeVar('V')

PLANS_PAGE_SIZE = 200  # the maximum Maxio honours for this listing


def _v(value: V | UnsetType) -> V | None:
    """An SDK member as a plain value: UNSET becomes None before it leaves this module."""
    return None if isinstance(value, UnsetType) else value


def _iso(value: datetime | None | UnsetType) -> str | None:
    value = _v(value)
    return value.isoformat() if value is not None else None


def _amount(cents: int | None) -> str | None:
    return None if cents is None else str((Decimal(cents) / 100).quantize(Decimal('0.01')))


# --- references -------------------------------------------------------------

def reference_prefix() -> str:
    """Unique to this install, so installs sharing a Maxio site never share a reference."""
    configured: str = settings.MAXIO_REFERENCE_PREFIX
    if configured:
        return configured
    install, _ = BillingInstall.objects.get_or_create(
        pk=1, defaults={'install_id': uuid.uuid4().hex})
    return f'oscar-{install.install_id[:12]}'


def customer_reference(user: AbstractBaseUser) -> str:
    return f'{reference_prefix()}:user:{user.pk}:customer'


def subscription_reference(user: AbstractBaseUser, plan_handle: str,
                           idempotency_key: str | None) -> str:
    base = f'{reference_prefix()}:user:{user.pk}:subscription'
    if idempotency_key:
        # The caller's key: the same key is a repeat, a new key is a second subscription.
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        return f'{base}:key:{digest}'
    # No key: one subscription per user and plan, so a double-click is a repeat.
    return f'{base}:{plan_handle}'


# --- status -> outcome ------------------------------------------------------

def subscription_outcome(state: object) -> str:
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return BillingWrite.DONE
        case (SubscriptionState.PENDING | SubscriptionState.ASSESSING
              | SubscriptionState.AWAITING_SIGNUP):
            return BillingWrite.PENDING  # accepted, not in effect yet
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE
              | SubscriptionState.UNPAID | SubscriptionState.PAUSED
              | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            return BillingWrite.PENDING  # exists, but something needs attention: not done
        case SubscriptionState.FAILED_TO_CREATE:
            return BillingWrite.FAILED
        case SubscriptionState.CANCELED | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED:
            return BillingWrite.FAILED  # happened, then ended: no longer in effect
        case _:
            return BillingWrite.UNKNOWN  # unlisted or absent: neither done nor failed


def customer_outcome(customer_id: object) -> str:
    # A customer has no status: it exists once Maxio names its id.
    return BillingWrite.DONE if isinstance(customer_id, int) else BillingWrite.UNKNOWN


# --- site -------------------------------------------------------------------

@dataclass(frozen=True)
class SiteBilling:
    collection_method: CollectionMethod
    currency: str | None


_site_billing: dict[str, SiteBilling] = {}
_site_billing_lock = threading.Lock()


def site_billing(client: MaxioAdvancedBillingClient) -> SiteBilling:
    """
    How this Maxio site collects payment, read once per process.

    Subscriptions are created without a payment method, so they must be billed by
    invoice rather than charged automatically: `remittance` on a Relationship
    Invoicing site, `invoice` on a legacy Statements site.
    """
    cached = _site_billing.get('site')
    if cached is not None:
        return cached
    with guarded_read():
        site = client.sites.read_site().site
    relationship = _v(site.relationship_invoicing_enabled)
    if relationship is None:
        raise BillingError(502, 'The billing provider did not describe its invoicing mode.',
                           code='provider_unreadable')
    result = SiteBilling(
        collection_method=CollectionMethod.REMITTANCE if relationship else CollectionMethod.INVOICE,
        currency=_v(site.currency))
    with _site_billing_lock:
        _site_billing['site'] = result
    return result


# --- plans ------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    handle: str
    product_id: int | None
    name: str | None
    description: str | None
    price_in_cents: int | None
    interval: int | None
    interval_unit: str | None
    require_credit_card: bool | None
    currency: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            'planHandle': self.handle,
            'productId': self.product_id,
            'name': self.name,
            'description': self.description,
            'priceInCents': self.price_in_cents,
            'price': _amount(self.price_in_cents),
            'currency': self.currency,
            'interval': self.interval,
            'intervalUnit': self.interval_unit,
            'requiresPaymentMethod': self.require_credit_card,
        }


def _plan(product: Product, currency: str | None) -> Plan | None:
    handle = _v(product.handle)
    if not handle or _v(product.archived_at) is not None:
        return None
    unit = _v(product.interval_unit)
    return Plan(
        handle=handle,
        product_id=_v(product.id),
        name=_v(product.name),
        description=_v(product.description),
        price_in_cents=_v(product.price_in_cents),
        interval=_v(product.interval),
        interval_unit=None if unit is None else str(unit),
        require_credit_card=_v(product.require_credit_card),
        currency=currency,
    )


def list_plans(client: MaxioAdvancedBillingClient) -> list[Plan]:
    family = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    if not family:
        raise BillingError(503, 'MAXIO_DEFAULT_PRODUCT_FAMILY is not configured.',
                           code='billing_not_configured')
    currency = site_billing(client).currency
    plans: list[Plan] = []
    page = 1
    while True:
        with guarded_read():
            batch = client.product_families.list_products_for_product_family(
                f'handle:{family}', page=page, per_page=PLANS_PAGE_SIZE)
        plans.extend(p for p in (_plan(item.product, currency) for item in batch) if p is not None)
        if len(batch) < PLANS_PAGE_SIZE:
            return plans
        page += 1


def get_plan(client: MaxioAdvancedBillingClient, handle: str) -> Plan:
    for plan in list_plans(client):
        if plan.handle == handle:
            return plan
    raise BillingError(404, f'No subscription plan with handle "{handle}".', code='plan_not_found')


# --- subscriptions as the caller sees them ----------------------------------

def summarize(subscription: Subscription) -> dict[str, Any]:
    product = _v(subscription.product)
    state = _v(subscription.state)
    price = _v(subscription.product_price_in_cents)
    return {
        'subscriptionId': _v(subscription.id),
        'reference': _v(subscription.reference),
        'state': None if state is None else str(state),
        'outcome': subscription_outcome(state),
        'plan': None if product is None else {
            'planHandle': _v(product.handle),
            'name': _v(product.name),
            'interval': _v(product.interval),
            'intervalUnit': None if _v(product.interval_unit) is None
            else str(product.interval_unit),
        },
        'priceInCents': price,
        'price': _amount(price),
        'currency': _v(subscription.currency),
        'nextBillingAt': _iso(subscription.next_assessment_at),
        'currentPeriodEndsAt': _iso(subscription.current_period_ends_at),
        'activatedAt': _iso(subscription.activated_at),
        'createdAt': _iso(subscription.created_at),
    }


def _subscription_answer(response: SubscriptionResponse) -> Answer:
    sub = _v(response.subscription)
    if sub is None:
        return Answer(provider_id='', status=None, provider_time=None)
    sub_id = _v(sub.id)
    product = _v(sub.product)
    state = _v(sub.state)
    return Answer(
        provider_id='' if sub_id is None else str(sub_id),
        status=state,
        provider_time=_v(sub.created_at),
        echo=(_v(sub.product_price_in_cents), None if product is None else _v(product.handle)),
        provider_state='' if state is None else str(state),
        snapshot=summarize(sub),
    )


def _customer_answer(response: CustomerResponse) -> Answer:
    customer_id = _v(response.customer.id)
    return Answer(
        provider_id='' if customer_id is None else str(customer_id),
        status=customer_id,
        provider_time=_v(response.customer.created_at),
        snapshot={'customerId': customer_id, 'reference': _v(response.customer.reference)},
    )


def _not_found_is_none(call: Any, *args: Any, **kwargs: Any) -> Any:
    """A lookup whose 404 means "no record carries this reference"."""
    try:
        return call(*args, **kwargs)
    except ApiError as e:
        if e.status_code == 404:
            return None
        raise


# --- writes -----------------------------------------------------------------

def ensure_customer(client: MaxioAdvancedBillingClient, user: Any) -> WriteResult:
    """The caller's Maxio customer, created at most once per user (keyed by reference)."""
    email = (user.email or '').strip()
    if not email:
        raise BillingError(400, 'Your account needs an email address before subscribing.',
                           code='email_required')
    first_name = (user.first_name or '').strip() or email.split('@')[0]
    last_name = (user.last_name or '').strip() or 'Customer'
    ref = customer_reference(user)

    def send(key: str) -> CustomerResponse:
        return client.customers.create_customer(body=CreateCustomerRequest(customer=CreateCustomer(
            first_name=first_name, last_name=last_name, email=email, reference=key)))

    def find(key: str) -> CustomerResponse | None:
        found: CustomerResponse | None = _not_found_is_none(
            client.customers.read_customer_by_reference, key)
        return found

    return SafeWrite(
        reference=ref,
        claim_fields={'user': user, 'kind': BillingWrite.KIND_CUSTOMER},
        send=send, find=find, read=_customer_answer, outcome_of=customer_outcome,
    ).run()


@dataclass(frozen=True)
class SubscribeResult:
    step: str  # 'customer' or 'subscription': which write the outcome belongs to
    write: WriteResult


def subscribe(client: MaxioAdvancedBillingClient, user: Any, plan_handle: str,
              idempotency_key: str | None = None) -> SubscribeResult:
    plan = get_plan(client, plan_handle)
    customer = ensure_customer(client, user)
    if customer.record.outcome != BillingWrite.DONE:
        return SubscribeResult('customer', customer)  # nothing downstream runs
    customer_id = int(customer.record.provider_id)
    collection_method = site_billing(client).collection_method
    ref = subscription_reference(user, plan.handle, idempotency_key)

    def send(key: str) -> SubscriptionResponse:
        return client.subscriptions.create_subscription(
            body=CreateSubscriptionRequest(subscription=CreateSubscription(
                product_handle=plan.handle, customer_id=customer_id,
                payment_collection_method=collection_method, reference=key)))

    def find(key: str) -> SubscriptionResponse | None:
        found: SubscriptionResponse | None = _not_found_is_none(
            client.subscriptions.find_subscription, reference=key)
        if found is not None:
            sub = _v(found.subscription)
            if sub is None or _v(sub.reference) != key:
                return None
        return found

    written = SafeWrite(
        reference=ref,
        claim_fields={'user': user, 'kind': BillingWrite.KIND_SUBSCRIPTION,
                      'plan_handle': plan.handle,
                      'expected_price_in_cents': plan.price_in_cents},
        send=send, find=find, read=_subscription_answer, outcome_of=subscription_outcome,
        sent=(plan.price_in_cents, plan.handle),
    ).run()
    return SubscribeResult('subscription', written)


# --- reads ------------------------------------------------------------------

def find_customer_id(client: MaxioAdvancedBillingClient, user: Any) -> int | None:
    """The caller's Maxio customer id, or None when they have none (a read; never creates)."""
    ref = customer_reference(user)
    record = BillingWrite.objects.filter(reference=ref, outcome=BillingWrite.DONE).first()
    if record is not None and record.provider_id:
        return int(record.provider_id)
    with guarded_read():
        found: CustomerResponse | None = _not_found_is_none(
            client.customers.read_customer_by_reference, ref)
    return None if found is None else _v(found.customer.id)


def my_subscriptions(client: MaxioAdvancedBillingClient, user: Any) -> dict[str, Any]:
    customer_id = find_customer_id(client, user)
    subscriptions: list[dict[str, Any]] = []
    if customer_id is not None:
        with guarded_read():
            listed = client.customers.list_customer_subscriptions(customer_id)
        subscriptions = [summarize(s) for s in (_v(r.subscription) for r in listed) if s is not None]
    # Requests Maxio has not confirmed as a subscription: in flight, unknown or needing review.
    open_requests = [
        {
            'reference': w.reference,
            'planHandle': w.plan_handle,
            'outcome': w.outcome,
            'subscriptionId': int(w.provider_id) if w.provider_id else None,
            'requestedAt': w.claimed_at.isoformat(),
            'detail': w.detail,
        }
        for w in BillingWrite.objects.filter(
            user=user, kind=BillingWrite.KIND_SUBSCRIPTION,
            outcome__in=[BillingWrite.SENDING, BillingWrite.UNKNOWN, BillingWrite.NEEDS_REVIEW])
    ]
    return {'customerId': customer_id, 'subscriptions': subscriptions,
            'openRequests': open_requests}


def get_subscription(client: MaxioAdvancedBillingClient, user: Any,
                     subscription_id: int) -> dict[str, Any]:
    not_found = BillingError(404, 'Subscription not found.', code='subscription_not_found')
    customer_id = find_customer_id(client, user)
    if customer_id is None:
        raise not_found
    with guarded_read():
        response = _not_found_is_none(client.subscriptions.read_subscription, subscription_id)
    sub = None if response is None else _v(response.subscription)
    customer = None if sub is None else _v(sub.customer)
    if sub is None or customer is None or _v(customer.id) != customer_id:
        raise not_found  # never reveal another customer's subscription
    return summarize(sub)
