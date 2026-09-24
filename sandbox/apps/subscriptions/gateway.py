"""
Every call this app makes to Maxio Advanced Billing.

SDK types stop here: callers get plain dataclasses, and every SDK failure is
translated into a ``BillingError`` by one ladder (``_translate``).
"""
import datetime
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn, TypeVar

import httpx
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import ApiError, RawError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest, CustomerError,
    CustomerErrorResponse1, CustomerResponse, ErrorListResponse1, Product, Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import CollectionMethod
from pydantic import ValidationError

from . import maxio
from .exceptions import (
    BillingError, BillingNotConfigured, ProviderRejected, ProviderUnavailable, ProviderUnreadable)

logger = logging.getLogger(__name__)

T = TypeVar('T')

# Failures raised before the request left: nothing can have happened at Maxio.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Provider statuses that are a verdict on what the caller asked for.
CALLER_STATUSES = (400, 404, 409, 422)

# Statuses after which a read is worth one more attempt.
RETRYABLE_READ_STATUSES = (502, 503, 504)

PLANS_PAGE_SIZE = 200


@dataclass(frozen=True)
class Plan:
    handle: str
    product_id: int | None
    name: str
    description: str
    price_in_cents: int | None
    interval: int | None
    interval_unit: str | None


@dataclass(frozen=True)
class BillingCustomer:
    id: int
    reference: str | None


@dataclass(frozen=True)
class BillingSubscription:
    id: int
    reference: str | None
    state: str | None
    plan_handle: str | None
    plan_name: str | None
    price_in_cents: int | None
    currency: str | None
    interval: int | None
    interval_unit: str | None
    next_billing_at: datetime.datetime | None
    current_period_ends_at: datetime.datetime | None
    created_at: datetime.datetime | None
    customer_id: int | None
    customer_reference: str | None


def _value(value: T | UnsetType | None) -> T | None:
    """Collapse the SDK's UNSET sentinel to None; UNSET must not leave this module."""
    return None if isinstance(value, UnsetType) else value


def _text(value: object) -> str | None:
    """Open enums arrive as an Enum member or, for values newer than the SDK, a str."""
    if value is None or isinstance(value, UnsetType):
        return None
    return str(value)


# --- error translation ---------------------------------------------------------------------------------


def _customer_error_details(error: CustomerErrorResponse1) -> list[str]:
    errors = _value(error.errors)
    if isinstance(errors, list):
        return list(errors)
    if isinstance(errors, CustomerError):
        detail = _value(errors.customer)
        return [detail] if detail else []
    return []


def _raw_error_details(error: RawError) -> list[str]:
    try:
        body = error.json()
    except ValueError:
        return []
    errors = body.get('errors') if isinstance(body, dict) else None
    if isinstance(errors, list):
        return [str(item) for item in errors]
    if isinstance(errors, str):
        return [errors]
    return []


def _error_details(error: object) -> list[str]:
    if isinstance(error, ErrorListResponse1):
        return list(error.errors)
    if isinstance(error, CustomerErrorResponse1):
        return _customer_error_details(error)
    if isinstance(error, RawError):
        return _raw_error_details(error)
    if isinstance(error, str) and error:
        return [error]
    return []


