"""
Subscription billing through Maxio Advanced Billing.

Every Maxio write that creates something goes through the same steps:

1. **claim** – insert a local row (``outcome=sending``) carrying the reference
   we will send, in its own committed transaction, *before* calling Maxio.
   The database's unique constraints decide which of two concurrent requests
   may call Maxio; the loser answers from the row instead.
2. **call** – send the write with that reference.
3. **check** – when the answer is missing or unreadable, look the write up in
   Maxio by the same reference; if that cannot settle it the row stays
   ``unknown`` (never ``failed``) and is looked up again later.
4. **verify** and **complete** – record what Maxio *said* (its status), not
   the fact that it answered.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest, Customer,
    Subscription)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from . import maxio
from .errors import NEVER_SENT, BillingError, OutcomeUnknown, provider_messages, translate_provider_error
from .models import BillingCustomer, Outcome, SubscriptionEnrollment

logger = logging.getLogger('apps.subscriptions')

PLANS_CACHE_SECONDS = 60
PLANS_PAGE_SIZE = 200

# What a claim-loser does next
WON = 'won'                    # it holds the claim now: send
CHECK = 'check'                # it holds a stale/unknown claim: look it up, do not create
IN_PROGRESS = 'in_progress'    # someone else is sending right now
EXISTING = 'existing'          # settled earlier: answer from the row

RowT = TypeVar('RowT', BillingCustomer, SubscriptionEnrollment)
T = TypeVar('T')


class InProgress(BillingError):
    def __init__(self, what: str) -> None:
        super().__init__(202, 'in_progress', f'{what} is already being processed; retry shortly.')


def unset_to_none(value: T | UnsetType | None) -> T | None:
    """SDK values must not leave this module as UNSET."""
    return None if isinstance(value, UnsetType) else value


def send_window() -> timedelta:
    """How long a ``sending`` claim is presumed alive (a create is never retried, so one timeout + margin)."""
    return timedelta(seconds=max(60.0, 3 * maxio.timeout()))


# ---------------------------------------------------------------------------
# Provider status -> our outcome
# ---------------------------------------------------------------------------

def status_from_provider(state: object) -> Outcome:
    """The ONE place a Maxio subscription state becomes an outcome of ours."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return Outcome.DONE
        case (SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP
              | SubscriptionState.PAUSED | SubscriptionState.SOFT_FAILURE | SubscriptionState.PAST_DUE
              | SubscriptionState.UNPAID | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            # Exists, but not (or no longer fully) in effect
            return Outcome.PENDING
        case (SubscriptionState.FAILED_TO_CREATE | SubscriptionState.CANCELED | SubscriptionState.EXPIRED
              | SubscriptionState.TRIAL_ENDED):
            # Never took effect, or took effect and was undone
            return Outcome.FAILED
        case _:
            # A state newer than this SDK, or none at all: neither done nor failed
            return Outcome.UNKNOWN


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    handle: str
    name: str
    description: str
    product_id: int | None
    price_in_cents: int | None
    currency: str | None
    interval: int | None
    interval_unit: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            'planHandle': self.handle,
            'name': self.name,
            'description': self.description,
            'productId': self.product_id,
            'priceInCents': self.price_in_cents,
            'price': format_cents(self.price_in_cents),
            'currency': self.currency,
            'interval': self.interval,
            'intervalUnit': self.interval_unit,
        }


def format_cents(cents: int | None) -> str | None:
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal('0.01')))


@dataclass(frozen=True)
class SiteInfo:
    currency: str | None
    relationship_invoicing: bool


def site_info() -> SiteInfo:
    """The Maxio site's currency and invoicing architecture (cached briefly)."""
    key = 'subscriptions:site'
    info: SiteInfo | None = cache.get(key)
    if info is None:
        client = maxio.get_client()
        try:
            site = maxio.read(lambda: client.sites.read_site()).site
        except Exception as e:
            raise translate_provider_error(e) from e
        info = SiteInfo(currency=unset_to_none(site.currency),
                        relationship_invoicing=unset_to_none(site.relationship_invoicing_enabled) is True)
        cache.set(key, info, PLANS_CACHE_SECONDS)
    return info


