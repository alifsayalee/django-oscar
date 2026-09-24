"""
Boundary to Maxio Advanced Billing.

Everything that touches the Maxio SDK lives here: building the (single, process-wide) client from
Django settings, translating every SDK failure into one ``ProviderError`` family, retrying reads,
and mapping provider subscription states onto the outcomes the rest of the app acts on.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import Environment, MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import (
    ApiError,
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    UnsetType,
)
from maxio_advanced_billing.models import (
    CustomerError,
    CustomerErrorResponse1,
    ErrorListResponse1,
    Product,
    Subscription,
)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState, SubscriptionStateOrStr

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Outcomes a provider subscription state maps onto. Only DONE is success.
DONE = "done"
PENDING = "pending"
ATTENTION = "attention"
ENDED = "ended"
UNKNOWN = "unknown"
LIVE_OUTCOMES = (DONE, PENDING, ATTENTION)

# Transport failures raised before the request left: nothing can have happened upstream.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)
# Provider statuses that are a verdict on the caller's input, passed through as-is.
CALLER_STATUSES = (400, 404, 409, 422)
# Provider statuses worth another attempt on an idempotent read.
TRANSIENT_STATUSES = (429, 502, 503, 504)

READ_ATTEMPTS = 3
MAX_RETRY_AFTER = 5.0


# Errors
# ======

class ProviderError(Exception):
    """A failed interaction with Maxio, carrying the status this app answers with."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        details: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        # Whether the operation may have taken effect at Maxio despite the failure.
        self.outcome_unknown = outcome_unknown
        self.details = details or []


class ProviderRejected(ProviderError):
    """Maxio rejected the request because of the caller's input."""


class ProviderConfigError(ProviderError):
    """Our own configuration or credentials are wrong; never the caller's fault."""


class ProviderUnavailable(ProviderError):
    """Maxio could not be reached, timed out, rate-limited us or failed."""


class ProviderUnreadable(ProviderError):
    """Maxio answered, but not with something we can read."""


def _error_messages(error: object) -> list[str]:
    if isinstance(error, ErrorListResponse1):
        return [str(message) for message in error.errors]
    if isinstance(error, CustomerErrorResponse1):
        errors = error.errors
        if isinstance(errors, list):
            return [str(message) for message in errors]
        if isinstance(errors, CustomerError) and isinstance(errors.customer, str):
            return [errors.customer]
        return []
    if isinstance(error, str):
        return [error] if error else []
    return []


