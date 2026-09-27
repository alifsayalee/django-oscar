"""
The one Maxio Advanced Billing client for this process.

Django runs under WSGI with sync views, so this is the sync client. It is built
lazily on first use (after any worker fork), reused for the life of the process
and closed at exit.
"""
import atexit
import logging
import threading
import time

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import (
    BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient)
from maxio_advanced_billing.server import Environment, ServerConfigDict

logger = logging.getLogger(__name__)

# MAXIO_ENVIRONMENT -> SDK environment. An unknown value is a configuration
# error: omitting the environment would silently select "us".
ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

_client: MaxioAdvancedBillingClient | None = None
_client_lock = threading.Lock()


class LoggingTransport:
    """Logs method, URL, status and latency of every Maxio call - never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('maxio %s %s -> %s (%.0f ms)', request.method, request.url,
                           type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info('maxio %s %s -> %s (%.0f ms)', request.method, request.url,
                    response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def _server_config(environment: Environment) -> ServerConfigDict:
    base_url = settings.MAXIO_BASE_URL
    subdomain = settings.MAXIO_SITE_SUBDOMAIN
    if not base_url and not subdomain:
        raise ImproperlyConfigured('MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL) must be set.')
    if environment == 'eu':
        if base_url:
            return {'production': {'eu': {'base_url': base_url}}}
        return {'production': {'eu': {'site': subdomain}}}
    if base_url:
        return {'production': {'us': {'base_url': base_url}}}
    return {'production': {'us': {'site': subdomain}}}


def build_client(transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    """Build a client from Django settings; `transport` replaces the HTTP transport (tests)."""
    api_key = settings.MAXIO_API_KEY
    if not api_key:
        raise ImproperlyConfigured('MAXIO_API_KEY must be set.')
    try:
        environment = ENVIRONMENTS[settings.MAXIO_ENVIRONMENT.strip().lower()]
    except KeyError:
        raise ImproperlyConfigured(
            'MAXIO_ENVIRONMENT must be one of: %s.' % ', '.join(sorted(ENVIRONMENTS))) from None
    timeout = float(settings.MAXIO_TIMEOUT)
    if transport is None:
        # The client's own timeout= only builds its default transport, so set it here.
        transport = LoggingTransport(HttpxClient(timeout=timeout))
    return MaxioAdvancedBillingClient(
        environment=environment,
        timeout=timeout,
        server_config=_server_config(environment),
        custom_http_client=transport,
        # Maxio API keys authenticate as the basic-auth username with password "x".
        basic_auth=BasicAuthCredentials(username=api_key, password='x'),
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client