def _translate(exc: Exception, *, operation: str, write: bool) -> BillingError:
    """
    Map one SDK/transport failure onto this API's error. ``write`` marks calls
    that change state at Maxio: their failures can leave the outcome unknown.
    """
    if isinstance(exc, ImproperlyConfigured):
        logger.error("Maxio %s: billing is not configured: %s", operation, exc)
        return BillingNotConfigured("Subscription billing is not configured.")

    if isinstance(exc, ApiError):
        status = exc.status_code
        details = _error_details(exc.error)
        if status in (401, 403):
            logger.error("Maxio %s: credentials refused (HTTP %s)", operation, status)
            return ProviderUnavailable("The billing provider refused our credentials.")
        if status == 429:
            logger.warning("Maxio %s: rate limited", operation)
            return ProviderUnavailable("The billing provider is busy; try again shortly.", status_code=503)
        if status in CALLER_STATUSES:
            logger.info("Maxio %s: rejected (HTTP %s): %s", operation, status, details)
            return ProviderRejected("The billing provider rejected the request.", status_code=status,
                                    details=details)
        # A 5xx on a write may have landed; an unmapped 4xx was a rejection of something of ours.
        logger.error("Maxio %s: HTTP %s: %s", operation, status, details)
        return ProviderUnavailable("The billing provider failed to process the request.",
                                   outcome_unknown=write and status >= 500)

    if isinstance(exc, ValidationError | ValueError):
        # A 2xx we cannot decode: the call may well have succeeded.
        logger.error("Maxio %s: unreadable response: %s", operation, type(exc).__name__)
        return ProviderUnreadable("The billing provider's response could not be read.", outcome_unknown=write)

    if isinstance(exc, NEVER_SENT):
        logger.warning("Maxio %s: not sent: %s", operation, type(exc).__name__)
        return ProviderUnavailable("The billing provider could not be reached.")

    if isinstance(exc, httpx.RequestError):
        logger.warning("Maxio %s: no response: %s", operation, type(exc).__name__)
        return ProviderUnavailable("The billing provider did not respond.", status_code=504, outcome_unknown=write)

    raise exc


def _raise(exc: Exception, *, operation: str, write: bool) -> NoReturn:
    raise _translate(exc, operation=operation, write=write) from exc


def _client() -> MaxioAdvancedBillingClient:
    try:
        return maxio.get_client()
    except ImproperlyConfigured as exc:
        _raise(exc, operation='configure', write=False)


def _read(operation: str, call: Callable[[MaxioAdvancedBillingClient], T]) -> T:
    """Run a read, retrying once when it was never sent or the provider was briefly unavailable."""
    client = _client()
    for attempt in (1, 2):
        try:
            return call(client)
        except NEVER_SENT:
            if attempt == 2:
                raise
        except ApiError as exc:
            if attempt == 2 or exc.status_code not in RETRYABLE_READ_STATUSES:
                raise
        logger.info("Maxio %s: retrying once", operation)
        time.sleep(0.5)
    raise AssertionError('unreachable')


# --- mapping ---------------------------------------------------------------------------------------------


def _plan(product: Product) -> Plan | None:
    handle = _value(product.handle)
    if not handle:
        return None
    return Plan(
        handle=handle,
        product_id=_value(product.id),
        name=_value(product.name) or handle,
        description=_value(product.description) or '',
        price_in_cents=_value(product.price_in_cents),
        interval=_value(product.interval),
        interval_unit=_text(product.interval_unit),
    )


def _customer(response: CustomerResponse, *, write: bool = False) -> BillingCustomer:
    customer_id = _value(response.customer.id)
    if customer_id is None:
        raise ProviderUnreadable("The billing provider returned a customer without an id.", outcome_unknown=write)
    return BillingCustomer(id=customer_id, reference=_value(response.customer.reference))


def _subscription(response: SubscriptionResponse, *, write: bool = False) -> BillingSubscription:
    subscription = _value(response.subscription)
    if subscription is None:
        raise ProviderUnreadable("The billing provider returned no subscription.", outcome_unknown=write)
    return _subscription_from(subscription, write=write)


def _subscription_from(subscription: Subscription, *, write: bool = False) -> BillingSubscription:
    subscription_id = _value(subscription.id)
    if subscription_id is None:
        raise ProviderUnreadable("The billing provider returned a subscription without an id.",
                                 outcome_unknown=write)
    product = _value(subscription.product)
    customer = _value(subscription.customer)
    return BillingSubscription(
        id=subscription_id,
        reference=_value(subscription.reference),
        state=_text(subscription.state),
        plan_handle=_value(product.handle) if product else None,
        plan_name=_value(product.name) if product else None,
        price_in_cents=_value(subscription.product_price_in_cents),
        currency=_value(subscription.currency),
        interval=_value(product.interval) if product else None,
        interval_unit=_text(product.interval_unit) if product else None,
        next_billing_at=_value(subscription.next_assessment_at),
        current_period_ends_at=_value(subscription.current_period_ends_at),
        created_at=_value(subscription.created_at),
        customer_id=_value(customer.id) if customer else None,
        customer_reference=_value(customer.reference) if customer else None,
    )


