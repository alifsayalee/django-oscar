"""
Subscription billing flows, with Maxio as the system of record.

Every write follows one order: claim a row in this site's database (a unique
constraint refuses a second claim), call Maxio, record the answer. A write
that may have landed without a readable answer is settled by looking it up in
Maxio by the reference it carried.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest, Customer, Product,
    Subscription,
)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from .maxio import MaxioError, maxio_call
from .models import MaxioCustomer, MaxioSubscription

logger = logging.getLogger(__name__)

T = TypeVar("T")

PLANS_CACHE_SECONDS = 60
PLANS_PAGE_SIZE = 200  # Maxio's maximum per_page
# A pending claim older than this belongs to a request that died mid-call. It is
# comfortably longer than one un-retried POST can take.
STALE_CLAIM_AFTER = timedelta(minutes=2)

# What a subscription's state says about the enrolment asked for.
DONE_STATES = frozenset({SubscriptionState.ACTIVE, SubscriptionState.TRIALING})
NOT_DONE_YET_STATES = frozenset({
    SubscriptionState.PENDING, SubscriptionState.ASSESSING, SubscriptionState.AWAITING_SIGNUP,
})
FAILED_STATES = frozenset({
    SubscriptionState.FAILED_TO_CREATE, SubscriptionState.SOFT_FAILURE, SubscriptionState.PAST_DUE,
    SubscriptionState.UNPAID, SubscriptionState.ON_HOLD, SubscriptionState.PAUSED, SubscriptionState.SUSPENDED,
    SubscriptionState.CANCELED, SubscriptionState.EXPIRED, SubscriptionState.TRIAL_ENDED,
})
# States after which the subscription no longer bills; the plan may be taken again.
TERMINAL_STATES = frozenset({SubscriptionState.CANCELED, SubscriptionState.EXPIRED, SubscriptionState.FAILED_TO_CREATE})


class PlanNotFound(Exception):
    pass


class CustomerSetupInProgress(Exception):
    pass


class InvalidCustomerDetails(Exception):
    pass


@dataclass(frozen=True)
class SubscribeResult:
    record: MaxioSubscription
    subscription: dict[str, Any]
    # False when an existing open subscription for the plan was returned instead.
    created: bool


def _value(value: T | UnsetType | None) -> T | None:
    return None if isinstance(value, UnsetType) else value


def _state_value(state: object) -> str | None:
    if state is None or isinstance(state, UnsetType):
        return None
    return state.value if isinstance(state, SubscriptionState) else str(state)


def enrollment(state: str | None) -> str:
    """'active', 'pending' or 'failed' for a Maxio subscription state; 'unknown' if unrecognised."""
    if state in {s.value for s in DONE_STATES}:
        return "active"
    if state in {s.value for s in NOT_DONE_YET_STATES}:
        return "pending"
    if state in {s.value for s in FAILED_STATES}:
        return "failed"
    return "unknown"


def _money(cents: int | None) -> str | None:
    # Maxio reports amounts in cents: two decimal places.
    return None if cents is None else str((Decimal(cents) / 100).quantize(Decimal("0.01")))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _plan_family() -> str:
    family = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    if not family:
        raise MaxioError(503, "Subscriptions are not configured on this site.")
    return str(family)


# Plans
# =====

def plan_to_dict(product: Product) -> dict[str, Any]:
    price_in_cents = _value(product.price_in_cents)
    interval_unit = _value(product.interval_unit)
    trial_interval_unit = _value(product.trial_interval_unit)
    return {
        "planHandle": _value(product.handle),
        "productId": _value(product.id),
        "name": _value(product.name),
        "description": _value(product.description) or "",
        "priceInCents": price_in_cents,
        "price": _money(price_in_cents),
        "interval": _value(product.interval),
        "intervalUnit": str(interval_unit) if interval_unit is not None else None,
        "trialPriceInCents": _value(product.trial_price_in_cents),
        "trialInterval": _value(product.trial_interval),
        "trialIntervalUnit": str(trial_interval_unit) if trial_interval_unit is not None else None,
        "setupFeeInCents": _value(product.initial_charge_in_cents),
        "requiresPaymentMethod": bool(_value(product.require_credit_card)),
    }


def list_plans(client: MaxioAdvancedBillingClient) -> list[dict[str, Any]]:
    """Active (non-archived) plans in the default product family, cached briefly."""
    family = _plan_family()
    cache_key = f"subscriptions:plans:{family}"
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)
    plans: list[dict[str, Any]] = []
    page = 1
    while True:
        with maxio_call("list plans"):
            products = client.product_families.list_products_for_product_family(
                f"handle:{family}", page=page, per_page=PLANS_PAGE_SIZE,
            )
        for response in products:
            product = response.product
            archived_at = _value(product.archived_at)
            handle = _value(product.handle)
            if archived_at is None and handle:
                plans.append(plan_to_dict(product))
        if len(products) < PLANS_PAGE_SIZE:
            break
        page += 1
    cache.set(cache_key, plans, PLANS_CACHE_SECONDS)
    return plans


def get_plan(client: MaxioAdvancedBillingClient, plan_handle: str) -> Product:
    """The plan with this handle, if it is a live product of the default family."""
    family = _plan_family()
    try:
        with maxio_call("read plan"):
            product = client.products.read_product_by_handle(plan_handle).product
    except MaxioError as exc:
        if exc.provider_status == 404:
            raise PlanNotFound(plan_handle) from exc
        raise
    product_family = _value(product.product_family)
    family_handle = _value(product_family.handle) if product_family is not None else None
    if family_handle != family or _value(product.archived_at) is not None:
        raise PlanNotFound(plan_handle)
    return product


# Customers
# =========

def _customer_reference(user: AbstractBaseUser) -> str:
    return f"{settings.MAXIO_CUSTOMER_REFERENCE_PREFIX}{user.pk}"


def _customer_id(customer: Customer) -> int:
    customer_id = _value(customer.id)
    if not isinstance(customer_id, int):
        raise MaxioError(502, "The billing provider returned a customer without an id.", outcome_unknown=True)
    return customer_id


def _find_customer(client: MaxioAdvancedBillingClient, reference: str) -> int | None:
    try:
        with maxio_call("find customer"):
            customer = client.customers.read_customer_by_reference(reference).customer
    except MaxioError as exc:
        if exc.provider_status == 404:
            return None
        raise
    return _customer_id(customer)


def _take_lease(model: type[MaxioCustomer] | type[MaxioSubscription], pk: int, pending_status: str) -> bool:
    """Atomically take over a stale pending claim; exactly one caller wins."""
    now = timezone.now()
    return model.objects.filter(pk=pk, status=pending_status, lease_at__lt=now - STALE_CLAIM_AFTER).update(
        lease_at=now) == 1


def _record_customer(record: MaxioCustomer, customer_id: int) -> int:
    record.maxio_customer_id = customer_id
    record.status = MaxioCustomer.CREATED
    record.save(update_fields=["maxio_customer_id", "status", "updated_at"])
    return customer_id


def ensure_customer(client: MaxioAdvancedBillingClient, user: Any) -> int:
    """The Maxio customer id for this user, creating the customer exactly once."""
    email = (user.email or "").strip()
    if not email:
        raise InvalidCustomerDetails("An email address is required to subscribe.")

    reference = _customer_reference(user)
    try:
        with transaction.atomic():
            record = MaxioCustomer.objects.create(user=user, reference=reference, lease_at=timezone.now())
    except IntegrityError:
        record = MaxioCustomer.objects.get(user=user)
        if record.maxio_customer_id is not None:
            return record.maxio_customer_id
        # Someone else holds the claim. Only take it over once it has gone stale.
        if not _take_lease(MaxioCustomer, record.pk, MaxioCustomer.PENDING):
            raise CustomerSetupInProgress()
        existing = _find_customer(client, record.reference)
        if existing is not None:
            return _record_customer(record, existing)

    local_part = email.split("@", 1)[0]
    body = CreateCustomerRequest(customer=CreateCustomer(
        first_name=(user.first_name or "").strip() or local_part,
        last_name=(user.last_name or "").strip() or local_part,
        email=email,
        reference=record.reference,
    ))
    try:
        with maxio_call("create customer"):
            customer = client.customers.create_customer(body=body).customer
        return _record_customer(record, _customer_id(customer))
    except MaxioError as exc:
        # Unknown outcome, or a 422 that may mean the reference is already taken
        # (an earlier attempt that did land): settle by looking the reference up.
        if not (exc.outcome_unknown or exc.provider_status == 422):
            raise
        try:
            existing = _find_customer(client, record.reference)
        except MaxioError:
            existing = None
        if existing is not None:
            logger.info("Settled customer %s by reference after: %s", record.reference, exc.message)
            return _record_customer(record, existing)
        # Still unsettled: the pending claim stays, and the next attempt settles it.
        raise


# Subscriptions
# =============

def subscription_to_dict(subscription: Subscription, *, plan_handle: str | None = None) -> dict[str, Any]:
    product = _value(subscription.product)
    state = _state_value(subscription.state)
    price_in_cents = _value(subscription.product_price_in_cents)
    if price_in_cents is None and product is not None:
        price_in_cents = _value(product.price_in_cents)
    interval_unit = _value(product.interval_unit) if product is not None else None
    return {
        "subscriptionId": _value(subscription.id),
        "reference": _value(subscription.reference),
        "planHandle": (_value(product.handle) if product is not None else None) or plan_handle,
        "planName": _value(product.name) if product is not None else None,
        "state": state,
        "enrollment": enrollment(state),
        "priceInCents": price_in_cents,
        "price": _money(price_in_cents),
        "currency": _value(subscription.currency),
        "interval": _value(product.interval) if product is not None else None,
        "intervalUnit": str(interval_unit) if interval_unit is not None else None,
        "nextBillingAt": _iso(_value(subscription.next_assessment_at)),
        "currentPeriodEndsAt": _iso(_value(subscription.current_period_ends_at)),
        "activatedAt": _iso(_value(subscription.activated_at)),
        "createdAt": _iso(_value(subscription.created_at)),
        "canceledAt": _iso(_value(subscription.canceled_at)),
    }


def _local_enrollment(record: MaxioSubscription) -> str:
    if record.status == MaxioSubscription.UNKNOWN:
        return "unknown"
    if record.status == MaxioSubscription.PENDING:
        return "pending"
    return enrollment(record.state or None)


def local_subscription_to_dict(record: MaxioSubscription) -> dict[str, Any]:
    """What this site's record says about a subscription (used when Maxio was not re-read)."""
    return {
        "subscriptionId": record.maxio_subscription_id,
        "reference": record.reference,
        "planHandle": record.plan_handle,
        "planName": record.plan_name or None,
        "state": record.state or None,
        "enrollment": _local_enrollment(record),
        "priceInCents": record.price_in_cents,
        "price": _money(record.price_in_cents),
        "currency": record.currency or None,
        "interval": None,
        "intervalUnit": None,
        "nextBillingAt": _iso(record.next_billing_at),
        "currentPeriodEndsAt": None,
        "activatedAt": None,
        "createdAt": _iso(record.created_at),
        "canceledAt": None,
    }


