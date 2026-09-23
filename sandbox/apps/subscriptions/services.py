"""Business logic for the Maxio subscription capability.

Every Maxio SDK call is wrapped by this layer, which converts the SDK's failure kinds
(``ApiError``, ``httpx`` transport errors, pydantic ``ValidationError``) into the app's own
``SubscriptionError`` hierarchy, and implements the durable-row idempotency that makes
subscribe safe against double-clicks and concurrent retries. Contract facts (signatures,
model members, enum values, error unions) come from ``maxio-advanced-billing-plan.md``.
"""

import logging
from typing import Any, Callable, Optional

import httpx
from django.db import IntegrityError, transaction
from pydantic import ValidationError

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import (
    UNSET,
    ApiError,
    Failure,
    OAuthProviderError,
    Success,
)
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
)

from .exceptions import (
    CallerError,
    ConfigurationError,
    ProviderRejected,
    ProviderUnavailable,
    ProviderUnreadable,
    SubscriptionError,
)
from .maxio_client import get_client, is_not_found, resolve_family_id
from .models import MaxioSubscription, SubscriptionStatus

logger = logging.getLogger("maxio.subscriptions")

Payload = dict[str, Any]

# httpx failures that provably never reached the provider (nothing landed).
NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

# SubscriptionState wire values, bucketed (from enums/subscription_state.py; recorded in the plan).
_STATE_LIVE = {"active", "trialing", "assessing"}
_STATE_PENDING = {"pending", "awaiting_signup"}
_STATE_PROBLEM = {"past_due", "soft_failure", "paused", "on_hold", "suspended", "unpaid"}
_STATE_ENDED = {"canceled", "expired", "failed_to_create", "trial_ended"}

REMITTANCE = "remittance"  # CollectionMethodOrStr is an open enum; the string is accepted.


# --------------------------------------------------------------------------- helpers


def _val(value: Any) -> Any:
    """Resolve an SDK optional member (``UNSET``/``None``) to a plain value or None."""
    if value is UNSET or value is None:
        return None
    return value


def _iso(dt: Any) -> Optional[str]:
    dt = _val(dt)
    return dt.isoformat() if dt is not None else None


def status_from_state(state: Any) -> Any:
    # Returns a ``SubscriptionStatus`` member (a str subclass). Annotated ``Any`` because
    # Django's ``TextChoices`` has no type stubs, so mypy reads members as raw tuples.
    """Map a Maxio subscription state (enum or open-enum string) to our status bucket.

    The default arm is UNKNOWN, never ENDED: a state newer than this SDK is neither live
    nor ended and must not silently free the (user, plan) claim.
    """
    if state is UNSET or state is None:
        return SubscriptionStatus.UNKNOWN
    value = str(state)
    if value in _STATE_LIVE:
        return SubscriptionStatus.ACTIVE
    if value in _STATE_PENDING:
        return SubscriptionStatus.PENDING
    if value in _STATE_PROBLEM:
        return SubscriptionStatus.PROBLEM
    if value in _STATE_ENDED:
        return SubscriptionStatus.ENDED
    return SubscriptionStatus.UNKNOWN


def _error_detail(error: Any) -> str:
    """Extract a safe, human-readable detail from a typed error body, if any."""
    errors = getattr(error, "errors", None)
    if isinstance(errors, list) and errors:
        return "; ".join(str(e) for e in errors)
    single = getattr(error, "error", None)
    if isinstance(single, str) and single:
        return single
    return "The billing provider rejected the request."


def _translate_status_error(status: int, error: Any) -> SubscriptionError:
    """Map a provider (status, error-body) to an app exception. Whose fault each status is
    follows python-error-handling's table."""
    if isinstance(error, OAuthProviderError):
        return ConfigurationError("Billing credentials were rejected.")
    if status in (401, 403):
        return ConfigurationError("The billing provider refused our credentials.")
    if status == 429:
        return ConfigurationError(
            "The billing provider is rate limiting requests; try again shortly.",
            status_code=503,
        )
    if status in (400, 404, 409, 422):
        return ProviderRejected(_error_detail(error), status_code=status)
    return ConfigurationError("The billing provider returned an error.", status_code=502)


