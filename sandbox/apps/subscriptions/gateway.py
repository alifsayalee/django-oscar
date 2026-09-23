"""
The one place this app talks to Maxio Advanced Billing.

Everything above this module deals in plain Python values and the
``ProviderError`` family below; nothing else imports the SDK's exception types
or knows about httpx.

- One long-lived, lazily built sync client per process (built on first use, so
  after any worker fork, and never at import time).
- One error ladder (``translate``) that maps every failure the SDK can surface
  onto a status for our API and an ``outcome_unknown`` flag for writes.
- Reads are retried on transient failures; writes are never retried here --
  the services layer reconciles them by the reference it sent.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import (
    ApiError,
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    RawError,
    UnsetType,
)
from maxio_advanced_billing.models import (
    CreateCustomer,
    CreateCustomerRequest,
    CreateSubscription,
    CreateSubscriptionRequest,
    Customer,
    CustomerErrorResponse1,
    ErrorListResponse1,
    Subscription,
)
from maxio_advanced_billing.models.enums import CollectionMethod
from maxio_advanced_billing.server import Environment
from pydantic import ValidationError

logger = logging.getLogger("apps.subscriptions.maxio")

T = TypeVar("T")

# Explicit map: an unknown value must fail, never fall through to the SDK's
# silent "us" default.
ENVIRONMENTS: dict[str, Environment] = {"us": "us", "eu": "eu"}

READ_ATTEMPTS = 3
READ_BACKOFF_SECONDS = 0.3
MAX_RETRY_AFTER_SECONDS = 5.0
PLANS_MAX_PAGES = 10
PLANS_PER_PAGE = 200  # the provider's documented maximum

# Transport failures that happen before the request leaves: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)
TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})
CALLER_FAULT_STATUSES = frozenset({400, 404, 409, 422})


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ProviderError(Exception):
    """A Maxio call did not produce a usable answer.

    ``status_code`` is the status our API should answer with; ``outcome_unknown``
    says whether a write may nevertheless have taken effect upstream.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool,
        messages: list[str] | None = None,
        provider_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.messages = messages or []
        self.provider_status = provider_status


class ProviderConfigError(ProviderError):
    """Our configuration or credentials are wrong; nothing was done."""


class ProviderRejected(ProviderError):
    """Maxio refused the request because of the caller's input."""


class ProviderUnavailable(ProviderError):
    """Maxio could not be reached, or answered with an outage."""


class ProviderFailure(ProviderUnavailable):
    """Maxio answered with an error we do not attribute to the caller."""


class ProviderUnreadable(ProviderError):
    """Maxio answered, but the body could not be decoded."""


def _error_messages(error: object) -> list[str]:
    if isinstance(error, ErrorListResponse1):
        return [str(m) for m in error.errors]
    if isinstance(error, CustomerErrorResponse1):
        return _flatten(error.to_dict(exclude_unset=True).get("errors"))
    if isinstance(error, RawError):
        try:
            return _flatten(error.json())
        except ValueError:
            text = error.text().strip()
            return [text[:500]] if text else []
    if isinstance(error, str):
        return [error] if error else []
    return []


