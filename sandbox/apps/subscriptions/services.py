"""Subscription business logic — the only module that talks to Maxio.

Every flow the API exposes is a function here; each Maxio call is grounded in the contract sheet
in ``maxio-advanced-billing-plan.md``.  The subscribe flow follows the durable-claim pattern from
``python-configuration-resilience``: claim a local row before the provider call and reconcile by
the reference we send.
"""

import threading

from django.conf import settings
from django.db import IntegrityError, transaction

from maxio_advanced_billing.core import ApiError, Success
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
)

from .client import get_client, translate_errors
from .exceptions import ProviderError, ProviderRejected, ProviderUnreadable
from .models import ClaimStatus, MaxioCustomer, MaxioSubscription
from .serializers import (
    _present,
    is_live,
    outcome_from_state,
    serialize_plan,
    serialize_subscription,
    state_value,
)

DEFAULT_PLAN_HANDLE = "eshop-pro"

# Cache of product-family handle -> numeric id (the family-products endpoint needs the id, not the
# handle). Small and process-local; the mapping is stable for the life of the process.
_family_id_cache: dict[str, int] = {}
_family_lock = threading.Lock()


def _family_handle():
    return getattr(settings, "MAXIO_DEFAULT_PRODUCT_FAMILY", "") or ""


def _resolve_family_id(client, handle):
    if handle in _family_id_cache:
        return _family_id_cache[handle]
    with _family_lock:
        if handle in _family_id_cache:
            return _family_id_cache[handle]
        with translate_errors(writes=False):
            families = client.product_families.list_product_families()
        for f in families:
            fam = _present(f.product_family)
            if fam is not None and _present(fam.handle) == handle:
                _family_id_cache[handle] = fam.id
                return fam.id
        raise ProviderRejected(
            f"product family '{handle}' not found on the Maxio site",
            status_code=502,  # our configuration names a family the site does not have
        )


# --- Plans -----------------------------------------------------------------

def list_plans():
    """List active plans for the configured product family (``GET /api/subscription-plans``)."""
    handle = _family_handle()
    if not handle:
        raise ProviderRejected(
            "MAXIO_DEFAULT_PRODUCT_FAMILY is not configured", status_code=502
        )
    client = get_client()
    family_id = _resolve_family_id(client, handle)
    with translate_errors(writes=False):
        products = client.product_families.list_products_for_product_family(
            str(family_id), per_page=200
        )
    plans = []
    for p in products:
        product = _present(p.product)
        if product is None:
            continue
        if _present(product.archived_at) is not None:  # skip archived plans
            continue
        plans.append(serialize_plan(product))
    return plans


# --- Customers -------------------------------------------------------------

def ensure_customer(user):
    """Idempotently ensure a Maxio customer exists for ``user``; return the ``MaxioCustomer`` row.

    The local row (unique on user) is the claim; the Maxio ``reference`` is the cross-process guard.
    """
    reference = f"oscar-user-{user.pk}"
    row, _created = MaxioCustomer.objects.get_or_create(
        user=user, defaults={"reference": reference}
    )
    if row.customer_id:
        return row

    client = get_client()
    customer = _lookup_customer_by_reference(client, row.reference)
    if customer is None:
        customer = _create_customer(client, user, row.reference)

    row.customer_id = customer.id
    row.save(update_fields=["customer_id", "updated_at"])
    return row


def _lookup_customer_by_reference(client, reference):
    """Return the Maxio customer for a reference, or ``None`` when it does not exist (404)."""
    with translate_errors(writes=False):
        result = client.customers.with_raw_response.read_customer_by_reference(reference)
        if isinstance(result, Success):
            return _present(result.payload.customer)
        if result.response.status_code == 404:
            return None
        result.unwrap()  # any other status: raise -> translated by the context manager


def _create_customer(client, user, reference):
    first = (getattr(user, "first_name", "") or "").strip() or "Oscar"
    last = (getattr(user, "last_name", "") or "").strip() or "Shopper"
    email = (getattr(user, "email", "") or "").strip() or f"user-{user.pk}@example.invalid"
    body = CreateCustomerRequest(
        customer=CreateCustomer(
            first_name=first, last_name=last, email=email, reference=reference
        )
    )
    try:
        with translate_errors(writes=True):
            resp = client.customers.create_customer(body=body)
    except ProviderRejected:
        # A 422 here is most likely "reference already used" — an earlier attempt landed. Look it
        # up and keep it rather than treating a landing as a failure.
        existing = _lookup_customer_by_reference(client, reference)
        if existing is not None:
            return existing
        raise
    customer = _present(resp.customer)
    if customer is None or _present(customer.id) is None:
        raise ProviderUnreadable(
            "Maxio create_customer returned no customer id", status_code=502
        )
    return customer


# --- Subscriptions ---------------------------------------------------------

