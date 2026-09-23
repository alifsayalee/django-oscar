"""Business logic for the Maxio subscriptions integration.

Every Maxio SDK call goes through the error ladder here (``_provider_call`` /
``_translate_api``), so the views see one failure vocabulary (``exceptions``). The subscribe
flow is the durable-claim pattern from python-configuration-resilience: claim a local row
first, then call the provider, then reconcile a may-have-landed write by the reference we
sent. Contract facts (signatures, members, enums) come from maxio-advanced-billing-plan.md.
"""

import contextlib
from decimal import Decimal

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from pydantic import ValidationError

from maxio_advanced_billing.core import ApiError, RawError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
)
from maxio_advanced_billing.models.enums import CollectionMethod

from .exceptions import (
    MaxioConfigError,
    MaxioError,
    MaxioRejected,
    MaxioUnavailable,
    MaxioUnreadable,
    OutcomeUnknown,
    PlanNotFound,
)
from .maxio_client import get_client
from .models import SubscriptionIntent

# Transport failures that happened before the request left the client: nothing can have
# landed (python-error-handling). Everything else in httpx.RequestError may have landed.
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

# Resolved once per process: MAXIO_DEFAULT_PRODUCT_FAMILY may be a handle or a numeric id.
_family_id_cache: dict[str, int] = {}


# --------------------------------------------------------------------------- helpers


def _v(value):
    """Normalise an SDK member to a plain value: UNSET/None both become None."""
    if value is None or isinstance(value, UnsetType):
        return None
    return value


def _s(value):
    """String (wire) value of an open enum member, or None."""
    value = _v(value)
    return str(value) if value is not None else None


def _dt(value):
    """ISO-8601 string for an RFC3339DateTime member, or None."""
    value = _v(value)
    return value.isoformat() if value is not None else None


def _format_money(cents):
    """Format integer cents as a USD display string (site currency is USD)."""
    if cents is None:
        return None
    amount = (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))
    return f"${amount}"


def _error_message(exc):
    """Best-effort human message from an ApiError's decoded body (no secrets)."""
    err = exc.error
    if isinstance(err, str):
        return err[:300]
    errors = getattr(err, "errors", None)
    if errors:
        try:
            return "; ".join(str(x) for x in errors)[:300]
        except Exception:
            pass
    if isinstance(err, RawError):
        try:
            return (err.text() or f"HTTP {exc.status_code}")[:300]
        except Exception:
            return f"HTTP {exc.status_code}"
    return (str(err) or f"HTTP {exc.status_code}")[:300]


def _translate_api(exc):
    """Map an ApiError onto a domain exception and raise it (never returns)."""
    status = exc.status_code
    if status in (401, 403):
        raise MaxioConfigError("Subscription provider rejected our credentials.") from exc
    if status == 429:
        raise MaxioUnavailable(
            "Subscription provider is rate limiting requests.", status_code=503
        ) from exc
    if status in (400, 404, 409, 422):
        raise MaxioRejected(_error_message(exc), status_code=status) from exc
    raise MaxioUnavailable("Subscription provider error.", status_code=502) from exc


@contextlib.contextmanager
def _provider_call():
    """Wrap a single SDK call, converting every failure kind to a domain exception."""
    try:
        yield
    except ApiError as exc:
        _translate_api(exc)
    except ValidationError as exc:
        raise MaxioUnreadable("Unreadable response from subscription provider.") from exc
    except _NEVER_SENT as exc:
        raise MaxioUnavailable(
            "Subscription provider is unreachable.", status_code=502, outcome_unknown=False
        ) from exc
    except httpx.RequestError as exc:
        raise MaxioUnavailable(
            "No response from subscription provider.", status_code=504, outcome_unknown=True
        ) from exc


def status_from_provider(state):
    """Map a Maxio subscription state to a create-outcome (open enum: default unknown)."""
    wire = _s(state)
    if wire in ("active", "trialing"):
        return "done"
    if wire in ("pending", "assessing", "awaiting_signup"):
        return "pending"
    if wire in ("failed_to_create", "canceled", "expired"):
        return "failed"
    return "unknown"


# --------------------------------------------------------------------------- plans