def _flatten(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [m for item in value for m in _flatten(item)]
    if isinstance(value, dict):
        if "errors" in value:
            return _flatten(value["errors"])
        out: list[str] = []
        for key, item in value.items():
            out.extend(f"{key}: {m}" for m in _flatten(item))
        return out
    return [str(value)]


def translate(exc: BaseException) -> ProviderError:
    """Map any failure an SDK call can raise onto one ``ProviderError``."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        status = exc.status_code
        if status in (401, 403):
            return ProviderConfigError(
                502, "Billing provider refused our credentials.",
                outcome_unknown=False, provider_status=status,
            )
        if status == 429:
            return ProviderUnavailable(
                503, "Billing provider is rate limiting; try again shortly.",
                outcome_unknown=False, provider_status=status,
            )
        if status in CALLER_FAULT_STATUSES:
            return ProviderRejected(
                status, "Billing provider rejected the request.",
                outcome_unknown=False, messages=_error_messages(exc.error), provider_status=status,
            )
        # A 5xx on a write may still have landed; an unmapped 4xx did not.
        return ProviderFailure(
            502, "Billing provider error.", outcome_unknown=status >= 500, provider_status=status,
        )
    if isinstance(exc, (ValidationError, ValueError)):
        return ProviderUnreadable(
            502, "Billing provider returned an unreadable response.", outcome_unknown=True,
        )
    if isinstance(exc, NEVER_SENT):
        return ProviderUnavailable(502, "Billing provider is unreachable.", outcome_unknown=False)
    if isinstance(exc, httpx.RequestError):
        return ProviderUnavailable(504, "Billing provider did not respond.", outcome_unknown=True)
    raise exc


_SDK_FAILURES = (ApiError, ValidationError, ValueError, httpx.RequestError)


def _write(fn: Callable[[], T]) -> T:
    """Run a write exactly once; translate whatever goes wrong."""
    try:
        return fn()
    except _SDK_FAILURES as exc:
        raise translate(exc) from exc


def _retry_delay(exc: BaseException, attempt: int) -> float | None:
    """Seconds to wait before retrying a read, or None when it must not be retried."""
    if isinstance(exc, ApiError):
        if exc.status_code not in TRANSIENT_STATUSES:
            return None
        header = exc.response.headers.get("retry-after")
        if header:
            try:
                return min(max(float(header), 0.0), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
    elif not isinstance(exc, httpx.TransportError):
        return None  # decode failures and the like cannot succeed on a second try
    return float(READ_BACKOFF_SECONDS * 2.0**attempt)


def _read(fn: Callable[[], T]) -> T:
    """Run an idempotent read, retrying transient failures a bounded number of times."""
    for attempt in range(READ_ATTEMPTS):
        try:
            return fn()
        except _SDK_FAILURES as exc:
            delay = _retry_delay(exc, attempt)
            if delay is None or attempt == READ_ATTEMPTS - 1:
                raise translate(exc) from exc
            logger.warning("Maxio read failed (%s); retrying in %.1fs", type(exc).__name__, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _not_found_is_none(fn: Callable[[], T]) -> T | None:
    """A read whose provider 404 means absence. Any other failure propagates."""
    try:
        return _read(fn)
    except ProviderRejected as exc:
        if exc.provider_status == 404:
            return None
        raise


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class LoggingTransport:
    """Wraps the SDK transport and logs method, URL, status and duration.

    Headers and bodies are never logged: the Authorization header carries the API key.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "Maxio %s %s -> %s (%.0f ms)", request.method, request.url,
                type(exc).__name__, (time.monotonic() - started) * 1000,
            )
            raise
        logger.info(
            "Maxio %s %s -> %s (%.0f ms)", request.method, request.url,
            response.status_code, (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    """Build a client from Django settings. Raises ImproperlyConfigured when incomplete."""
    missing = [
        name
        for name in ("MAXIO_API_KEY", "MAXIO_DEFAULT_PRODUCT_FAMILY")
        if not getattr(settings, name, "")
    ]
    base_url: str = getattr(settings, "MAXIO_BASE_URL", "") or ""
    subdomain: str = getattr(settings, "MAXIO_SITE_SUBDOMAIN", "") or ""
    if not base_url and not subdomain:
        missing.append("MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL)")
    if missing:
        raise ImproperlyConfigured("Maxio billing is not configured; missing: " + ", ".join(missing))

    env_name = str(getattr(settings, "MAXIO_ENVIRONMENT", "us") or "us").strip().lower()
    if env_name not in ENVIRONMENTS:
        raise ImproperlyConfigured(
            f"MAXIO_ENVIRONMENT must be one of {sorted(ENVIRONMENTS)}, got {env_name!r}"
        )
    environment = ENVIRONMENTS[env_name]

    # Several servers x several environments: the base URL sits inside the environment's variant.
    production: dict[str, str] = {"base_url": base_url} if base_url else {"site": subdomain}
    server_config: Any = {"production": {environment: production}}

    timeout = float(getattr(settings, "MAXIO_TIMEOUT_SECONDS", 10.0))
    if transport is None:
        # The timeout must be set on the transport we pass: the client's own
        # ``timeout=`` only configures the transport it would build itself.
        transport = LoggingTransport(HttpxClient(timeout=timeout))

    return MaxioAdvancedBillingClient(
        environment=environment,
        server_config=server_config,
        timeout=timeout,
        custom_http_client=transport,
        # Maxio's Basic auth: the API key is the username, "x" the password.
        basic_auth=BasicAuthCredentials(username=settings.MAXIO_API_KEY, password="x"),
    )


_client: MaxioAdvancedBillingClient | None = None
_client_lock = threading.Lock()


def get_client() -> MaxioAdvancedBillingClient:
    global _client  # pylint: disable=global-statement
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def use_client(client: MaxioAdvancedBillingClient | None) -> MaxioAdvancedBillingClient | None:
    """Replace the process-wide client (tests); returns the previous one without closing it."""
    global _client, _collection_method  # pylint: disable=global-statement
    with _client_lock:
        previous, _client = _client, client
        _collection_method = None  # site facts belong to the client's site
    return previous


@atexit.register
def close_client() -> None:
    global _client  # pylint: disable=global-statement
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def default_product_family() -> str:
    return str(settings.MAXIO_DEFAULT_PRODUCT_FAMILY)


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def present(value: T | UnsetType | None) -> T | None:
    """UNSET -> None, so SDK values never leak the sentinel past this module."""
    return None if isinstance(value, UnsetType) else value


@dataclass(frozen=True)
class Plan:
    handle: str
    name: str
    description: str
    price_in_cents: int
    interval: int | None
    interval_unit: str | None
    product_id: int | None


@dataclass(frozen=True)
class PlanList:
    plans: list[Plan]
    truncated: bool


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


def list_plans() -> PlanList:
    """Live, non-archived products of the default product family."""
    client = get_client()
    family = "handle:" + default_product_family()
    plans: list[Plan] = []
    truncated = False
    page = 1
    for _ in range(PLANS_MAX_PAGES):
        batch = _read(
            partial(
                client.product_families.list_products_for_product_family,
                family, page=page, per_page=PLANS_PER_PAGE,
            )
        )
        for item in batch:
            product = item.product
            handle = present(product.handle)
            price = present(product.price_in_cents)
            if present(product.archived_at) is not None or not handle or price is None:
                continue
            unit = present(product.interval_unit)
            plans.append(
                Plan(
                    handle=handle,
                    name=present(product.name) or handle,
                    description=present(product.description) or "",
                    price_in_cents=price,
                    interval=present(product.interval),
                    interval_unit=str(unit) if unit is not None else None,
                    product_id=present(product.id),
                )
            )
        if len(batch) < PLANS_PER_PAGE:
            break
        page += 1
    else:
        truncated = True
        logger.warning("Maxio plan list truncated at %d pages", PLANS_MAX_PAGES)
    return PlanList(plans=plans, truncated=truncated)


def find_customer_by_reference(reference: str) -> Customer | None:
    client = get_client()
    found = _not_found_is_none(lambda: client.customers.read_customer_by_reference(reference))
    return found.customer if found is not None else None


def create_customer(*, first_name: str, last_name: str, email: str, reference: str) -> Customer:
    client = get_client()
    body = CreateCustomerRequest(
        customer=CreateCustomer(
            first_name=first_name, last_name=last_name, email=email, reference=reference,
        )
    )
    return _write(lambda: client.customers.create_customer(body=body)).customer


_collection_method: CollectionMethod | None = None


def invoice_collection_method() -> CollectionMethod:
    """The collection method that bills by invoice instead of charging a stored card.

    This API captures no payment method, so "automatic" collection fails at
    signup ("No payment method was on file"). Relationship Invoicing sites call
    invoice billing ``remittance``; legacy Statements sites call it ``invoice``.
    """
    global _collection_method  # pylint: disable=global-statement
    if _collection_method is None:
        client = get_client()
        site = _read(client.sites.read_site).site
        relationship = present(site.relationship_invoicing_enabled)
        # Unknown -> the current architecture (Relationship Invoicing).
        _collection_method = CollectionMethod.INVOICE if relationship is False else CollectionMethod.REMITTANCE
    return _collection_method


def create_subscription(*, product_handle: str, customer_id: int, reference: str) -> Subscription | None:
    """Returns None when Maxio answered 2xx without a subscription (outcome unknown)."""
    client = get_client()
    collection_method = invoice_collection_method()
    body = CreateSubscriptionRequest(
        subscription=CreateSubscription(
            product_handle=product_handle, customer_id=customer_id, reference=reference,
            payment_collection_method=collection_method,
        )
    )
    return present(_write(lambda: client.subscriptions.create_subscription(body=body)).subscription)


def find_subscription_by_reference(reference: str) -> Subscription | None:
    client = get_client()
    found = _not_found_is_none(lambda: client.subscriptions.find_subscription(reference=reference))
    return present(found.subscription) if found is not None else None


def read_subscription(subscription_id: int) -> Subscription | None:
    client = get_client()
    found = _not_found_is_none(lambda: client.subscriptions.read_subscription(subscription_id))
    return present(found.subscription) if found is not None else None


def list_customer_subscriptions(customer_id: int) -> list[Subscription]:
    client = get_client()
    items = _read(lambda: client.customers.list_customer_subscriptions(customer_id))
    return [s for s in (present(i.subscription) for i in items) if s is not None]


__all__ = [
    "Plan",
    "PlanList",
    "ProviderConfigError",
    "ProviderError",
    "ProviderFailure",
    "ProviderRejected",
    "ProviderUnavailable",
    "ProviderUnreadable",
    "build_client",
    "close_client",
    "create_customer",
    "create_subscription",
    "find_customer_by_reference",
    "find_subscription_by_reference",
    "get_client",
    "list_customer_subscriptions",
    "list_plans",
    "present",
    "read_subscription",
    "translate",
    "use_client",
]
