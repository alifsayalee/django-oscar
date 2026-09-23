"""
Subscription billing flows. Each write follows the same shape:

1. CLAIM   -- insert the local row first; a unique constraint picks one winner.
2. CALL    -- only the winner calls Maxio, sending the reference it generated.
3. RECONCILE -- if the answer is missing or unreadable, look it up by reference.
4. VERIFY  -- compare what Maxio echoed with what we asked for.
5. SETTLE  -- record the status Maxio reported, mapped by name.

Views must run these outside a request-wide transaction (ATOMIC_REQUESTS is on
in the sandbox): the claim has to be committed before the provider is called.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from apps.subscriptions import gateway
from apps.subscriptions.gateway import (
    Plan,
    ProviderError,
    ProviderRejected,
    RemoteCustomer,
    RemoteSubscription,
)
from apps.subscriptions.models import (
    RELEASED_STATUSES,
    BillingCustomer,
    ClaimStatus,
    SubscriptionEnrollment,
)

if TYPE_CHECKING:
    # Typing only; at runtime the user model is AUTH_USER_MODEL.
    from django.contrib.auth.models import User  # pylint: disable=imported-auth-user

logger = logging.getLogger("apps.subscriptions.services")

# A 'sending' claim younger than this may still have a call in flight (one
# write attempt x timeout, plus margin); older ones are treated as stale and
# reconciled by lookup.
SEND_WINDOW = timedelta(seconds=60)


class SubscriptionFlowError(Exception):
    """A failure our API reports directly to the caller."""

    def __init__(self, status_code: int, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


class InProgress(SubscriptionFlowError):
    """Another request holds the claim and may still be talking to Maxio."""

    def __init__(self, message: str, **extra: object) -> None:
        super().__init__(202, message, **extra)


# --------------------------------------------------------------------------
# Status mapping -- the one place a Maxio state becomes ours
# --------------------------------------------------------------------------


def status_from_state(state: str | None) -> str:
    try:
        member = SubscriptionState(state) if state is not None else None
    except ValueError:
        member = None  # a state newer than this SDK
    match member:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return ClaimStatus.ACTIVE
        case (
            SubscriptionState.PAST_DUE
            | SubscriptionState.SOFT_FAILURE
            | SubscriptionState.UNPAID
            | SubscriptionState.PAUSED
            | SubscriptionState.ON_HOLD
            | SubscriptionState.SUSPENDED
        ):
            return ClaimStatus.PROBLEM
        case (
            SubscriptionState.PENDING
            | SubscriptionState.ASSESSING
            | SubscriptionState.AWAITING_SIGNUP
        ):
            return ClaimStatus.PENDING
        case (
            SubscriptionState.CANCELED
            | SubscriptionState.EXPIRED
            | SubscriptionState.TRIAL_ENDED
        ):
            return ClaimStatus.ENDED
        case SubscriptionState.FAILED_TO_CREATE:
            return ClaimStatus.FAILED
        case _:
            return ClaimStatus.UNKNOWN


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------


def _family() -> str:
    family = str(getattr(settings, "MAXIO_DEFAULT_PRODUCT_FAMILY", "") or "").strip()
    if not family:
        raise SubscriptionFlowError(503, "Subscription billing is not configured.")
    return family


def list_plans() -> tuple[list[Plan], bool]:
    return gateway.list_plans(_family())


def get_plan(plan_handle: str) -> Plan:
    """The plan must be one the plan listing offers (cross-operation invariant)."""
    plans, _truncated = list_plans()
    for plan in plans:
        if plan.handle == plan_handle:
            return plan
    raise SubscriptionFlowError(
        400, "Unknown plan.", field="planHandle", planHandle=plan_handle
    )


# --------------------------------------------------------------------------
# Customer
# --------------------------------------------------------------------------


def collection_method() -> CollectionMethod:
    """
    How Maxio collects payment. This integration captures no payment method, so
    'automatic' cannot succeed at signup; the default 'remittance' invoices the
    customer instead.
    """
    value = str(getattr(settings, "MAXIO_PAYMENT_COLLECTION_METHOD", "") or "remittance")
    try:
        return CollectionMethod(value.strip().lower())
    except ValueError:
        raise SubscriptionFlowError(503, "Subscription billing is misconfigured.") from None


def _prefix() -> str:
    return str(getattr(settings, "MAXIO_REFERENCE_PREFIX", "") or "oscar-sandbox").strip()


def customer_reference(user: User) -> str:
    return f"{_prefix()}-user-{user.pk}"


def _customer_names(user: User) -> tuple[str, str]:
    local_part = user.email.split("@", 1)[0]
    first = (user.first_name or "").strip() or local_part
    last = (user.last_name or "").strip() or user.get_username() or local_part
    return first, last


def _settle_customer(
    row: BillingCustomer, remote: RemoteCustomer, user: User
) -> BillingCustomer:
    # Verify before keeping: a customer found by our reference must be this user.
    if (remote.email or "").lower() != user.email.lower():
        row.status = ClaimStatus.NEEDS_REVIEW
        row.maxio_customer_id = remote.id
        row.last_error = "Maxio customer found by reference has a different email."
        row.save()
        logger.error("maxio customer %s email mismatch for user %s", remote.id, user.pk)
        raise SubscriptionFlowError(
            409, "Billing account needs review; please contact support."
        )
    row.maxio_customer_id = remote.id
    row.status = ClaimStatus.ACTIVE
    row.last_error = ""
    row.save()
    return row


def _mark(
    row: BillingCustomer | SubscriptionEnrollment, status: str, error: str = ""
) -> None:
    row.status = status
    row.last_error = error
    row.save()


def ensure_customer(user: User) -> BillingCustomer:
    """
    Make sure a Maxio customer exists for ``user``; idempotent. Maxio enforces
    a unique customer reference, so re-sending a create under the *same*
    reference is safe: a duplicate is answered 422 and resolved by lookup.
    """
    if not user.email:
        raise SubscriptionFlowError(
            422, "An email address is required to create a billing account."
        )
    reference = customer_reference(user)

    # 1. CLAIM
    try:
        with transaction.atomic():
            row = BillingCustomer.objects.create(
                user=user, reference=reference, status=ClaimStatus.SENDING
            )
    except IntegrityError:
        row = BillingCustomer.objects.get(user=user)
        if row.status == ClaimStatus.ACTIVE:
            return row
        if row.status == ClaimStatus.NEEDS_REVIEW:
            raise SubscriptionFlowError(
                409, "Billing account needs review; please contact support."
            )
        stale = timezone.now() - SEND_WINDOW
        if row.status == ClaimStatus.SENDING and row.date_updated > stale:
            raise InProgress("Billing account is being created.")
        # failed, unknown or a stale sender: look first, never blindly create.
        if row.status != ClaimStatus.FAILED:
            found = gateway.find_customer(row.reference)
            if found is not None:
                return _settle_customer(row, found, user)
        # Take the claim over with one conditional write; losing it means
        # another request just did the same.
        taken = (
            BillingCustomer.objects.filter(pk=row.pk, status=row.status)
            .filter(date_updated=row.date_updated)
            .update(status=ClaimStatus.SENDING, date_updated=timezone.now())
        )
        if not taken:
            raise InProgress("Billing account is being created.")
        row.refresh_from_db()

    # 2. CALL
    first_name, last_name = _customer_names(user)
    try:
        remote = gateway.create_customer(
            reference=row.reference,
            email=user.email,
            first_name=first_name,
            last_name=last_name,
        )
    except ProviderRejected as exc:
        # Possibly "reference already taken": an earlier attempt landed.
        found = _lookup_customer_or_unknown(row, exc)
        if found is None:
            _mark(row, ClaimStatus.FAILED, "; ".join(exc.details) or exc.message)
            raise
        return _settle_customer(row, found, user)
    except ProviderError as exc:
        if not exc.outcome_unknown:
            _mark(row, ClaimStatus.FAILED, exc.message)
            raise
        # 3. RECONCILE
        found = _lookup_customer_or_unknown(row, exc)
        if found is None:
            _mark(row, ClaimStatus.UNKNOWN, exc.message)
            raise
        return _settle_customer(row, found, user)

    # 4. VERIFY + 5. SETTLE
    if remote.reference != row.reference:
        _mark(row, ClaimStatus.NEEDS_REVIEW, "Maxio echoed a different reference.")
        raise SubscriptionFlowError(502, "Billing account needs review.")
    return _settle_customer(row, remote, user)


def _lookup_customer_or_unknown(
    row: BillingCustomer, cause: ProviderError
) -> RemoteCustomer | None:
    try:
        return gateway.find_customer(row.reference)
    except ProviderError:
        _mark(row, ClaimStatus.UNKNOWN, cause.message)
        raise cause from None


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------


@dataclass
class SubscribeResult:
    enrollment: SubscriptionEnrollment
    subscription: RemoteSubscription | None
    created: bool
    plan: Plan | None = None


def _verify_and_settle(
    row: SubscriptionEnrollment,
    remote: RemoteSubscription,
    customer: BillingCustomer,
) -> SubscriptionEnrollment:
    row.maxio_subscription_id = remote.id
    row.maxio_state = remote.state or ""
    row.maxio_created_at = remote.created_at
    mismatches = []
    if remote.product_handle != row.plan_handle:
        mismatches.append(f"plan {remote.product_handle!r} != {row.plan_handle!r}")
    if remote.customer_id != customer.maxio_customer_id:
        mismatches.append(f"customer {remote.customer_id} != {customer.maxio_customer_id}")
    if remote.price_in_cents != row.expected_price_in_cents:
        mismatches.append(f"price {remote.price_in_cents} != {row.expected_price_in_cents}")
    if remote.payment_collection_method not in (None, collection_method().value):
        mismatches.append(f"collection method {remote.payment_collection_method!r}")
    if remote.reference is not None and remote.reference != row.reference:
        mismatches.append("reference differs")
    if mismatches:
        row.status = ClaimStatus.NEEDS_REVIEW
        row.last_error = "; ".join(mismatches)
        logger.error("maxio subscription %s needs review: %s", remote.id, row.last_error)
    else:
        row.status = status_from_state(remote.state)
        row.last_error = ""
    row.save()
    return row


def _reconcile(row: SubscriptionEnrollment) -> RemoteSubscription | None:
    """Look an enrollment up at Maxio. None means *not found yet*, never 'failed'."""
    if row.maxio_subscription_id:
        return gateway.read_subscription(row.maxio_subscription_id)
    return gateway.find_subscription(row.reference)


def _held_result(
    row: SubscriptionEnrollment, customer: BillingCustomer
) -> SubscribeResult:
    """Answer a subscribe that lost the claim, from the winner's row."""
    stale = timezone.now() - SEND_WINDOW
    if row.status == ClaimStatus.SENDING and row.date_updated > stale:
        return SubscribeResult(row, None, created=False)
    remote = _reconcile(row)
    if remote is None:
        if row.status == ClaimStatus.SENDING:
            _mark(row, ClaimStatus.UNKNOWN, "Sender went stale; not found at Maxio yet.")
        return SubscribeResult(row, None, created=False)
    _verify_and_settle(row, remote, customer)
    return SubscribeResult(row, remote, created=False)


