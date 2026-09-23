"""
The only module that talks to Maxio Advanced Billing.

Every SDK call goes through ``_call``, which applies one error ladder: whatever
fails -- a provider rejection, our own credentials, a rate limit, an outage, an
unreadable body, a connection that never opened, a reply that never came -- it
leaves as a ``ProviderError`` carrying the HTTP status our API should answer
with and whether anything may have happened upstream.

The SDK performs no retries. Reads are retried here on transient failures;
writes never are -- a write whose outcome is unknown is reconciled by looking
it up by the reference we sent (see ``services``).

Results are mapped into plain dataclasses so the SDK's ``UNSET`` sentinel never
crosses into the rest of the app.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

import httpx
from maxio_advanced_billing.core import ApiError, RawError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
    Customer,
    CustomerError,
    CustomerErrorResponse1,
    ErrorListResponse1,
    Product,
    Subscription,
    SubscriptionResponse,
)
from maxio_advanced_billing.models.enums import (
    CollectionMethod,
    IntervalUnit,
    SubscriptionState,
)
from pydantic import ValidationError

from apps.subscriptions.maxio import get_client

logger = logging.getLogger("apps.subscriptions.gateway")

T = TypeVar("T")

# Transport failures raised before the request left: nothing can have landed.
NEVER_SENT: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

READ_ATTEMPTS = 3
READ_BACKOFF_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 5.0
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# Provider statuses that are the caller's doing, passed through by name.
CALLER_STATUSES = frozenset({400, 404, 409, 422})

# Indirection so tests can skip the wait.
_sleep = time.sleep

PLAN_PAGE_SIZE = 200
PLAN_MAX_PAGES = 10


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """A Maxio call failed. ``status_code`` is what our API answers with."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        provider_status: int | None = None,
        details: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.details = details or []


class ProviderConfigError(ProviderError):
    """Maxio refused *our* credentials or configuration. Not the caller's fault."""


class ProviderRejected(ProviderError):
    """Maxio rejected the request as invalid. Nothing happened."""


class ProviderUnavailable(ProviderError):
    """Maxio could not be reached, rate-limited us, or did not reply."""


class ProviderUnreadable(ProviderError):
    """Maxio answered with a body we cannot read."""


def _messages(error: object) -> list[str]:
    """Human-readable messages from a narrowed error body."""
    if isinstance(error, ErrorListResponse1):
        return [str(m) for m in error.errors]
    if isinstance(error, CustomerErrorResponse1):
        errors = error.errors
        if isinstance(errors, list):
            return [str(m) for m in errors]
        if isinstance(errors, CustomerError) and not isinstance(errors.customer, UnsetType):
            return [errors.customer]
        return []
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return []
        if isinstance(body, dict):
            raw_errors = body.get("errors")
            if isinstance(raw_errors, list):
                return [str(m) for m in raw_errors]
            if isinstance(raw_errors, str):
                return [raw_errors]
            if isinstance(body.get("error"), str):
                return [str(body["error"])]
        return []
    if isinstance(error, str):
        return [error] if error else []
    return []