def payment_collection_method() -> CollectionMethod:
    """
    How Maxio collects payment for new subscriptions.

    This integration captures no card, so unless configured otherwise the
    subscription is billed by invoice: ``remittance`` on Relationship
    Invoicing sites, ``invoice`` on legacy Statements sites.
    """
    configured = maxio.configured_collection_method()
    if configured is not None:
        return configured
    return CollectionMethod.REMITTANCE if site_info().relationship_invoicing else CollectionMethod.INVOICE


def _fetch_plans() -> list[Plan]:
    client = maxio.get_client()
    family = maxio.product_family()
    currency = site_info().currency
    plans: list[Plan] = []
    page = 1
    while True:
        batch = maxio.read(lambda: client.product_families.list_products_for_product_family(
            f'handle:{family}', page=page, per_page=PLANS_PAGE_SIZE))
        for entry in batch:
            product = entry.product
            handle = unset_to_none(product.handle)
            if not handle or unset_to_none(product.archived_at) is not None:
                continue
            interval_unit = unset_to_none(product.interval_unit)
            plans.append(Plan(
                handle=handle,
                name=unset_to_none(product.name) or handle,
                description=unset_to_none(product.description) or '',
                product_id=unset_to_none(product.id),
                price_in_cents=unset_to_none(product.price_in_cents),
                currency=currency,
                interval=unset_to_none(product.interval),
                interval_unit=None if interval_unit is None else str(interval_unit),
            ))
        if len(batch) < PLANS_PAGE_SIZE:
            return plans
        page += 1


def list_plans() -> list[Plan]:
    """The plans of the configured product family, read from Maxio (cached briefly)."""
    key = f'subscriptions:plans:{maxio.product_family()}'
    plans: list[Plan] | None = cache.get(key)
    if plans is None:
        try:
            plans = _fetch_plans()
        except Exception as e:
            raise translate_provider_error(e) from e
        cache.set(key, plans, PLANS_CACHE_SECONDS)
    return plans


def get_plan(handle: str) -> Plan | None:
    return next((p for p in list_plans() if p.handle == handle), None)


# ---------------------------------------------------------------------------
# Claims (shared by customers and subscriptions)
# ---------------------------------------------------------------------------

def _provider_id(row: BillingCustomer | SubscriptionEnrollment) -> int | None:
    if isinstance(row, BillingCustomer):
        return row.maxio_customer_id
    return row.maxio_subscription_id


def _take_over(row: RowT) -> bool:
    """Atomically move a claim we observed to a fresh ``sending`` claim; only one caller can win."""
    now = timezone.now()
    taken = type(row).objects.filter(
        pk=row.pk, outcome=row.outcome, claimed_at=row.claimed_at,
    ).update(outcome=Outcome.SENDING, claimed_at=now) == 1
    if taken:
        row.outcome = Outcome.SENDING
        row.claimed_at = now
    return taken


def _settle_loser(row: RowT) -> tuple[RowT, str]:
    """What a request that did not insert the claim may do with the existing one."""
    if row.outcome == Outcome.SENDING and row.claimed_at > timezone.now() - send_window():
        return row, IN_PROGRESS
    if row.outcome in (Outcome.SENDING, Outcome.UNKNOWN):
        # A stale sender or an unresolved write: only a check, never a fresh create
        return (row, CHECK) if _take_over(row) else (row, IN_PROGRESS)
    if row.outcome == Outcome.FAILED and _provider_id(row) is None:
        # Nothing happened at Maxio: the claim is free again, under the same reference
        return (row, WON) if _take_over(row) else (row, IN_PROGRESS)
    return row, EXISTING


def _complete(row: RowT, outcome: Outcome, **fields: Any) -> RowT:
    row.outcome = outcome
    for name, value in fields.items():
        setattr(row, name, value)
    row.save()
    return row


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def customer_reference(user: Any) -> str:
    return f'{maxio.reference_prefix()}-cust-u{user.pk}'


def _customer_body(user: Any, reference: str) -> CreateCustomerRequest:
    email = (user.email or '').strip()
    if not email:
        raise BillingError(422, 'email_required', 'Your account needs an email address before subscribing.')
    local_part = email.split('@', 1)[0]
    return CreateCustomerRequest(customer=CreateCustomer(
        first_name=(user.first_name or '').strip() or local_part,
        last_name=(user.last_name or '').strip() or 'Customer',
        email=email,
        reference=reference,
    ))


def _find_customer(reference: str) -> Customer | None:
    """Maxio's customer carrying ``reference``, or None when Maxio has none (404)."""
    client = maxio.get_client()
    try:
        response = maxio.read(lambda: client.customers.read_customer_by_reference(reference))
    except ApiError as e:
        if e.status_code == 404:
            return None
        raise
    return response.customer