def subscribe(user, plan_handle=None):
    """Subscribe ``user`` to a plan, idempotently. Returns ``(claim, subscription_or_none)``.

    A second concurrent request for the same plan loses the DB claim and never calls
    ``create_subscription``; it reconciles the winner's outcome instead.
    """
    plan_handle = (plan_handle or DEFAULT_PLAN_HANDLE).strip()

    # Cross-operation invariant: the plan must be one the plans endpoint returns.
    available = {p["planHandle"] for p in list_plans()}
    if plan_handle not in available:
        raise ProviderRejected(
            f"unknown plan '{plan_handle}'",
            status_code=400,
            detail="planHandle must be one returned by GET /api/subscription-plans",
        )

    customer = ensure_customer(user)
    reference = f"oscar-sub-{user.pk}-{plan_handle}"

    won, claim = _claim(user, plan_handle, reference, customer.customer_id)
    if not won:
        return _reconcile_existing_claim(claim)

    client = get_client()
    try:
        # Provider-side guard: a subscription already created for this reference or plan.
        existing = _find_subscription_by_reference(client, reference)
        if existing is not None and is_live(existing.state):
            return _settle(claim, existing)
        live_for_plan = _find_live_subscription_for_plan(client, customer.customer_id, plan_handle)
        if live_for_plan is not None:
            return _settle(claim, live_for_plan)

        body = CreateSubscriptionRequest(
            subscription=CreateSubscription(
                product_handle=plan_handle,
                customer_id=customer.customer_id,
                reference=reference,
            )
        )
        with translate_errors(writes=True):
            resp = client.subscriptions.create_subscription(body=body)
        subscription = _present(resp.subscription)
        if subscription is None or _present(subscription.id) is None:
            raise ProviderUnreadable(
                "Maxio create_subscription returned no subscription id",
                status_code=504,
                outcome_unknown=True,
            )
        return _settle(claim, subscription)
    except ProviderError as e:
        # A landing may have happened (duplicate-reference 422, 5xx, timeout, decode failure) —
        # reconcile by the reference we sent before deciding the row's fate.
        found = None
        try:
            found = _find_subscription_by_reference(client, reference)
        except ProviderError:
            found = None
        if found is not None:
            return _settle(claim, found)
        _finalize_failed_claim(claim, unknown=e.outcome_unknown)
        raise


def _claim(user, plan_handle, reference, customer_id):
    """Insert the claim row. Returns ``(won, claim)``; ``won`` is False if one already exists."""
    try:
        with transaction.atomic():
            claim = MaxioSubscription.objects.create(
                user=user,
                plan_handle=plan_handle,
                reference=reference,
                customer_id=customer_id,
                status=ClaimStatus.SENDING,
            )
        return True, claim
    except IntegrityError:
        claim = (
            MaxioSubscription.objects.filter(user=user, plan_handle=plan_handle)
            .exclude(status=ClaimStatus.FAILED)
            .order_by("-created_at")
            .first()
        )
        return False, claim


def _reconcile_existing_claim(claim):
    """Resolve the outcome of a claim we did not win, without creating anything."""
    client = get_client()
    if claim.subscription_id:
        sub = _read_subscription(client, claim.subscription_id)
        if sub is not None:
            return _settle(claim, sub)
    found = _find_subscription_by_reference(client, claim.reference)
    if found is not None:
        return _settle(claim, found)
    # Still in flight or genuinely unknown — return the claim without a subscription.
    return claim, None


def list_my_subscriptions(user):
    """List the user's Maxio subscriptions (``GET /api/my-subscriptions``)."""
    row = MaxioCustomer.objects.filter(user=user).first()
    if row is None or not row.customer_id:
        return []
    client = get_client()
    with translate_errors(writes=False):
        subs = client.customers.list_customer_subscriptions(row.customer_id)
    out = []
    for item in subs:
        subscription = _present(item.subscription)
        if subscription is not None:
            out.append(serialize_subscription(subscription))
    return out


# --- Maxio read helpers ----------------------------------------------------

def _find_subscription_by_reference(client, reference):
    with translate_errors(writes=False):
        result = client.subscriptions.with_raw_response.find_subscription(reference=reference)
        if isinstance(result, Success):
            return _present(result.payload.subscription)
        if result.response.status_code == 404:
            return None
        result.unwrap()


def _read_subscription(client, subscription_id):
    with translate_errors(writes=False):
        result = client.subscriptions.with_raw_response.read_subscription(subscription_id)
        if isinstance(result, Success):
            return _present(result.payload.subscription)
        if result.response.status_code == 404:
            return None
        result.unwrap()


def _find_live_subscription_for_plan(client, customer_id, plan_handle):
    with translate_errors(writes=False):
        subs = client.customers.list_customer_subscriptions(customer_id)
    for item in subs:
        subscription = _present(item.subscription)
        if subscription is None:
            continue
        product = _present(subscription.product)
        handle = _present(product.handle) if product is not None else None
        if handle == plan_handle and is_live(subscription.state):
            return subscription
    return None


def _settle(claim, subscription):
    """Record the provider's outcome on the claim row from the returned state (not from 'an id came
    back'), and return ``(claim, subscription)``."""
    claim.subscription_id = _present(subscription.id)
    claim.maxio_state = state_value(subscription.state)
    claim.status = outcome_from_state(subscription.state)
    claim.save(update_fields=["subscription_id", "maxio_state", "status", "updated_at"])
    return claim, subscription


def _finalize_failed_claim(claim, *, unknown):
    claim.status = ClaimStatus.UNKNOWN if unknown else ClaimStatus.FAILED
    claim.save(update_fields=["status", "updated_at"])