def translate(exc: Exception, *, write: bool) -> ProviderError:
    """Map any failure of an SDK call onto our one error type."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        status = exc.status_code
        details = _messages(exc.error)
        if status in (401, 403):
            return ProviderConfigError(
                502, "Billing provider refused our credentials.", provider_status=status
            )
        if status == 429:
            return ProviderUnavailable(
                503, "Billing provider is rate-limiting requests.", provider_status=status
            )
        if status in CALLER_STATUSES:
            return ProviderRejected(
                status,
                "Billing provider rejected the request.",
                provider_status=status,
                details=details,
            )
        # 5xx and every unmapped 4xx. A 5xx on a write may still have landed.
        return ProviderError(
            502,
            "Billing provider error.",
            outcome_unknown=write and status >= 500,
            provider_status=status,
        )
    if isinstance(exc, NEVER_SENT):
        return ProviderUnavailable(502, "Billing provider unreachable; nothing was sent.")
    if isinstance(exc, httpx.RequestError):
        return ProviderUnavailable(
            504, "Billing provider did not reply.", outcome_unknown=write
        )
    if isinstance(exc, ValueError):  # includes pydantic.ValidationError
        return ProviderUnreadable(
            502, "Billing provider response was unreadable.", outcome_unknown=write
        )
    raise exc


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, ApiError):
        return exc.status_code in RETRYABLE_STATUSES
    return isinstance(
        exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)
    )


def _retry_after(exc: Exception) -> float | None:
    if not isinstance(exc, ApiError):
        return None
    value = exc.response.headers.get("retry-after")
    try:
        return min(float(value), MAX_RETRY_AFTER_SECONDS) if value else None
    except ValueError:
        return None


def _call(operation: str, fn: Callable[[], T], *, write: bool) -> T:
    """Run one SDK call through the ladder. Reads retry; writes never do."""
    attempts = 1 if write else READ_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (ApiError, httpx.RequestError, ValidationError, ValueError) as exc:
            if attempt < attempts and _is_transient(exc):
                delay = _retry_after(exc) or READ_BACKOFF_SECONDS * 2 ** (attempt - 1)
                logger.info("maxio %s transient failure; retrying in %.1fs", operation, delay)
                _sleep(delay)
                continue
            error = translate(exc, write=write)
            logger.warning(
                "maxio %s failed: %s (provider status %s, outcome unknown %s, details %s)",
                operation,
                type(exc).__name__,
                error.provider_status,
                error.outcome_unknown,
                error.details,
            )
            raise error from exc
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------
# Plain results
# --------------------------------------------------------------------------


def _v(value: T | UnsetType | None) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _enum_value(value: object) -> str | None:
    """Wire value of an open enum member (a known member or a newer string)."""
    if value is None or isinstance(value, UnsetType):
        return None
    if isinstance(value, (CollectionMethod, IntervalUnit, SubscriptionState)):
        return str(value.value)
    return str(value)


@dataclass(frozen=True)
class Plan:
    handle: str
    id: int | None
    name: str | None
    description: str | None
    price_in_cents: int | None
    interval: int | None
    interval_unit: str | None
    trial_price_in_cents: int | None
    trial_interval: int | None
    trial_interval_unit: str | None
    initial_charge_in_cents: int | None
    requires_payment_method: bool | None
    product_family_handle: str | None


@dataclass(frozen=True)
class RemoteCustomer:
    id: int
    reference: str | None
    email: str | None


@dataclass(frozen=True)
class RemoteSubscription:
    id: int
    state: str | None
    reference: str | None
    customer_id: int | None
    product_handle: str | None
    product_name: str | None
    price_in_cents: int | None
    current_billing_amount_in_cents: int | None
    currency: str | None
    payment_collection_method: str | None
    interval: int | None
    interval_unit: str | None
    next_billing_at: datetime | None
    current_period_ends_at: datetime | None
    activated_at: datetime | None
    created_at: datetime | None
    canceled_at: datetime | None


def _plan(product: Product) -> Plan | None:
    handle = _v(product.handle)
    if not handle:
        return None
    family = _v(product.product_family)
    return Plan(
        handle=handle,
        id=_v(product.id),
        name=_v(product.name),
        description=_v(product.description),
        price_in_cents=_v(product.price_in_cents),
        interval=_v(product.interval),
        interval_unit=_enum_value(product.interval_unit),
        trial_price_in_cents=_v(product.trial_price_in_cents),
        trial_interval=_v(product.trial_interval),
        trial_interval_unit=_enum_value(product.trial_interval_unit),
        initial_charge_in_cents=_v(product.initial_charge_in_cents),
        requires_payment_method=_v(product.require_credit_card),
        product_family_handle=_v(family.handle) if family is not None else None,
    )


def _customer(customer: Customer) -> RemoteCustomer:
    customer_id = _v(customer.id)
    if customer_id is None:
        raise ProviderUnreadable(
            502, "Billing provider returned a customer without an id.", outcome_unknown=True
        )
    return RemoteCustomer(
        id=customer_id, reference=_v(customer.reference), email=_v(customer.email)
    )


def _subscription(response: SubscriptionResponse) -> RemoteSubscription:
    subscription = response.subscription
    if isinstance(subscription, UnsetType):
        raise ProviderUnreadable(
            502, "Billing provider returned no subscription.", outcome_unknown=True
        )
    return _subscription_model(subscription)


def _subscription_model(subscription: Subscription) -> RemoteSubscription:
    subscription_id = _v(subscription.id)
    if subscription_id is None:
        raise ProviderUnreadable(
            502, "Billing provider returned a subscription without an id.", outcome_unknown=True
        )
    product = _v(subscription.product)
    customer = _v(subscription.customer)
    return RemoteSubscription(
        id=subscription_id,
        state=_enum_value(subscription.state),
        reference=_v(subscription.reference),
        customer_id=_v(customer.id) if customer is not None else None,
        product_handle=_v(product.handle) if product is not None else None,
        product_name=_v(product.name) if product is not None else None,
        price_in_cents=_v(subscription.product_price_in_cents),
        current_billing_amount_in_cents=_v(subscription.current_billing_amount_in_cents),
        currency=_v(subscription.currency),
        payment_collection_method=_enum_value(subscription.payment_collection_method),
        interval=_v(product.interval) if product is not None else None,
        interval_unit=_enum_value(product.interval_unit) if product is not None else None,
        next_billing_at=_v(subscription.next_assessment_at),
        current_period_ends_at=_v(subscription.current_period_ends_at),
        activated_at=_v(subscription.activated_at),
        created_at=_v(subscription.created_at),
        canceled_at=_v(subscription.canceled_at),
    )


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def list_plans(product_family_handle: str) -> tuple[list[Plan], bool]:
    """
    Non-archived products of one product family. Returns ``(plans, truncated)``;
    ``truncated`` is True when the page cap, not the provider, ended the walk.
    """
    client = get_client()
    plans: list[Plan] = []
    for page in range(1, PLAN_MAX_PAGES + 1):
        try:
            products = _call(
                "list_products_for_product_family",
                functools.partial(
                    client.product_families.list_products_for_product_family,
                    "handle:" + product_family_handle,
                    page=page,
                    per_page=PLAN_PAGE_SIZE,
                ),
                write=False,
            )
        except ProviderRejected as exc:
            if exc.provider_status == 404:
                # The configured family does not exist: our configuration, not the caller's.
                raise ProviderConfigError(
                    502, "Configured product family was not found.", provider_status=404
                ) from exc
            raise
        for entry in products:
            plan = _plan(entry.product)
            if plan is not None and _v(entry.product.archived_at) is None:
                plans.append(plan)
        if len(products) < PLAN_PAGE_SIZE:
            return plans, False
    logger.warning("maxio plan listing truncated at %d pages", PLAN_MAX_PAGES)
    return plans, True


def find_customer(reference: str) -> RemoteCustomer | None:
    """Look a customer up by our reference. Only Maxio's 404 means absent."""
    client = get_client()
    try:
        response = _call(
            "read_customer_by_reference",
            lambda: client.customers.read_customer_by_reference(reference),
            write=False,
        )
    except ProviderRejected as exc:
        if exc.provider_status == 404:
            return None
        raise
    return _customer(response.customer)


