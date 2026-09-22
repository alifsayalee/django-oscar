"""Business logic for Maxio subscription billing.

This module is the single place that talks to the Maxio SDK. It exposes four
capabilities the API layer drives:

- :func:`list_plans`            — the plans the configured product family offers.
- :func:`ensure_customer`       — idempotently map a Django user to a Maxio customer.
- :func:`subscribe`             — enroll a user in a plan (idempotent on double-submit).
- :func:`list_my_subscriptions` — a user's subscriptions, read back from Maxio.

Every SDK/transport failure is translated to a single :class:`MaxioServiceError`
(see :func:`_translate`) so callers branch on one type. SDK response models never
leave this module: values are mapped to plain JSON-safe dicts, resolving the SDK's
``UNSET`` sentinel to ``None`` so nothing downstream trips over it.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
from django.conf import settings
from pydantic import ValidationError

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import ApiError, RawError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
)
from maxio_advanced_billing.models.enums import CollectionMethod

from .client import get_client
from .errors import MaxioServiceError, PlanNotFound

# The default subscribe target when a caller names no plan (a stable handle, not a
# secret). Callers may pass any handle the product family actually offers.
DEFAULT_PLAN_HANDLE = "eshop-pro"

# States in which a subscription is finished and a fresh one may be created. Any
# other state means the customer already has a live subscription to that plan, so a
# repeat subscribe reuses it rather than creating a second (idempotency).
TERMINAL_STATES = frozenset({"canceled", "expired", "failed_to_create"})

# Exceptions raised by the SDK request path. ``ApiError`` for a non-2xx, pydantic
# ``ValidationError`` for a body that will not decode (bypasses both response modes),
# and ``httpx.HTTPError`` for every transport failure (raised unwrapped).
_WIRE_ERRORS = (ApiError, ValidationError, httpx.HTTPError)

# Transport failures where the request provably never reached the provider, so the
# outcome is known: nothing happened.
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #
def _error_detail(error: Any) -> str:
    """A short, safe description of a provider error body (never surfaced raw)."""
    if isinstance(error, RawError):
        try:
            return error.text()[:500]
        except Exception:  # noqa: BLE001 - body may not be decodable text
            return f"HTTP {error.status_code}"
    errors = getattr(error, "errors", None)
    if errors:
        return "; ".join(str(x) for x in errors)[:500]
    single = getattr(error, "error", None)
    if single:
        return str(single)[:500]
    try:
        return str(error.to_dict())[:500]
    except Exception:  # noqa: BLE001
        return "the billing provider rejected the request"


def _map_api_error(action: str, exc: ApiError) -> MaxioServiceError:
    status = exc.status_code
    # The caller's fault — pass the status through (named list, never a range).
    if status in (400, 404, 409, 422):
        return MaxioServiceError(
            f"{action}: {_error_detail(exc.error)}", status_code=status
        )
    # Our credentials/quota — never surface as the caller's 401/403/429.
    if status in (401, 403):
        return MaxioServiceError(
            f"{action}: billing provider refused our credentials", status_code=502
        )
    if status == 429:
        return MaxioServiceError(
            f"{action}: billing provider temporarily unavailable", status_code=503
        )
    # 5xx and every unmapped 4xx.
    return MaxioServiceError(
        f"{action}: billing provider error (HTTP {status})", status_code=502
    )


def _translate(action: str, exc: Exception, *, write: bool = False) -> MaxioServiceError:
    """Map a low-level SDK/transport exception to a boundary error.

    ``write`` marks operations whose effect may have landed upstream even when we
    cannot confirm it, so an unreadable response / read-timeout is flagged
    ``outcome_unknown`` for reconciliation rather than reported as a clean failure.
    """
    if isinstance(exc, ApiError):
        return _map_api_error(action, exc)
    if isinstance(exc, ValidationError):
        return MaxioServiceError(
            f"{action}: unreadable response from billing provider",
            status_code=502,
            outcome_unknown=write,
        )
    if isinstance(exc, _NEVER_SENT):
        return MaxioServiceError(
            f"{action}: billing provider unreachable",
            status_code=502,
            outcome_unknown=False,
        )
    if isinstance(exc, httpx.RequestError):
        # Sent, but no usable reply — it may have landed.
        return MaxioServiceError(
            f"{action}: no response from billing provider",
            status_code=504,
            outcome_unknown=write,
        )
    # An HTTPStatusError or anything else in httpx.HTTPError we did not name.
    return MaxioServiceError(f"{action}: billing provider error", status_code=502)


# --------------------------------------------------------------------------- #
# SDK value -> JSON-safe helpers (resolve UNSET to None; never leak the sentinel)
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> Any:
    """Return ``None`` for UNSET/None, otherwise the value unchanged."""
    if value is None or isinstance(value, UnsetType):
        return None
    return value


def _enum_str(value: Any) -> Any:
    """A string enum's wire value, or the bare string for an unknown member."""
    if value is None or isinstance(value, UnsetType):
        return None
    return getattr(value, "value", value)


