"""
Subscription billing flows, with Maxio Advanced Billing as the system of record.

Every Maxio write follows one order: claim a row in our own database (committed,
guarded by a uniqueness constraint), then call Maxio, then record the result.
A second concurrent request is refused by the database before it reaches Maxio.
When a write's outcome is unknown (no reply, unreadable reply) the claim stays
pending and is reconciled by re-reading Maxio before anything is created again.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
    Customer,
    Product,
)
from maxio_advanced_billing.models import Subscription as MaxioSubscription
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from . import maxio
from .errors import BillingError, maxio_call
from .models import BillingCustomer, Subscription

logger = logging.getLogger("apps.subscriptions")

# A pending claim older than this is presumed abandoned (its request died) and may be taken over.
STALE_CLAIM_AFTER = timedelta(seconds=60)
PLANS_CACHE_SECONDS = 60
_PLANS_PAGE_SIZE = 200

# States after which a subscription no longer blocks a new one.
TERMINAL_STATES = frozenset(
    str(state) for state in (SubscriptionState.CANCELED, SubscriptionState.EXPIRED, SubscriptionState.FAILED_TO_CREATE)
)

T = TypeVar("T")


def _v(value: T | None | UnsetType) -> T | None:
    """Resolve the SDK's UNSET sentinel to None before a value leaves the SDK layer."""
    return None if isinstance(value, UnsetType) else value


def format_cents(cents: int | None) -> str | None:
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    handle: str
    name: str
    description: str
    price_in_cents: int | None
    interval: int | None
    interval_unit: str | None
    requires_payment_method: bool
    setup_fee_in_cents: int | None
    trial_price_in_cents: int | None
    trial_interval: int | None
    trial_interval_unit: str | None

    @classmethod
    def from_product(cls, product: Product, handle: str) -> "Plan":
        interval_unit = _v(product.interval_unit)
        trial_unit = _v(product.trial_interval_unit)
        return cls(
            handle=handle,
            name=_v(product.name) or handle,
            description=_v(product.description) or "",
            price_in_cents=_v(product.price_in_cents),
            interval=_v(product.interval),
            interval_unit=str(interval_unit) if interval_unit is not None else None,
            requires_payment_method=bool(_v(product.require_credit_card)),
            setup_fee_in_cents=_v(product.initial_charge_in_cents),
            trial_price_in_cents=_v(product.trial_price_in_cents),
            trial_interval=_v(product.trial_interval),
            trial_interval_unit=str(trial_unit) if trial_unit is not None else None,
        )

    def to_json(self) -> dict[str, Any]:
        trial = None
        if self.trial_interval:
            trial = {
                "interval": self.trial_interval,
                "intervalUnit": self.trial_interval_unit,
                "priceInCents": self.trial_price_in_cents,
            }
        return {
            "planHandle": self.handle,
            "name": self.name,
            "description": self.description,
            "price": {"amountInCents": self.price_in_cents, "amount": format_cents(self.price_in_cents)},
            "interval": self.interval,
            "intervalUnit": self.interval_unit,
            "setupFeeInCents": self.setup_fee_in_cents,
            "trial": trial,
            "requiresPaymentMethod": self.requires_payment_method,
        }


def _plans_cache_key(family: str) -> str:
    return f"subscriptions:plans:{family}"


def list_plans(*, refresh: bool = False) -> list[Plan]:
    """Active plans in the configured product family (cached briefly)."""
    family = maxio.product_family()
    key = _plans_cache_key(family)
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return [Plan(**item) for item in cached]

    client = maxio.get_client()
    plans: list[Plan] = []
    page = 1
    while True:
        with maxio_call("list_products_for_product_family"):
            try:
                batch = client.product_families.list_products_for_product_family(
                    f"handle:{family}", page=page, per_page=_PLANS_PAGE_SIZE
                )
            except ApiError as exc:
                if exc.status_code == 404:
                    logger.error("Maxio product family %r does not exist", family)
                    raise BillingError(503, "billing_misconfigured",
                                       "The configured plan catalogue is unavailable.") from exc
                raise
        for item in batch:
            product = item.product
            handle = _v(product.handle)
            if not handle or _v(product.archived_at) is not None:
                continue
            plans.append(Plan.from_product(product, handle))
        if len(batch) < _PLANS_PAGE_SIZE:
            break
        page += 1

    cache.set(key, [asdict(plan) for plan in plans], PLANS_CACHE_SECONDS)
    return plans


def get_plan(handle: str) -> Plan:
    for refresh in (False, True):
        for plan in list_plans(refresh=refresh):
            if plan.handle == handle:
                return plan
    raise BillingError(400, "unknown_plan", "No such plan is available.", details=[handle])


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


