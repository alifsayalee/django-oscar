"""
Every call this app makes to Maxio, and the one place SDK failures become ours.

Callers above this module see only ``BillingError`` subclasses, each carrying the
HTTP status our API should answer and whether the provider call may have taken
effect (``outcome_unknown``).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from maxio_advanced_billing.core import ApiError, RawError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest, Customer,
    CustomerErrorResponse1, ErrorListResponse1, Product, Subscription)
from maxio_advanced_billing.models.enums import SubscriptionState
from pydantic import ValidationError

from .client import get_client

logger = logging.getLogger('apps.maxio_billing')

T = TypeVar('T')

# Failures raised before the request left this process: nothing can have happened.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)
# Provider statuses that describe the caller's input rather than our integration.
CALLER_STATUSES = (400, 404, 409, 422)


class BillingError(Exception):
    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False,
                 provider_status: int | None = None, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code          # what our API answers
        self.message = message                  # safe to show the caller
        self.outcome_unknown = outcome_unknown  # the provider call may have taken effect
        self.provider_status = provider_status
        self.details = details or []


class ProviderRejected(BillingError):
    """Maxio refused the request because of what was asked; nothing happened."""


class ProviderConfigError(BillingError):
    """Maxio refused *our* credentials or configuration; nothing happened."""


class ProviderUnavailable(BillingError):
    """No usable answer: never sent, rate-limited, provider error, timeout, unreadable body."""


def _messages(error: object) -> list[str]:
    if isinstance(error, ErrorListResponse1):
        return list(error.errors)
    if isinstance(error, CustomerErrorResponse1):
        errors = error.errors
        if isinstance(errors, list):
            return [str(m) for m in errors]
        if not isinstance(errors, UnsetType) and isinstance(errors.customer, str):
            return [errors.customer]
        return []
    if isinstance(error, str):
        return [error] if error else []
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return []
        if isinstance(body, dict):
            found = body.get('errors', body.get('error'))
            if isinstance(found, list):
                return [str(m) for m in found]
            if isinstance(found, str):
                return [found]
    return []


def guarded(operation: str, call: Callable[[], T]) -> T:
    """Run one SDK call, translating every failure kind into a BillingError."""
    try:
        return call()
    except ApiError as e:
        status = e.status_code
        details = _messages(e.error)
        logger.warning('maxio %s failed: HTTP %s %s', operation, status, details)
        if status in (401, 403):
            raise ProviderConfigError(502, 'Billing provider rejected our credentials.',
                                      provider_status=status) from e
        if status == 429:
            raise ProviderUnavailable(503, 'Billing provider is rate limiting; try again shortly.',
                                      provider_status=status) from e
        if status in CALLER_STATUSES:
            raise ProviderRejected(status, 'Billing provider rejected the request.',
                                   provider_status=status, details=details) from e
        # 5xx and every unmapped status: ours to handle; a 5xx on a write may have landed.
        raise ProviderUnavailable(502, 'Billing provider error.', provider_status=status,
                                  outcome_unknown=status >= 500) from e
    except ValidationError as e:
        logger.error('maxio %s returned an unreadable body: %s', operation, e.error_count())
        raise ProviderUnavailable(502, 'Unreadable response from billing provider.',
                                  outcome_unknown=True) from e
    except NEVER_SENT as e:
        logger.warning('maxio %s never sent: %s', operation, type(e).__name__)
        raise ProviderUnavailable(502, 'Billing provider unreachable.', outcome_unknown=False) from e
    except httpx.RequestError as e:
        logger.warning('maxio %s got no reply: %s', operation, type(e).__name__)
        raise ProviderUnavailable(504, 'No response from billing provider.', outcome_unknown=True) from e
    except ValueError as e:     # a non-JSON body where JSON was declared
        logger.error('maxio %s returned a non-JSON body', operation)
        raise ProviderUnavailable(502, 'Unreadable response from billing provider.',
                                  outcome_unknown=True) from e


def _is_not_found(error: BillingError) -> bool:
    return isinstance(error, ProviderRejected) and error.provider_status == 404


# ---------------------------------------------------------------------------
# Subscription state -> our status (the one place this mapping lives)
# ---------------------------------------------------------------------------

def status_from_state(state: object) -> str:
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return 'active'
        case SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP:
            return 'pending'
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID
              | SubscriptionState.PAUSED | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED
              | SubscriptionState.TRIAL_ENDED):
            return 'attention'
        case SubscriptionState.CANCELED | SubscriptionState.EXPIRED:
            return 'ended'
        case SubscriptionState.FAILED_TO_CREATE:
            return 'failed'
        case _:
            return 'unknown'


# ---------------------------------------------------------------------------
# Plain views of SDK models (UNSET never leaves this module)
# ---------------------------------------------------------------------------

def _set(value: Any) -> Any:
    return None if isinstance(value, UnsetType) else value


@dataclass(frozen=True)
class PlanView:
    handle: str
    name: str
    description: str
    price_in_cents: int | None
    interval: int | None
    interval_unit: str | None
    product_id: int | None


@dataclass(frozen=True)
class SubscriptionView:
    id: int
    state: str
    status: str
    reference: str | None
    customer_id: int | None
    customer_reference: str | None
    plan_handle: str | None
    plan_name: str | None
    price_in_cents: int | None
    currency: str | None
    interval: int | None
    interval_unit: str | None
    next_billing_at: Any
    current_period_ends_at: Any
    activated_at: Any
    created_at: Any
    canceled_at: Any
    payment_collection_method: str | None


def _plan_view(product: Product) -> PlanView | None:
    handle = _set(product.handle)
    if not handle:
        return None
    interval_unit = _set(product.interval_unit)
    return PlanView(
        handle=handle,
        name=_set(product.name) or handle,
        description=_set(product.description) or '',
        price_in_cents=_set(product.price_in_cents),
        interval=_set(product.interval),
        interval_unit=str(interval_unit) if interval_unit is not None else None,
        product_id=_set(product.id),
    )


def _subscription_view(subscription: Subscription | UnsetType, operation: str) -> SubscriptionView:
    if isinstance(subscription, UnsetType) or isinstance(subscription.id, UnsetType):
        # An answer without an id cannot be matched to anything: the outcome is unknown.
        raise ProviderUnavailable(502, f'Billing provider returned no subscription for {operation}.',
                                  outcome_unknown=True)
    state = _set(subscription.state)
    product = _set(subscription.product)
    customer: Customer | None = _set(subscription.customer)
    collection = _set(subscription.payment_collection_method)
    next_billing = _set(subscription.next_assessment_at) or _set(subscription.current_period_ends_at)
    return SubscriptionView(
        id=subscription.id,
        state=str(state) if state is not None else '',
        status=status_from_state(state),
        reference=_set(subscription.reference),
        customer_id=_set(customer.id) if customer else None,
        customer_reference=_set(customer.reference) if customer else None,
        plan_handle=_set(product.handle) if product else None,
        plan_name=_set(product.name) if product else None,
        price_in_cents=_set(subscription.product_price_in_cents),
        currency=_set(subscription.currency),
        interval=_set(product.interval) if product else None,
        interval_unit=str(_set(product.interval_unit)) if product and _set(product.interval_unit) else None,
        next_billing_at=next_billing,
        current_period_ends_at=_set(subscription.current_period_ends_at),
        activated_at=_set(subscription.activated_at),
        created_at=_set(subscription.created_at),
        canceled_at=_set(subscription.canceled_at),
        payment_collection_method=str(collection) if collection is not None else None,
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

PLAN_PAGE_SIZE = 200
PLAN_MAX_PAGES = 5


def list_plans(family_handle: str) -> tuple[list[PlanView], bool]:
    """Active (non-archived) products of the family; returns (plans, truncated)."""
    client = get_client()
    plans: list[PlanView] = []
    for page in range(1, PLAN_MAX_PAGES + 1):
        batch = guarded('list_products_for_product_family', lambda: (
            client.product_families.list_products_for_product_family(
                f'handle:{family_handle}', page=page, per_page=PLAN_PAGE_SIZE)))
        plans.extend(view for item in batch if (view := _plan_view(item.product)) is not None)
        if len(batch) < PLAN_PAGE_SIZE:
            return plans, False
    logger.warning('maxio plan listing truncated after %s pages', PLAN_MAX_PAGES)
    return plans, True


def find_customer_id(reference: str) -> int | None:
    """Maxio id of the customer carrying our reference; None only on Maxio's own 404."""
    client = get_client()
    try:
        response = guarded('read_customer_by_reference',
                           lambda: client.customers.read_customer_by_reference(reference))
    except ProviderRejected as e:
        if _is_not_found(e):
            return None
        raise
    customer_id = _set(response.customer.id)
    if customer_id is None:
        raise ProviderUnavailable(502, 'Billing provider returned a customer without an id.',
                                  outcome_unknown=True)
    return int(customer_id)