def _record_subscription(record: MaxioSubscription, subscription: Subscription) -> dict[str, Any]:
    """Store Maxio's answer on the claim and return the caller-facing view of it."""
    data = subscription_to_dict(subscription, plan_handle=record.plan_handle)
    subscription_id = data["subscriptionId"]
    if not isinstance(subscription_id, int):
        raise MaxioError(502, "The billing provider returned a subscription without an id.", outcome_unknown=True)
    if data["planHandle"] != record.plan_handle:
        # Maxio enrolled the customer in something other than what was asked.
        logger.error("Subscription %s is on plan %s, expected %s", subscription_id, data["planHandle"],
                     record.plan_handle)
    state = data["state"] or ""
    record.maxio_subscription_id = subscription_id
    record.state = state
    record.status = (MaxioSubscription.ENDED if state in {s.value for s in TERMINAL_STATES}
                     else MaxioSubscription.LIVE)
    record.plan_name = data["planName"] or record.plan_name
    record.price_in_cents = data["priceInCents"]
    record.currency = data["currency"] or ""
    next_billing = _value(subscription.next_assessment_at)
    record.next_billing_at = next_billing
    record.last_error = ""
    record.save()
    return data


def _mark(record: MaxioSubscription, status: str, error: str = "") -> None:
    record.status = status
    record.last_error = error
    record.save(update_fields=["status", "last_error", "updated_at"])