def _names(user: Any) -> tuple[str, str]:
    email: str = user.email
    first = (user.first_name or "").strip() or email.split("@", 1)[0] or user.get_username()
    last = (user.last_name or "").strip() or "Customer"
    return first, last


def _claim_customer(user: Any) -> BillingCustomer:
    """Insert (or take over) this user's customer claim. Refuses a concurrent second claimer."""
    for _ in range(3):
        now = timezone.now()
        try:
            with transaction.atomic():
                return BillingCustomer.objects.create(user=user, claimed_at=now)
        except IntegrityError:
            pass
        existing = BillingCustomer.objects.filter(user=user).first()
        if existing is None:  # released between our insert and our read
            continue
        if existing.status == BillingCustomer.READY:
            return existing
        if not existing.outcome_unknown and now - existing.claimed_at < STALE_CLAIM_AFTER:
            raise BillingError(409, "customer_setup_in_progress",
                               "Billing account setup is already in progress; retry shortly.")
        # Take over an abandoned or unresolved claim; the conditional update admits one taker.
        taken = BillingCustomer.objects.filter(
            pk=existing.pk, status=BillingCustomer.PENDING, claimed_at=existing.claimed_at
        ).update(claimed_at=now)
        if not taken:
            raise BillingError(409, "customer_setup_in_progress",
                               "Billing account setup is already in progress; retry shortly.")
        existing.refresh_from_db()
        return existing
    raise BillingError(409, "customer_setup_in_progress",
                       "Billing account setup is already in progress; retry shortly.")


def _lookup_customer(client: MaxioAdvancedBillingClient, reference: str) -> Customer | None:
    with maxio_call("read_customer_by_reference"):
        try:
            return client.customers.read_customer_by_reference(reference).customer
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise


def _create_or_adopt_customer(user: Any, claim: BillingCustomer) -> BillingCustomer:
    """Find the Maxio customer carrying our reference, or create it; then record its id."""
    client = maxio.get_client()
    try:
        found = _lookup_customer(client, claim.reference)
    except BillingError as err:
        # Nothing was written upstream; leave the claim for a retry to take over.
        _mark_customer_unknown(claim)
        raise err

    if found is None:
        first, last = _names(user)
        body = CreateCustomerRequest(
            customer=CreateCustomer(first_name=first, last_name=last, email=user.email, reference=claim.reference)
        )
        try:
            with maxio_call("create_customer"):
                found = client.customers.create_customer(body=body).customer
        except BillingError as err:
            # Lost reply, unreadable reply or a rejection: the reference tells us what exists.
            try:
                found = _lookup_customer(client, claim.reference)
            except BillingError:
                if err.outcome_unknown:
                    _mark_customer_unknown(claim)
                else:
                    _release_customer(claim)
                raise err from None
            if found is None:
                _release_customer(claim)
                err.outcome_unknown = False  # verified: no customer was created
                raise err

    customer_id = _v(found.id)
    if customer_id is None:
        _mark_customer_unknown(claim)
        raise BillingError(502, "billing_unreadable", "The billing provider returned no customer id.",
                           outcome_unknown=True)
    claim.maxio_customer_id = customer_id
    claim.status = BillingCustomer.READY
    claim.outcome_unknown = False
    claim.save(update_fields=["maxio_customer_id", "status", "outcome_unknown", "date_updated"])
    logger.info("Maxio customer %s linked to user %s", customer_id, user.pk)
    return claim


def _mark_customer_unknown(claim: BillingCustomer) -> None:
    BillingCustomer.objects.filter(pk=claim.pk, status=BillingCustomer.PENDING).update(outcome_unknown=True)


def _release_customer(claim: BillingCustomer) -> None:
    BillingCustomer.objects.filter(pk=claim.pk, status=BillingCustomer.PENDING).delete()


def ensure_customer(user: Any) -> BillingCustomer:
    """Idempotently ensure a Maxio customer exists for ``user``; returns the linked record."""
    if not user.email:
        raise BillingError(400, "email_required", "Your account needs an email address before subscribing.")
    claim = _claim_customer(user)
    if claim.status == BillingCustomer.READY:
        return claim
    return _create_or_adopt_customer(user, claim)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def _apply_snapshot(row: Subscription, sub: MaxioSubscription) -> None:
    """Copy Maxio's view of a subscription onto a local row (not saved)."""
    product = _v(sub.product)
    state = _v(sub.state)
    row.maxio_subscription_id = _v(sub.id)
    row.state = str(state) if state is not None else ""
    if product is not None:
        row.plan_handle = _v(product.handle) or row.plan_handle
        row.plan_name = _v(product.name) or row.plan_name
        row.interval = _v(product.interval)
        unit = _v(product.interval_unit)
        row.interval_unit = str(unit) if unit is not None else ""
        family = _v(product.product_family)
        if family is not None:
            row.product_family = _v(family.handle) or row.product_family
    price = _v(sub.product_price_in_cents)
    if price is None and product is not None:
        price = _v(product.price_in_cents)
    row.price_in_cents = price
    row.currency = _v(sub.currency) or ""
    next_billing: datetime | None = _v(sub.current_period_ends_at)
    row.next_billing_at = next_billing
    row.activated_at = _v(sub.activated_at)
    row.last_synced_at = timezone.now()


