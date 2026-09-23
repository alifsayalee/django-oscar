"""
Subscription use cases: plans, the caller's Maxio customer, subscribing, and
reading subscriptions back.

Every provider write follows the same shape: claim a durable row (unique
constraint decides the winner), call Maxio once with the reference stored on
that row, reconcile by that reference when the outcome is unclear, verify what
Maxio echoed, and only then settle the row from what Maxio said.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.models import Customer, Subscription
from maxio_advanced_billing.models.enums import SubscriptionState

from . import gateway
from .gateway import Plan, ProviderError, ProviderRejected, present
from .models import BillingCustomer, SubscriptionEnrollment

logger = logging.getLogger("apps.subscriptions")

# A claim still "sending" after this long belongs to a request that died:
# longer than one write timeout plus the lookup that follows it.
SEND_WINDOW = timedelta(seconds=60)
PLANS_CACHE_SECONDS = 60


class SubscriptionServiceError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str, **extra: object) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra


class InvalidRequest(SubscriptionServiceError):
    code = "invalid_request"

    def __init__(self, status_code: int, message: str, **extra: object) -> None:
        super().__init__(message, **extra)
        self.status_code = status_code


class InProgress(SubscriptionServiceError):
    status_code = 409
    code = "in_progress"


class OutcomeUnknown(SubscriptionServiceError):
    """The write may have landed; it is recorded and will be reconciled by reference."""

    status_code = 504
    code = "outcome_unknown"


# --------------------------------------------------------------------------- #
# Status mapping -- the one place a Maxio state becomes ours
# --------------------------------------------------------------------------- #


def status_from_provider(state: object) -> str:
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return SubscriptionEnrollment.DONE
        case SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP:
            return SubscriptionEnrollment.PENDING
        case (
            SubscriptionState.PAST_DUE
            | SubscriptionState.SOFT_FAILURE
            | SubscriptionState.UNPAID
            | SubscriptionState.PAUSED
            | SubscriptionState.ON_HOLD
            | SubscriptionState.SUSPENDED
        ):
            return SubscriptionEnrollment.NEEDS_ATTENTION
        case SubscriptionState.CANCELED | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED:
            return SubscriptionEnrollment.ENDED
        case SubscriptionState.FAILED_TO_CREATE:
            return SubscriptionEnrollment.FAILED
        case _:
            return SubscriptionEnrollment.UNKNOWN


def _new_reference(kind: str, user) -> str:
    return f"oscar-{kind}-{user.pk}-{uuid.uuid4().hex[:16]}"


def _is_fresh(updated_at) -> bool:
    return updated_at > timezone.now() - SEND_WINDOW


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


def list_plans() -> list[Plan]:
    key = f"subscriptions:plans:{gateway.default_product_family()}"
    plans = cache.get(key)
    if plans is None:
        plans = gateway.list_plans().plans
        cache.set(key, plans, PLANS_CACHE_SECONDS)
    return plans


def get_plan(plan_handle: str) -> Plan:
    for plan in list_plans():
        if plan.handle == plan_handle:
            return plan
    raise InvalidRequest(404, "Unknown plan.", planHandle=plan_handle)


# --------------------------------------------------------------------------- #
# Billing customer
# --------------------------------------------------------------------------- #


def _customer_names(user) -> tuple[str, str]:
    fallback = user.email.split("@", 1)[0] or user.get_username()
    return (user.first_name or fallback), (user.last_name or fallback)


def _settle_customer(row: BillingCustomer, customer: Customer) -> BillingCustomer:
    customer_id = present(customer.id)
    reference = present(customer.reference)
    if not isinstance(customer_id, int) or reference != row.reference:
        row.status = BillingCustomer.UNKNOWN
        row.save(update_fields=["status", "date_updated"])
        raise OutcomeUnknown("Billing account could not be confirmed.", reference=row.reference)
    row.maxio_customer_id = customer_id
    row.status = BillingCustomer.DONE
    row.save(update_fields=["maxio_customer_id", "status", "date_updated"])
    return row


def _mark(row, status: str) -> None:
    row.status = status
    row.save(update_fields=["status", "date_updated"])


def _reconcile_customer(row: BillingCustomer) -> BillingCustomer:
    """The create may have landed: look it up by the reference we sent."""
    try:
        found = gateway.find_customer_by_reference(row.reference)
    except ProviderError:
        _mark(row, BillingCustomer.UNKNOWN)
        raise OutcomeUnknown("Billing account creation could not be confirmed.", reference=row.reference)
    if found is None:
        _mark(row, BillingCustomer.UNKNOWN)
        raise OutcomeUnknown("Billing account creation could not be confirmed.", reference=row.reference)
    return _settle_customer(row, found)


def _send_customer(row: BillingCustomer, user) -> BillingCustomer:
    first_name, last_name = _customer_names(user)
    try:
        customer = gateway.create_customer(
            first_name=first_name, last_name=last_name, email=user.email, reference=row.reference,
        )
    except ProviderRejected:
        # A 422 can mean "reference already taken": an earlier attempt landed.
        try:
            found = gateway.find_customer_by_reference(row.reference)
        except ProviderError:
            _mark(row, BillingCustomer.UNKNOWN)
            raise
        if found is not None:
            return _settle_customer(row, found)
        _mark(row, BillingCustomer.FAILED)
        raise
    except ProviderError as exc:
        if not exc.outcome_unknown:
            _mark(row, BillingCustomer.FAILED)  # never sent, or refused: nothing happened
            raise
        return _reconcile_customer(row)
    return _settle_customer(row, customer)


def ensure_customer(user) -> BillingCustomer:
    """Return the caller's Maxio customer, creating it at most once."""
    if not user.email:
        raise InvalidRequest(422, "Your account needs an email address before it can be billed.")

    try:
        with transaction.atomic():
            row, created = BillingCustomer.objects.get_or_create(
                user=user,
                defaults={"reference": _new_reference("cust", user), "status": BillingCustomer.SENDING},
            )
    except IntegrityError:
        row, created = BillingCustomer.objects.get(user=user), False

    if created:
        return _send_customer(row, user)

    if row.status == BillingCustomer.DONE:
        return row
    if row.status == BillingCustomer.SENDING and _is_fresh(row.date_updated):
        raise InProgress("Billing account creation is already in progress.")
    if row.status in (BillingCustomer.SENDING, BillingCustomer.UNKNOWN):
        found = gateway.find_customer_by_reference(row.reference)
        if found is not None:
            return _settle_customer(row, found)

    # Failed, or unresolved and not found. Maxio rejects a duplicate customer
    # reference, so resending under the SAME reference cannot create a second one.
    claimed = BillingCustomer.objects.filter(
        pk=row.pk, status=row.status, date_updated=row.date_updated,
    ).update(status=BillingCustomer.SENDING, date_updated=timezone.now())
    if not claimed:
        raise InProgress("Billing account creation is already in progress.")
    row.refresh_from_db()
    return _send_customer(row, user)


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #


