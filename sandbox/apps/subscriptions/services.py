"""Application service layer for Maxio Advanced Billing subscriptions.

This module owns every interaction with the Maxio SDK and translates the SDK's
failure kinds into a single family of domain exceptions
(:class:`SubscriptionError` and subclasses), each carrying the HTTP status the
view should return. Views never touch the SDK directly.

Failure translation follows the SDK's error model:

* ``ApiError`` with a ``RawError`` payload -> mapped by HTTP status (a provider
  4xx becomes a client 4xx; anything else a 502).
* ``ApiError`` with a typed payload (``CustomerErrorResponse1`` /
  ``ErrorListResponse1``) -> a 400 provider-rejection carrying the messages.
* ``pydantic.ValidationError`` / ``ValueError`` (a decode failure) -> "outcome
  unknown"; never mapped onto a domain absence.
* ``httpx.HTTPError`` (transport failure) -> provider unreachable.

We never surface ``str(exc)`` from the SDK to the caller; we log detail and
return messages we wrote.
"""

from __future__ import annotations

import logging

import httpx
from django.db import transaction
from pydantic import ValidationError

from maxio_advanced_billing.core import ApiError, RawError, UNSET
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
)

from .client import get_client
from .models import MaxioCustomer

logger = logging.getLogger('sandbox.subscriptions')

#: Subscription states that mean a subscription is current enough that we should
#: reuse it rather than create a duplicate on a repeated subscribe (idempotency).
LIVE_STATES = frozenset(
    {'active', 'trialing', 'pending', 'assessing', 'awaiting_signup'}
)

#: Payment-collection method for new subscriptions. 'remittance' (invoice-based,
#: Relationship Invoicing) lets a subscription activate without a card on file,
#: which is what "subscribe without card capture / 3-DS" requires for plans that
#: carry an immediate balance.
COLLECTION_METHOD = 'remittance'


# --------------------------------------------------------------------------- #
# Domain exceptions
# --------------------------------------------------------------------------- #
class SubscriptionError(Exception):
    """Base error; ``http_status`` is what the view returns."""

    http_status = 502

    def __init__(self, message, *, http_status=None, detail=None):
        super().__init__(message)
        self.message = message
        if http_status is not None:
            self.http_status = http_status
        self.detail = detail


class NotConfigured(SubscriptionError):
    http_status = 503


class PlanNotFound(SubscriptionError):
    http_status = 404


class ProviderRejected(SubscriptionError):
    """The provider deliberately rejected the request (a 4xx)."""

    http_status = 400


class ProviderUnavailable(SubscriptionError):
    """The provider could not be reached, or answered with a 5xx."""

    http_status = 502


class ProviderUnreadable(SubscriptionError):
    """The provider answered but the body could not be read; outcome unknown."""

    http_status = 502


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean(value):
    """Turn an SDK member (which may be ``UNSET``/``None``/datetime) into a
    JSON-serialisable value."""
    if value is UNSET or value is None:
        return None
    isoformat = getattr(value, 'isoformat', None)
    if callable(isoformat):
        return isoformat()
    return value


def _safe_text(raw):
    try:
        return raw.text()
    except Exception:  # pragma: no cover - defensive
        return None


def _flatten_messages(value):
    """Flatten a typed error body into a list of human-readable strings."""
    if value is UNSET or value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_flatten_messages(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_flatten_messages(item))
        return out
    to_dict = getattr(value, 'to_dict', None)
    if callable(to_dict):
        try:
            return _flatten_messages(to_dict())
        except Exception:  # pragma: no cover - defensive
            pass
    return [str(value)]


