"""
The one Maxio Advanced Billing client this process uses.

Built lazily on first use (never at import, so tests and management commands run
without credentials, and a forking server builds it after the fork), reused for
the life of the process, and closed at exit.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient
from maxio_advanced_billing.server import ServerConfigDict

logger = logging.getLogger('apps.maxio_billing')

_lock = threading.Lock()
_client: MaxioAdvancedBillingClient | None = None


class LoggingTransport:
    """Logs method, path, status and latency of every Maxio call - never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('maxio %s %s -> %s', request.method, path, type(exc).__name__)
            raise
        logger.info('maxio %s %s -> %s (%.0f ms)', request.method, path, response.status_code,
                    (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def _server_config(environment: str, subdomain: str, base_url: str) -> ServerConfigDict:
    # MAXIO_BASE_URL, when set, replaces the derived address verbatim.
    if environment == 'us':
        if base_url:
            return {'production': {'us': {'base_url': base_url}}}
        return {'production': {'us': {'site': subdomain}}}
    if base_url:
        return {'production': {'eu': {'base_url': base_url}}}
    return {'production': {'eu': {'site': subdomain}}}


def build_client(transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    api_key = settings.MAXIO_API_KEY
    subdomain = settings.MAXIO_SITE_SUBDOMAIN
    base_url = settings.MAXIO_BASE_URL
    missing = [name for name, value in (
        ('MAXIO_API_KEY', api_key),
        ('MAXIO_DEFAULT_PRODUCT_FAMILY', settings.MAXIO_DEFAULT_PRODUCT_FAMILY),
    ) if not value]
    if not subdomain and not base_url:
        missing.append('MAXIO_SITE_SUBDOMAIN')
    if missing:
        raise ImproperlyConfigured('Maxio billing is not configured; missing: ' + ', '.join(missing))

    environment = settings.MAXIO_ENVIRONMENT.strip().lower()
    if environment not in ('us', 'eu'):
        raise ImproperlyConfigured(f'MAXIO_ENVIRONMENT must be "us" or "eu", got {environment!r}')

    timeout = float(settings.MAXIO_TIMEOUT_SECONDS)
    if transport is None:
        # The client's own timeout only reaches a transport it builds itself,
        # so the timeout is set here, on the transport we hand it.
        transport = LoggingTransport(HttpxClient(timeout=timeout))
    return MaxioAdvancedBillingClient(
        environment='us' if environment == 'us' else 'eu',
        timeout=timeout,
        server_config=_server_config(environment, subdomain, base_url),
        custom_http_client=transport,
        # Maxio authenticates with the API key as the username and "x" as the password.
        basic_auth=BasicAuthCredentials(username=api_key, password='x'),
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def use_client(client: MaxioAdvancedBillingClient | None) -> MaxioAdvancedBillingClient | None:
    """Swap the process client (tests); returns the previous one."""
    global _client
    with _lock:
        previous, _client = _client, client
    return previous
