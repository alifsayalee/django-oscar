"""Business logic for Maxio-backed subscription billing.

Each capability is a separate, independently-invocable function:

* :func:`list_plans` -- plans in the configured product family.
* :func:`subscribe` -- ensure a Maxio customer (idempotent), then subscribe,
  guarded by a durable ledger row so a double-click never double-subscribes.
* :func:`list_my_subscriptions` -- the caller's subscriptions, read from Maxio.

All Maxio contract facts (signatures, model members, enum values) come from the
project's ``maxio-advanced-billing-plan.md`` contract sheet.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

import httpx
from pydantic import ValidationError

from maxio_advanced_billing.core import UNSET, ApiError
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
)
from maxio_advanced_billing.models.enums import CollectionMethod

from django.conf import settings

from .client import get_client
from .errors import MaxioError, MaxioConfigurationError, maxio_call, translate_api_error
from .models import MaxioSubscription
from .serializers import serialize_plan, serialize_subscription

# How long a "sending" claim is considered still in-flight before a later
# request treats it as stale and reconciles. Comfortably above one call's
# timeout, since the SDK does no retries.
_SEND_WINDOW = timedelta(seconds=60)

# Subscription states, mapped to our ledger outcome once a subscription exists.
_DONE_STATES = {'active', 'trialing'}
_PENDING_STATES = {'pending', 'assessing', 'awaiting_signup'}


def _present(value):
    return value is not UNSET and value is not None


# --------------------------------------------------------------------------- #
# References -- deterministic, so a retry reuses them and Maxio can dedupe.
# --------------------------------------------------------------------------- #

def customer_reference(user):
    return f'oscar-cust-u{user.pk}'


def subscription_reference(user, plan_handle):
    return f'oscar-sub-u{user.pk}-{plan_handle}'


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #

def _resolve_product_family_id():
    configured = getattr(settings, 'MAXIO_DEFAULT_PRODUCT_FAMILY', '') or ''
    if not configured:
        raise MaxioConfigurationError('MAXIO_DEFAULT_PRODUCT_FAMILY is not configured.')
    # Numeric ids are reassigned on re-seed; handles are stable. Accept either.
    if str(configured).isdigit():
        return str(configured)
    client = get_client()
    with maxio_call():
        families = client.product_families.list_product_families()
    for family in families:
        pf = family.product_family
        if _present(pf.handle) and pf.handle == configured and _present(pf.id):
            return str(pf.id)
    raise MaxioError(404, f"Product family '{configured}' was not found in Maxio.")


def list_plans():
    """List subscribable plans in the configured product family."""
    family_id = _resolve_product_family_id()
    client = get_client()
    with maxio_call():
        products = client.product_families.list_products_for_product_family(family_id)
    return [serialize_plan(p.product) for p in products if _present(p.product)]


def _find_plan(plan_handle):
    """Return the plan dict for ``plan_handle`` or raise (cross-op invariant:
    a subscribe target must be a handle Maxio actually offers)."""
    for plan in list_plans():
        if plan.get('planHandle') == plan_handle:
            return plan
    raise MaxioError(404, f"Unknown plan '{plan_handle}'.")


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

def _customer_identity(user):
    """Derive Maxio's required first/last/email from the Django user."""
    first = (user.first_name or '').strip() or 'Oscar'
    last = (user.last_name or '').strip() or (user.get_username() or 'User')
    email = (getattr(user, 'email', '') or '').strip()
    if not email:
        email = f'{user.get_username() or f"user{user.pk}"}@sandbox.invalid'
    return first, last, email


def _lookup_customer_id(reference):
    """Return the Maxio customer id for ``reference``, or None if absent."""
    client = get_client()
    try:
        response = client.customers.read_customer_by_reference(reference)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        translate_api_error(exc)
    except (ValidationError, httpx.RequestError):
        # Unreadable / no reply: cannot confirm absence.
        raise MaxioError(502, 'Could not look up the Maxio customer.', outcome_unknown=False)
    customer = response.customer
    if not _present(customer) or not _present(customer.id):
        return None
    return int(customer.id)


