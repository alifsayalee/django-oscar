"""
Subscription billing against Maxio Advanced Billing.

Each public function is one separately invocable capability; the views are a
thin HTTP layer over them.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest, CustomerResponse,
    Product, Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from .errors import BillingNotConfigured, ProviderRejected, ProviderUnavailable, translate
from .maxio_client import get_client
from .models import MaxioInstall, MaxioWriteClaim
from .safe_write import Answer, Unreadable, WriteResult, WriteStep, safe_write, settle

logger = logging.getLogger('apps.subscriptions.maxio')

_READ_FAILURES = (ApiError, httpx.RequestError, ValueError)
_PLANS_PAGE_SIZE = 200  # Maxio's maximum per_page
_PLANS_MAX_PAGES = 10


def status_from_provider(state: object) -> str:
    """The ONE place a Maxio subscription state becomes this app's outcome."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return MaxioWriteClaim.DONE
        case (SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP
              | SubscriptionState.SOFT_FAILURE | SubscriptionState.PAST_DUE | SubscriptionState.UNPAID
              | SubscriptionState.PAUSED | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            # Exists, but not in effect with nothing outstanding (yet).
            return MaxioWriteClaim.PENDING
        case (SubscriptionState.FAILED_TO_CREATE | SubscriptionState.CANCELED | SubscriptionState.EXPIRED
              | SubscriptionState.TRIAL_ENDED):
            # Never created, or created and since undone.
            return MaxioWriteClaim.FAILED
        case _:
            # A state this SDK does not list, or none at all: neither done nor failed.
            return MaxioWriteClaim.UNKNOWN


def _value(value: Any) -> Any:
    """An SDK member as a plain value: UNSET becomes None."""
    return None if isinstance(value, UnsetType) else value


def _iso(value: Any) -> str | None:
    value = _value(value)
    return value.isoformat() if isinstance(value, datetime) else None


def format_price(cents: int | None) -> str | None:
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal('0.01')))


# -- references ----------------------------------------------------------------

def reference_prefix() -> str:
    return settings.MAXIO_REFERENCE_PREFIX or 'oscar-%s' % MaxioInstall.current_token()


def _user_key(user: Any) -> str:
    # date_joined distinguishes a user from an earlier one that had the same pk
    # before the database was rebuilt.
    return 'u%s-%d' % (user.pk, int(user.date_joined.timestamp()))


def customer_reference(user: Any) -> str:
    return '%s-%s-customer' % (reference_prefix(), _user_key(user))


def subscription_reference(user: Any, plan_handle: str) -> str:
    return '%s-%s-sub-%s' % (reference_prefix(), _user_key(user), plan_handle)


# -- site ---------------------------------------------------------------------

@dataclass(frozen=True)
class SiteInfo:
    currency: str | None
    # Subscriptions are invoice-collected: this integration captures no payment
    # method, so automatic (card) collection would refuse any non-zero balance.
    collection_method: CollectionMethod


_site_lock = threading.Lock()
_site_cache: tuple[MaxioAdvancedBillingClient, SiteInfo] | None = None


def site_info() -> SiteInfo:
    """The Maxio site's currency and invoice collection method, read once per client."""
    global _site_cache
    client = get_client()
    cached = _site_cache
    if cached is not None and cached[0] is client:
        return cached[1]
    try:
        site = client.sites.read_site().site
    except _READ_FAILURES as exc:
        raise translate(exc, action='read site') from exc
    relationship_invoicing = _value(site.relationship_invoicing_enabled)
    if relationship_invoicing is None:
        logger.error('Maxio site does not say whether Relationship Invoicing is enabled')
        raise ProviderUnavailable(502, 'billing_unreadable', 'Billing returned an unreadable response.')
    if _value(site.test) is False:
        logger.warning('Maxio site %s is a live (non-test) site', _value(site.subdomain))
    info = SiteInfo(
        currency=_value(site.currency),
        # Relationship Invoicing sites collect invoices by "remittance"; legacy
        # Statements sites by "invoice" (CollectionMethod docs).
        collection_method=CollectionMethod.REMITTANCE if relationship_invoicing else CollectionMethod.INVOICE,
    )
    with _site_lock:
        _site_cache = (client, info)
    return info


# -- plans -------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    handle: str
    name: str | None
    description: str | None
    price_in_cents: int | None
    interval: int | None
    interval_unit: str | None
    product_id: int | None
    currency: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            'planHandle': self.handle,
            'name': self.name,
            'description': self.description,
            'priceInCents': self.price_in_cents,
            'price': format_price(self.price_in_cents),
            'currency': self.currency,
            'interval': self.interval,
            'intervalUnit': self.interval_unit,
            'productId': self.product_id,
        }