def create_customer(*, reference: str, first_name: str, last_name: str, email: str) -> int:
    client = get_client()
    body = CreateCustomerRequest(customer=CreateCustomer(
        first_name=first_name, last_name=last_name, email=email, reference=reference))
    response = guarded('create_customer', lambda: client.customers.create_customer(body=body))
    customer_id = _set(response.customer.id)
    if customer_id is None:
        raise ProviderUnavailable(502, 'Billing provider returned a customer without an id.',
                                  outcome_unknown=True)
    echoed = _set(response.customer.reference)
    if echoed != reference:
        logger.error('maxio customer %s echoed reference %r, sent %r', customer_id, echoed, reference)
    return int(customer_id)


def create_subscription(*, reference: str, customer_id: int, plan_handle: str,
                        payment_collection_method: str) -> SubscriptionView:
    client = get_client()
    body = CreateSubscriptionRequest(subscription=CreateSubscription(
        product_handle=plan_handle,
        customer_id=customer_id,
        reference=reference,
        payment_collection_method=payment_collection_method,
    ))
    response = guarded('create_subscription', lambda: client.subscriptions.create_subscription(body=body))
    return _subscription_view(response.subscription, 'create_subscription')


def find_subscription(reference: str) -> SubscriptionView | None:
    """The subscription carrying our reference; None only on Maxio's own 404."""
    client = get_client()
    try:
        response = guarded('find_subscription',
                           lambda: client.subscriptions.find_subscription(reference=reference))
    except ProviderRejected as e:
        if _is_not_found(e):
            return None
        raise
    return _subscription_view(response.subscription, 'find_subscription')


def read_subscription(subscription_id: int) -> SubscriptionView | None:
    client = get_client()
    try:
        response = guarded('read_subscription',
                           lambda: client.subscriptions.read_subscription(subscription_id))
    except ProviderRejected as e:
        if _is_not_found(e):
            return None
        raise
    return _subscription_view(response.subscription, 'read_subscription')


def list_customer_subscriptions(customer_id: int) -> list[SubscriptionView]:
    client = get_client()
    items = guarded('list_customer_subscriptions',
                    lambda: client.customers.list_customer_subscriptions(customer_id))
    return [_subscription_view(item.subscription, 'list_customer_subscriptions') for item in items]