# --- operations ------------------------------------------------------------------------------------------


def list_plans(product_family_handle: str) -> list[Plan]:
    """Every (non-archived) product in the family, by handle."""
    family = 'handle:' + product_family_handle
    plans: list[Plan] = []
    page = 1
    while True:
        try:
            products = _read('list_products_for_product_family', lambda c: c.product_families
                             .list_products_for_product_family(family, page=page, per_page=PLANS_PAGE_SIZE))
        except BillingError:
            raise
        except Exception as exc:
            _raise(exc, operation='list_products_for_product_family', write=False)
        for item in products:
            plan = _plan(item.product)
            if plan is not None:
                plans.append(plan)
        if len(products) < PLANS_PAGE_SIZE:
            return plans
        page += 1


def find_customer(reference: str) -> BillingCustomer | None:
    """The customer carrying our reference, or None when Maxio has none."""
    try:
        return _customer(_read('read_customer_by_reference',
                               lambda c: c.customers.read_customer_by_reference(reference)))
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        _raise(exc, operation='read_customer_by_reference', write=False)
    except BillingError:
        raise
    except Exception as exc:
        _raise(exc, operation='read_customer_by_reference', write=False)


def create_customer(*, reference: str, first_name: str, last_name: str, email: str) -> BillingCustomer:
    body = CreateCustomerRequest(customer=CreateCustomer(
        first_name=first_name, last_name=last_name, email=email, reference=reference))
    try:
        return _customer(_client().customers.create_customer(body=body), write=True)
    except BillingError:
        raise
    except Exception as exc:
        _raise(exc, operation='create_customer', write=True)


def create_subscription(*, product_handle: str, customer_reference: str, reference: str,
                        payment_collection_method: str) -> BillingSubscription:
    try:
        collection_method = CollectionMethod(payment_collection_method)
    except ValueError:
        _raise(ImproperlyConfigured('MAXIO_PAYMENT_COLLECTION_METHOD must be one of %s'
                                    % [m.value for m in CollectionMethod]), operation='configure', write=False)
    body = CreateSubscriptionRequest(subscription=CreateSubscription(
        product_handle=product_handle, customer_reference=customer_reference, reference=reference,
        payment_collection_method=collection_method))
    try:
        return _subscription(_client().subscriptions.create_subscription(body=body), write=True)
    except BillingError:
        raise
    except Exception as exc:
        _raise(exc, operation='create_subscription', write=True)


def find_subscription(reference: str) -> BillingSubscription | None:
    """The subscription carrying our reference, or None when Maxio has none (yet)."""
    try:
        return _subscription(_read('find_subscription',
                                   lambda c: c.subscriptions.find_subscription(reference=reference)))
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        _raise(exc, operation='find_subscription', write=False)
    except BillingError:
        raise
    except Exception as exc:
        _raise(exc, operation='find_subscription', write=False)


def read_subscription(subscription_id: int) -> BillingSubscription | None:
    """The subscription with this id, or None when Maxio has none."""
    try:
        return _subscription(_read('read_subscription',
                                   lambda c: c.subscriptions.read_subscription(subscription_id)))
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        _raise(exc, operation='read_subscription', write=False)
    except BillingError:
        raise
    except Exception as exc:
        _raise(exc, operation='read_subscription', write=False)


def list_customer_subscriptions(customer_id: int) -> list[BillingSubscription]:
    try:
        responses = _read('list_customer_subscriptions',
                          lambda c: c.customers.list_customer_subscriptions(customer_id))
        return [_subscription(response) for response in responses]
    except BillingError:
        raise
    except Exception as exc:
        _raise(exc, operation='list_customer_subscriptions', write=False)
