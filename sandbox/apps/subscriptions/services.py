"""
Subscription billing use cases, with Maxio Advanced Billing as the system of record.

Every Maxio write goes through ``safe_write``; every read goes through
``read_with_retry``; every failure leaves through ``from_provider_error``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

import httpx
from django.conf import settings
from django.core.cache import cache
from maxio_advanced_billing.core import ApiError, RawError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
    CustomerResponse,
    Product,
    Subscription,
    SubscriptionResponse,
)

from . import maxio
from .errors import BillingError, from_provider_error
from .models import MaxioWrite
from .outcomes import status_from_provider, state_value
from .safe_write import Answer, WriteSpec, safe_write

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger("apps.subscriptions")

PLANS_CACHE_SECONDS = 60
PLANS_PAGE_SIZE = 200  # the provider's maximum per_page


def _v(value: Any) -> Any:
    """Resolve the SDK's UNSET sentinel to None before a value leaves the app."""
    return None if isinstance(value, UnsetType) else value


def _iso(value: Any) -> str | None:
    value = _v(value)
    return value.isoformat() if isinstance(value, datetime) else None


def _money(cents: Any) -> str | None:
    cents = _v(cents)
    if not isinstance(cents, int):
        return None
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


# --- references ----------------------------------------------------------------

def _prefix() -> str:
    return str(settings.MAXIO_REFERENCE_PREFIX)


def customer_reference(user: User) -> str:
    return f"{_prefix()}-cust-u{user.pk}"


def subscription_reference(user: User, plan_handle: str, generation: int) -> str:
    return f"{_prefix()}-sub-u{user.pk}-{plan_handle}-g{generation}"