def _complete_customer(row: BillingCustomer, customer: Customer) -> BillingCustomer:
    customer_id = unset_to_none(customer.id)
    if customer_id is None:
        # Maxio answered without naming what it created: still unknown
        _complete(row, Outcome.UNKNOWN, last_error='Maxio returned a customer without an id')
        raise OutcomeUnknown(row.reference)
    return _complete(row, Outcome.DONE, maxio_customer_id=customer_id, last_error='')


def _claim_customer(user: Any) -> tuple[BillingCustomer, str]:
    try:
        with transaction.atomic():
            row = BillingCustomer.objects.create(
                user=user, reference=customer_reference(user), outcome=Outcome.SENDING,
                claimed_at=timezone.now())
        return row, WON
    except IntegrityError:
        return _settle_loser(BillingCustomer.objects.get(user=user))


def get_customer(user: Any) -> BillingCustomer | None:
    return BillingCustomer.objects.filter(user=user).first()


def ensure_customer(user: Any) -> BillingCustomer:
    """
    The caller's Maxio customer, created once.

    Maxio accepts only one customer per reference, so an unanswered create is
    checked by looking the reference up and, when absent, by resending under
    the same reference.
    """
    body = _customer_body(user, customer_reference(user))   # validate before claiming
    row, action = _claim_customer(user)
    if action == EXISTING:
        if row.outcome == Outcome.DONE:
            return row
        raise BillingError(409, 'billing_customer_needs_review',
                           'Your billing account needs attention from support.')
    if action == IN_PROGRESS:
        raise InProgress('Your billing account')

    resending = action == CHECK
    if resending:
        try:
            found = _find_customer(row.reference)
        except Exception as e:
            _complete(row, Outcome.UNKNOWN, last_error=f'lookup failed: {type(e).__name__}')
            raise OutcomeUnknown(row.reference) from e
        if found is not None:
            return _complete_customer(row, found)
        # Not there: a resend under the same reference is safe (one customer per reference)

    client = maxio.get_client()
    try:
        response = client.customers.create_customer(body=body)
    except NEVER_SENT as e:
        # Never left: nothing happened (a check that never left settles nothing)
        _complete(row, Outcome.UNKNOWN if resending else Outcome.FAILED, last_error=type(e).__name__)
        if resending:
            raise OutcomeUnknown(row.reference) from e
        raise translate_provider_error(e, write=True) from e
    except ApiError as e:
        if e.status_code >= 500 or e.status_code == 422:
            # 5xx: may have landed. 422: may be "reference already taken" by an earlier attempt.
            pass
        else:
            _complete(row, Outcome.UNKNOWN if resending else Outcome.FAILED,
                      last_error=f'HTTP {e.status_code}')
            if resending:
                raise OutcomeUnknown(row.reference) from e
            raise translate_provider_error(e, write=True) from e
        rejection: ApiError | None = e
    except (httpx.RequestError, ValueError):
        rejection = None
    else:
        return _complete_customer(row, response.customer)

    # No usable answer: look the reference up
    try:
        found = _find_customer(row.reference)
    except Exception as lookup_error:
        _complete(row, Outcome.UNKNOWN, last_error=f'lookup failed: {type(lookup_error).__name__}')
        raise OutcomeUnknown(row.reference) from lookup_error
    if found is not None:
        return _complete_customer(row, found)
    if rejection is not None and rejection.status_code == 422 and not resending:
        # Refused, and nothing carries our reference: a genuine validation failure
        messages = provider_messages(rejection.error)
        _complete(row, Outcome.FAILED, last_error='; '.join(messages) or 'HTTP 422')
        raise translate_provider_error(rejection, write=True) from rejection
    _complete(row, Outcome.UNKNOWN, last_error='no confirmation from Maxio')
    raise OutcomeUnknown(row.reference)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def _slug(value: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9_-]+', '-', value).strip('-')
    if len(slug) > 40 or not slug:
        slug = hashlib.sha256(value.encode()).hexdigest()[:16]
    return slug


