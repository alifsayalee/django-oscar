"""
Construction and lifetime of the Maxio Advanced Billing client.

One sync client per process (the sandbox runs under WSGI), built lazily on first
use so that forking servers construct it after the fork, and closed at exit.
"""
import atexit
import logging
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import (
    BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient)
from maxio_advanced_billing.server import Environment, ServerConfigDict

logger = logging.getLogger(__name__)

_ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

_lock = threading.Lock()
_client: MaxioAdvancedBillingClient | None = None


class LoggingTransport:
    """
    Wraps the SDK's own transport to log method, path, status and latency.

    Headers and bodies are never logged: the Authorization header carries the
    API key.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning("Maxio %s %s failed after %.0f ms: %s", request.method, path,
                           (time.monotonic() - started) * 1000, type(exc).__name__)
            raise
        logger.info("Maxio %s %s -> %s (%.0f ms)", request.method, path, response.status_code,
                    (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def _environment() -> Environment:
    value = str(settings.MAXIO_ENVIRONMENT).strip().lower()
    try:
        return _ENVIRONMENTS[value]
    except KeyError:
        raise ImproperlyConfigured(
            "MAXIO_ENVIRONMENT must be one of %s, got %r" % (sorted(_ENVIRONMENTS), value)) from None


def _server_config(environment: Environment) -> ServerConfigDict:
    base_url = settings.MAXIO_BASE_URL.strip()
    subdomain = settings.MAXIO_SITE_SUBDOMAIN.strip()
    if not base_url and not subdomain:
        raise ImproperlyConfigured("Set MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL) to reach Maxio")
    if environment == 'eu':
        return {'production': {'eu': {'base_url': base_url} if base_url else {'site': subdomain}}}
    return {'production': {'us': {'base_url': base_url} if base_url else {'site': subdomain}}}


def build_client(http_client: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    """
    Build a client from settings. ``http_client`` replaces the network
    transport (tests pass a stub here).
    """
    api_key = settings.MAXIO_API_KEY.strip()
    if not api_key:
        # Without credentials the SDK would silently send unauthenticated requests.
        raise ImproperlyConfigured("MAXIO_API_KEY is not set")
    environment = _environment()
    timeout = float(settings.MAXIO_TIMEOUT)
    transport = http_client if http_client is not None else HttpxClient(timeout=timeout)
    return MaxioAdvancedBillingClient(
        environment=environment,
        timeout=timeout,
        server_config=_server_config(environment),
        # Maxio authenticates with the API key as the Basic username and "x" as the password.
        basic_auth=BasicAuthCredentials(username=api_key, password='x'),
        custom_http_client=LoggingTransport(transport),
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: MaxioAdvancedBillingClient | None) -> None:
    """Replace the process-wide client, closing the previous one."""
    global _client
    with _lock:
        previous, _client = _client, client
    if previous is not None and previous is not client:
        previous.close()


atexit.register(set_client, None)
