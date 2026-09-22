"""Application service for Maxio subscription billing.

This module is the single boundary between the sandbox's HTTP layer and the
Maxio Advanced Billing SDK. It exposes three capabilities, each separately
invocable:

* :meth:`MaxioSubscriptionService.list_plans` — the plans on offer.
* :meth:`MaxioSubscriptionService.subscribe` — idempotently enroll the caller.
* :meth:`MaxioSubscriptionService.list_my_subscriptions` — the caller's subscriptions.

Every SDK failure kind is translated here into a single :class:`MaxioServiceError`
carrying a suggested HTTP status, so the views never have to know about the SDK's
exception taxonomy. The SDK performs no retries; the read paths here are safe to
retry but we deliberately keep the create paths single-attempt.
"""

import logging

import httpx
from pydantic import ValidationError

from django.conf import settings

from maxio_advanced_billing.core import UNSET, ApiError, RawError
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
    CustomerErrorResponse1,
    ErrorListResponse1,
)

from .client import get_client

logger = logging.getLogger('apps.subscriptions')

# Subscription states that mean "this subscription is gone"; a caller with only
# subscriptions in these states may subscribe again. Everything else (live,
# problem, awaiting-signup, or a state newer than this SDK) blocks a duplicate
# create, so a double-clicked subscribe returns the existing subscription.
_TERMINAL_STATES = frozenset({
    'canceled', 'expired', 'failed_to_create', 'trial_ended',
})


class MaxioServiceError(Exception):
    """A failure talking to Maxio, translated for our HTTP boundary.

    ``http_status`` is the status our own API should return; ``detail`` is an
    optional machine-readable payload (e.g. the provider's validation messages).
    The message is safe to show to a caller — it never contains a raw SDK
    exception string or a traceback.
    """

    def __init__(self, message, http_status=502, detail=None):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.detail = detail


def _unset_to_none(value):
    """Resolve an SDK ``UNSET`` sentinel (or an already-None) to ``None``."""
    return None if value is UNSET or value is None else value