@dataclass
class SubscribeResult:
    enrollment: SubscriptionEnrollment
    subscription: Subscription | None
    created: bool  # True when this request made the enrollment


def _apply_subscription(row: SubscriptionEnrollment, sub: Subscription, *, verify: bool) -> None:
    """Record what Maxio says about the subscription behind ``row``."""
    sub_id = present(sub.id)
    if not isinstance(sub_id, int):
        row.status = SubscriptionEnrollment.UNKNOWN
        row.status_detail = "Maxio returned a subscription without an id."
        row.save()
        raise OutcomeUnknown("Subscription could not be confirmed.", reference=row.reference)

    state = present(sub.state)
    row.maxio_subscription_id = sub_id
    row.maxio_state = str(state) if state is not None else ""
    row.maxio_created_at = present(sub.created_at)

    if verify:
        product = present(sub.product)
        customer = present(sub.customer)
        echoed = (
            present(product.handle) if product is not None else None,
            present(sub.product_price_in_cents),
            present(customer.id) if customer is not None else None,
        )
        asked = (row.plan_handle, row.expected_price_in_cents, row.billing_customer.maxio_customer_id)
        if echoed != asked:
            row.status = SubscriptionEnrollment.NEEDS_REVIEW
            row.status_detail = f"Maxio echoed (plan, price, customer)={echoed}, expected {asked}."
            logger.error("Subscription %s needs review: %s", row.reference, row.status_detail)
            row.save()
            return

    if row.status != SubscriptionEnrollment.NEEDS_REVIEW or verify:
        row.status = status_from_provider(state)
        row.status_detail = ""
    row.save()


def _reconcile_subscription(row: SubscriptionEnrollment) -> Subscription:
    try:
        found = gateway.find_subscription_by_reference(row.reference)
    except ProviderError:
        _mark(row, SubscriptionEnrollment.UNKNOWN)
        raise OutcomeUnknown("Subscription could not be confirmed yet.", reference=row.reference)
    if found is None:
        # An empty lookup cannot prove the write did not happen.
        _mark(row, SubscriptionEnrollment.UNKNOWN)
        raise OutcomeUnknown("Subscription could not be confirmed yet.", reference=row.reference)
    _apply_subscription(row, found, verify=True)
    return found