def idempotency_key(reference: str) -> str:
    """The same Idempotency-Key on every attempt of one write (the SDK would send a random one)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"maxio:{reference}"))


def next_generation(user: User, plan_handle: str) -> int:
    """
    1 + the number of this user's subscriptions to this plan that ended
    (failed with a provider id). Racing requests compute the same value, so
    they derive the same reference and the claim picks one winner.
    """
    ended = MaxioWrite.objects.filter(
        user=user,
        kind=MaxioWrite.KIND_SUBSCRIPTION,
        plan_handle=plan_handle,
        outcome=MaxioWrite.FAILED,
        provider_id__isnull=False,
    ).count()
    return ended + 1


# --- plans -----------------------------------------------------------------------

def _serialize_plan(product: Product) -> dict[str, Any]:
    family = _v(product.product_family)
    return {
        "planHandle": _v(product.handle),
        "planId": _v(product.id),
        "name": _v(product.name),
        "description": _v(product.description),
        "priceInCents": _v(product.price_in_cents),
        "price": _money(product.price_in_cents),
        "interval": _v(product.interval),
        "intervalUnit": state_value(product.interval_unit),
        "trialPriceInCents": _v(product.trial_price_in_cents),
        "initialChargeInCents": _v(product.initial_charge_in_cents),
        "requiresPaymentMethod": _v(product.require_credit_card),
        "productFamily": _v(family.handle) if family is not None else None,
    }


def list_plans() -> list[dict[str, Any]]:
    """Active (non-archived) plans of the configured product family."""
    cache_key = f"maxio:plans:{settings.MAXIO_DEFAULT_PRODUCT_FAMILY}"
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)

    client = maxio.get_client()
    family = f"handle:{settings.MAXIO_DEFAULT_PRODUCT_FAMILY}"
    plans: list[dict[str, Any]] = []
    page = 1
    try:
        while True:
            batch = maxio.read_with_retry(
                lambda: client.product_families.list_products_for_product_family(
                    family, page=page, per_page=PLANS_PAGE_SIZE
                )
            )
            for item in batch:
                product = item.product
                if _v(product.archived_at) is not None or not _v(product.handle):
                    continue
                plans.append(_serialize_plan(product))
            if len(batch) < PLANS_PAGE_SIZE:
                break
            page += 1
    except ApiError as exc:
        if exc.status_code == 404:
            logger.error("Maxio product family %r not found", settings.MAXIO_DEFAULT_PRODUCT_FAMILY)
            raise BillingError(503, "billing_misconfigured", "Subscription plans are unavailable.") from exc
        raise from_provider_error(exc) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise from_provider_error(exc) from exc

    cache.set(cache_key, plans, PLANS_CACHE_SECONDS)
    return plans


def get_plan(plan_handle: str) -> dict[str, Any]:
    for plan in list_plans():
        if plan["planHandle"] == plan_handle:
            return plan
    raise BillingError(404, "unknown_plan", f"No subscription plan with handle {plan_handle!r}.")


# --- customer --------------------------------------------------------------------

def _customer_answer(response: CustomerResponse) -> Answer:
    customer_id = _v(response.customer.id)
    # A customer has no status of its own: an id means it exists.
    return Answer(
        provider_id=customer_id if isinstance(customer_id, int) else None,
        outcome=MaxioWrite.DONE,
    )


def _find_customer(reference: str) -> CustomerResponse | None:
    client = maxio.get_client()
    try:
        return maxio.read_with_retry(lambda: client.customers.read_customer_by_reference(reference))
    except ApiError as exc:
        if exc.status_code == 404 and isinstance(exc.error, RawError):
            return None
        raise


def _customer_names(user: User) -> tuple[str, str]:
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    email = (getattr(user, "email", "") or "").strip()
    if not first:
        first = email.split("@", 1)[0] if email else str(user.get_username())
    return first, last or "-"


def ensure_customer(user: User) -> MaxioWrite:
    """Exactly one Maxio customer per user, keyed by a reference Maxio itself keeps unique."""
    email = (getattr(user, "email", "") or "").strip()
    if not email:
        raise BillingError(400, "email_required", "Your account needs an email address to subscribe.")
    first_name, last_name = _customer_names(user)
    reference = customer_reference(user)
    client = maxio.get_client()

    def send(ref: str) -> CustomerResponse:
        return client.customers.create_customer(
            body=CreateCustomerRequest(
                customer=CreateCustomer(first_name=first_name, last_name=last_name, email=email, reference=ref)
            ),
            request_options={"extra_headers": {"Idempotency-Key": idempotency_key(ref)}},
        )

    def is_duplicate(exc: ApiError) -> bool:
        # Maxio allows one customer per reference: a 422 may mean an earlier
        # attempt already created it. Only a lookup can tell.
        if exc.status_code != 422:
            return False
        try:
            return _find_customer(reference) is not None
        except (ApiError, httpx.RequestError, ValueError):
            return True  # cannot tell: let the check settle it

    record = safe_write(
        WriteSpec(
            reference=reference,
            kind=MaxioWrite.KIND_CUSTOMER,
            user=user,
            send=send,
            find=_find_customer,
            read=_customer_answer,
            repeat_is_safe=True,
            is_duplicate=is_duplicate,
        )
    )
    if record.outcome != MaxioWrite.DONE or record.provider_id is None:
        raise BillingError(
            502, "customer_not_ready", "Your billing account could not be confirmed.",
            outcome_unknown=record.outcome in (MaxioWrite.UNKNOWN, MaxioWrite.SENDING),
            reference=reference,
        )
    return record


def existing_customer_id(user: User) -> int | None:
    """The caller's Maxio customer id, without creating one."""
    record = (
        MaxioWrite.objects.filter(user=user, kind=MaxioWrite.KIND_CUSTOMER, outcome=MaxioWrite.DONE)
        .exclude(provider_id__isnull=True)
        .first()
    )
    if record is not None and record.provider_id is not None:
        return record.provider_id
    try:
        found = _find_customer(customer_reference(user))
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise from_provider_error(exc) from exc
    if found is None:
        return None
    customer_id = _v(found.customer.id)
    return customer_id if isinstance(customer_id, int) else None


# --- subscriptions ---------------------------------------------------------------