def settle_subscription(client: MaxioAdvancedBillingClient, record: MaxioSubscription) -> MaxioSubscription:
    """Resolve a claim whose create call has no known outcome by looking up its reference."""
    try:
        with maxio_call("find subscription"):
            response = client.subscriptions.find_subscription(reference=record.reference)
    except MaxioError as exc:
        if exc.provider_status == 404:
            # The create never landed; release the claim.
            _mark(record, MaxioSubscription.REJECTED, "Not found at the billing provider after an unknown outcome.")
        elif record.status == MaxioSubscription.PENDING:
            _mark(record, MaxioSubscription.UNKNOWN, exc.message)
        return record
    subscription = _value(response.subscription)
    if subscription is not None:
        try:
            _record_subscription(record, subscription)
        except MaxioError:
            pass  # Unreadable: leave it unsettled for the next attempt.
    return record


def _needs_settling(record: MaxioSubscription) -> bool:
    if record.status == MaxioSubscription.UNKNOWN:
        return True
    return record.status == MaxioSubscription.PENDING and record.lease_at < timezone.now() - STALE_CLAIM_AFTER


def _claim_subscription(user: Any, plan_handle: str) -> MaxioSubscription | None:
    try:
        with transaction.atomic():
            return MaxioSubscription.objects.create(
                user=user, plan_handle=plan_handle, reference=f"oscar-sub-{uuid.uuid4().hex}",
                status=MaxioSubscription.PENDING, lease_at=timezone.now(),
            )
    except IntegrityError:
        return None