def _record(row: Subscription, sub: MaxioSubscription) -> Subscription:
    _apply_snapshot(row, sub)
    row.claim_state = Subscription.CONFIRMED
    row.outcome_unknown = False
    row.is_live = row.state not in TERMINAL_STATES
    row.save()
    return row


def _claim_subscription(user: Any, family: str, plan: Plan) -> tuple[Subscription, bool]:
    """Insert (or take over) the live claim for ``user`` + ``family``.

    Returns the live row and whether it must be reconciled with Maxio before use
    (an abandoned or unresolved pending claim that we have just taken over).
    """
    now = timezone.now()
    try:
        with transaction.atomic():
            row = Subscription.objects.create(
                user=user, product_family=family, plan_handle=plan.handle,
                plan_name=plan.name, price_in_cents=plan.price_in_cents,
                interval=plan.interval, interval_unit=plan.interval_unit or "",
                claimed_at=now,
            )
            return row, False
    except IntegrityError:
        pass
    existing = Subscription.objects.filter(user=user, product_family=family, is_live=True).first()
    if existing is None:  # released between our insert and our read
        raise _Retry()
    if existing.claim_state == Subscription.CONFIRMED:
        return existing, False
    if not existing.outcome_unknown and now - existing.claimed_at < STALE_CLAIM_AFTER:
        raise BillingError(409, "subscription_in_progress",
                           "A subscription request is already in progress; retry shortly.")
    taken = Subscription.objects.filter(
        pk=existing.pk, claim_state=Subscription.PENDING, claimed_at=existing.claimed_at
    ).update(claimed_at=now)
    if not taken:
        raise BillingError(409, "subscription_in_progress",
                           "A subscription request is already in progress; retry shortly.")
    existing.refresh_from_db()
    return existing, True


class _Retry(Exception):
    pass


def _find_by_reference(
    client: MaxioAdvancedBillingClient, customer: BillingCustomer, reference: str
) -> MaxioSubscription | None:
    assert customer.maxio_customer_id is not None
    with maxio_call("list_customer_subscriptions"):
        items = client.customers.list_customer_subscriptions(customer.maxio_customer_id)
    for item in items:
        sub = _v(item.subscription)
        if sub is not None and _v(sub.reference) == reference:
            return sub
    return None


def _release_subscription(row: Subscription) -> None:
    Subscription.objects.filter(pk=row.pk, claim_state=Subscription.PENDING).update(
        claim_state=Subscription.RELEASED, is_live=False
    )


def _mark_subscription_unknown(row: Subscription) -> None:
    Subscription.objects.filter(pk=row.pk, claim_state=Subscription.PENDING).update(outcome_unknown=True)


def _refresh_live(client: MaxioAdvancedBillingClient, row: Subscription) -> Subscription:
    """Re-read a confirmed subscription so a canceled/expired one stops blocking a new one."""
    assert row.maxio_subscription_id is not None
    with maxio_call("read_subscription"):
        sub = _v(client.subscriptions.read_subscription(row.maxio_subscription_id).subscription)
    if sub is not None and _v(sub.id) is not None:
        _record(row, sub)
    return row


def _create_subscription_in_maxio(
    client: MaxioAdvancedBillingClient, customer: BillingCustomer, row: Subscription, plan: Plan
) -> Subscription:
    assert customer.maxio_customer_id is not None
    body = CreateSubscriptionRequest(
        subscription=CreateSubscription(
            product_handle=plan.handle,
            customer_id=customer.maxio_customer_id,
            reference=row.reference,
            # No card is captured in this flow: bill by invoice (remittance) rather than
            # attempting an automatic charge against a payment method that does not exist.
            payment_collection_method=CollectionMethod.REMITTANCE,
        )
    )
    try:
        with maxio_call("create_subscription"):
            response = client.subscriptions.create_subscription(body=body)
    except BillingError as err:
        if err.outcome_unknown:
            _mark_subscription_unknown(row)
        else:
            _release_subscription(row)
        raise
    sub = _v(response.subscription)
    if sub is None or _v(sub.id) is None:
        _mark_subscription_unknown(row)
        raise BillingError(502, "billing_unreadable", "The billing provider returned no subscription.",
                           outcome_unknown=True)
    _record(row, sub)
    logger.info("Maxio subscription %s created for user %s (%s)", row.maxio_subscription_id, row.user_id,
                plan.handle)
    return row