def _plan(product: Product, currency: str | None) -> Plan | None:
    handle = _value(product.handle)
    if not handle or _value(product.archived_at) is not None:
        return None
    interval_unit = _value(product.interval_unit)
    return Plan(
        handle=handle,
        name=_value(product.name),
        description=_value(product.description),
        price_in_cents=_value(product.price_in_cents),
        interval=_value(product.interval),
        interval_unit=str(interval_unit) if interval_unit is not None else None,
        product_id=_value(product.id),
        currency=currency,
    )


def list_plans() -> list[Plan]:
    """The subscribable plans: the non-archived products of the configured product family."""
    family = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    if not family:
        raise BillingNotConfigured()
    currency = site_info().currency
    client = get_client()
    plans: list[Plan] = []
    for page in range(1, _PLANS_MAX_PAGES + 1):
        try:
            responses = client.product_families.list_products_for_product_family(
                'handle:' + family, page=page, per_page=_PLANS_PAGE_SIZE)
        except ApiError as exc:
            if exc.status_code == 404:
                # Our configuration names a family the site does not have: not the caller's fault.
                logger.error('Maxio product family %r not found', family)
                raise ProviderUnavailable(502, 'billing_misconfigured', 'Billing is misconfigured.') from exc
            raise translate(exc, action='list plans') from exc
        except _READ_FAILURES as exc:
            raise translate(exc, action='list plans') from exc
        plans.extend(plan for plan in (_plan(r.product, currency) for r in responses) if plan is not None)
        if len(responses) < _PLANS_PAGE_SIZE:
            break
    return plans


def get_plan(plan_handle: str) -> Plan:
    for plan in list_plans():
        if plan.handle == plan_handle:
            return plan
    raise ProviderRejected(400, 'unknown_plan', 'No subscription plan with that planHandle.')


# -- customer step -------------------------------------------------------------

def _customer_names(user: Any) -> tuple[str, str]:
    first = (user.first_name or '').strip() or user.email.split('@', 1)[0]
    last = (user.last_name or '').strip() or 'Customer'
    return first, last


def _customer_answer(reference: str) -> Any:
    def read(response: CustomerResponse) -> Answer:
        customer = response.customer
        customer_id = _value(customer.id)
        if customer_id is None:
            raise Unreadable('customer response carries no id')
        echoed = _value(customer.reference)
        return Answer(
            provider_id=str(customer_id),
            # A customer has no status: it exists once it has an id.
            outcome=MaxioWriteClaim.DONE,
            provider_time=_value(customer.created_at),
            mismatch=None if echoed == reference else 'customer reference %r came back' % echoed,
        )
    return read


def _find_customer(reference: str) -> CustomerResponse | None:
    try:
        return get_client().customers.read_customer_by_reference(reference)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise


def ensure_customer(user: Any) -> WriteResult:
    """The user's Maxio customer, created at most once however often this is called."""
    if not user.email:
        raise ProviderRejected(400, 'email_required', 'Your account needs an email address to subscribe.')
    reference = customer_reference(user)
    first_name, last_name = _customer_names(user)

    def send(key: str) -> CustomerResponse:
        return get_client().customers.create_customer(body=CreateCustomerRequest(customer=CreateCustomer(
            first_name=first_name, last_name=last_name, email=user.email, reference=key)))

    step: WriteStep[CustomerResponse] = WriteStep(
        kind=MaxioWriteClaim.KIND_CUSTOMER,
        reference=reference,
        send=send,
        find=_find_customer,
        read=_customer_answer(reference),
        # Maxio allows one customer per reference, so a same-reference resend
        # cannot create a second one.
        repeat_is_safe=True,
    )
    return safe_write(step, user=user)


# -- subscription step ---------------------------------------------------------

def _subscription_snapshot(subscription: Subscription) -> dict[str, Any]:
    product = _value(subscription.product)
    return {
        'plan_name': (_value(product.name) if product is not None else None) or '',
        'price_in_cents': _value(subscription.product_price_in_cents),
        'currency': _value(subscription.currency) or '',
        'next_billing_at': _value(subscription.next_assessment_at),
    }


def subscription_answer(subscription: Subscription, *, expected_handle: str | None,
                        expected_reference: str | None) -> Answer:
    subscription_id = _value(subscription.id)
    if subscription_id is None:
        raise Unreadable('subscription response carries no id')
    product = _value(subscription.product)
    handle = _value(product.handle) if product is not None else None
    echoed_reference = _value(subscription.reference)
    mismatch = None
    if expected_handle is not None and handle != expected_handle:
        mismatch = 'plan %r came back for %r' % (handle, expected_handle)
    elif expected_reference is not None and echoed_reference != expected_reference:
        mismatch = 'subscription reference %r came back' % echoed_reference
    state = _value(subscription.state)
    return Answer(
        provider_id=str(subscription_id),
        outcome=status_from_provider(state),
        provider_state=str(state) if state is not None else '',
        provider_time=_value(subscription.created_at),
        mismatch=mismatch,
        snapshot=_subscription_snapshot(subscription),
    )


