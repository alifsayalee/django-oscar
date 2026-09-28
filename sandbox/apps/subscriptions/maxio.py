"""
The process-wide Maxio client, built from Django settings on first use.

The client is created lazily so that forking servers (uWSGI) build it in the
worker after the fork, never in the master. It is closed at interpreter exit.
"""
import atexit
import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import (
    ApiError, BasicAuthCredentials, HttpRequest, HttpResponse, HttpxClient)
from maxio_advanced_billing.server import Environment

logger = logging.getLogger(__name__)

T = TypeVar('T')

# MAXIO_ENVIRONMENT values accepted, mapped to the SDK's environment names.
# An unknown value fails loudly rather than silently falling back to "us".
ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

DEFAULT_TIMEOUT = 10.0
# Creates can take several seconds at Maxio; a timeout there costs a lookup and
# an unsettled outcome, so writes get longer than reads.
WRITE_TIMEOUT = 30.0

# Failures raised before the request left this process: nothing reached Maxio.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

READ_ATTEMPTS = 3
RETRYABLE_STATUSES = {429, 502, 503, 504}
MAX_RETRY_DELAY = 2.0

_client: MaxioAdvancedBillingClient | None = None
_lock = threading.Lock()


class LoggingTransport:
    """Logs method, path and status of every Maxio call - never headers or bodies."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except httpx.HTTPError as exc:
            logger.warning('Maxio %s %s failed: %s', request.method, path, type(exc).__name__)
            raise
        logger.info('Maxio %s %s -> %s (%.0f ms)', request.method, path,
                    response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def _setting(name: str) -> str:
    value = getattr(settings, name, None)
    if not value:
        raise ImproperlyConfigured(f'{name} is not set')
    return str(value)


def server_config(environment: Environment) -> ServerConfigDict:
    """MAXIO_BASE_URL verbatim when set, else the site subdomain in the region's template."""
    base_url = getattr(settings, 'MAXIO_BASE_URL', None)
    if base_url:
        if environment == 'eu':
            return {'production': {'eu': {'base_url': str(base_url)}}}
        return {'production': {'us': {'base_url': str(base_url)}}}
    site = _setting('MAXIO_SITE_SUBDOMAIN')
    if environment == 'eu':
        return {'production': {'eu': {'site': site}}}
    return {'production': {'us': {'site': site}}}


def build_client() -> MaxioAdvancedBillingClient:
    environment_name = str(getattr(settings, 'MAXIO_ENVIRONMENT', None) or 'us').lower()
    if environment_name not in ENVIRONMENTS:
        raise ImproperlyConfigured(
            f'MAXIO_ENVIRONMENT must be one of {sorted(ENVIRONMENTS)}, got {environment_name!r}')
    environment = ENVIRONMENTS[environment_name]

    timeout = float(getattr(settings, 'MAXIO_TIMEOUT', None) or DEFAULT_TIMEOUT)
    return MaxioAdvancedBillingClient(
        environment=environment,
        server_config=server_config(environment),
        # Maxio authenticates with the API key as the username and "x" as the password.
        basic_auth=BasicAuthCredentials(username=_setting('MAXIO_API_KEY'), password='x'),
        # A custom transport ignores the client's timeout, so it is set here.
        custom_http_client=LoggingTransport(HttpxClient(timeout=timeout)),
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: MaxioAdvancedBillingClient | None) -> None:
    """Replace the process-wide client (tests inject a stub transport this way)."""
    global _client
    with _lock:
        _client = client


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


def _retry_delay(attempt: int, exc: Exception) -> float:
    if isinstance(exc, ApiError):
        retry_after: str | None = exc.response.headers.get('retry-after')
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), MAX_RETRY_DELAY)
    return min(0.25 * 2.0 ** (attempt - 1), MAX_RETRY_DELAY)


def read(call: Callable[[], T]) -> T:
    """
    Run an idempotent read, retrying transient failures a bounded number of
    times. Never use this for a write: a write goes through the safe write.
    """
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return call()
        except (ApiError, httpx.RequestError) as exc:
            transient = (isinstance(exc, httpx.RequestError)
                         or exc.status_code in RETRYABLE_STATUSES)
            if not transient or attempt == READ_ATTEMPTS:
                raise
            delay = _retry_delay(attempt, exc)
            logger.info('Maxio read failed (%s), retrying in %.2fs', type(exc).__name__, delay)
            time.sleep(delay)
    raise AssertionError('unreachable')