def subscribe(user: User, plan_handle: str) -> SubscribeResult:
    """
    Enroll ``user`` on ``plan_handle``. A user holds at most one live
    enrollment per plan, so a double-click or a retry returns the first one.
    """
    method = collection_method()
    plan = get_plan(plan_handle)
    if plan.price_in_cents is None:
        raise SubscriptionFlowError(502, "Plan has no price at the billing provider.")
    customer = ensure_customer(user)
    customer_id = customer.maxio_customer_id
    if customer_id is None:  # an ACTIVE billing customer always has one
        raise SubscriptionFlowError(502, "Billing account is incomplete.")

    for _attempt in range(2):
        # 1. CLAIM
        row = SubscriptionEnrollment(
            user=user,
            billing_customer=customer,
            plan_handle=plan.handle,
            reference=f"{_prefix()}-sub-{uuid.uuid4().hex}",
            status=ClaimStatus.SENDING,
            expected_price_in_cents=plan.price_in_cents,
        )
        try:
            with transaction.atomic():
                row.save()
        except IntegrityError:
            held = (
                SubscriptionEnrollment.objects.filter(user=user, plan_handle=plan.handle)
                .exclude(status__in=RELEASED_STATUSES)
                .first()
            )
            if held is None:
                continue  # released between our insert and our read: claim again
            result = _held_result(held, customer)
            if held.status in RELEASED_STATUSES:
                continue  # it had ended at Maxio: this request may enroll afresh
            result.plan = plan
            return result
        break
    else:
        raise InProgress("Subscription is being created.")

    # 2. CALL -- a request that lost the claim never reaches this line.
    try:
        remote = gateway.create_subscription(
            reference=row.reference,
            customer_id=customer_id,
            product_handle=plan.handle,
            payment_collection_method=method,
        )
    except ProviderError as exc:
        if not exc.outcome_unknown:
            _mark(row, ClaimStatus.FAILED, "; ".join(exc.details) or exc.message)
            raise
        # 3. RECONCILE -- it may have landed; look it up by the reference we sent.
        try:
            found = gateway.find_subscription(row.reference)
        except ProviderError:
            _mark(row, ClaimStatus.UNKNOWN, exc.message)
            raise exc from None
        if found is None:
            # Not found *yet*: a timed-out write can still land. Keep the claim.
            _mark(row, ClaimStatus.UNKNOWN, exc.message)
            raise
        remote = found

    # 4. VERIFY + 5. SETTLE
    _verify_and_settle(row, remote, customer)
    return SubscribeResult(row, remote, created=True, plan=plan)


