"""Business logic for the Maxio subscription capability.

This is the ONLY module that talks to the Maxio SDK. It exposes four separately-invocable
operations used by the views:

- ``list_plans()``                    — the plans available in the configured product family.
- ``ensure_customer(user)``           — idempotently ensure a Maxio customer for a Django user.
- ``subscribe(user, plan_handle)``    — idempotently enroll a user on a plan.
- ``list_my_subscriptions(user)``     — the user's subscriptions.

Maxio is the system of record: no local Django models mirror its customers or subscriptions.
Idempotency is achieved with a stable per-user customer reference and by checking existing
subscriptions in Maxio before creating a new one.

Every Maxio SDK failure is translated here into a ``BillingError`` subclass so that the view
layer only ever deals with our own exception types.
"""

import logging

import httpx
from django.conf import settings
from pydantic import ValidationError

from maxio_advanced_billing.core import UNSET, ApiError

from . import client as maxio_client
from .exceptions import PlanNotFound, ProviderRejected, ProviderUnavailable

logger = logging.getLogger('oscar')

# Subscription states that mean "the customer is already enrolled on this plan", so a repeat
# subscribe (e.g. a double-click) should return the existing subscription rather than create a
# second one. Everything else (canceled/expired/failed) is treated as not currently enrolled.
_TERMINAL_STATES = {'canceled', 'cancelled', 'expired', 'failed_to_create', 'trial_ended'}


def _customer_reference(user):
    """A stable, unique-per-user reference used to make customer creation idempotent."""
    return f'oscar-user-{user.pk}'


def _value(member, default=None):
    """Resolve an SDK model member to a plain Python value, treating UNSET as absent."""
    if member is UNSET or member is None:
        return default
    return member


def _enum_str(member, default=None):
    """Render an open-enum member (which may be a real Enum or a plain str) as its wire value."""
    if member is UNSET or member is None:
        return default
    return str(member)


def _isoformat(member):
    """Render an optional RFC3339 datetime member as an ISO-8601 string, or None."""
    value = _value(member)
    if value is None:
        return None
    return value.isoformat()


# ---------------------------------------------------------------------------
# Error boundary
# ---------------------------------------------------------------------------

def _raise_from_api_error(exc, *, operation):
    """Translate a Maxio ``ApiError`` into one of our domain exceptions."""
    status = exc.status_code
    # A provider 4xx (other than not-found, handled by callers) is actionable by our caller and
    # is surfaced as a client error carrying the provider's own message where we can read one.
    if 400 <= status < 500:
        detail = _describe_api_error(exc)
        logger.warning('Maxio %s rejected request (HTTP %s): %s', operation, status, detail)
        raise ProviderRejected(
            f'The billing provider rejected the request: {detail}', status_code=422
        ) from exc
    logger.error('Maxio %s failed (HTTP %s)', operation, status)
    raise ProviderUnavailable(
        'The billing provider returned an unexpected error.', status_code=502
    ) from exc


def _describe_api_error(exc):
    """Best-effort human-readable detail from an ApiError without leaking internals."""
    error = exc.error
    # RawError is the catch-all arm; prefer its text, falling back to the type name.
    text = getattr(error, 'text', None)
    if callable(text):
        try:
            body = error.text()
            if body:
                return body[:500]
        except Exception:  # pragma: no cover - defensive
            pass
    # Typed error bodies (e.g. ErrorListResponse1 / CustomerErrorResponse1) expose ``errors``.
    errors = getattr(error, 'errors', None)
    if errors:
        return '; '.join(str(e) for e in errors)[:500]
    return f'HTTP {exc.status_code}'


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def _resolve_family_id(client):
    """Resolve the configured product family (by handle) to its numeric id."""
    family_handle = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    try:
        families = client.product_families.list_product_families()
    except ApiError as exc:
        _raise_from_api_error(exc, operation='list_product_families')
    except (httpx.HTTPError, ValidationError) as exc:
        raise ProviderUnavailable() from exc

    for wrapper in families:
        family = wrapper.product_family
        if _value(family.handle) == family_handle:
            family_id = _value(family.id)
            if family_id is None:
                raise ProviderUnavailable('Product family is missing an id; outcome unknown.')
            return family_id
    raise PlanNotFound(
        f'Configured product family {family_handle!r} was not found in Maxio.', status_code=500
    )