def _dt(value: Any) -> Any:
    """An ISO-8601 string for a date-time member, or ``None`` when unset/null."""
    if value is None or isinstance(value, UnsetType):
        return None
    return value.isoformat()


def _plan_to_dict(product: Any) -> dict[str, Any]:
    return {
        "planHandle": _clean(product.handle),
        "name": _clean(product.name),
        "description": _clean(product.description),
        "priceInCents": _clean(product.price_in_cents),
        "interval": _clean(product.interval),
        "intervalUnit": _enum_str(product.interval_unit),
        "productPricePointHandle": _clean(product.product_price_point_handle),
        "requireCreditCard": _clean(product.require_credit_card),
    }


def _product_of(subscription: Any) -> Any | None:
    product = subscription.product
    if product is None or isinstance(product, UnsetType):
        return None
    return product


def _subscription_to_dict(subscription: Any) -> dict[str, Any]:
    product = _product_of(subscription)
    next_billing = _dt(subscription.current_period_ends_at)
    return {
        "subscriptionId": _clean(subscription.id),
        "state": _enum_str(subscription.state),
        "planHandle": _clean(product.handle) if product is not None else None,
        "planName": _clean(product.name) if product is not None else None,
        "priceInCents": _clean(subscription.product_price_in_cents),
        "nextBillingDate": next_billing,
        "currentPeriodEndsAt": next_billing,
        "nextAssessmentAt": _dt(subscription.next_assessment_at),
        "activatedAt": _dt(subscription.activated_at),
        "createdAt": _dt(subscription.created_at),
    }


# --------------------------------------------------------------------------- #
# Customer identity
# --------------------------------------------------------------------------- #
def customer_reference(user: Any) -> str:
    """Deterministic Maxio customer reference for a Django user (idempotency key)."""
    return f"eshop-user-{user.pk}"


def _lookup_customer_id(client: MaxioAdvancedBillingClient, reference: str) -> int | None:
    """Return the Maxio customer id for ``reference``, or ``None`` if none exists."""
    action = "look up customer"
    try:
        response = client.customers.read_customer_by_reference(reference)
    except ApiError as exc:
        if exc.status_code == 404:
            return None  # a genuine miss, not an error
        raise _translate(action, exc) from exc
    except (ValidationError, httpx.HTTPError) as exc:
        raise _translate(action, exc) from exc
    return cast("int | None", _clean(response.customer.id))


def ensure_customer(client: MaxioAdvancedBillingClient, user: Any) -> int:
    """Return the Maxio customer id for ``user``, creating one if needed.

    Idempotent: keyed on a deterministic reference, so a double-submit never creates
    two customers. A create that races another and loses (reference already taken,
    422) is recovered by re-looking-up the winner.
    """
    reference = customer_reference(user)

    existing = _lookup_customer_id(client, reference)
    if existing is not None:
        return existing

    first_name = (getattr(user, "first_name", "") or getattr(user, "username", "") or "Customer")
    last_name = getattr(user, "last_name", "") or "Shopper"
    email = getattr(user, "email", "") or f"{reference}@example.com"

    body = CreateCustomerRequest(
        customer=CreateCustomer(
            first_name=first_name,
            last_name=last_name,
            email=email,
            reference=reference,
        )
    )

    action = "create customer"
    try:
        response = client.customers.create_customer(body=body)
    except ApiError as exc:
        if exc.status_code == 422:
            # Lost a create race (reference/email already taken): reuse the winner.
            winner = _lookup_customer_id(client, reference)
            if winner is not None:
                return winner
        raise _translate(action, exc, write=True) from exc
    except (ValidationError, httpx.HTTPError) as exc:
        raise _translate(action, exc, write=True) from exc

    customer_id = _clean(response.customer.id)
    if customer_id is None:
        raise MaxioServiceError(
            "create customer: provider returned no customer id; outcome unknown",
            status_code=502,
            outcome_unknown=True,
        )
    return cast(int, customer_id)


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
def _resolve_family_id(client: MaxioAdvancedBillingClient) -> int:
    """Resolve the configured product-family handle to its current numeric id.

    IDs are reassigned on re-seed, so the handle is the stable key: we list families
    and match by handle rather than trusting any hard-coded id.
    """
    handle = getattr(settings, "MAXIO_DEFAULT_PRODUCT_FAMILY", None)
    if not handle:
        raise MaxioServiceError(
            "MAXIO_DEFAULT_PRODUCT_FAMILY is not configured", status_code=502
        )

    action = "resolve product family"
    try:
        families = client.product_families.list_product_families()
    except _WIRE_ERRORS as exc:
        raise _translate(action, exc) from exc

    for wrapper in families:
        family = wrapper.product_family
        if isinstance(family, UnsetType):
            continue
        if _clean(family.handle) == handle:
            family_id = _clean(family.id)
            if family_id is not None:
                return cast(int, family_id)

    raise MaxioServiceError(
        f"product family '{handle}' not found on the Maxio site", status_code=502
    )