def subscribe(client: MaxioAdvancedBillingClient, user: Any, plan_handle: str) -> SubscribeResult:
    """Enroll the user in a plan, at most once per plan while a subscription to it is open."""
    product = get_plan(client, plan_handle)
    customer_id = ensure_customer(client, user)

    record = _claim_subscription(user, plan_handle)
    if record is None:
        existing = MaxioSubscription.objects.get(
            user=user, plan_handle=plan_handle, status__in=MaxioSubscription.OPEN_STATUSES)
        if _needs_settling(existing) and (existing.status == MaxioSubscription.UNKNOWN or _take_lease(
                MaxioSubscription, existing.pk, MaxioSubscription.PENDING)):
            existing = settle_subscription(client, existing)
        if existing.status in MaxioSubscription.OPEN_STATUSES:
            return SubscribeResult(existing, local_subscription_to_dict(existing), created=False)
        # The earlier attempt turned out not to exist or to have ended: claim afresh.
        record = _claim_subscription(user, plan_handle)
        if record is None:
            existing = MaxioSubscription.objects.get(
                user=user, plan_handle=plan_handle, status__in=MaxioSubscription.OPEN_STATUSES)
            return SubscribeResult(existing, local_subscription_to_dict(existing), created=False)

    record.plan_name = _value(product.name) or ""
    record.price_in_cents = _value(product.price_in_cents)
    record.save(update_fields=["plan_name", "price_in_cents", "updated_at"])

    body = CreateSubscriptionRequest(subscription=CreateSubscription(
        product_handle=plan_handle, customer_id=customer_id, reference=record.reference,
        payment_collection_method=CollectionMethod(settings.MAXIO_PAYMENT_COLLECTION_METHOD),
    ))
    try:
        with maxio_call("create subscription"):
            response = client.subscriptions.create_subscription(body=body)
        subscription = _value(response.subscription)
        if subscription is None:
            raise MaxioError(502, "The billing provider returned no subscription.", outcome_unknown=True)
        return SubscribeResult(record, _record_subscription(record, subscription), created=True)
    except MaxioError as exc:
        if not exc.outcome_unknown:
            # Maxio refused it (or it was never sent): nothing exists; release the claim.
            _mark(record, MaxioSubscription.REJECTED, "; ".join(exc.details) or exc.message)
            raise
        _mark(record, MaxioSubscription.UNKNOWN, exc.message)
        record = settle_subscription(client, record)
        if record.status == MaxioSubscription.LIVE or record.status == MaxioSubscription.ENDED:
            return SubscribeResult(record, local_subscription_to_dict(record), created=True)
        if record.status == MaxioSubscription.REJECTED:
            raise
        # Still unknown: reported as such; later reads settle it.
        return SubscribeResult(record, local_subscription_to_dict(record), created=True)


def my_subscriptions(client: MaxioAdvancedBillingClient, user: Any) -> list[dict[str, Any]]:
    """The user's subscriptions as Maxio reports them, plus any this site has not settled yet."""
    for record in MaxioSubscription.objects.filter(
            user=user, status__in=(MaxioSubscription.PENDING, MaxioSubscription.UNKNOWN)):
        if _needs_settling(record) and (record.status == MaxioSubscription.UNKNOWN or _take_lease(
                MaxioSubscription, record.pk, MaxioSubscription.PENDING)):
            settle_subscription(client, record)

    results: list[dict[str, Any]] = []
    customer = MaxioCustomer.objects.filter(user=user, maxio_customer_id__isnull=False).first()
    if customer is not None and customer.maxio_customer_id is not None:
        with maxio_call("list customer subscriptions"):
            responses = client.customers.list_customer_subscriptions(customer.maxio_customer_id)
        records = {r.maxio_subscription_id: r for r in MaxioSubscription.objects.filter(
            user=user, maxio_subscription_id__isnull=False)}
        for response in responses:
            subscription = _value(response.subscription)
            if subscription is None:
                continue
            known = records.get(_value(subscription.id))
            if known is not None:
                results.append(_record_subscription(known, subscription))
            else:
                results.append(subscription_to_dict(subscription))

    for record in MaxioSubscription.objects.filter(
            user=user, status__in=(MaxioSubscription.PENDING, MaxioSubscription.UNKNOWN)):
        results.append(local_subscription_to_dict(record))
    return results