def serialize_subscription(sub: Subscription) -> dict[str, Any]:
    product = _v(sub.product)
    customer = _v(sub.customer)
    return {
        "subscriptionId": _v(sub.id),
        "reference": _v(sub.reference),
        "state": state_value(sub.state),
        "outcome": status_from_provider(sub.state),
        "planHandle": _v(product.handle) if product is not None else None,
        "planName": _v(product.name) if product is not None else None,
        "priceInCents": _v(sub.product_price_in_cents),
        "price": _money(sub.product_price_in_cents),
        "currency": _v(sub.currency),
        "interval": _v(product.interval) if product is not None else None,
        "intervalUnit": state_value(product.interval_unit) if product is not None else None,
        "nextBillingAt": _iso(sub.next_assessment_at),
        "currentPeriodEndsAt": _iso(sub.current_period_ends_at),
        "activatedAt": _iso(sub.activated_at),
        "createdAt": _iso(sub.created_at),
        "canceledAt": _iso(sub.canceled_at),
        "customerId": _v(customer.id) if customer is not None else None,
    }


def _subscription_answer(plan_handle: str, customer_id: int) -> Callable[[SubscriptionResponse], Answer]:
    def read(response: SubscriptionResponse) -> Answer:
        sub = _v(response.subscription)
        if sub is None:
            return Answer(provider_id=None, outcome=MaxioWrite.UNKNOWN)
        sub_id = _v(sub.id)
        product = _v(sub.product)
        customer = _v(sub.customer)
        mismatch = []
        got_plan = _v(product.handle) if product is not None else None
        if got_plan != plan_handle:
            mismatch.append(f"plan {got_plan!r} != requested {plan_handle!r}")
        got_customer = _v(customer.id) if customer is not None else None
        if got_customer != customer_id:
            mismatch.append(f"customer {got_customer!r} != {customer_id!r}")
        return Answer(
            provider_id=sub_id if isinstance(sub_id, int) else None,
            outcome=status_from_provider(sub.state),
            provider_state=state_value(sub.state),
            provider_time=_v(sub.created_at),
            mismatch="; ".join(mismatch),
        )

    return read


def _find_subscription(reference: str) -> SubscriptionResponse | None:
    client = maxio.get_client()
    try:
        response = maxio.read_with_retry(lambda: client.subscriptions.find_subscription(reference=reference))
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise
    return None if isinstance(response.subscription, UnsetType) else response


@dataclass(frozen=True)
class SubscribeResult:
    record: MaxioWrite
    subscription: dict[str, Any] | None


def subscribe(user: User, plan_handle: str) -> SubscribeResult:
    plan = get_plan(plan_handle)  # only plans of the configured family, not archived
    customer = ensure_customer(user)
    customer_id = customer.provider_id
    assert customer_id is not None

    reference = subscription_reference(user, plan_handle, next_generation(user, plan_handle))
    collection = maxio.collection_method()
    client = maxio.get_client()

    def send(ref: str) -> SubscriptionResponse:
        return client.subscriptions.create_subscription(
            body=CreateSubscriptionRequest(
                subscription=CreateSubscription(
                    product_handle=plan["planHandle"],
                    customer_id=customer_id,
                    reference=ref,
                    payment_collection_method=collection,
                )
            ),
            request_options={"extra_headers": {"Idempotency-Key": idempotency_key(ref)}},
        )

    record = safe_write(
        WriteSpec(
            reference=reference,
            kind=MaxioWrite.KIND_SUBSCRIPTION,
            user=user,
            plan_handle=plan_handle,
            send=send,
            find=_find_subscription,
            read=_subscription_answer(plan_handle, customer_id),
            # Maxio does not document subscription references as unique, so a
            # resend could create a second one: the check is a lookup only.
            repeat_is_safe=False,
        )
    )
    return SubscribeResult(record=record, subscription=refresh(record))


def refresh(record: MaxioWrite) -> dict[str, Any] | None:
    """
    Read the subscription live and bring the stored outcome up to date
    (pending -> done, done -> failed after a cancel, ...). A failed read
    leaves the stored outcome as it is.
    """
    if record.provider_id is None or record.outcome == MaxioWrite.NEEDS_REVIEW:
        return None
    client = maxio.get_client()
    subscription_id = record.provider_id
    try:
        response = maxio.read_with_retry(lambda: client.subscriptions.read_subscription(subscription_id))
    except (ApiError, httpx.RequestError, ValueError) as exc:
        logger.warning("Could not re-read subscription %s: %s", subscription_id, type(exc).__name__)
        return None
    sub = _v(response.subscription)
    if sub is None:
        return None
    outcome = status_from_provider(sub.state)
    state = state_value(sub.state)
    if outcome != record.outcome or state != record.provider_state:
        MaxioWrite.objects.filter(pk=record.pk).update(outcome=outcome, provider_state=state)
        record.outcome, record.provider_state = outcome, state
    return serialize_subscription(sub)