def ensure_customer(user):
    """Ensure a Maxio customer exists for ``user`` and return its id.

    Idempotent: looks the customer up by its deterministic reference first and
    only creates one when absent. A create/create race is closed by Maxio's own
    unique-reference constraint -- a losing create is re-looked-up.
    """
    reference = customer_reference(user)
    existing = _lookup_customer_id(reference)
    if existing is not None:
        return existing

    first, last, email = _customer_identity(user)
    client = get_client()
    try:
        with maxio_call(write=True):
            response = client.customers.create_customer(
                body=CreateCustomerRequest(
                    customer=CreateCustomer(
                        first_name=first, last_name=last, email=email,
                        reference=reference,
                    )
                )
            )
    except MaxioError as exc:
        # A rejection may mean a concurrent request already created it.
        if exc.status_code in (400, 409, 422):
            found = _lookup_customer_id(reference)
            if found is not None:
                return found
        raise

    customer = response.customer
    if not _present(customer) or not _present(customer.id):
        # A create that returned no id: it may have landed. Re-look-up.
        found = _lookup_customer_id(reference)
        if found is not None:
            return found
        raise MaxioError(502, 'Maxio did not return a customer id.', outcome_unknown=True)
    return int(customer.id)


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #

def _outcome_from_state(state):
    """Map a Maxio subscription state onto our ledger status."""
    if not _present(state):
        return MaxioSubscription.STATUS_UNKNOWN
    value = str(state)
    if value in _DONE_STATES:
        return MaxioSubscription.STATUS_DONE
    if value in _PENDING_STATES:
        return MaxioSubscription.STATUS_PENDING
    if value == 'failed_to_create':
        return MaxioSubscription.STATUS_FAILED
    # Every other state (canceled, past_due, suspended, ...) is a subscription
    # that genuinely exists; treat it as pending/live for our purposes so we
    # never try to create a second one.
    return MaxioSubscription.STATUS_PENDING


def _find_subscription(reference):
    """Reconcile: return the Maxio subscription carrying ``reference``, or None.

    Used both to adopt a duplicate and to reconcile a may-have-landed write.
    Any inability to answer (404, unreadable, no reply) returns None -- a lookup
    can prove a write landed but never that it did not.
    """
    client = get_client()
    try:
        response = client.subscriptions.find_subscription(reference=reference)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        translate_api_error(exc)
    except (ValidationError, httpx.RequestError):
        return None
    subscription = response.subscription
    if not _present(subscription) or not _present(subscription.id):
        return None
    return subscription


def _settle(claim, subscription):
    """Record the created/found subscription on the ledger row."""
    claim.maxio_subscription_id = int(subscription.id)
    claim.state = str(subscription.state) if _present(subscription.state) else ''
    claim.status = _outcome_from_state(subscription.state)
    claim.save(update_fields=['maxio_subscription_id', 'state', 'status', 'updated_at'])


def _result(claim, plan, subscription, http_status):
    payload = serialize_subscription(subscription)
    payload['status'] = claim.status
    payload['reference'] = claim.reference
    if payload.get('planHandle') is None:
        payload['planHandle'] = claim.plan_handle
    if payload.get('planName') is None and plan is not None:
        payload['planName'] = plan.get('name')
    if payload.get('priceInCents') is None and plan is not None:
        payload['priceInCents'] = plan.get('priceInCents')
    return payload, http_status


def _in_progress(claim):
    return {
        'subscriptionId': claim.maxio_subscription_id,
        'status': claim.status,
        'reference': claim.reference,
        'planHandle': claim.plan_handle,
        'message': 'A subscribe request for this plan is already being processed.',
    }, 202