def _guard(fn: Callable[[], Any], *, write: bool = False) -> Any:
    """Run an SDK call and translate every failure kind to a SubscriptionError.

    ``write`` marks a call with side effects: a no-response transport failure then carries
    ``outcome_unknown=True`` so the caller reconciles rather than assuming failure.
    """
    try:
        return fn()
    except ApiError as e:
        logger.warning("Maxio ApiError %s: %s", e.status_code, _error_detail(e.error))
        raise _translate_status_error(e.status_code, e.error) from e
    except ValidationError as e:
        logger.error("Maxio response could not be decoded", exc_info=True)
        raise ProviderUnreadable("The billing provider returned an unreadable response.") from e
    except NEVER_SENT as e:
        logger.warning("Maxio request never sent: %s", e)
        raise ProviderUnavailable(
            "The billing provider is unreachable.", status_code=502, outcome_unknown=False
        ) from e
    except httpx.RequestError as e:
        logger.warning("Maxio request without response: %s", e)
        raise ProviderUnavailable(
            "The billing provider did not respond.", status_code=504, outcome_unknown=write
        ) from e


def _customer_reference(user: Any) -> str:
    return f"oscar-user-{user.pk}"


def _subscription_reference(user: Any, plan_handle: str) -> str:
    return f"oscar-user-{user.pk}-{plan_handle}"


# --------------------------------------------------------------------------- plans


def list_plans() -> list[Payload]:
    """List the subscribe-able plans in the configured product family."""
    client = get_client()
    family_id = _guard(lambda: resolve_family_id(client))
    products = _guard(
        lambda: client.product_families.list_products_for_product_family(str(family_id))
    )
    plans = []
    for entry in products:
        product = entry.product
        handle = _val(product.handle)
        if not handle:
            # Without a handle a plan cannot be subscribed to via the API; skip it.
            continue
        price_cents = _val(product.price_in_cents)
        plans.append(
            {
                "planHandle": handle,
                "name": _val(product.name),
                "description": _val(product.description),
                "priceInCents": price_cents,
                "priceFormatted": _format_price(price_cents),
                "interval": _val(product.interval),
                "intervalUnit": str(product.interval_unit) if _val(product.interval_unit) else None,
            }
        )
    plans.sort(key=lambda p: (p["priceInCents"] is None, p["priceInCents"] or 0))
    return plans


def _format_price(cents: Optional[int]) -> Optional[str]:
    if cents is None:
        return None
    return f"${cents / 100:.2f}"


# --------------------------------------------------------------------------- customers


def ensure_customer(user: Any, *, create: bool) -> Any:
    """Return the user's Maxio customer, creating it idempotently when ``create`` is set.

    Idempotency rests on a stable per-user reference plus Maxio's uniqueness on that
    reference: a concurrent create that loses the race is caught as a 422 and resolved by
    re-reading the customer. Returns the ``Customer`` model, or None when absent and not
    creating.
    """
    client = get_client()
    reference = _customer_reference(user)

    existing = _read_customer_by_reference(client, reference)
    if existing is not None:
        return existing
    if not create:
        return None

    body = CreateCustomerRequest(
        customer=CreateCustomer(
            first_name=_first_name(user),
            last_name=_last_name(user),
            email=_email(user),
            reference=reference,
        )
    )
    try:
        response = _guard(lambda: client.customers.create_customer(body=body))
    except ProviderRejected:
        # A rejection here is most likely the reference already existing (a concurrent
        # create landed). Re-read: if it now exists, that create is our answer.
        recovered = _read_customer_by_reference(client, reference)
        if recovered is not None:
            return recovered
        raise
    customer = response.customer
    if customer is None or _val(customer.id) is None:
        raise ProviderUnreadable("The billing provider did not return a customer id.")
    return customer


def _read_customer_by_reference(client: MaxioAdvancedBillingClient, reference: str) -> Any:
    """Return the customer for a reference, or None on a 404 'not found'."""
    try:
        response = client.customers.read_customer_by_reference(reference)
    except ApiError as e:
        if is_not_found(e.error):
            return None
        raise _translate_status_error(e.status_code, e.error) from e
    except ValidationError as e:
        raise ProviderUnreadable("The billing provider returned an unreadable response.") from e
    except NEVER_SENT as e:
        raise ProviderUnavailable(
            "The billing provider is unreachable.", status_code=502, outcome_unknown=False
        ) from e
    except httpx.RequestError as e:
        raise ProviderUnavailable(
            "The billing provider did not respond.", status_code=504, outcome_unknown=False
        ) from e
    return response.customer


def _first_name(user: Any) -> str:
    return (getattr(user, "first_name", "") or "").strip() or user.get_username() or "Customer"


def _last_name(user: Any) -> str:
    return (getattr(user, "last_name", "") or "").strip() or "Subscriber"


def _email(user: Any) -> str:
    email = (getattr(user, "email", "") or "").strip()
    return email or f"user-{user.pk}@sandbox.local"


