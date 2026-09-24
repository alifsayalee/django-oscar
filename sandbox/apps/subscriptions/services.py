"""
Subscription billing flows: list plans, ensure the Maxio customer, subscribe, read back.

Writes to Maxio are made exactly once per attempt. A write whose outcome is unknown (timeout,
provider 5xx, unreadable answer) is reconciled by looking the record up under the reference we
sent; it is never resent under a new reference, and never declared failed without an answer.
"""

from dataclasses import dataclass
from functools import partial

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
    Subscription,
)

from . import maxio
from .models import BillingCustomer, SubscriptionAttempt, WriteStatus

PLANS_CACHE_TTL = 60
PLANS_PAGE_SIZE = 200  # the provider's maximum page size
PLANS_MAX_PAGES = 10

UNRESOLVED = (WriteStatus.SENDING, WriteStatus.UNKNOWN)


class RequestInvalid(Exception):
    """The caller's request cannot be served as asked (our own validation)."""

    def __init__(self, status_code, message, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details or []


class OutcomeUnknown(maxio.ProviderError):
    """A subscribe request may or may not have taken effect; it is recorded for reconciliation."""

    def __init__(self, status_code, message, *, reference):
        super().__init__(status_code, message, outcome_unknown=True)
        self.reference = reference


@dataclass(frozen=True)
class SubscribeResult:
    subscription: Subscription
    created: bool


# Plans
# =====

def list_plans():
    """Active plans of the configured product family, as JSON-safe dicts (cached briefly)."""
    family = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    if not family:
        raise maxio.ProviderConfigError(503, 'Subscription billing is not configured.')
    cache_key = 'maxio:plans:%s' % family
    plans = cache.get(cache_key)
    if plans is None:
        plans = _fetch_plans(family)
        cache.set(cache_key, plans, PLANS_CACHE_TTL)
    return plans


def _fetch_plans(family):
    client = maxio.get_client()
    plans = []
    for page in range(1, PLANS_MAX_PAGES + 1):
        try:
            batch = maxio.read(partial(
                client.product_families.list_products_for_product_family,
                'handle:%s' % family, page=page, per_page=PLANS_PAGE_SIZE))
        except maxio.ProviderRejected as exc:
            # The family comes from our settings, so a rejection is our misconfiguration.
            raise maxio.ProviderConfigError(
                502, 'The configured product family could not be loaded.') from exc
        for item in batch:
            product = item.product
            if maxio.opt(product.archived_at) is not None or not maxio.opt(product.handle):
                continue
            plans.append(maxio.plan_payload(product))
        if len(batch) < PLANS_PAGE_SIZE:
            break
    return plans


# Customers
# =========

def ensure_customer(user):
    """Return the user's ``BillingCustomer``, creating the Maxio customer at most once."""
    row = _customer_row(user)
    if row.status == WriteStatus.DONE and row.maxio_customer_id:
        return row
    client = maxio.get_client()
    customer_id = _find_customer(client, row.reference)
    if customer_id is None:
        customer_id = _create_customer(client, row, user)
    row.maxio_customer_id = customer_id
    row.status = WriteStatus.DONE
    row.save(update_fields=['maxio_customer_id', 'status', 'date_updated'])
    return row


def _customer_row(user):
    row = BillingCustomer.objects.filter(user=user).first()
    if row is not None:
        return row
    try:
        with transaction.atomic():
            return BillingCustomer.objects.create(user=user, reference=maxio.customer_reference(user.pk))
    except IntegrityError:
        # A concurrent request for the same user created it first.
        return BillingCustomer.objects.get(user=user)


def _find_customer(client, reference):
    response = maxio.read_or_none(partial(client.customers.read_customer_by_reference, reference))
    if response is None:
        return None
    customer_id = maxio.opt(response.customer.id)
    if customer_id is None:
        raise maxio.ProviderUnreadable(502, 'The billing provider returned an incomplete customer.')
    return customer_id


def _customer_body(user, reference):
    email = (user.email or '').strip()
    if not email:
        raise RequestInvalid(422, 'Your account needs an email address before it can be billed.')
    local_part = email.split('@', 1)[0]
    return CreateCustomerRequest(customer=CreateCustomer(
        first_name=(user.first_name or '').strip() or local_part,
        last_name=(user.last_name or '').strip() or local_part,
        email=email,
        reference=reference,
    ))


def _create_customer(client, row, user):
    body = _customer_body(user, row.reference)
    row.status = WriteStatus.SENDING
    row.save(update_fields=['status', 'date_updated'])
    try:
        response = maxio.write(partial(client.customers.create_customer, body=body))
        customer_id = maxio.opt(response.customer.id)
        if customer_id is None:
            raise maxio.ProviderUnreadable(
                502, 'The billing provider returned an incomplete customer.', outcome_unknown=True)
        return customer_id
    except maxio.ProviderError as exc:
        # Customer references are unique at Maxio, so a 422 may mean an earlier or concurrent
        # attempt already created it; an unknown outcome may have landed. Look before failing.
        if exc.outcome_unknown or exc.status_code == 422:
            try:
                found = _find_customer(client, row.reference)
            except maxio.ProviderError:
                found = None
            if found is not None:
                return found
        row.status = WriteStatus.UNKNOWN if exc.outcome_unknown else WriteStatus.FAILED
        row.save(update_fields=['status', 'date_updated'])
        raise


# Subscriptions
# =============

def subscribe(user, plan_handle):
    """Subscribe ``user`` to ``plan_handle`` at most once; a repeat returns the same subscription."""
    plans = list_plans()
    if not any(plan['planHandle'] == plan_handle for plan in plans):
        raise RequestInvalid(
            422, 'Unknown planHandle.', details=['Available plans: %s' % ', '.join(
                plan['planHandle'] for plan in plans)])
    customer = ensure_customer(user)
    client = maxio.get_client()

    latest = (SubscriptionAttempt.objects
              .filter(user=user, plan_handle=plan_handle)
              .order_by('-sequence')
              .first())
    if latest is not None:
        previous = _previous_attempt_result(client, latest)
        if previous is not None:
            return previous

    sequence = latest.sequence + 1 if latest is not None else 1
    try:
        with transaction.atomic():
            row = SubscriptionAttempt.objects.create(
                user=user, plan_handle=plan_handle, sequence=sequence,
                reference=maxio.subscription_reference(user.pk, plan_handle, sequence))
    except IntegrityError:
        # A concurrent request (double-click) claimed this attempt first.
        row = SubscriptionAttempt.objects.get(user=user, plan_handle=plan_handle, sequence=sequence)
        return _reconcile(client, row, _in_progress(), created=False)

    body = CreateSubscriptionRequest(subscription=CreateSubscription(
        product_handle=plan_handle,
        customer_id=customer.maxio_customer_id,
        reference=row.reference,
        payment_collection_method=maxio.payment_collection_method(),
    ))
    try:
        response = maxio.write(partial(client.subscriptions.create_subscription, body=body))
        subscription = maxio.require_subscription(response.subscription)
    except maxio.ProviderError as exc:
        if exc.outcome_unknown:
            return _reconcile(client, row, exc, created=True)
        row.status = WriteStatus.FAILED
        row.last_error = '; '.join([exc.message] + exc.details)
        row.save(update_fields=['status', 'last_error', 'date_updated'])
        raise
    _record_success(row, subscription)
    return SubscribeResult(subscription, created=True)


def _previous_attempt_result(client, latest):
    """Answer from the user's latest attempt for the plan, or None if a new one may be made."""
    if latest.status in UNRESOLVED or (latest.status == WriteStatus.DONE and not latest.maxio_subscription_id):
        # An earlier request is in flight or its outcome is unknown: look, never create.
        return _reconcile(client, latest, _in_progress(), created=False)
    if latest.status != WriteStatus.DONE:
        return None  # it failed: nothing exists at Maxio
    existing = _read_subscription(client, latest.maxio_subscription_id)
    if existing is None:
        return None  # gone at Maxio
    _record_success(latest, existing)
    outcome = maxio.status_from_provider(existing.state)
    if outcome in maxio.LIVE_OUTCOMES:
        return SubscribeResult(existing, created=False)
    if outcome == maxio.UNKNOWN:
        raise RequestInvalid(409, 'Your existing subscription to this plan is in a state that needs review.')
    return None  # ended: a new subscription may be created


def _in_progress():
    return maxio.ProviderError(202, 'A subscription request for this plan is still being processed.')


def _find_subscription(client, reference):
    response = maxio.read_or_none(partial(client.subscriptions.find_subscription, reference=reference))
    return None if response is None else maxio.require_subscription(response.subscription)


def _read_subscription(client, subscription_id):
    response = maxio.read_or_none(partial(client.subscriptions.read_subscription, subscription_id))
    return None if response is None else maxio.require_subscription(response.subscription)


def _reconcile(client, row, failure, *, created):
    """The outcome of ``row``'s write is unknown: ask Maxio by the reference we sent."""
    try:
        found = _find_subscription(client, row.reference)
    except maxio.ProviderError:
        found = None  # the check itself failed: still unknown
    if found is not None:
        _record_success(row, found)
        return SubscribeResult(found, created=created)
    # Not found is not "never happened": a timed-out write can still land. Only move rows that
    # are still unresolved, so a concurrent request that got its answer is never overwritten.
    (SubscriptionAttempt.objects
     .filter(pk=row.pk, status__in=UNRESOLVED)
     .update(status=WriteStatus.UNKNOWN, last_error=failure.message))
    if failure.status_code == 202:
        raise OutcomeUnknown(202, failure.message, reference=row.reference)
    raise OutcomeUnknown(
        failure.status_code,
        'The subscription request may not have completed. It has been recorded and will be '
        'reconciled; do not resubmit it as a different request.',
        reference=row.reference)


def _record_success(row, subscription):
    row.status = WriteStatus.DONE
    row.maxio_subscription_id = maxio.opt(subscription.id)
    row.provider_state = str(maxio.opt(subscription.state) or '')
    row.last_error = ''
    row.save(update_fields=['status', 'maxio_subscription_id', 'provider_state', 'last_error', 'date_updated'])


# Reading back
# ============

def list_my_subscriptions(user):
    """The user's subscriptions as Maxio holds them, plus attempts whose outcome is unresolved."""
    customer = BillingCustomer.objects.filter(
        user=user, status=WriteStatus.DONE, maxio_customer_id__isnull=False).first()
    subscriptions = []
    if customer is not None:
        client = maxio.get_client()
        responses = maxio.read(partial(
            client.customers.list_customer_subscriptions, customer.maxio_customer_id))
        subscriptions = [maxio.require_subscription(response.subscription) for response in responses]
        _reconcile_attempts(user, subscriptions)
    unresolved = list(user.subscription_attempts.filter(status__in=UNRESOLVED))
    return subscriptions, unresolved


def _reconcile_attempts(user, subscriptions):
    by_reference = {maxio.opt(s.reference): s for s in subscriptions if maxio.opt(s.reference)}
    by_id = {maxio.opt(s.id): s for s in subscriptions}
    for attempt in user.subscription_attempts.exclude(status=WriteStatus.FAILED):
        subscription = by_reference.get(attempt.reference) or by_id.get(attempt.maxio_subscription_id)
        if subscription is not None:
            state = str(maxio.opt(subscription.state) or '')
            if attempt.status != WriteStatus.DONE or attempt.provider_state != state:
                _record_success(attempt, subscription)


def get_subscription(user, subscription_id):
    """One subscription, only if it belongs to the user's Maxio customer; otherwise None."""
    customer = BillingCustomer.objects.filter(
        user=user, status=WriteStatus.DONE, maxio_customer_id__isnull=False).first()
    if customer is None:
        return None
    subscription = _read_subscription(maxio.get_client(), subscription_id)
    if subscription is None or maxio.subscription_customer_id(subscription) != customer.maxio_customer_id:
        return None
    return subscription