def _resolve_family_id(client):
    key = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    if key in _family_id_cache:
        return _family_id_cache[key]
    if str(key).isdigit():
        family_id = int(key)
    else:
        with _provider_call():
            families = client.product_families.list_product_families()
        family_id = None
        for wrapper in families:
            family = wrapper.product_family
            if _v(family.handle) == key or str(_v(family.id)) == str(key):
                family_id = _v(family.id)
                break
        if family_id is None:
            raise MaxioConfigError(f"Configured product family '{key}' was not found.")
    _family_id_cache[key] = family_id
    return family_id


def _list_products(client):
    family_id = _resolve_family_id(client)
    with _provider_call():
        return client.product_families.list_products_for_product_family(str(family_id))


def _plan_dict(product):
    cents = _v(product.price_in_cents)
    return {
        "planHandle": _v(product.handle),
        "productId": _v(product.id),
        "name": _v(product.name),
        "priceInCents": cents,
        "priceFormatted": _format_money(cents),
        "currency": "USD",
        "interval": _v(product.interval),
        "intervalUnit": _s(product.interval_unit),
    }


def list_plans():
    """List the subscribe-able plans (products) of the configured product family."""
    client = get_client()
    products = _list_products(client)
    return [_plan_dict(p.product) for p in products if _v(p.product.handle)]


def _find_plan(client, plan_handle):
    for wrapper in _list_products(client):
        if _v(wrapper.product.handle) == plan_handle:
            return wrapper.product
    raise PlanNotFound(f"Unknown plan '{plan_handle}'.")


# --------------------------------------------------------------------------- customer


def _customer_reference(user):
    return f"oscar-user-{user.pk}"


def _customer_names(user):
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    if not first:
        first = (user.get_username() or "Customer").split("@")[0]
    if not last:
        last = "Subscriber"
    return first, last


def _lookup_customer_id(client, reference):
    """Return the Maxio customer id for a reference, or None if no such customer."""
    try:
        response = client.customers.read_customer_by_reference(reference)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        _translate_api(exc)
    except ValidationError as exc:
        raise MaxioUnreadable("Unreadable response from subscription provider.") from exc
    except _NEVER_SENT as exc:
        raise MaxioUnavailable(
            "Subscription provider is unreachable.", status_code=502, outcome_unknown=False
        ) from exc
    except httpx.RequestError as exc:
        raise MaxioUnavailable(
            "No response from subscription provider.", status_code=504, outcome_unknown=True
        ) from exc
    return _v(response.customer.id)


def ensure_customer(client, user):
    """Idempotently ensure a Maxio customer exists for this user; return its id.

    Keyed by a deterministic reference, so a repeat call (or a concurrent create that
    already landed) resolves to the same customer rather than a duplicate.
    """
    reference = _customer_reference(user)
    existing = _lookup_customer_id(client, reference)
    if existing is not None:
        return existing

    first, last = _customer_names(user)
    email = (getattr(user, "email", "") or "").strip() or f"user-{user.pk}@example.invalid"
    body = CreateCustomerRequest(
        customer=CreateCustomer(
            first_name=first, last_name=last, email=email, reference=reference
        )
    )
    try:
        with _provider_call():
            response = client.customers.create_customer(body=body)
    except MaxioRejected as exc:
        # A concurrent create under the same reference may have landed first.
        if exc.status_code == 422:
            again = _lookup_customer_id(client, reference)
            if again is not None:
                return again
        raise
    customer_id = _v(response.customer.id)
    if customer_id is None:
        raise MaxioUnreadable("Customer was created but the provider returned no id.")
    return customer_id


# --------------------------------------------------------------------------- subscribe


def _claim(user, plan_handle):
    """Acquire the single live claim for (user, plan). Returns (intent, created)."""
    existing = (
        SubscriptionIntent.objects.filter(user=user, plan_handle=plan_handle)
        .exclude(status=SubscriptionIntent.STATUS_FAILED)
        .first()
    )
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            intent = SubscriptionIntent.objects.create(
                user=user, plan_handle=plan_handle, status=SubscriptionIntent.STATUS_PENDING
            )
    except IntegrityError:
        # Lost the race: the partial-unique constraint rejected our insert.
        existing = (
            SubscriptionIntent.objects.filter(user=user, plan_handle=plan_handle)
            .exclude(status=SubscriptionIntent.STATUS_FAILED)
            .first()
        )
        if existing is None:
            raise MaxioUnavailable("Could not acquire a subscription claim; try again.")
        return existing, False
    intent.reference = f"oscar-sub-{user.pk}-{plan_handle}-{intent.pk}"
    intent.save(update_fields=["reference", "updated_at"])
    return intent, True