def list_plans(client: MaxioAdvancedBillingClient | None = None) -> list[dict[str, Any]]:
    """List the subscription plans the configured product family offers."""
    client = client or get_client()
    family_id = _resolve_family_id(client)

    action = "list subscription plans"
    try:
        products = client.product_families.list_products_for_product_family(
            str(family_id), per_page=200
        )
    except _WIRE_ERRORS as exc:
        raise _translate(action, exc) from exc

    return [_plan_to_dict(wrapper.product) for wrapper in products]


# --------------------------------------------------------------------------- #
# Subscribe / read back
# --------------------------------------------------------------------------- #
def _list_customer_subscriptions(
    client: MaxioAdvancedBillingClient, customer_id: int
) -> list[Any]:
    action = "list customer subscriptions"
    try:
        wrappers = client.customers.list_customer_subscriptions(customer_id)
    except _WIRE_ERRORS as exc:
        raise _translate(action, exc) from exc
    subscriptions = []
    for wrapper in wrappers:
        subscription = wrapper.subscription
        if subscription is not None and not isinstance(subscription, UnsetType):
            subscriptions.append(subscription)
    return subscriptions


def _find_reusable_subscription(
    client: MaxioAdvancedBillingClient, customer_id: int, plan_handle: str
) -> Any | None:
    """Return an existing non-terminal subscription to ``plan_handle``, if any."""
    for subscription in _list_customer_subscriptions(client, customer_id):
        product = _product_of(subscription)
        handle = _clean(product.handle) if product is not None else None
        state = _enum_str(subscription.state)
        if handle == plan_handle and state not in TERMINAL_STATES:
            return subscription
    return None


def subscribe(
    user: Any,
    plan_handle: str | None = None,
    client: MaxioAdvancedBillingClient | None = None,
) -> tuple[dict[str, Any], bool]:
    """Subscribe ``user`` to ``plan_handle``.

    Returns ``(subscription_dict, created)`` where ``created`` is ``False`` when an
    existing live subscription to the same plan was reused. Idempotent against a
    double-click: the second call finds the first call's subscription and reuses it.
    """
    client = client or get_client()
    plan_handle = plan_handle or DEFAULT_PLAN_HANDLE

    # Cross-operation invariant: only a handle the family actually offers is legal.
    valid_handles = {plan["planHandle"] for plan in list_plans(client)}
    if plan_handle not in valid_handles:
        raise PlanNotFound(f"unknown plan handle '{plan_handle}'")

    customer_id = ensure_customer(client, user)

    reused = _find_reusable_subscription(client, customer_id, plan_handle)
    if reused is not None:
        return _subscription_to_dict(reused), False

    # Plans require no payment method (require_credit_card=False). The provider default
    # (automatic collection) would still try to charge the first period immediately and
    # fail with "no payment method on file", so we bill by invoice (remittance): the
    # subscription activates without card capture, exactly as these plans intend.
    body = CreateSubscriptionRequest(
        subscription=CreateSubscription(
            product_handle=plan_handle,
            customer_id=customer_id,
            payment_collection_method=CollectionMethod.REMITTANCE,
        )
    )

    action = "create subscription"
    try:
        response = client.subscriptions.create_subscription(body=body)
    except _WIRE_ERRORS as exc:
        raise _translate(action, exc, write=True) from exc

    subscription = response.subscription
    if (
        subscription is None
        or isinstance(subscription, UnsetType)
        or _clean(subscription.id) is None
    ):
        raise MaxioServiceError(
            "create subscription: provider returned no subscription id; outcome unknown",
            status_code=502,
            outcome_unknown=True,
        )
    return _subscription_to_dict(subscription), True


def list_my_subscriptions(
    user: Any, client: MaxioAdvancedBillingClient | None = None
) -> list[dict[str, Any]]:
    """List ``user``'s subscriptions, read back from Maxio. Empty if no customer."""
    client = client or get_client()
    customer_id = _lookup_customer_id(client, customer_reference(user))
    if customer_id is None:
        return []
    return [
        _subscription_to_dict(subscription)
        for subscription in _list_customer_subscriptions(client, customer_id)
    ]