def _plan_from_product(product):
    price_in_cents = _value(product.price_in_cents)
    return {
        'planHandle': _value(product.handle),
        'name': _value(product.name),
        'description': _value(product.description),
        'priceInCents': price_in_cents,
        'priceFormatted': _format_price(price_in_cents),
        'currency': 'USD',
        'interval': _value(product.interval),
        'intervalUnit': _enum_str(product.interval_unit),
    }


def _format_price(price_in_cents):
    if price_in_cents is None:
        return None
    return f'{price_in_cents / 100:.2f}'


def list_plans():
    """Return the list of available plans in the configured product family."""
    client = maxio_client.get_client()
    family_id = _resolve_family_id(client)
    try:
        products = client.product_families.list_products_for_product_family(
            str(family_id), per_page=200
        )
    except ApiError as exc:
        _raise_from_api_error(exc, operation='list_products_for_product_family')
    except (httpx.HTTPError, ValidationError) as exc:
        raise ProviderUnavailable() from exc

    plans = [_plan_from_product(w.product) for w in products]
    # Only surface plans that actually carry a handle (the caller subscribes by handle).
    return [p for p in plans if p['planHandle']]


def get_plan(plan_handle):
    """Return a single plan dict by handle, or raise PlanNotFound."""
    for plan in list_plans():
        if plan['planHandle'] == plan_handle:
            return plan
    raise PlanNotFound(f'Plan {plan_handle!r} is not available.')


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def _customer_attributes(user):
    """Build the CreateCustomer attributes from a Django user, with sensible fallbacks.

    Maxio requires first_name, last_name and email; Oscar users may have blanks, so we derive
    reasonable defaults rather than sending empty strings.
    """
    email = (user.email or '').strip() or f'{user.get_username()}@example.invalid'
    first_name = (user.first_name or '').strip() or user.get_username()
    last_name = (user.last_name or '').strip() or 'Subscriber'
    return {
        'first_name': first_name,
        'last_name': last_name,
        'email': email,
        'reference': _customer_reference(user),
    }


def _find_customer(client, reference):
    """Return the Customer for a reference, or None if Maxio has no such customer (404)."""
    try:
        response = client.customers.read_customer_by_reference(reference)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        _raise_from_api_error(exc, operation='read_customer_by_reference')
    except (httpx.HTTPError, ValidationError) as exc:
        raise ProviderUnavailable() from exc
    return response.customer


def ensure_customer(user):
    """Idempotently ensure a Maxio customer exists for ``user``; return its numeric id.

    Looks the customer up by its stable reference first, so repeated calls (and a double-click)
    never create a second customer. On the race where two concurrent creates collide on the
    reference, Maxio rejects the second with 422 and we re-read the now-existing customer.
    """
    client = maxio_client.get_client()
    reference = _customer_reference(user)

    existing = _find_customer(client, reference)
    if existing is not None:
        customer_id = _value(existing.id)
        if customer_id is None:
            raise ProviderUnavailable('Customer lookup returned no id; outcome unknown.')
        return customer_id

    body = {'customer': _customer_attributes(user)}
    try:
        response = client.customers.create_customer(body=body)
    except ApiError as exc:
        # Reference already taken (concurrent create) -> re-read the existing customer.
        if exc.status_code == 422:
            existing = _find_customer(client, reference)
            if existing is not None and _value(existing.id) is not None:
                return existing.id
        _raise_from_api_error(exc, operation='create_customer')
    except (httpx.HTTPError, ValidationError) as exc:
        raise ProviderUnavailable() from exc

    customer_id = _value(response.customer.id)
    if customer_id is None:
        # A 2xx that did not carry an id: the create may have taken effect but we cannot name it.
        raise ProviderUnavailable('Customer creation returned no id; outcome unknown.')
    return customer_id


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def _subscription_summary(subscription):
    product = _value(subscription.product)
    plan_handle = _value(product.handle) if product is not None else None
    plan_name = _value(product.name) if product is not None else None
    # Next billing date: prefer next_assessment_at, else current_period_ends_at.
    next_billing = _isoformat(subscription.next_assessment_at) or _isoformat(
        subscription.current_period_ends_at
    )
    price_in_cents = _value(product.price_in_cents) if product is not None else None
    return {
        'subscriptionId': _value(subscription.id),
        'planHandle': plan_handle,
        'planName': plan_name,
        'state': _enum_str(subscription.state),
        'priceInCents': price_in_cents,
        'priceFormatted': _format_price(price_in_cents),
        'currency': 'USD',
        'currentPeriodEndsAt': _isoformat(subscription.current_period_ends_at),
        'nextBillingDate': next_billing,
    }