def subscribe(user: Any, plan_handle: str) -> tuple[Subscription, bool]:
    """Subscribe ``user`` to a plan. Returns the subscription and whether it was created by this call.

    Repeating the request for the plan the user already has returns that subscription.
    """
    plan = get_plan(plan_handle)
    if plan.requires_payment_method:
        raise BillingError(422, "payment_method_required",
                           "This plan needs a payment method, which this API does not collect.",
                           details=[plan.handle])
    family = maxio.product_family()
    customer = ensure_customer(user)
    client = maxio.get_client()

    for _ in range(3):
        try:
            row, reconcile = _claim_subscription(user, family, plan)
        except _Retry:
            continue

        if reconcile:
            # A previous attempt's outcome is unknown: find out before creating anything.
            found = _find_by_reference(client, customer, row.reference)
            if found is None:
                _release_subscription(row)
                continue
            _record(row, found)

        if row.claim_state == Subscription.CONFIRMED:
            if row.state in TERMINAL_STATES or (row.plan_handle != plan.handle):
                row = _refresh_live(client, row)
                if not row.is_live:
                    continue
            if row.plan_handle != plan.handle:
                raise BillingError(409, "already_subscribed",
                                   "You already have a subscription in this catalogue.",
                                   details=[row.plan_handle])
            return row, False

        return _create_subscription_in_maxio(client, customer, row, plan), True

    raise BillingError(409, "subscription_in_progress",
                       "A subscription request is already in progress; retry shortly.")


def my_subscriptions(user: Any) -> tuple[list[Subscription], str]:
    """The user's subscriptions, read through from Maxio.

    Returns the rows and their source: ``"maxio"`` when read live, ``"cache"`` when
    Maxio was unreachable and the last recorded snapshot is returned instead.
    """
    local = list(Subscription.objects.filter(user=user, claim_state=Subscription.CONFIRMED))
    customer = BillingCustomer.objects.filter(user=user, status=BillingCustomer.READY).first()
    if customer is None or customer.maxio_customer_id is None:
        return local, "maxio"
    client = maxio.get_client()
    try:
        with maxio_call("list_customer_subscriptions"):
            items = client.customers.list_customer_subscriptions(customer.maxio_customer_id)
    except BillingError as err:
        if err.status_code >= 500:
            return local, "cache"
        raise

    by_id = {row.maxio_subscription_id: row for row in local}
    result: list[Subscription] = []
    for item in items:
        sub = _v(item.subscription)
        sub_id = _v(sub.id) if sub is not None else None
        if sub is None or sub_id is None:
            continue
        row = by_id.get(sub_id)
        if row is not None:
            result.append(_record(row, sub))
        else:
            # Exists in Maxio (the system of record) but was not created through this site.
            detached = Subscription(user=user, claim_state=Subscription.CONFIRMED, claimed_at=timezone.now())
            _apply_snapshot(detached, sub)
            result.append(detached)
    result.sort(key=lambda row: row.maxio_subscription_id or 0, reverse=True)
    return result, "maxio"


def get_subscription(user: Any, subscription_id: int) -> Subscription:
    """One of the user's subscriptions, read live from Maxio. Other users' ids are a 404."""
    not_found = BillingError(404, "not_found", "No such subscription.")
    customer = BillingCustomer.objects.filter(user=user, status=BillingCustomer.READY).first()
    if customer is None:
        raise not_found
    client = maxio.get_client()
    try:
        with maxio_call("read_subscription"):
            sub = _v(client.subscriptions.read_subscription(subscription_id).subscription)
    except BillingError as err:
        if err.status_code == 404:
            raise not_found from err
        raise
    owner = _v(sub.customer) if sub is not None else None
    if sub is None or owner is None or _v(owner.id) != customer.maxio_customer_id:
        raise not_found
    row = Subscription.objects.filter(user=user, maxio_subscription_id=subscription_id).first()
    if row is not None:
        return _record(row, sub)
    detached = Subscription(user=user, claim_state=Subscription.CONFIRMED, claimed_at=timezone.now())
    _apply_snapshot(detached, sub)
    return detached


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def subscription_to_json(row: Subscription) -> dict[str, Any]:
    return {
        "subscriptionId": row.maxio_subscription_id,
        "planHandle": row.plan_handle,
        "planName": row.plan_name,
        "state": row.state,
        "price": {
            "amountInCents": row.price_in_cents,
            "amount": format_cents(row.price_in_cents),
            "currency": row.currency or None,
        },
        "interval": row.interval,
        "intervalUnit": row.interval_unit or None,
        "nextBillingAt": _iso(row.next_billing_at),
        "activatedAt": _iso(row.activated_at),
        "lastSyncedAt": _iso(row.last_synced_at),
    }
