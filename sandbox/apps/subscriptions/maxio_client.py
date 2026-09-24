"""
The one Maxio Advanced Billing client for this process.

The sandbox is a sync WSGI app, so this is the SDK's sync client. It owns a
connection pool, so it is built once per process (lazily, i.e. after any
worker fork) and closed at interpreter exit -- never per request.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient
from maxio_advanced_billing.server import Environment

logger = logging.getLogger('apps.subscriptions.maxio')

# Maxio hosting regions this integration supports, keyed by MAXIO_ENVIRONMENT.
# The API-gateway environment authenticates with a connector bearer token,
# which this integration is not configured for.
_ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

# Maxio's basic-auth scheme takes the API key as the username and "x" as the
# password (SDK README: ``curl -u <api_key>:x``).
_API_KEY_PASSWORD = 'x'

_lock = threading.Lock()
_client: MaxioAdvancedBillingClient | None = None


class LoggingTransport:
    """Logs method, URL, status and latency of every Maxio call -- never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                'Maxio %s %s failed after %.0f ms: %s',
                request.method, request.url, (time.monotonic() - started) * 1000, type(exc).__name__)
            raise
        logger.info(
            'Maxio %s %s -> %s (%.0f ms)',
            request.method, request.url, response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def _environment() -> Environment:
    name = str(settings.MAXIO_ENVIRONMENT).strip().lower()
    try:
        return _ENVIRONMENTS[name]
    except KeyError:
        raise ImproperlyConfigured(
            'MAXIO_ENVIRONMENT must be one of %s' % ', '.join(sorted(_ENVIRONMENTS))) from None


def _server_config(environment: Environment) -> ServerConfigDict:
    subdomain = settings.MAXIO_SITE_SUBDOMAIN
    base_url = settings.MAXIO_BASE_URL
    if not subdomain and not base_url:
        raise ImproperlyConfigured('MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL) must be set')
    if environment == 'eu':
        if base_url:
            return {'production': {'eu': {'base_url': base_url, 'site': subdomain}}}
        return {'production': {'eu': {'site': subdomain}}}
    if base_url:
        return {'production': {'us': {'base_url': base_url, 'site': subdomain}}}
    return {'production': {'us': {'site': subdomain}}}


def build_client(transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    """Build a client from Django settings; ``transport`` replaces the network (tests)."""
    api_key = settings.MAXIO_API_KEY
    if not api_key:
        # Omitting basic_auth would silently send every request unauthenticated.
        raise ImproperlyConfigured('MAXIO_API_KEY must be set')
    timeout = float(settings.MAXIO_TIMEOUT)
    environment = _environment()
    return MaxioAdvancedBillingClient(
        environment=environment,
        server_config=_server_config(environment),
        timeout=timeout,
        # The client's timeout= only configures its own default transport, so
        # the transport we supply carries it.
        custom_http_client=LoggingTransport(transport or HttpxClient(timeout=timeout)),
        basic_auth=BasicAuthCredentials(username=api_key, password=_API_KEY_PASSWORD),
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


@contextmanager
def use_client(client: MaxioAdvancedBillingClient) -> Iterator[MaxioAdvancedBillingClient]:
    """Temporarily make ``client`` the process client (tests inject a stub transport this way)."""
    global _client
    with _lock:
        previous, _client = _client, client
    try:
        yield client
    finally:
        with _lock:
            _client = previous
        client.close()


@atexit.register
def _close_client() -> None:
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        client.close()
