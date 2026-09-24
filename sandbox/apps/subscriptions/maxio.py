"""
The one Maxio Advanced Billing client for this process.

Django here runs under WSGI and is synchronous, so this is the sync client.
It is built lazily on first use (so a forking server builds it after the
fork), shared by every thread, and closed at interpreter exit.
"""

import atexit
import logging
import threading
import time
from typing import Callable, TypeVar

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
)
from maxio_advanced_billing.models.enums import CollectionMethod
from maxio_advanced_billing.server import (
    Environment,
    ProductionEuConfigDict,
    ProductionUsConfigDict,
    ServerConfigDict,
)

logger = logging.getLogger("apps.subscriptions.maxio")

# Explicit map: an unknown value fails loudly instead of silently falling
# back to the SDK's default environment.
ENVIRONMENTS: dict[str, Environment] = {"us": "us", "eu": "eu"}

T = TypeVar("T")

_lock = threading.Lock()
_client: MaxioAdvancedBillingClient | None = None


class BillingNotConfigured(Exception):
    """Maxio credentials/settings are absent; the billing API is unavailable."""


class LoggingTransport:
    """
    Wraps the SDK's transport to log method, URL, status and latency.

    Never headers (the Authorization header carries the API key) or bodies.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "maxio %s %s -> %s (%.0f ms)",
                request.method, request.url, type(exc).__name__,
                (time.monotonic() - started) * 1000,
            )
            raise
        logger.info(
            "maxio %s %s -> %s (%.0f ms)",
            request.method, request.url, response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def is_configured() -> bool:
    return bool(
        settings.MAXIO_API_KEY
        and (settings.MAXIO_SITE_SUBDOMAIN or settings.MAXIO_BASE_URL)
        and settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    )


def environment() -> Environment:
    value = (settings.MAXIO_ENVIRONMENT or "").strip().lower()
    try:
        return ENVIRONMENTS[value]
    except KeyError:
        raise ImproperlyConfigured(
            "MAXIO_ENVIRONMENT must be one of %s, got %r" % (sorted(ENVIRONMENTS), value)
        ) from None


def collection_method() -> CollectionMethod:
    value = (settings.MAXIO_PAYMENT_COLLECTION_METHOD or "").strip().lower()
    try:
        return CollectionMethod(value)
    except ValueError:
        raise ImproperlyConfigured(
            "MAXIO_PAYMENT_COLLECTION_METHOD must be one of %s, got %r"
            % (sorted(m.value for m in CollectionMethod), value)
        ) from None


def server_config(env: Environment) -> ServerConfigDict:
    # Both environments' production variants carry the same two fields.
    variant: ProductionUsConfigDict = {}
    if settings.MAXIO_SITE_SUBDOMAIN:
        variant["site"] = settings.MAXIO_SITE_SUBDOMAIN
    if settings.MAXIO_BASE_URL:
        # The override is used verbatim as the API base address.
        variant["base_url"] = settings.MAXIO_BASE_URL
    if env == "eu":
        eu: ProductionEuConfigDict = {**variant}
        return {"production": {"eu": eu}}
    return {"production": {"us": variant}}


def build_client(transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    if not is_configured():
        raise BillingNotConfigured(
            "Set MAXIO_API_KEY, MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL) and "
            "MAXIO_DEFAULT_PRODUCT_FAMILY to enable subscription billing."
        )
    env = environment()
    if transport is None:
        # With a custom transport the client's own timeout never reaches the
        # wire, so the timeout lives here.
        transport = LoggingTransport(HttpxClient(timeout=float(settings.MAXIO_TIMEOUT)))
    return MaxioAdvancedBillingClient(
        environment=env,
        server_config=server_config(env),
        custom_http_client=transport,
        basic_auth=BasicAuthCredentials(username=settings.MAXIO_API_KEY, password=""),
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: MaxioAdvancedBillingClient | None) -> None:
    """Replace the process client (tests; credential rotation). Closes the old one."""
    global _client
    with _lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    set_client(None)


# --- bounded retries, for idempotent reads only ------------------------------

# Failures that happen before the request leaves: nothing reached Maxio.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
READ_ATTEMPTS = 3


def read_with_retry(call: Callable[[], T]) -> T:
    """
    Run a GET. Retries only connect-phase failures and 429/502/503/504,
    honouring Retry-After (capped). Never used for writes.
    """
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return call()
        except NEVER_SENT:
            if attempt == READ_ATTEMPTS:
                raise
            delay = 0.25 * 2 ** (attempt - 1)
        except ApiError as exc:
            if exc.status_code not in RETRYABLE_STATUSES or attempt == READ_ATTEMPTS:
                raise
            delay = _retry_after(exc) or 0.25 * 2 ** (attempt - 1)
        time.sleep(min(delay, 2.0))
    raise AssertionError("unreachable")  # pragma: no cover


def _retry_after(exc: ApiError) -> float | None:
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