def _send_subscription(row: SubscriptionEnrollment) -> Subscription:
    try:
        sub = gateway.create_subscription(
            product_handle=row.plan_handle,
            customer_id=row.billing_customer.maxio_customer_id,
            reference=row.reference,
        )
    except ProviderRejected as exc:
        try:
            found = gateway.find_subscription_by_reference(row.reference)
        except ProviderError:
            _mark(row, SubscriptionEnrollment.UNKNOWN)
            raise
        if found is not None:  # an earlier attempt under this reference landed
            _apply_subscription(row, found, verify=True)
            return found
        row.status = SubscriptionEnrollment.FAILED
        row.status_detail = "; ".join(exc.messages)[:2000]
        row.save()
        raise
    except ProviderError as exc:
        if not exc.outcome_unknown:
            _mark(row, SubscriptionEnrollment.FAILED)
            raise
        return _reconcile_subscription(row)
    if sub is None:
        return _reconcile_subscription(row)
    _apply_subscription(row, sub, verify=True)
    return sub


def _answer_held(held: SubscriptionEnrollment) -> SubscribeResult:
    """A repeat of a subscribe request: answer from the claim that already exists."""
    if held.status == SubscriptionEnrollment.SENDING and _is_fresh(held.date_updated):
        raise InProgress("A subscription to this plan is already being created.", reference=held.reference)
    if held.status in (SubscriptionEnrollment.SENDING, SubscriptionEnrollment.UNKNOWN):
        # Look, never create: subscription reference uniqueness is not documented.
        return SubscribeResult(held, _reconcile_subscription(held), created=False)
    sub: Subscription | None = None
    if held.maxio_subscription_id:
        try:
            sub = gateway.read_subscription(held.maxio_subscription_id)
        except ProviderError:
            logger.warning("Could not refresh subscription %s; answering from stored state", held.reference)
        if sub is not None:
            _apply_subscription(held, sub, verify=False)
    return SubscribeResult(held, sub, created=False)


def subscribe(user, plan_handle: str) -> SubscribeResult:
    plan = get_plan(plan_handle)
    customer_row = ensure_customer(user)

    try:
        with transaction.atomic():
            row = SubscriptionEnrollment.objects.create(
                user=user,
                billing_customer=customer_row,
                plan_handle=plan.handle,
                expected_price_in_cents=plan.price_in_cents,
                reference=_new_reference("sub", user),
                status=SubscriptionEnrollment.SENDING,
            )
    except IntegrityError:
        held = (
            SubscriptionEnrollment.objects.filter(user=user, plan_handle=plan.handle)
            .exclude(status__in=SubscriptionEnrollment.RELEASED_STATUSES)
            .select_related("billing_customer")
            .first()
        )
        if held is None:
            raise InProgress("A subscription to this plan is already being created.")
        result = _answer_held(held)
        if result.enrollment.status in SubscriptionEnrollment.RELEASED_STATUSES:
            # The held subscription has ended since; the caller may subscribe again.
            return subscribe(user, plan_handle)
        return result

    sub = _send_subscription(row)
    return SubscribeResult(row, sub, created=True)


def _customer_id_for(user) -> int | None:
    row = BillingCustomer.objects.filter(user=user, status=BillingCustomer.DONE).first()
    return row.maxio_customer_id if row is not None else None


def my_subscriptions(user) -> tuple[list[Subscription], list[SubscriptionEnrollment]]:
    """Live subscriptions on the caller's Maxio customer, plus unresolved local claims."""
    enrollments = list(
        SubscriptionEnrollment.objects.filter(user=user).select_related("billing_customer")
    )
    customer_id = _customer_id_for(user)
    live = gateway.list_customer_subscriptions(customer_id) if customer_id is not None else []

    by_id = {e.maxio_subscription_id: e for e in enrollments if e.maxio_subscription_id}
    by_ref = {e.reference: e for e in enrollments}
    for sub in live:
        row = by_id.get(present(sub.id)) or by_ref.get(present(sub.reference) or "")
        if row is not None:
            # A claim left unknown settles here once Maxio lists it.
            was_unresolved = row.status in (SubscriptionEnrollment.SENDING, SubscriptionEnrollment.UNKNOWN)
            _apply_subscription(row, sub, verify=was_unresolved)

    live_ids = {present(s.id) for s in live}
    unresolved = [
        e for e in enrollments
        if e.status in (SubscriptionEnrollment.SENDING, SubscriptionEnrollment.UNKNOWN)
        and e.maxio_subscription_id not in live_ids
    ]
    return live, unresolved


def get_subscription(user, subscription_id: int) -> tuple[Subscription, SubscriptionEnrollment | None]:
    customer_id = _customer_id_for(user)
    if customer_id is None:
        raise InvalidRequest(404, "Subscription not found.")
    sub = gateway.read_subscription(subscription_id)
    customer = present(sub.customer) if sub is not None else None
    if sub is None or customer is None or present(customer.id) != customer_id:
        raise InvalidRequest(404, "Subscription not found.")
    row = (
        SubscriptionEnrollment.objects.filter(user=user, maxio_subscription_id=subscription_id)
        .select_related("billing_customer")
        .first()
    )
    if row is not None:
        _apply_subscription(row, sub, verify=False)
    return sub, row


def format_cents(cents: int | None) -> str | None:
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))