def my_subscriptions(
    user: User,
) -> tuple[list[RemoteSubscription], list[SubscriptionEnrollment]]:
    """
    The user's subscriptions as Maxio reports them, plus local enrollments Maxio
    does not show yet (in flight or unresolved). Local rows are brought up to
    date from what Maxio says.
    """
    customer = BillingCustomer.objects.filter(
        user=user, status=ClaimStatus.ACTIVE
    ).first()
    if customer is None or customer.maxio_customer_id is None:
        return [], list(
            SubscriptionEnrollment.objects.filter(user=user).exclude(
                status__in=RELEASED_STATUSES
            )
        )

    remote = gateway.list_customer_subscriptions(customer.maxio_customer_id)
    by_reference = {s.reference: s for s in remote if s.reference}
    by_id = {s.id: s for s in remote}

    unresolved = []
    for row in SubscriptionEnrollment.objects.filter(user=user).exclude(
        status__in=RELEASED_STATUSES
    ):
        match = by_reference.get(row.reference) or (
            by_id.get(row.maxio_subscription_id) if row.maxio_subscription_id else None
        )
        if match is not None:
            if row.status != ClaimStatus.NEEDS_REVIEW:
                _verify_and_settle(row, match, customer)
        elif row.status == ClaimStatus.SENDING and row.date_updated > (
            timezone.now() - SEND_WINDOW
        ):
            unresolved.append(row)
        else:
            if row.status == ClaimStatus.SENDING:
                _mark(row, ClaimStatus.UNKNOWN, "Not found at Maxio yet.")
            unresolved.append(row)
    return remote, unresolved


def get_subscription(user: User, subscription_id: int) -> RemoteSubscription:
    """One subscription, only if it belongs to the caller's Maxio customer."""
    customer = BillingCustomer.objects.filter(
        user=user, status=ClaimStatus.ACTIVE
    ).first()
    if customer is None:
        raise SubscriptionFlowError(404, "Subscription not found.")
    remote = gateway.read_subscription(subscription_id)
    if remote is None or remote.customer_id != customer.maxio_customer_id:
        raise SubscriptionFlowError(404, "Subscription not found.")
    row = SubscriptionEnrollment.objects.filter(maxio_subscription_id=remote.id).first()
    if row is not None and row.status != ClaimStatus.NEEDS_REVIEW:
        _verify_and_settle(row, remote, customer)
    return remote