def create_customer(
    *, reference: str, email: str, first_name: str, last_name: str
) -> RemoteCustomer:
    client = get_client()
    body = CreateCustomerRequest(
        customer=CreateCustomer(
            first_name=first_name,
            last_name=last_name,
            email=email,
            reference=reference,
        )
    )
    response = _call(
        "create_customer", lambda: client.customers.create_customer(body=body), write=True
    )
    return _customer(response.customer)


def create_subscription(
    *,
    reference: str,
    customer_id: int,
    product_handle: str,
    payment_collection_method: CollectionMethod,
) -> RemoteSubscription:
    client = get_client()
    body = CreateSubscriptionRequest(
        subscription=CreateSubscription(
            product_handle=product_handle,
            customer_id=customer_id,
            reference=reference,
            payment_collection_method=payment_collection_method,
        )
    )
    response = _call(
        "create_subscription",
        lambda: client.subscriptions.create_subscription(body=body),
        write=True,
    )
    return _subscription(response)


def find_subscription(reference: str) -> RemoteSubscription | None:
    """Look a subscription up by our reference. Only Maxio's 404 means absent."""
    client = get_client()
    try:
        response = _call(
            "find_subscription",
            lambda: client.subscriptions.find_subscription(reference=reference),
            write=False,
        )
    except ProviderRejected as exc:
        if exc.provider_status == 404:
            return None
        raise
    return _subscription(response)


def read_subscription(subscription_id: int) -> RemoteSubscription | None:
    client = get_client()
    try:
        response = _call(
            "read_subscription",
            lambda: client.subscriptions.read_subscription(subscription_id),
            write=False,
        )
    except ProviderRejected as exc:
        if exc.provider_status == 404:
            return None
        raise
    return _subscription(response)


def list_customer_subscriptions(customer_id: int) -> list[RemoteSubscription]:
    client = get_client()
    responses = _call(
        "list_customer_subscriptions",
        lambda: client.customers.list_customer_subscriptions(customer_id),
        write=False,
    )
    return [_subscription(r) for r in responses]


__all__ = [
    "Plan",
    "ProviderConfigError",
    "ProviderError",
    "ProviderRejected",
    "ProviderUnavailable",
    "ProviderUnreadable",
    "RemoteCustomer",
    "RemoteSubscription",
    "create_customer",
    "create_subscription",
    "find_customer",
    "find_subscription",
    "list_customer_subscriptions",
    "list_plans",
    "read_subscription",
    "translate",
]