# --------------------------------------------------------------------------- subscribe


def subscribe(user: Any, plan_handle: str) -> tuple[Payload, bool]:
    """Subscribe ``user`` to ``plan_handle`` idempotently.

    Returns ``(payload, created)`` where ``created`` is True only when this call created a
    new subscription at the provider.
    """
    client = get_client()

    # 1. The requested plan must be one the plan listing offers (cross-operation invariant).
    plan = next((p for p in list_plans() if p["planHandle"] == plan_handle), None)
    if plan is None:
        raise CallerError(f"Unknown plan handle {plan_handle!r}.", status_code=404)

    # 2. Ensure the Maxio customer exists (idempotent).
    customer = ensure_customer(user, create=True)
    customer_id = _val(customer.id)

    subscription_reference = _subscription_reference(user, plan_handle)

    # 3. Claim the (user, plan) pair with a durable row, committed BEFORE the provider call.
    #    The partial unique constraint makes the winner unambiguous; the loser resolves from
    #    the existing row rather than calling the provider again.
    try:
        with transaction.atomic():
            row = MaxioSubscription.objects.create(
                user=user,
                plan_handle=plan_handle,
                customer_reference=_customer_reference(user),
                subscription_reference=subscription_reference,
                provider_customer_id=customer_id,
                status=SubscriptionStatus.SENDING,
            )
    except IntegrityError:
        return _resolve_existing_claim(client, user, plan_handle, customer_id)

    # 4. Create the subscription and settle the row from the outcome.
    return _create_subscription(client, row, plan, customer_id)


def _resolve_existing_claim(
    client: MaxioAdvancedBillingClient, user: Any, plan_handle: str, customer_id: Any
) -> tuple[Payload, bool]:
    """A live claim already exists for (user, plan): answer from it, never re-create."""
    row = (
        MaxioSubscription.objects.filter(user=user, plan_handle=plan_handle)
        .exclude(status__in=[SubscriptionStatus.ENDED, SubscriptionStatus.FAILED])
        .order_by("-created_at")
        .first()
    )
    if row is None:
        # The holding claim ended/failed between our insert and this read.
        raise CallerError(
            "The subscription state changed concurrently; please retry.", status_code=409
        )

    lookup_customer = customer_id or row.provider_customer_id
    existing = None
    if lookup_customer is not None:
        existing = _find_subscription_by_reference(
            client, lookup_customer, row.subscription_reference
        )
    if existing is not None:
        _apply_subscription_to_row(row, existing)
        row.save()
    return _row_payload(row), False


def _create_subscription(
    client: MaxioAdvancedBillingClient, row: Any, plan: Payload, customer_id: Any
) -> tuple[Payload, bool]:
    body = CreateSubscriptionRequest(
        subscription=CreateSubscription(
            product_handle=plan["planHandle"],
            customer_id=customer_id,
            payment_collection_method=REMITTANCE,
            reference=row.subscription_reference,
        )
    )
    try:
        result = client.subscriptions.with_raw_response.create_subscription(body=body)
    except ApiError as e:
        # with_raw_response only raises here for an auth/token failure — nothing was sent.
        row.status = SubscriptionStatus.FAILED
        row.save(update_fields=["status", "updated_at"])
        raise _translate_status_error(e.status_code, e.error) from e
    except NEVER_SENT as e:
        row.status = SubscriptionStatus.FAILED
        row.save(update_fields=["status", "updated_at"])
        raise ProviderUnavailable(
            "The billing provider is unreachable.", status_code=502, outcome_unknown=False
        ) from e
    except (ValidationError, httpx.RequestError) as e:
        # The request may have landed; reconcile by the reference we sent.
        return _reconcile_uncertain(client, row, customer_id, e)

    if isinstance(result, Failure):
        return _handle_create_failure(client, row, customer_id, result)

    subscription = _val(result.payload.subscription)
    if subscription is None or _val(subscription.id) is None:
        # A 2xx with no usable id: outcome unknown, reconcile.
        return _reconcile_uncertain(client, row, customer_id, None)

    _apply_subscription_to_row(row, subscription)
    row.save()
    return _row_payload(row), True


def _handle_create_failure(
    client: MaxioAdvancedBillingClient, row: Any, customer_id: Any, result: Any
) -> tuple[Payload, bool]:
    status = result.response.status_code
    # An earlier attempt under the same reference may have already landed.
    existing = None
    if customer_id is not None:
        try:
            existing = _find_subscription_by_reference(
                client, customer_id, row.subscription_reference
            )
        except SubscriptionError:
            existing = None
    if existing is not None:
        _apply_subscription_to_row(row, existing)
        row.save()
        return _row_payload(row), False

    row.status = SubscriptionStatus.FAILED
    row.save(update_fields=["status", "updated_at"])
    raise _translate_status_error(status, result.error)