class MaxioService:
    """Coordinates Django users, the local link table and the Maxio SDK."""

    def __init__(self, client=None, family_handle=None):
        self._client_override = client
        if family_handle is None:
            from django.conf import settings

            family_handle = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
        self.family_handle = family_handle

    # -- SDK plumbing ------------------------------------------------------- #
    def _client(self):
        if self._client_override is not None:
            return self._client_override
        return get_client()

    def _call(self, what, fn):
        """Run an SDK call, translating the non-``ApiError`` failure kinds.

        ``ApiError`` is deliberately left to propagate so the caller can handle
        expected statuses (e.g. a 404 lookup miss) or read the typed body.
        """
        try:
            return fn()
        except ApiError:
            raise
        except ValidationError as exc:
            logger.warning('Maxio decode failure while %s: %s', what, exc)
            raise ProviderUnreadable(
                f'Maxio returned an unreadable response while {what}; outcome unknown.'
            ) from exc
        except ValueError as exc:
            logger.warning('Maxio non-JSON response while %s: %s', what, exc)
            raise ProviderUnreadable(
                f'Maxio returned an unreadable response while {what}; outcome unknown.'
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning('Maxio unreachable while %s: %s', what, exc)
            raise ProviderUnavailable(
                f'Maxio could not be reached while {what}.'
            ) from exc

    def _reraise(self, exc, what):
        """Translate an ``ApiError`` into a domain error and raise it."""
        err = exc.error
        if isinstance(err, RawError):
            status = exc.status_code
            detail = _safe_text(err)
            logger.warning('Maxio HTTP %s while %s: %s', status, what, detail)
            if 400 <= status < 500:
                raise ProviderRejected(
                    f'Maxio rejected the request while {what} (HTTP {status}).',
                    http_status=400,
                    detail=detail,
                )
            raise ProviderUnavailable(
                f'Maxio error while {what} (HTTP {status}).', detail=detail
            )
        messages = _flatten_messages(err)
        logger.warning('Maxio rejected %s: %s', what, messages)
        raise ProviderRejected(
            '; '.join(messages) or f'Maxio rejected the request while {what}.',
            http_status=400,
            detail=messages,
        )

    @staticmethod
    def _require(value, name):
        if value is UNSET or value is None:
            raise ProviderUnreadable(
                f'Maxio response was missing the {name}; outcome unknown.'
            )
        return value

    # -- Plans -------------------------------------------------------------- #
    def list_plans(self):
        """Return the plans of the configured product family, each with its
        ``planHandle``."""
        client = self._client()
        try:
            products = self._call(
                'loading plans', lambda: client.products.list_products(per_page=200)
            )
        except ApiError as exc:
            self._reraise(exc, 'loading plans')

        plans = []
        for item in products:
            product = getattr(item, 'product', None)
            if not product or product is UNSET:
                continue
            family = getattr(product, 'product_family', None)
            family_handle = None
            if family and family is not UNSET:
                family_handle = _clean(getattr(family, 'handle', None))
            if self.family_handle and family_handle != self.family_handle:
                continue
            handle = _clean(product.handle)
            if not handle:
                continue
            price_cents = _clean(product.price_in_cents)
            plans.append(
                {
                    'planHandle': handle,
                    'name': _clean(product.name),
                    'priceInCents': price_cents,
                    'priceFormatted': None
                    if price_cents is None
                    else f'{price_cents / 100:.2f}',
                    'interval': _clean(product.interval),
                    'intervalUnit': None
                    if product.interval_unit is UNSET
                    else str(product.interval_unit),
                    'productFamilyHandle': family_handle,
                    'requiresPaymentMethod': _clean(product.require_credit_card),
                }
            )
        return plans

    # -- Customer link ------------------------------------------------------ #
    @staticmethod
    def _reference_for(user):
        return f'oscar-user-{user.pk}'

    def ensure_customer(self, user):
        """Return the :class:`MaxioCustomer` link for ``user``, creating the
        Maxio customer if needed. Idempotent under concurrent first requests."""
        reference = self._reference_for(user)
        link, _created = MaxioCustomer.objects.get_or_create(
            user=user, defaults={'reference': reference}
        )
        if link.maxio_customer_id is not None:
            return link

        # Serialise concurrent first requests for this user: whoever locks the
        # row first resolves the Maxio customer; the rest see the filled value.
        with transaction.atomic():
            locked = MaxioCustomer.objects.select_for_update().get(pk=link.pk)
            if locked.maxio_customer_id is None:
                customer_id = self._find_or_create_customer(user, locked.reference)
                locked.maxio_customer_id = customer_id
                locked.save(update_fields=['maxio_customer_id', 'updated_at'])
            return locked

    def _find_or_create_customer(self, user, reference):
        client = self._client()
        # 1. Look up by our stable reference (idempotent).
        try:
            resp = self._call(
                'looking up the customer',
                lambda: client.customers.read_customer_by_reference(reference),
            )
        except ApiError as exc:
            if exc.status_code == 404:
                resp = None
            else:
                self._reraise(exc, 'looking up the customer')
        if resp is not None:
            return self._require(resp.customer.id, 'customer id')

        # 2. Not found -> create.
        first_name = (getattr(user, 'first_name', '') or '').strip()
        last_name = (getattr(user, 'last_name', '') or '').strip()
        email = (getattr(user, 'email', '') or '').strip()
        username = user.get_username() if hasattr(user, 'get_username') else str(user.pk)
        if not first_name:
            first_name = username or 'Sandbox'
        if not last_name:
            last_name = 'Subscriber'
        if not email:
            email = f'{reference}@example.invalid'

        body = CreateCustomerRequest(
            customer=CreateCustomer(
                first_name=first_name,
                last_name=last_name,
                email=email,
                reference=reference,
            )
        )
        try:
            created = self._call(
                'creating the customer',
                lambda: client.customers.create_customer(body=body),
            )
        except ApiError as exc:
            self._reraise(exc, 'creating the customer')
        return self._require(created.customer.id, 'customer id')

    # -- Subscriptions ------------------------------------------------------ #
    def subscribe(self, user, plan_handle):
        """Subscribe ``user`` to ``plan_handle``. Idempotent: a live
        subscription to the same plan is returned rather than duplicated."""
        if not plan_handle:
            raise PlanNotFound('A planHandle is required.', http_status=400)

        valid_handles = {p['planHandle'] for p in self.list_plans()}
        if plan_handle not in valid_handles:
            raise PlanNotFound(f'Unknown plan handle {plan_handle!r}.')

        link = self.ensure_customer(user)
        customer_id = link.maxio_customer_id
        client = self._client()

        # Idempotency: reuse a live subscription to the same plan.
        try:
            existing = self._call(
                'checking existing subscriptions',
                lambda: client.customers.list_customer_subscriptions(customer_id),
            )
        except ApiError as exc:
            if exc.status_code == 404:
                existing = []
            else:
                self._reraise(exc, 'checking existing subscriptions')
        for item in existing or []:
            sub = self._unwrap_subscription(item)
            if sub is None:
                continue
            if self._sub_handle(sub) == plan_handle and self._is_live(sub):
                return self._serialize_subscription(sub, reused=True)

        # Create a new subscription. product_handle selects the plan; customer_id
        # ties it to the ensured customer. We use 'remittance' (invoice-based)
        # collection so the subscription activates immediately without capturing
        # a card or triggering 3-DS -- these plans require no payment method, but
        # automatic collection of the first balance would still demand a card on
        # file. Every other field is left UNSET so the plan's own configuration
        # (trial, setup fee, expiry, taxes) governs.
        body = CreateSubscriptionRequest(
            subscription=CreateSubscription(
                product_handle=plan_handle,
                customer_id=customer_id,
                payment_collection_method=COLLECTION_METHOD,
            )
        )
        try:
            resp = self._call(
                'creating the subscription',
                lambda: client.subscriptions.create_subscription(body=body),
            )
        except ApiError as exc:
            self._reraise(exc, 'creating the subscription')

        sub = getattr(resp, 'subscription', None)
        if sub is None or sub is UNSET:
            raise ProviderUnreadable(
                'Maxio did not return the created subscription; outcome unknown.'
            )
        self._require(sub.id, 'subscription id')
        return self._serialize_subscription(sub, reused=False)

    def list_my_subscriptions(self, user):
        """Return the caller's subscriptions (empty if they have no Maxio
        customer yet)."""
        link = MaxioCustomer.objects.filter(user=user).first()
        if link is None or link.maxio_customer_id is None:
            return []
        client = self._client()
        try:
            items = self._call(
                'listing subscriptions',
                lambda: client.customers.list_customer_subscriptions(
                    link.maxio_customer_id
                ),
            )
        except ApiError as exc:
            if exc.status_code == 404:
                return []
            self._reraise(exc, 'listing subscriptions')

        subscriptions = []
        for item in items or []:
            sub = self._unwrap_subscription(item)
            if sub is not None:
                subscriptions.append(self._serialize_subscription(sub))
        return subscriptions

    # -- Subscription serialisation ---------------------------------------- #
    @staticmethod
    def _unwrap_subscription(item):
        sub = getattr(item, 'subscription', item)
        if sub is None or sub is UNSET:
            return None
        return sub

    @staticmethod
    def _sub_handle(sub):
        product = getattr(sub, 'product', None)
        if not product or product is UNSET:
            return None
        return _clean(product.handle)

    @staticmethod
    def _is_live(sub):
        state = getattr(sub, 'state', None)
        if state is UNSET or state is None:
            return False
        return str(state) in LIVE_STATES

    def _serialize_subscription(self, sub, reused=None):
        product = getattr(sub, 'product', None)
        plan_handle = plan_name = None
        if product and product is not UNSET:
            plan_handle = _clean(product.handle)
            plan_name = _clean(product.name)
        customer = getattr(sub, 'customer', None)
        customer_id = None
        if customer and customer is not UNSET:
            customer_id = _clean(customer.id)
        state = getattr(sub, 'state', None)
        next_billing = _clean(getattr(sub, 'current_period_ends_at', None))
        data = {
            'subscriptionId': _clean(sub.id),
            'state': None if state is UNSET or state is None else str(state),
            'planHandle': plan_handle,
            'planName': plan_name,
            'priceInCents': _clean(getattr(sub, 'product_price_in_cents', None)),
            'nextBillingDate': next_billing,
            'currentPeriodEndsAt': next_billing,
            'customerId': customer_id,
        }
        if reused is not None:
            data['reused'] = reused
        return data