def subscription_of(response: SubscriptionResponse) -> Subscription:
    """The subscription inside its envelope; the SDK types the envelope member as optional."""
    subscription = response.subscription
    if isinstance(subscription, UnsetType):
        raise Unreadable('subscription response carries no subscription')
    return subscription


def _find_subscription(reference: str) -> SubscriptionResponse | None:
    try:
        return get_client().subscriptions.find_subscription(reference=reference)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise


@dataclass(frozen=True)
class SubscribeResult:
    customer: WriteResult
    subscription: WriteResult | None   # None when the customer step is not done
    plan: Plan


def subscribe(user: Any, plan_handle: str) -> SubscribeResult:
    """Enroll the user's Maxio customer in a plan -- at most one subscription per user and plan."""
    plan = get_plan(plan_handle)
    customer = ensure_customer(user)
    if customer.record.outcome != MaxioWriteClaim.DONE:
        return SubscribeResult(customer=customer, subscription=None, plan=plan)
    customer_id = int(customer.record.provider_id)
    reference = subscription_reference(user, plan.handle)
    collection_method = site_info().collection_method

    def send(key: str) -> SubscriptionResponse:
        return get_client().subscriptions.create_subscription(body=CreateSubscriptionRequest(
            subscription=CreateSubscription(
                product_handle=plan.handle, customer_id=customer_id, reference=key,
                payment_collection_method=collection_method)))

    step: WriteStep[SubscriptionResponse] = WriteStep(
        kind=MaxioWriteClaim.KIND_SUBSCRIPTION,
        reference=reference,
        send=send,
        find=_find_subscription,
        read=lambda response: subscription_answer(
            subscription_of(response), expected_handle=plan.handle, expected_reference=reference),
        # Maxio documents no uniqueness for subscription references, so only a
        # lookup is a safe check.
        repeat_is_safe=False,
    )
    return SubscribeResult(customer=customer, subscription=safe_write(step, user=user, plan_handle=plan.handle),
                           plan=plan)


# -- read back -------------------------------------------------------------------

def subscription_json(subscription: Subscription) -> dict[str, Any]:
    product = _value(subscription.product)
    state = _value(subscription.state)
    price_in_cents = _value(subscription.product_price_in_cents)
    return {
        'subscriptionId': _value(subscription.id),
        'reference': _value(subscription.reference),
        'planHandle': _value(product.handle) if product is not None else None,
        'planName': _value(product.name) if product is not None else None,
        'state': str(state) if state is not None else None,
        'outcome': status_from_provider(state),
        'priceInCents': price_in_cents,
        'price': format_price(price_in_cents),
        'currency': _value(subscription.currency),
        'nextBillingAt': _iso(subscription.next_assessment_at),
        'currentPeriodEndsAt': _iso(subscription.current_period_ends_at),
        'activatedAt': _iso(subscription.activated_at),
        'createdAt': _iso(subscription.created_at),
    }


def claim_json(record: MaxioWriteClaim) -> dict[str, Any]:
    return {
        'reference': record.reference,
        'planHandle': record.plan_handle or None,
        'outcome': record.outcome,
        'subscriptionId': int(record.provider_id) if record.provider_id else None,
        'state': record.provider_state or None,
        'claimedAt': record.claimed_at.isoformat(),
    }


def my_subscriptions(user: Any) -> dict[str, Any]:
    """The user's subscriptions as Maxio has them, plus local attempts Maxio has not confirmed."""
    customer = MaxioWriteClaim.objects.filter(user=user, kind=MaxioWriteClaim.KIND_CUSTOMER).first()
    claims = {c.reference: c for c in MaxioWriteClaim.objects.filter(
        user=user, kind=MaxioWriteClaim.KIND_SUBSCRIPTION)}

    subscriptions: list[dict[str, Any]] = []
    if customer is not None and customer.outcome == MaxioWriteClaim.DONE:
        try:
            responses = get_client().customers.list_customer_subscriptions(int(customer.provider_id))
        except _READ_FAILURES as exc:
            raise translate(exc, action='list customer subscriptions') from exc
        for response in responses:
            try:
                subscription = subscription_of(response)
            except Unreadable:
                logger.warning('Maxio listed a subscription envelope with no subscription; skipped')
                continue
            subscriptions.append(subscription_json(subscription))
            # Reconcile: a record this app holds for the same reference is
            # settled from the provider's current word.
            record = claims.pop(str(_value(subscription.reference)), None)
            if record is not None:
                try:
                    settle(record, subscription_answer(
                        subscription, expected_handle=record.plan_handle or None, expected_reference=None))
                except Unreadable:
                    logger.warning('Maxio subscription for %s unreadable during reconciliation', record.reference)

    unsettled = [claim_json(record) for record in claims.values() if record.outcome != MaxioWriteClaim.FAILED]
    return {
        'customerId': int(customer.provider_id) if customer is not None and customer.provider_id else None,
        'subscriptions': subscriptions,
        'unsettled': unsettled,
    }