def translate(exc: BaseException, *, write: bool) -> ProviderError:
    """Map any failure of an SDK call onto a ``ProviderError``; re-raise anything else."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        status = exc.status_code
        if status in (401, 403):
            logger.error("Maxio refused our credentials (HTTP %s)", status)
            return ProviderConfigError(502, "The billing provider refused our credentials.")
        if status == 429:
            return ProviderUnavailable(503, "The billing provider is rate-limiting requests; try again shortly.")
        if status in CALLER_STATUSES:
            return ProviderRejected(
                status, "The billing provider rejected the request.", details=_error_messages(exc.error)
            )
        logger.warning("Maxio answered HTTP %s", status)
        return ProviderUnavailable(
            502, "The billing provider failed to handle the request.", outcome_unknown=write and status >= 500
        )
    if isinstance(exc, ValueError):  # pydantic.ValidationError included: an undecodable body
        logger.warning("Unreadable Maxio response: %s", type(exc).__name__)
        return ProviderUnreadable(502, "The billing provider returned an unreadable response.", outcome_unknown=write)
    if isinstance(exc, NEVER_SENT):
        return ProviderUnavailable(502, "The billing provider could not be reached.")
    if isinstance(exc, httpx.RequestError):
        return ProviderUnavailable(504, "The billing provider did not answer in time.", outcome_unknown=write)
    raise exc


def _retry_delay(attempt: int, exc: BaseException) -> float:
    if isinstance(exc, ApiError):
        retry_after = exc.response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), MAX_RETRY_AFTER)
    return 0.5 * 2.0 ** (attempt - 1)


def read(call: Callable[[], T]) -> T:
    """Run an idempotent read, retrying transient failures, translating the rest."""
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return call()
        except ApiError as exc:
            if exc.status_code not in TRANSIENT_STATUSES or attempt == READ_ATTEMPTS:
                raise translate(exc, write=False) from exc
            delay = _retry_delay(attempt, exc)
        except httpx.RequestError as exc:
            if not isinstance(exc, NEVER_SENT + (httpx.ReadTimeout, httpx.RemoteProtocolError)) \
                    or attempt == READ_ATTEMPTS:
                raise translate(exc, write=False) from exc
            delay = _retry_delay(attempt, exc)
        except ValueError as exc:
            raise translate(exc, write=False) from exc
        logger.info("Retrying Maxio read (attempt %s of %s)", attempt + 1, READ_ATTEMPTS)
        time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def read_or_none(call: Callable[[], T]) -> T | None:
    """Like ``read``, but a provider 404 means "no such record" and returns None."""
    try:
        return read(call)
    except ProviderRejected as exc:
        if exc.status_code == 404:
            return None
        raise


def write(call: Callable[[], T]) -> T:
    """Run a write exactly once; callers reconcile an unknown outcome by reference."""
    try:
        return call()
    except (ApiError, ValueError, httpx.RequestError) as exc:
        raise translate(exc, write=True) from exc


# Client
# ======

class LoggingTransport:
    """Logs method, URL, status and duration of each request. Never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except httpx.RequestError as exc:
            logger.warning("Maxio %s %s failed: %s", request.method, request.url, type(exc).__name__)
            raise
        logger.info(
            "Maxio %s %s -> %s (%.0f ms)",
            request.method, request.url, response.status_code, (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


_ENVIRONMENTS: dict[str, Environment] = {"us": "us", "eu": "eu"}


def _server_config(environment: Environment, subdomain: str, base_url: str) -> ServerConfigDict:
    if environment == "eu":
        if base_url:
            return {"production": {"eu": {"base_url": base_url}}}
        return {"production": {"eu": {"site": subdomain}}}
    if base_url:
        return {"production": {"us": {"base_url": base_url}}}
    return {"production": {"us": {"site": subdomain}}}


def build_client(http_client: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    """Build a client from Django settings. ``http_client`` replaces the transport (tests)."""
    api_key: str = settings.MAXIO_API_KEY
    subdomain: str = settings.MAXIO_SITE_SUBDOMAIN
    base_url: str = settings.MAXIO_BASE_URL
    if not api_key:
        raise ImproperlyConfigured("MAXIO_API_KEY is not set.")
    if not subdomain and not base_url:
        raise ImproperlyConfigured("Set MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL).")
    environment = _ENVIRONMENTS.get(str(settings.MAXIO_ENVIRONMENT).strip().lower())
    if environment is None:
        raise ImproperlyConfigured("MAXIO_ENVIRONMENT must be 'US' or 'EU'.")
    timeout = float(settings.MAXIO_TIMEOUT)
    transport = http_client or LoggingTransport(HttpxClient(timeout=timeout))
    server_config = _server_config(environment, subdomain, base_url)
    credentials = BasicAuthCredentials(username=api_key, password="x")
    return MaxioAdvancedBillingClient(
        environment=environment,
        timeout=timeout,
        server_config=server_config,
        custom_http_client=transport,
        basic_auth=credentials,
    )


_client: MaxioAdvancedBillingClient | None = None
_client_lock = threading.Lock()


def get_client() -> MaxioAdvancedBillingClient:
    """The process-wide client, built lazily (after any worker fork) and closed at exit."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                try:
                    _client = build_client()
                except ImproperlyConfigured as exc:
                    logger.error("Maxio billing is not configured: %s", exc)
                    raise ProviderConfigError(503, "Subscription billing is not configured.") from exc
    return _client


def set_client(client: MaxioAdvancedBillingClient | None) -> None:
    """Replace the process-wide client (used by tests); closes the previous one."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    if previous is not None and previous is not client:
        previous.close()


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


def payment_collection_method() -> CollectionMethod:
    """How new subscriptions are paid, from settings; an unknown value is a configuration error."""
    configured = str(settings.MAXIO_PAYMENT_COLLECTION_METHOD).strip().lower()
    try:
        return CollectionMethod(configured)
    except ValueError as exc:
        raise ProviderConfigError(503, "Subscription billing is misconfigured.") from exc


# References
# ==========

def reference_prefix() -> str:
    """Prefix unique to this install, so installs sharing a Maxio site never collide."""
    configured: str = settings.MAXIO_REFERENCE_PREFIX
    if configured:
        return configured
    digest = hashlib.sha256(str(settings.SECRET_KEY).encode()).hexdigest()[:10]
    return f"oscar-{digest}"


def customer_reference(user_pk: object) -> str:
    return f"{reference_prefix()}-cust-{user_pk}"


def subscription_reference(user_pk: object, plan_handle: str, sequence: int) -> str:
    return f"{reference_prefix()}-sub-{user_pk}-{plan_handle}-{sequence}"


# Mapping
# =======

def status_from_provider(state: SubscriptionStateOrStr | UnsetType) -> str:
    """The ONE place a Maxio subscription state becomes an outcome of ours."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return DONE
        case SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP:
            return PENDING
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID
              | SubscriptionState.PAUSED | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            return ATTENTION
        case (SubscriptionState.CANCELED | SubscriptionState.EXPIRED | SubscriptionState.TRIAL_ENDED
              | SubscriptionState.FAILED_TO_CREATE):
            return ENDED
        case _:
            return UNKNOWN


def opt(value: T | UnsetType) -> T | None:
    """Resolve the SDK's UNSET sentinel to None before a value leaves this module."""
    return None if isinstance(value, UnsetType) else value


def _iso(value: datetime | None | UnsetType) -> str | None:
    resolved = opt(value)
    return resolved.isoformat() if resolved is not None else None


def _text(value: object) -> str | None:
    return None if value is None or isinstance(value, UnsetType) else str(value)


def require_subscription(subscription: Subscription | UnsetType) -> Subscription:
    """Assert the members we depend on; a 2xx without them is an unreadable answer."""
    if isinstance(subscription, UnsetType) or isinstance(subscription.id, UnsetType) \
            or isinstance(subscription.state, UnsetType):
        raise ProviderUnreadable(
            502, "The billing provider returned an incomplete subscription.", outcome_unknown=True
        )
    return subscription


def subscription_customer_id(subscription: Subscription) -> int | None:
    customer = opt(subscription.customer)
    return None if customer is None else opt(customer.id)


def plan_payload(product: Product) -> dict[str, Any]:
    return {
        "planHandle": opt(product.handle),
        "productId": opt(product.id),
        "name": opt(product.name),
        "description": opt(product.description),
        "priceInCents": opt(product.price_in_cents),
        "interval": opt(product.interval),
        "intervalUnit": _text(product.interval_unit),
        "requiresPaymentMethod": opt(product.require_credit_card),
    }


def subscription_payload(subscription: Subscription) -> dict[str, Any]:
    product = opt(subscription.product)
    return {
        "subscriptionId": opt(subscription.id),
        "reference": opt(subscription.reference),
        "state": _text(subscription.state),
        "status": status_from_provider(subscription.state),
        "plan": {
            "planHandle": opt(product.handle) if product else None,
            "name": opt(product.name) if product else None,
            "interval": opt(product.interval) if product else None,
            "intervalUnit": _text(product.interval_unit) if product else None,
        },
        "priceInCents": opt(subscription.product_price_in_cents),
        "currentBillingAmountInCents": opt(subscription.current_billing_amount_in_cents),
        "currency": opt(subscription.currency),
        "nextBillingAt": _iso(subscription.current_period_ends_at),
        "nextAssessmentAt": _iso(subscription.next_assessment_at),
        "activatedAt": _iso(subscription.activated_at),
        "createdAt": _iso(subscription.created_at),
    }
