"""
The Maxio Advanced Billing client for this site.

One long-lived, sync client per process (the sandbox is a WSGI app), built
lazily on first use so that forking servers build it after the fork, and
closed at interpreter exit.
"""
from __future__ import annotations

import atexit
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import (
    ApiError, BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient)
from maxio_advanced_billing.models.enums import CollectionMethod
from maxio_advanced_billing.server import Environment, ProductionUsConfigDict, ServerConfigDict

logger = logging.getLogger('apps.subscriptions.maxio')

T = TypeVar('T')

# MAXIO_ENVIRONMENT value -> SDK environment. The API gateway environment
# needs a connector bearer token rather than the site API key, so it is not
# offered here.
ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

# Reads are retried on these; writes never are (see services.py)
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
READ_ATTEMPTS = 3
READ_BACKOFF_SECONDS = (0.5, 1.0)
MAX_RETRY_AFTER_SECONDS = 5.0

_client: MaxioAdvancedBillingClient | None = None
_client_lock = threading.Lock()


class LoggingTransport:
    """Wraps the SDK's transport to log method, URL, status and latency (never headers or bodies)."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except httpx.HTTPError as e:
            logger.warning('Maxio %s %s failed after %.0f ms: %s', request.method, request.url,
                           (time.monotonic() - started) * 1000, type(e).__name__)
            raise
        logger.info('Maxio %s %s -> %s (%.0f ms)', request.method, request.url,
                    response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def environment() -> Environment:
    value = str(settings.MAXIO_ENVIRONMENT).strip().lower()
    try:
        return ENVIRONMENTS[value]
    except KeyError:
        raise ImproperlyConfigured(
            f'MAXIO_ENVIRONMENT must be one of {sorted(ENVIRONMENTS)}') from None


def server_config(env: Environment) -> ServerConfigDict:
    """Point the ``production`` server at our site, or at MAXIO_BASE_URL verbatim when set."""
    base_url = str(settings.MAXIO_BASE_URL).strip()
    subdomain = str(settings.MAXIO_SITE_SUBDOMAIN).strip()
    if not base_url and not subdomain:
        raise ImproperlyConfigured('MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL) is not set')
    server: ProductionUsConfigDict = {'base_url': base_url} if base_url else {'site': subdomain}
    if env == 'eu':
        return {'production': {'eu': server}}
    return {'production': {'us': server}}


def product_family() -> str:
    family = str(settings.MAXIO_DEFAULT_PRODUCT_FAMILY).strip()
    if not family:
        raise ImproperlyConfigured('MAXIO_DEFAULT_PRODUCT_FAMILY is not set')
    return family


def configured_collection_method() -> CollectionMethod | None:
    """MAXIO_PAYMENT_COLLECTION_METHOD, or None to derive it from the site."""
    value = str(settings.MAXIO_PAYMENT_COLLECTION_METHOD).strip().lower()
    if not value:
        return None
    try:
        return CollectionMethod(value)
    except ValueError:
        raise ImproperlyConfigured(
            f'MAXIO_PAYMENT_COLLECTION_METHOD must be one of {[m.value for m in CollectionMethod]}') from None


def timeout() -> float:
    return float(settings.MAXIO_TIMEOUT)


def reference_prefix() -> str:
    """A prefix unique to this install, so references never collide on a shared Maxio site."""
    prefix = str(settings.MAXIO_REFERENCE_PREFIX).strip()
    if prefix:
        return prefix
    digest = hashlib.sha256(str(settings.SECRET_KEY).encode()).hexdigest()[:8]
    return f'oscar-{digest}'


def build_client(transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    api_key = str(settings.MAXIO_API_KEY).strip()
    if not api_key:
        # Without it the SDK would silently send unauthenticated requests
        raise ImproperlyConfigured('MAXIO_API_KEY is not set')
    env = environment()
    return MaxioAdvancedBillingClient(
        environment=env,
        server_config=server_config(env),
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=timeout())),
        # Maxio takes the API key as the Basic-auth user and "x" as the password
        basic_auth=BasicAuthCredentials(username=api_key, password='x'),
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


atexit.register(close_client)


@contextmanager
def use_client(client: MaxioAdvancedBillingClient) -> Iterator[MaxioAdvancedBillingClient]:
    """Temporarily replace the process client (tests)."""
    global _client
    previous = _client
    _client = client
    try:
        yield client
    finally:
        _client = previous


def _retry_delay(error: BaseException, attempt: int) -> float | None:
    """Seconds to wait before retrying a read, or None when it must not be retried."""
    if isinstance(error, ApiError):
        if error.status_code not in RETRYABLE_STATUSES:
            return None
        retry_after = error.response.headers.get('retry-after')
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
        return READ_BACKOFF_SECONDS[min(attempt, len(READ_BACKOFF_SECONDS) - 1)]
    if isinstance(error, httpx.TransportError):
        return READ_BACKOFF_SECONDS[min(attempt, len(READ_BACKOFF_SECONDS) - 1)]
    return None


def read(call: Callable[[], T]) -> T:
    """
    Run an idempotent Maxio read with bounded retries.

    Only transport errors and 429/5xx are retried; a 4xx or an unreadable
    body never is. Never use this for a write.
    """
    for attempt in range(READ_ATTEMPTS):
        try:
            return call()
        except (ApiError, httpx.TransportError) as e:
            delay = _retry_delay(e, attempt)
            if delay is None or attempt == READ_ATTEMPTS - 1:
                raise
            logger.info('Retrying Maxio read in %.1fs after %s', delay, type(e).__name__)
            time.sleep(delay)
    raise AssertionError('unreachable')
