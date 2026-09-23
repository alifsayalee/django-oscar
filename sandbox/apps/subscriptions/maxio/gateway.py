"""
Every Maxio call the subscriptions app makes, and nothing else.

Results are mapped into this module's own dataclasses so that SDK sentinels
(``UNSET``) never cross into views or the database. Reads are retried once on
failures that cannot have changed anything; writes are never resent here - the
caller owns reconciliation because it owns the claim row.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
    Product,
    Subscription,
)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from .errors import (
    ProviderConfigError,
    ProviderError,
    ProviderFailure,
    ProviderNotFound,
    ProviderUnavailable,
    ProviderUnreadable,
    translate,
)
from .states import Bucket, bucket_for

T = TypeVar("T")

PER_PAGE = 200  # the provider's maximum page size
MAX_PAGES = 10  # backstop: a product family with more than 2000 plans is not a plan list
READ_ATTEMPTS = 2
READ_BACKOFF_SECONDS = 0.5


def _opt(value: T | None | UnsetType) -> T | None:
    return None if isinstance(value, UnsetType) else value


@dataclass(frozen=True)
class Plan:
    handle: str
    name: str
    description: str | None
    price_in_cents: int
    interval: int | None
    interval_unit: str | None


@dataclass(frozen=True)
class PlanPage:
    plans: list[Plan]
    truncated: bool  # the page cap, not the provider, ended the walk


@dataclass(frozen=True)
class SubscriptionInfo:
    id: int
    state: str
    bucket: Bucket
    reference: str | None
    plan_handle: str | None
    plan_name: str | None
    price_in_cents: int | None
    currency: str | None
    next_billing_at: datetime | None
    created_at: datetime | None


def _wire(value: object) -> str:
    return value.value if isinstance(value, SubscriptionState) else str(value)


def subscription_info(subscription: Subscription | None | UnsetType) -> SubscriptionInfo:
    """Map a decoded subscription; a missing id or state means we cannot tell what exists."""
    sub = _opt(subscription)
    if sub is None:
        raise ProviderUnreadable(502, "Billing provider returned no subscription.", outcome_unknown=True)
    sub_id = _opt(sub.id)
    state = _opt(sub.state)
    if sub_id is None or state is None:
        raise ProviderUnreadable(502, "Billing provider returned an incomplete subscription.", outcome_unknown=True)
    product = _opt(sub.product)
    return SubscriptionInfo(
        id=sub_id,
        state=_wire(state),
        bucket=bucket_for(state),
        reference=_opt(sub.reference),
        plan_handle=_opt(product.handle) if product is not None else None,
        plan_name=_opt(product.name) if product is not None else None,
        price_in_cents=_opt(sub.product_price_in_cents),
        currency=_opt(sub.currency),
        next_billing_at=_opt(sub.next_assessment_at) or _opt(sub.current_period_ends_at),
        created_at=_opt(sub.created_at),
    )


class MaxioGateway:
    def __init__(self, client: MaxioAdvancedBillingClient, product_family: str, *,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = client
        self._family = product_family
        self._sleep = sleep

    @property
    def product_family(self) -> str:
        return self._family

    def close(self) -> None:
        self._client.close()

    # -- reads -------------------------------------------------------------

    def _read(self, call: Callable[[], T]) -> T:
        """Run an idempotent read, retrying once on a failure that cannot be the caller's."""
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                return call()
            except Exception as exc:
                error = translate(exc)
                transient = isinstance(error, ProviderUnavailable) or (
                    isinstance(error, ProviderFailure) and error.outcome_unknown)
                if transient and attempt < READ_ATTEMPTS:
                    self._sleep(READ_BACKOFF_SECONDS * attempt)
                    continue
                raise error from exc
        raise AssertionError("unreachable")

    def _plan(self, product: Product) -> Plan | None:
        handle = _opt(product.handle)
        price = _opt(product.price_in_cents)
        if not handle or price is None or _opt(product.archived_at) is not None:
            return None
        family = _opt(product.product_family)
        if family is not None and _opt(family.handle) not in (None, self._family):
            return None
        unit = _opt(product.interval_unit)
        return Plan(
            handle=handle,
            name=_opt(product.name) or handle,
            description=_opt(product.description) or None,
            price_in_cents=price,
            interval=_opt(product.interval),
            interval_unit=_wire(unit) if unit is not None else None,
        )

    def list_plans(self) -> PlanPage:
        plans: list[Plan] = []
        truncated = False
        for page in range(1, MAX_PAGES + 1):
            try:
                batch = self._read(lambda: self._client.product_families.list_products_for_product_family(
                    f"handle:{self._family}", page=page, per_page=PER_PAGE))
            except ProviderNotFound as exc:
                raise ProviderConfigError(502, "Configured product family does not exist.") from exc
            plans.extend(plan for item in batch if (plan := self._plan(item.product)) is not None)
            if len(batch) < PER_PAGE:
                break
        else:
            truncated = True
        return PlanPage(plans=plans, truncated=truncated)

    def get_plan(self, handle: str) -> Plan | None:
        """The plan, if it is a live product of the configured family; otherwise None."""
        try:
            response = self._read(lambda: self._client.products.read_product_by_handle(handle))
        except ProviderNotFound:
            return None
        product = response.product
        family = _opt(product.product_family)
        if family is None or _opt(family.handle) != self._family:
            return None
        return self._plan(product)

    def find_customer_id(self, reference: str) -> int | None:
        try:
            response = self._read(lambda: self._client.customers.read_customer_by_reference(reference))
        except ProviderNotFound:
            return None
        customer_id = _opt(response.customer.id)
        if customer_id is None:
            raise ProviderUnreadable(502, "Billing provider returned a customer without an id.")
        return customer_id

    def find_subscription(self, reference: str) -> SubscriptionInfo | None:
        try:
            response = self._read(lambda: self._client.subscriptions.find_subscription(reference=reference))
        except ProviderNotFound:
            return None
        return subscription_info(response.subscription)

    def list_customer_subscriptions(self, customer_id: int) -> list[SubscriptionInfo]:
        items = self._read(lambda: self._client.customers.list_customer_subscriptions(customer_id))
        return [subscription_info(item.subscription) for item in items]

    # -- writes (sent once; the caller reconciles by reference) --------------

    def create_customer(self, *, reference: str, first_name: str, last_name: str, email: str) -> int:
        body = CreateCustomerRequest(customer=CreateCustomer(
            first_name=first_name, last_name=last_name, email=email, reference=reference))
        try:
            response = self._client.customers.create_customer(body=body)
        except Exception as exc:
            raise translate(exc) from exc
        customer_id = _opt(response.customer.id)
        if customer_id is None:
            raise ProviderUnreadable(502, "Billing provider returned a customer without an id.",
                                     outcome_unknown=True)
        return customer_id

    def create_subscription(self, *, customer_id: int, plan_handle: str, reference: str) -> SubscriptionInfo:
        body = CreateSubscriptionRequest(subscription=CreateSubscription(
            product_handle=plan_handle,
            customer_id=customer_id,
            reference=reference,
            # No card is captured by this app: the shopper is invoiced instead of charged.
            payment_collection_method=CollectionMethod.REMITTANCE,
        ))
        try:
            response = self._client.subscriptions.create_subscription(body=body)
        except Exception as exc:
            raise translate(exc) from exc
        return subscription_info(response.subscription)


__all__ = [
    "MaxioGateway", "Plan", "PlanPage", "ProviderError", "SubscriptionInfo", "subscription_info",
]