def subscription_reference(user: Any, plan_handle: str, idempotency_key: str) -> str:
    """
    The reference sent to Maxio for this subscribe request.

    With an Idempotency-Key it is derived from the key (same key = same
    request). Without one it is derived from the user, the plan and how many
    earlier subscriptions to that plan have ended, so a repeat while one is
    live maps to the same reference.
    """
    prefix = maxio.reference_prefix()
    if idempotency_key:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        return f'{prefix}-sub-u{user.pk}-k{digest}'
    ended = SubscriptionEnrollment.objects.filter(
        user=user, plan_handle=plan_handle, outcome=Outcome.FAILED,
        maxio_subscription_id__isnull=False).count()
    return f'{prefix}-sub-u{user.pk}-{_slug(plan_handle)}-{ended}'


def _claim_enrollment(user: Any, customer: BillingCustomer, plan_handle: str,
                      idempotency_key: str) -> tuple[SubscriptionEnrollment, str]:
    reference = subscription_reference(user, plan_handle, idempotency_key)
    try:
        with transaction.atomic():
            row = SubscriptionEnrollment.objects.create(
                user=user, customer=customer, plan_handle=plan_handle, reference=reference,
                idempotency_key=idempotency_key, outcome=Outcome.SENDING, claimed_at=timezone.now())
        return row, WON
    except IntegrityError:
        pass
    existing = SubscriptionEnrollment.objects.filter(reference=reference).first()
    if existing is None:
        # A different request for the same plan is live: this one is a repeat of it
        existing = SubscriptionEnrollment.objects.filter(
            user=user, plan_handle=plan_handle, outcome__in=[
                Outcome.SENDING, Outcome.DONE, Outcome.PENDING, Outcome.UNKNOWN, Outcome.NEEDS_REVIEW,
            ]).first()
        if existing is None:
            raise InProgress('Your subscription')
    if existing.user_id != user.pk or existing.plan_handle != plan_handle:
        raise BillingError(422, 'idempotency_key_reused',
                           'This Idempotency-Key was already used for a different request.')
    return _settle_loser(existing)


def _find_subscription(reference: str) -> Subscription | None:
    """Maxio's subscription carrying ``reference``, or None when Maxio has none (404)."""
    client = maxio.get_client()
    try:
        response = maxio.read(lambda: client.subscriptions.find_subscription(reference=reference))
    except ApiError as e:
        if e.status_code == 404:
            return None
        raise
    return unset_to_none(response.subscription)


def _subscription_fields(subscription: Subscription) -> dict[str, Any]:
    state = unset_to_none(subscription.state)
    next_billing = unset_to_none(subscription.next_assessment_at) or unset_to_none(
        subscription.current_period_ends_at)
    return {
        'provider_state': '' if state is None else str(state),
        'price_in_cents': unset_to_none(subscription.product_price_in_cents),
        'currency': unset_to_none(subscription.currency) or '',
        'next_billing_at': next_billing,
        'provider_updated_at': unset_to_none(subscription.updated_at),
    }


def _complete_enrollment(row: SubscriptionEnrollment, subscription: Subscription | None) -> SubscriptionEnrollment:
    """Verify what Maxio returned against what we asked, then record Maxio's own status."""
    subscription_id = None if subscription is None else unset_to_none(subscription.id)
    if subscription is None or subscription_id is None:
        _complete(row, Outcome.UNKNOWN, last_error='Maxio returned no subscription id')
        raise OutcomeUnknown(row.reference)

    product = unset_to_none(subscription.product)
    product_handle = None if product is None else unset_to_none(product.handle)
    customer = unset_to_none(subscription.customer)
    customer_id = None if customer is None else unset_to_none(customer.id)
    fields = _subscription_fields(subscription)
    if product_handle != row.plan_handle or customer_id != row.customer.maxio_customer_id:
        # It happened, but not as asked: visible, never "done"
        return _complete(row, Outcome.NEEDS_REVIEW, maxio_subscription_id=subscription_id,
                         last_error=f'Maxio returned plan {product_handle!r} for customer {customer_id!r}',
                         **fields)
    return _complete(row, status_from_provider(unset_to_none(subscription.state)),
                     maxio_subscription_id=subscription_id, last_error='', **fields)


def _check_enrollment(row: SubscriptionEnrollment) -> SubscriptionEnrollment:
    """Settle a claim whose create may have landed, by the reference it was sent with."""
    try:
        found = _find_subscription(row.reference)
    except Exception as e:
        _complete(row, Outcome.UNKNOWN, last_error=f'lookup failed: {type(e).__name__}')
        raise OutcomeUnknown(row.reference) from e
    if found is None:
        # Not found yet: a timed-out create can still land, so this proves nothing
        _complete(row, Outcome.UNKNOWN, last_error='not found by reference yet')
        raise OutcomeUnknown(row.reference)
    return _complete_enrollment(row, found)