class MaxioSubscriptionService:
    """Coordinates Oscar users with Maxio customers and subscriptions."""

    def __init__(self, client=None, product_family_handle=None):
        self._client = client or get_client()
        self._family_handle = (
            product_family_handle
            if product_family_handle is not None
            else settings.MAXIO_DEFAULT_PRODUCT_FAMILY
        )

    # -- Identity -------------------------------------------------------------

    @staticmethod
    def customer_reference(user):
        """Stable, per-user Maxio customer reference.

        Derived solely from the primary key so it is deterministic across
        requests — the key to idempotent customer creation.
        """
        return 'oscar-user-{}'.format(user.pk)

    # -- Plans ----------------------------------------------------------------

    def list_plans(self):
        """Return the plans (Maxio products) in the configured product family."""
        try:
            products = self._client.products.list_products(per_page=200)
        except ApiError as exc:
            raise self._translate_api_error(exc, 'list subscription plans')
        except ValidationError as exc:
            raise self._translate_decode_error(exc, 'list subscription plans')
        except httpx.HTTPError as exc:
            raise self._translate_transport_error(exc, 'list subscription plans')

        plans = []
        for wrapper in products:
            product = wrapper.product
            family = _unset_to_none(product.product_family)
            family_handle = _unset_to_none(family.handle) if family else None
            # Only surface products in the configured family; when no family is
            # configured, surface everything.
            if self._family_handle and family_handle != self._family_handle:
                continue
            plans.append(product)
        return plans

    def _require_plan(self, plan_handle):
        """Return the plan product for ``plan_handle`` or raise a 404-ish error."""
        for product in self.list_plans():
            if _unset_to_none(product.handle) == plan_handle:
                return product
        raise MaxioServiceError(
            "Unknown plan '{}'.".format(plan_handle),
            http_status=404,
        )

    # -- Customers ------------------------------------------------------------

    def _find_customer(self, user):
        """Return the Maxio customer for ``user``, or ``None`` if absent."""
        reference = self.customer_reference(user)
        try:
            response = self._client.customers.read_customer_by_reference(reference=reference)
        except ApiError as exc:
            # A 404 is the documented "no such customer" signal, not a failure.
            if exc.status_code == 404:
                return None
            raise self._translate_api_error(exc, 'look up customer')
        except ValidationError as exc:
            raise self._translate_decode_error(exc, 'look up customer')
        except httpx.HTTPError as exc:
            raise self._translate_transport_error(exc, 'look up customer')
        return response.customer

    def ensure_customer(self, user):
        """Return the Maxio customer for ``user``, creating one if needed.

        Idempotent: the customer reference is derived from the user's primary
        key, so repeated calls (including a double-click) resolve to the same
        Maxio customer rather than creating duplicates.
        """
        existing = self._find_customer(user)
        if existing is not None:
            return existing

        first_name, last_name = self._names_for(user)
        body = CreateCustomerRequest(
            customer=CreateCustomer(
                first_name=first_name,
                last_name=last_name,
                email=self._email_for(user),
                reference=self.customer_reference(user),
            )
        )
        try:
            response = self._client.customers.create_customer(body=body)
        except ApiError as exc:
            raise self._translate_api_error(exc, 'create customer')
        except ValidationError as exc:
            raise self._translate_decode_error(exc, 'create customer')
        except httpx.HTTPError as exc:
            raise self._translate_transport_error(exc, 'create customer')

        customer = response.customer
        if _unset_to_none(customer.id) is None:
            raise MaxioServiceError(
                'Maxio did not return a customer id; the outcome is unknown.',
                http_status=502,
            )
        return customer

    @staticmethod
    def _names_for(user):
        first = (getattr(user, 'first_name', '') or '').strip()
        last = (getattr(user, 'last_name', '') or '').strip()
        if not first and not last:
            # Fall back to the username / email local part so the required
            # first/last name fields are always populated.
            base = (getattr(user, 'username', '') or getattr(user, 'email', '') or 'Oscar').strip()
            base = base.split('@')[0] or 'Oscar'
            first, last = base, 'Subscriber'
        elif not last:
            last = 'Subscriber'
        elif not first:
            first = 'Oscar'
        return first, last

    @staticmethod
    def _email_for(user):
        email = (getattr(user, 'email', '') or '').strip()
        if email:
            return email
        username = (getattr(user, 'username', '') or 'user-{}'.format(user.pk)).strip()
        return '{}@example.invalid'.format(username)

    # -- Subscriptions --------------------------------------------------------

    def _customer_subscriptions(self, customer_id, context):
        try:
            results = self._client.customers.list_customer_subscriptions(customer_id=customer_id)
        except ApiError as exc:
            raise self._translate_api_error(exc, context)
        except ValidationError as exc:
            raise self._translate_decode_error(exc, context)
        except httpx.HTTPError as exc:
            raise self._translate_transport_error(exc, context)
        return [wrapper.subscription for wrapper in results if _unset_to_none(wrapper.subscription)]

    @staticmethod
    def _is_active_for_plan(subscription, plan_handle):
        product = _unset_to_none(subscription.product)
        handle = _unset_to_none(product.handle) if product else None
        if handle != plan_handle:
            return False
        state = _unset_to_none(subscription.state)
        # Open enum: compare on the wire value (str(member) yields it for a str
        # enum, and an unknown value is already a plain str).
        state_value = str(state) if state is not None else ''
        return state_value not in _TERMINAL_STATES

    def subscribe(self, user, plan_handle):
        """Idempotently enroll ``user`` on ``plan_handle``.

        Returns a ``(subscription, created)`` tuple: ``created`` is ``False``
        when an existing non-terminal subscription to the same plan was reused.
        """
        # Validate the requested plan up front so a bad handle is a clean 404
        # rather than a provider-side 422.
        self._require_plan(plan_handle)

        customer = self.ensure_customer(user)
        customer_id = customer.id

        # Idempotency: reuse a live subscription to the same plan if one exists.
        for existing in self._customer_subscriptions(customer_id, 'check existing subscriptions'):
            if self._is_active_for_plan(existing, plan_handle):
                logger.info(
                    'Reusing existing Maxio subscription %s for user %s on plan %s',
                    _unset_to_none(existing.id), user.pk, plan_handle,
                )
                return existing, False

        # The seeded plans require no payment method, but an "automatic"
        # collection subscription still tries to charge the initial balance and
        # is rejected when no card is on file. Using invoice/remittance
        # collection creates the subscription with an open invoice instead, so
        # subscribe works without card capture (as the task requires).
        collection_method = getattr(
            settings, 'MAXIO_PAYMENT_COLLECTION_METHOD', 'remittance') or 'remittance'
        body = CreateSubscriptionRequest(
            subscription=CreateSubscription(
                product_handle=plan_handle,
                customer_id=customer_id,
                payment_collection_method=collection_method,
            )
        )
        try:
            response = self._client.subscriptions.create_subscription(body=body)
        except ApiError as exc:
            raise self._translate_api_error(exc, 'create subscription')
        except ValidationError as exc:
            raise self._translate_decode_error(exc, 'create subscription')
        except httpx.HTTPError as exc:
            raise self._translate_transport_error(exc, 'create subscription')

        subscription = _unset_to_none(response.subscription)
        if subscription is None or _unset_to_none(subscription.id) is None:
            # The create may have taken effect server-side; the outcome is
            # unknown because we cannot read back an id.
            raise MaxioServiceError(
                'Maxio did not return a subscription id; the outcome is unknown.',
                http_status=502,
            )
        logger.info(
            'Created Maxio subscription %s for user %s on plan %s',
            subscription.id, user.pk, plan_handle,
        )
        return subscription, True

    def list_my_subscriptions(self, user):
        """Return the caller's subscriptions, newest first.

        A caller who has never subscribed has no Maxio customer yet; that is an
        empty list, not an error.
        """
        customer = self._find_customer(user)
        if customer is None:
            return []
        subscriptions = self._customer_subscriptions(customer.id, 'list my subscriptions')
        # Newest first by created_at when available.
        return sorted(
            subscriptions,
            key=lambda s: str(_unset_to_none(s.created_at) or ''),
            reverse=True,
        )

    # -- Error translation ----------------------------------------------------

    def _translate_api_error(self, exc, context):
        """Map an SDK ``ApiError`` onto a :class:`MaxioServiceError`."""
        error = exc.error
        # Typed 422 validation bodies -> a client error carrying the messages.
        if isinstance(error, ErrorListResponse1):
            messages = _unset_to_none(error.errors) or []
            logger.warning('Maxio rejected %s (%s): %s', context, exc.status_code, messages)
            return MaxioServiceError(
                'Maxio could not {}.'.format(context),
                http_status=422 if exc.status_code == 422 else exc.status_code,
                detail=list(messages),
            )
        if isinstance(error, CustomerErrorResponse1):
            logger.warning('Maxio rejected %s (%s): %r', context, exc.status_code, error)
            return MaxioServiceError(
                'Maxio could not {}.'.format(context),
                http_status=422 if exc.status_code == 422 else exc.status_code,
                detail=error.to_dict(),
            )
        # Untyped (RawError) arm: preserve the status; a 4xx is the caller's
        # problem, anything else is treated as a bad gateway.
        status = exc.status_code
        if isinstance(error, RawError):
            logger.warning('Maxio error on %s: HTTP %s %s', context, status, error.text())
        else:
            logger.warning('Maxio error on %s: HTTP %s', context, status)
        http_status = status if 400 <= status < 500 else 502
        return MaxioServiceError(
            'Maxio could not {} (HTTP {}).'.format(context, status),
            http_status=http_status,
        )

    def _translate_decode_error(self, exc, context):
        # A response body we could not read: the outcome is unknown. We never
        # report this as a domain "no such thing" — see python-error-handling.
        logger.error('Unreadable Maxio response while trying to %s: %s', context, exc)
        return MaxioServiceError(
            'Maxio returned an unreadable response while trying to {}; '
            'the outcome is unknown.'.format(context),
            http_status=502,
        )

    def _translate_transport_error(self, exc, context):
        logger.error('Could not reach Maxio while trying to %s: %s', context, exc)
        return MaxioServiceError(
            'Could not reach Maxio while trying to {}.'.format(context),
            http_status=502,
        )