def reconcile_unsettled(user: User) -> None:
    """Settle this user's unknown / stale subscription writes by looking them up by reference."""
    for record in MaxioWrite.objects.filter(
        user=user, kind=MaxioWrite.KIND_SUBSCRIPTION, outcome=MaxioWrite.UNKNOWN
    ):
        try:
            found = _find_subscription(record.reference)
        except (ApiError, httpx.RequestError, ValueError):
            continue  # still unknown; a failed check settles nothing
        if found is None:
            continue  # not found YET is not "failed"
        sub = _v(found.subscription)
        sub_id = _v(sub.id) if sub is not None else None
        if sub is None or not isinstance(sub_id, int):
            continue
        MaxioWrite.objects.filter(pk=record.pk).update(
            outcome=status_from_provider(sub.state),
            provider_id=sub_id,
            provider_state=state_value(sub.state),
            provider_time=_v(sub.created_at),
            detail="settled by lookup",
        )


def my_subscriptions(user: User) -> dict[str, Any]:
    reconcile_unsettled(user)
    customer_id = existing_customer_id(user)
    subscriptions: list[dict[str, Any]] = []
    if customer_id is not None:
        client = maxio.get_client()
        try:
            items = maxio.read_with_retry(lambda: client.customers.list_customer_subscriptions(customer_id))
        except (ApiError, httpx.RequestError, ValueError) as exc:
            raise from_provider_error(exc) from exc
        for item in items:
            sub = _v(item.subscription)
            if sub is not None:
                subscriptions.append(serialize_subscription(sub))
        _sync_outcomes(user, subscriptions)

    unsettled = [
        {
            "reference": r.reference,
            "planHandle": r.plan_handle,
            "outcome": r.outcome,
            "subscriptionId": r.provider_id,
            "requestedAt": r.claimed_at.isoformat(),
        }
        for r in MaxioWrite.objects.filter(
            user=user,
            kind=MaxioWrite.KIND_SUBSCRIPTION,
            outcome__in=(MaxioWrite.SENDING, MaxioWrite.UNKNOWN, MaxioWrite.NEEDS_REVIEW),
        )
    ]
    return {"customerId": customer_id, "subscriptions": subscriptions, "unsettled": unsettled}


def _sync_outcomes(user: User, subscriptions: list[dict[str, Any]]) -> None:
    """Keep stored outcomes in step with what Maxio just said (e.g. a canceled subscription)."""
    by_id = {s["subscriptionId"]: s for s in subscriptions if s["subscriptionId"] is not None}
    for record in MaxioWrite.objects.filter(
        user=user, kind=MaxioWrite.KIND_SUBSCRIPTION, provider_id__in=list(by_id)
    ).exclude(outcome=MaxioWrite.NEEDS_REVIEW):
        live = by_id[record.provider_id]
        if record.outcome != live["outcome"] or record.provider_state != live["state"]:
            MaxioWrite.objects.filter(pk=record.pk).update(outcome=live["outcome"], provider_state=live["state"])


def get_my_subscription(user: User, subscription_id: int) -> dict[str, Any]:
    customer_id = existing_customer_id(user)
    if customer_id is None:
        raise BillingError(404, "not_found", "Subscription not found.")
    client = maxio.get_client()
    try:
        response = maxio.read_with_retry(lambda: client.subscriptions.read_subscription(subscription_id))
    except (ApiError, httpx.RequestError, ValueError) as exc:
        raise from_provider_error(exc) from exc
    sub = _v(response.subscription)
    if sub is None:
        raise BillingError(502, "provider_unreadable", "Unreadable billing provider response.")
    data = serialize_subscription(sub)
    if data["customerId"] != customer_id:
        # Someone else's subscription: indistinguishable from absent.
        raise BillingError(404, "not_found", "Subscription not found.")
    return data