def subscribe(user: Any, plan_handle: str, idempotency_key: str = '') -> tuple[SubscriptionEnrollment, bool]:
    """
    Subscribe ``user`` to ``plan_handle``.

    Returns the enrollment and whether this request created it (False when it
    answers a repeat from the stored outcome).
    """
    if get_plan(plan_handle) is None:
        raise BillingError(400, 'unknown_plan', f'There is no plan with handle {plan_handle!r}.')
    collection_method = payment_collection_method()
    customer = ensure_customer(user)

    row, action = _claim_enrollment(user, customer, plan_handle, idempotency_key)
    if action == EXISTING:
        return row, False
    if action == IN_PROGRESS:
        raise InProgress('Your subscription')
    if action == CHECK:
        # Subscription references are not documented as unique at Maxio, so
        # an unresolved create is only ever looked up, never resent.
        return _check_enrollment(row), False

    assert customer.maxio_customer_id is not None  # ensure_customer only returns a done customer
    body = CreateSubscriptionRequest(subscription=CreateSubscription(
        product_handle=plan_handle,
        customer_id=customer.maxio_customer_id,
        reference=row.reference,
        payment_collection_method=collection_method,
    ))
    client = maxio.get_client()
    try:
        response = client.subscriptions.create_subscription(body=body)
    except NEVER_SENT as e:
        _complete(row, Outcome.FAILED, last_error=type(e).__name__)
        raise translate_provider_error(e, write=True) from e
    except ApiError as e:
        if e.status_code < 500:
            # Refused. Unless an earlier life of this reference already exists at Maxio, nothing happened.
            try:
                found = _find_subscription(row.reference)
            except Exception:
                found = None
            if found is not None:
                return _complete_enrollment(row, found), True
            messages = provider_messages(e.error)
            _complete(row, Outcome.FAILED, last_error='; '.join(messages) or f'HTTP {e.status_code}')
            raise translate_provider_error(e, write=True) from e
        # 5xx: may have landed
    except (httpx.RequestError, ValueError):
        pass  # sent, and no readable answer: may have landed
    else:
        return _complete_enrollment(row, unset_to_none(response.subscription)), True
    return _check_enrollment(row), True


# ---------------------------------------------------------------------------
# Reading subscriptions back
# ---------------------------------------------------------------------------

def _refresh_row_from(row: SubscriptionEnrollment, subscription: Subscription) -> None:
    """Keep a settled enrollment in step with Maxio (e.g. canceled later -> no longer holds the plan)."""
    if row.outcome not in (Outcome.DONE, Outcome.PENDING):
        return
    _complete(row, status_from_provider(unset_to_none(subscription.state)), **_subscription_fields(subscription))


def reconcile(user: Any) -> None:
    """Re-check this user's unresolved creates by reference (best effort; failures leave them unknown)."""
    stale_before = timezone.now() - send_window()
    rows = SubscriptionEnrollment.objects.filter(user=user, outcome__in=[Outcome.UNKNOWN, Outcome.SENDING])
    for row in rows:
        if row.outcome == Outcome.SENDING and row.claimed_at > stale_before:
            continue
        if not _take_over(row):
            continue
        try:
            _check_enrollment(row)
        except OutcomeUnknown:
            logger.info('Enrollment %s is still unresolved', row.reference)


def my_subscriptions(user: Any) -> tuple[list[Subscription], list[SubscriptionEnrollment]]:
    """The user's subscriptions as Maxio has them, plus local requests Maxio has not confirmed."""
    reconcile(user)
    customer = get_customer(user)
    subscriptions: list[Subscription] = []
    if customer is not None and customer.outcome == Outcome.DONE and customer.maxio_customer_id is not None:
        client = maxio.get_client()
        customer_id = customer.maxio_customer_id
        try:
            entries = maxio.read(lambda: client.customers.list_customer_subscriptions(customer_id))
        except Exception as e:
            raise translate_provider_error(e) from e
        rows = {r.maxio_subscription_id: r for r in SubscriptionEnrollment.objects.filter(
            user=user, maxio_subscription_id__isnull=False)}
        for entry in entries:
            subscription = unset_to_none(entry.subscription)
            listed_id = None if subscription is None else unset_to_none(subscription.id)
            if subscription is None or listed_id is None:
                continue
            subscriptions.append(subscription)
            row = rows.get(listed_id)
            if row is not None:
                _refresh_row_from(row, subscription)
    unresolved = list(SubscriptionEnrollment.objects.filter(
        user=user, maxio_subscription_id__isnull=True,
        outcome__in=[Outcome.SENDING, Outcome.UNKNOWN]))
    return subscriptions, unresolved