def _mark(intent, status):
    intent.status = status
    intent.save(update_fields=["status", "updated_at"])


def _find_subscription_by_reference(client, reference):
    """Best-effort lookup of a subscription by the reference we sent; None if unconfirmed."""
    if not reference:
        return None
    try:
        response = client.subscriptions.find_subscription(reference=reference)
    except (ApiError, httpx.HTTPError, ValidationError, ValueError):
        return None
    subscription = _v(response.subscription)
    if subscription is None or _v(subscription.id) is None:
        return None
    return subscription


def _read_subscription(client, subscription_id):
    if not subscription_id:
        return None
    try:
        with _provider_call():
            response = client.subscriptions.read_subscription(int(subscription_id))
        return _v(response.subscription)
    except MaxioError:
        return None


def _settle(intent, customer_id, subscription):
    if customer_id is not None:
        intent.maxio_customer_id = customer_id
    subscription_id = _v(subscription.id) if subscription is not None else None
    if subscription_id is not None:
        intent.maxio_subscription_id = int(subscription_id)
    outcome = status_from_provider(subscription.state if subscription is not None else None)
    intent.status = (
        SubscriptionIntent.STATUS_FAILED
        if outcome == "failed"
        else SubscriptionIntent.STATUS_DONE
    )
    intent.save(
        update_fields=[
            "maxio_customer_id",
            "maxio_subscription_id",
            "status",
            "updated_at",
        ]
    )


def _find_live_subscription_for_product(client, customer_id, plan_handle):
    """A live (active/trialing/pending) subscription of this customer to this product, if any."""
    try:
        with _provider_call():
            subscriptions = client.customers.list_customer_subscriptions(int(customer_id))
    except MaxioError:
        # Best effort: the durable claim still guards concurrent double-clicks.
        return None
    for wrapper in subscriptions:
        subscription = _v(wrapper.subscription)
        if subscription is None:
            continue
        product = _v(subscription.product)
        handle = _v(product.handle) if product is not None else None
        if handle == plan_handle and status_from_provider(subscription.state) in ("done", "pending"):
            return subscription
    return None


def _create_or_reconcile(client, intent, customer_id, plan_handle):
    # Defense in depth: within the single-flight local claim, adopt an existing live
    # subscription of this customer to this product rather than creating a duplicate. This
    # covers the case where Maxio holds a subscription our local records do not (a reset
    # database, or a prior attempt that failed for us but landed there).
    adopted = _find_live_subscription_for_product(client, customer_id, plan_handle)
    if adopted is not None:
        return adopted

    body = CreateSubscriptionRequest(
        subscription=CreateSubscription(
            product_handle=plan_handle,
            customer_id=int(customer_id),
            reference=intent.reference,
            # Bill by remittance (invoice) instead of an automatic card charge, so the
            # subscription can be created without a payment method on file -- which is the
            # point of these "payment method not required" plans.
            payment_collection_method=CollectionMethod.REMITTANCE,
        )
    )
    try:
        with _provider_call():
            response = client.subscriptions.create_subscription(body=body)
    except MaxioRejected:
        # A 4xx: an earlier attempt under this reference may already have landed.
        found = _find_subscription_by_reference(client, intent.reference)
        if found is not None:
            return found
        _mark(intent, SubscriptionIntent.STATUS_FAILED)
        raise
    except MaxioUnreadable:
        found = _find_subscription_by_reference(client, intent.reference)
        if found is not None:
            return found
        _mark(intent, SubscriptionIntent.STATUS_UNKNOWN)
        raise OutcomeUnknown("Subscription outcome could not be confirmed.")
    except MaxioUnavailable as exc:
        if exc.outcome_unknown:
            found = _find_subscription_by_reference(client, intent.reference)
            if found is not None:
                return found
            _mark(intent, SubscriptionIntent.STATUS_UNKNOWN)
            raise OutcomeUnknown("Subscription outcome could not be confirmed.")
        _mark(intent, SubscriptionIntent.STATUS_FAILED)
        raise

    subscription = _v(response.subscription)
    if subscription is None or _v(subscription.id) is None:
        found = _find_subscription_by_reference(client, intent.reference)
        if found is not None:
            return found
        _mark(intent, SubscriptionIntent.STATUS_UNKNOWN)
        raise MaxioUnreadable("Subscription was accepted but no id was returned.")
    return subscription