def _reconcile_uncertain(
    client: MaxioAdvancedBillingClient, row: Any, customer_id: Any, exc: Optional[Exception]
) -> tuple[Payload, bool]:
    if exc is not None:
        logger.warning("Subscription create outcome uncertain: %s", exc)
    existing = None
    if customer_id is not None:
        try:
            existing = _find_subscription_by_reference(
                client, customer_id, row.subscription_reference
            )
        except SubscriptionError:
            existing = None
    if existing is not None:
        _apply_subscription_to_row(row, existing)
        row.save()
        return _row_payload(row), True

    # An empty lookup cannot prove the write did not land — leave the claim as UNKNOWN.
    row.status = SubscriptionStatus.UNKNOWN
    row.save(update_fields=["status", "updated_at"])
    raise ProviderUnavailable(
        "The subscription request was sent but its outcome is unknown; it will be reconciled.",
        status_code=504,
        outcome_unknown=True,
    )


def _find_subscription_by_reference(
    client: MaxioAdvancedBillingClient, customer_id: Any, reference: str
) -> Any:
    subscriptions = _guard(
        lambda: client.customers.list_customer_subscriptions(int(customer_id))
    )
    for entry in subscriptions:
        sub = entry.subscription
        if sub is not None and _val(sub.reference) == reference:
            return sub
    return None


def _apply_subscription_to_row(row: Any, sub: Any) -> None:
    row.provider_subscription_id = _val(sub.id)
    row.state_raw = str(sub.state) if _val(sub.state) is not None else ""
    row.status = status_from_state(sub.state)
    row.current_period_ends_at = _val(sub.current_period_ends_at)
    product = _val(sub.product)
    if product is not None:
        row.plan_name = _val(product.name) or row.plan_name
        row.price_in_cents = _val(sub.product_price_in_cents) or _val(product.price_in_cents)
    else:
        row.price_in_cents = _val(sub.product_price_in_cents) or row.price_in_cents


def _row_payload(row: Any) -> Payload:
    return {
        "subscriptionId": row.provider_subscription_id,
        "planHandle": row.plan_handle,
        "planName": row.plan_name or None,
        "state": row.state_raw or None,
        "status": row.status,
        "nextBillingDate": row.current_period_ends_at.isoformat()
        if row.current_period_ends_at
        else None,
        "priceInCents": row.price_in_cents,
        "priceFormatted": _format_price(row.price_in_cents),
    }


# --------------------------------------------------------------------------- read-back


def list_my_subscriptions(user: Any) -> list[Payload]:
    """Return the user's subscriptions as reported by Maxio (the record of what exists)."""
    client = get_client()
    customer = ensure_customer(user, create=False)
    if customer is None:
        return []

    customer_id = _val(customer.id)
    subscriptions = _guard(
        lambda: client.customers.list_customer_subscriptions(int(customer_id))
    )
    payloads = []
    for entry in subscriptions:
        sub = entry.subscription
        if sub is None:
            continue
        payloads.append(_subscription_payload(sub))
        _sync_local_row(user, sub)
    return payloads


def _subscription_payload(sub: Any) -> Payload:
    product = _val(sub.product)
    price_cents = _val(sub.product_price_in_cents) or (
        _val(product.price_in_cents) if product is not None else None
    )
    return {
        "subscriptionId": _val(sub.id),
        "planHandle": _val(product.handle) if product is not None else None,
        "planName": _val(product.name) if product is not None else None,
        "state": str(sub.state) if _val(sub.state) is not None else None,
        "status": status_from_state(sub.state),
        "nextBillingDate": _iso(sub.current_period_ends_at),
        "priceInCents": price_cents,
        "priceFormatted": _format_price(price_cents),
        "reference": _val(sub.reference),
    }


def _sync_local_row(user: Any, sub: Any) -> None:
    """Best-effort refresh of a local claim row from an authoritative provider read."""
    reference = _val(sub.reference)
    if not reference:
        return
    row = MaxioSubscription.objects.filter(
        user=user, subscription_reference=reference
    ).first()
    if row is None:
        return
    _apply_subscription_to_row(row, sub)
    try:
        row.save()
    except Exception:  # pragma: no cover - snapshot refresh must never break a read
        logger.warning("Failed to refresh local subscription row", exc_info=True)