def subscribe(user, plan_handle):
    """Subscribe ``user`` to ``plan_handle``. Returns ``(payload, http_status)``.

    Idempotent via a durable ledger row keyed by a deterministic reference: the
    unique constraint elects one winner that actually calls Maxio; concurrent or
    repeated requests read the outcome back off the row.
    """
    plan = _find_plan(plan_handle)  # validates the target against live plans
    reference = subscription_reference(user, plan_handle)
    now = timezone.now()

    # 1. CLAIM FIRST -- the unique constraint on `reference` is the race winner.
    try:
        with transaction.atomic():
            claim, created = MaxioSubscription.objects.get_or_create(
                reference=reference,
                defaults={
                    'user': user,
                    'plan_handle': plan_handle,
                    'status': MaxioSubscription.STATUS_SENDING,
                },
            )
    except IntegrityError:
        created = False
        claim = MaxioSubscription.objects.get(reference=reference)

    if not created:
        # A subscription already exists for this (user, plan): return it.
        if claim.maxio_subscription_id:
            found = _find_subscription(reference)
            if found is not None:
                _settle(claim, found)
                return _result(claim, plan, found, 200)
            # Ledger has an id but Maxio can't find it now (transient): echo row.
            return {
                'subscriptionId': claim.maxio_subscription_id,
                'status': claim.status,
                'reference': claim.reference,
                'planHandle': claim.plan_handle,
            }, 200
        # No id yet. A fresh "sending" row means another request is mid-flight.
        if claim.status == MaxioSubscription.STATUS_SENDING and claim.updated_at > now - _SEND_WINDOW:
            return _in_progress(claim)
        # Stale sender / unknown / failed with no id: try to adopt, else recreate.
        found = _find_subscription(reference)
        if found is not None:
            _settle(claim, found)
            return _result(claim, plan, found, 200)
        claim.status = MaxioSubscription.STATUS_SENDING
        claim.save(update_fields=['status', 'updated_at'])

    # 2. We hold the claim. Ensure the customer, then create the subscription.
    try:
        customer_id = ensure_customer(user)
    except MaxioError:
        # Customer step failed cleanly (nothing subscribed): release the claim.
        claim.status = MaxioSubscription.STATUS_FAILED
        claim.save(update_fields=['status', 'updated_at'])
        raise

    claim.maxio_customer_id = customer_id
    claim.save(update_fields=['maxio_customer_id', 'updated_at'])

    try:
        with maxio_call(write=True):
            response = get_client().subscriptions.create_subscription(
                body=CreateSubscriptionRequest(
                    subscription=CreateSubscription(
                        product_handle=plan_handle,
                        customer_id=customer_id,
                        reference=reference,
                        # Invoice (remittance) collection: the seeded plans do
                        # not require a stored payment method, so we bill by
                        # invoice rather than the default automatic card charge
                        # -- this is what lets subscribe succeed without card
                        # capture / 3-DS. Overrides the account default
                        # deliberately (python-calling-endpoints).
                        payment_collection_method=CollectionMethod.REMITTANCE,
                    )
                )
            )
        created_sub = response.subscription
    except MaxioError as exc:
        # A duplicate-reference rejection, or any landing we can confirm, is
        # adopted rather than reported as a failure.
        found = _find_subscription(reference)
        if found is not None:
            _settle(claim, found)
            return _result(claim, plan, found, 200)
        if exc.outcome_unknown:
            claim.status = MaxioSubscription.STATUS_UNKNOWN
            claim.save(update_fields=['status', 'updated_at'])
        else:
            claim.status = MaxioSubscription.STATUS_FAILED
            claim.save(update_fields=['status', 'updated_at'])
        raise

    # 3. Guard the success decode: an id back means accepted; assert it is there.
    if not _present(created_sub) or not _present(created_sub.id):
        found = _find_subscription(reference)
        if found is not None:
            _settle(claim, found)
            return _result(claim, plan, found, 201)
        claim.status = MaxioSubscription.STATUS_UNKNOWN
        claim.save(update_fields=['status', 'updated_at'])
        raise MaxioError(
            502, 'Maxio did not return a subscription id; outcome unknown.',
            outcome_unknown=True)

    # 4. SETTLE from what Maxio reported.
    _settle(claim, created_sub)
    return _result(claim, plan, created_sub, 201)


def list_my_subscriptions(user):
    """Return the caller's subscriptions, read back from Maxio (never creates)."""
    reference = customer_reference(user)
    customer_id = _lookup_customer_id(reference)
    if customer_id is None:
        return []
    client = get_client()
    with maxio_call():
        responses = client.customers.list_customer_subscriptions(customer_id)
    subscriptions = []
    for item in responses:
        subscription = item.subscription
        if _present(subscription):
            subscriptions.append(serialize_subscription(subscription))
    return subscriptions