def _subscribe_result(intent, plan, subscription, idempotent):
    cents = _v(plan.price_in_cents)
    state = _s(subscription.state) if subscription is not None else None
    return {
        "subscriptionId": intent.maxio_subscription_id,
        "reference": intent.reference,
        "planHandle": _v(plan.handle),
        "planName": _v(plan.name),
        "priceInCents": cents,
        "priceFormatted": _format_money(cents),
        "currency": "USD",
        "interval": _v(plan.interval),
        "intervalUnit": _s(plan.interval_unit),
        "state": state,
        "nextBillingAt": _dt(subscription.next_assessment_at) if subscription is not None else None,
        "currentPeriodEndsAt": (
            _dt(subscription.current_period_ends_at) if subscription is not None else None
        ),
        "customerId": intent.maxio_customer_id,
        "idempotent": idempotent,
    }


def subscribe(user, plan_handle):
    """Subscribe ``user`` to ``plan_handle`` (idempotent). Returns a result dict.

    ``subscriptionId`` is a top-level field of the result.
    """
    client = get_client()
    plan = _find_plan(client, plan_handle)  # PlanNotFound if the handle is not a family product
    intent, created = _claim(user, plan_handle)

    if not created:
        if intent.status == SubscriptionIntent.STATUS_DONE:
            subscription = _read_subscription(client, intent.maxio_subscription_id)
            return _subscribe_result(intent, plan, subscription, idempotent=True)
        # A pending/unknown/needs-review row: try to confirm the earlier attempt landed.
        found = _find_subscription_by_reference(client, intent.reference)
        if found is not None:
            _settle(intent, intent.maxio_customer_id, found)
            return _subscribe_result(intent, plan, found, idempotent=True)
        if intent.status != SubscriptionIntent.STATUS_PENDING:
            raise OutcomeUnknown(
                "A previous subscribe attempt is unresolved; please contact support."
            )
        # PENDING and nothing landed: resume the create below under the same reference.

    customer_id = intent.maxio_customer_id or ensure_customer(client, user)
    if intent.maxio_customer_id != customer_id:
        intent.maxio_customer_id = customer_id
        intent.save(update_fields=["maxio_customer_id", "updated_at"])

    subscription = _create_or_reconcile(client, intent, customer_id, plan_handle)
    _settle(intent, customer_id, subscription)
    return _subscribe_result(intent, plan, subscription, idempotent=False)


# --------------------------------------------------------------------------- my subs


def _subscription_summary(subscription):
    product = _v(subscription.product)
    cents = _v(product.price_in_cents) if product is not None else None
    return {
        "subscriptionId": _v(subscription.id),
        "planHandle": _v(product.handle) if product is not None else None,
        "planName": _v(product.name) if product is not None else None,
        "priceInCents": cents,
        "priceFormatted": _format_money(cents),
        "currency": "USD",
        "state": _s(subscription.state),
        "nextBillingAt": _dt(subscription.next_assessment_at),
        "currentPeriodEndsAt": _dt(subscription.current_period_ends_at),
    }


def list_my_subscriptions(user):
    """Return the caller's subscriptions (empty list if they have no Maxio customer yet)."""
    client = get_client()
    customer_id = _lookup_customer_id(client, _customer_reference(user))
    if customer_id is None:
        return []
    with _provider_call():
        subscriptions = client.customers.list_customer_subscriptions(int(customer_id))
    return [
        _subscription_summary(_v(s.subscription))
        for s in subscriptions
        if _v(s.subscription) is not None
    ]