def get_subscription(user: Any, subscription_id: int) -> Subscription:
    """One of the user's subscriptions, read live from Maxio."""
    customer = get_customer(user)
    if customer is None or customer.maxio_customer_id is None:
        raise BillingError(404, 'not_found', 'No such subscription.')
    client = maxio.get_client()
    try:
        response = maxio.read(lambda: client.subscriptions.read_subscription(subscription_id))
    except Exception as e:
        error = translate_provider_error(e)
        if error.status_code == 404:
            raise BillingError(404, 'not_found', 'No such subscription.') from e
        raise error from e
    subscription = unset_to_none(response.subscription)
    owner = None if subscription is None else unset_to_none(subscription.customer)
    if subscription is None or owner is None or unset_to_none(owner.id) != customer.maxio_customer_id:
        # Never reveal another customer's subscription
        raise BillingError(404, 'not_found', 'No such subscription.')
    row = SubscriptionEnrollment.objects.filter(user=user, maxio_subscription_id=subscription_id).first()
    if row is not None:
        _refresh_row_from(row, subscription)
    return subscription


# ---------------------------------------------------------------------------
# JSON shapes
# ---------------------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(dt_timezone.utc).isoformat()


def subscription_json(subscription: Subscription) -> dict[str, Any]:
    product = unset_to_none(subscription.product)
    state = unset_to_none(subscription.state)
    price = unset_to_none(subscription.product_price_in_cents)
    return {
        'subscriptionId': unset_to_none(subscription.id),
        'reference': unset_to_none(subscription.reference),
        'planHandle': None if product is None else unset_to_none(product.handle),
        'planName': None if product is None else unset_to_none(product.name),
        'state': None if state is None else str(state),
        'outcome': status_from_provider(state).value,
        'priceInCents': price,
        'price': format_cents(price),
        'currency': unset_to_none(subscription.currency),
        'interval': None if product is None else unset_to_none(product.interval),
        'intervalUnit': None if product is None or unset_to_none(product.interval_unit) is None
        else str(product.interval_unit),
        'nextBillingAt': _iso(unset_to_none(subscription.next_assessment_at)),
        'currentPeriodEndsAt': _iso(unset_to_none(subscription.current_period_ends_at)),
        'createdAt': _iso(unset_to_none(subscription.created_at)),
    }


def enrollment_json(row: SubscriptionEnrollment) -> dict[str, Any]:
    plan = get_plan_quietly(row.plan_handle)
    return {
        'subscriptionId': row.maxio_subscription_id,
        'reference': row.reference,
        'planHandle': row.plan_handle,
        'planName': plan.name if plan else None,
        'state': row.provider_state or None,
        'outcome': row.outcome,
        'priceInCents': row.price_in_cents,
        'price': format_cents(row.price_in_cents),
        'currency': row.currency or None,
        'interval': plan.interval if plan else None,
        'intervalUnit': plan.interval_unit if plan else None,
        'nextBillingAt': _iso(row.next_billing_at),
        'outcomeUnknown': row.outcome in (Outcome.UNKNOWN, Outcome.SENDING),
    }


def get_plan_quietly(handle: str) -> Plan | None:
    """Plan details for display only; a Maxio hiccup must not fail an answer we already have."""
    try:
        return get_plan(handle)
    except BillingError:
        return None


def customer_json(row: BillingCustomer | None) -> dict[str, Any]:
    if row is None:
        return {'customerId': None, 'reference': None, 'outcome': None}
    return {'customerId': row.maxio_customer_id, 'reference': row.reference, 'outcome': row.outcome}


__all__ = [
    'InProgress', 'Plan', 'list_plans', 'get_plan', 'ensure_customer', 'get_customer', 'subscribe',
    'my_subscriptions', 'get_subscription', 'status_from_provider', 'subscription_json', 'enrollment_json',
    'customer_json',
]