def _list_customer_subscriptions(client, customer_id):
    try:
        return client.customers.list_customer_subscriptions(customer_id)
    except ApiError as exc:
        _raise_from_api_error(exc, operation='list_customer_subscriptions')
    except (httpx.HTTPError, ValidationError) as exc:
        raise ProviderUnavailable() from exc


def _find_active_subscription(subscriptions, plan_handle):
    """Return an existing non-terminal subscription to ``plan_handle``, or None."""
    for wrapper in subscriptions:
        subscription = _value(wrapper.subscription)
        if subscription is None:
            continue
        product = _value(subscription.product)
        handle = _value(product.handle) if product is not None else None
        state = _enum_str(subscription.state)
        if handle == plan_handle and (state is None or state not in _TERMINAL_STATES):
            return subscription
    return None


def subscribe(user, plan_handle):
    """Idempotently enroll ``user`` on the plan identified by ``plan_handle``.

    Returns ``(summary_dict, created_bool)``. ``created`` is False when an existing live
    subscription to the same plan was reused. Validates the plan handle against the live plan
    list first, so an unknown handle yields a clean 400 rather than a provider error.
    """
    plan = get_plan(plan_handle)  # raises PlanNotFound for an unknown handle
    client = maxio_client.get_client()
    customer_id = ensure_customer(user)

    # Double-click / repeat protection: reuse a live subscription to the same plan if present.
    existing = _find_active_subscription(
        _list_customer_subscriptions(client, customer_id), plan_handle
    )
    if existing is not None:
        return _subscription_summary(existing), False

    body = {
        'subscription': {
            'product_handle': plan_handle,
            'customer_id': customer_id,
            # These plans require no payment method; remittance (invoice) billing lets the
            # subscription be created without capturing a card / 3-DS.
            'payment_collection_method': 'remittance',
        }
    }
    try:
        response = client.subscriptions.create_subscription(body=body)
    except ApiError as exc:
        _raise_from_api_error(exc, operation='create_subscription')
    except (httpx.HTTPError, ValidationError) as exc:
        raise ProviderUnavailable() from exc

    subscription = _value(response.subscription)
    if subscription is None or _value(subscription.id) is None:
        # A 2xx with no subscription id: the create may have taken effect but we cannot name it.
        raise ProviderUnavailable('Subscription creation returned no id; outcome unknown.')
    return _subscription_summary(subscription), True


def list_my_subscriptions(user):
    """Return the caller's subscriptions (empty list when the user has no Maxio customer yet)."""
    client = maxio_client.get_client()
    reference = _customer_reference(user)
    customer = _find_customer(client, reference)
    if customer is None:
        return []
    customer_id = _value(customer.id)
    if customer_id is None:
        return []
    subscriptions = _list_customer_subscriptions(client, customer_id)
    summaries = []
    for wrapper in subscriptions:
        subscription = _value(wrapper.subscription)
        if subscription is not None:
            summaries.append(_subscription_summary(subscription))
    return summaries
