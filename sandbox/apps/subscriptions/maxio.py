"""
The one Maxio Advanced Billing client this process uses.

The sandbox runs sync Django under WSGI, so this is the sync client. It is
built lazily on first use (after any worker fork), reused for the life of the
process and closed at exit.
"""
import atexit
import logging
import threading
import time
from typing import Final

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import HttpClient, HttpRequest, HttpResponse, HttpxClient
from maxio_advanced_billing.server import Environment, ServerConfigDict

logger = logging.getLogger('apps.subscriptions.maxio')

# Explicit map: an unknown value fails instead of silently selecting "us".
_ENVIRONMENTS: Final[dict[str, Environment]] = {'us': 'us', 'eu': 'eu'}

_client: MaxioAdvancedBillingClient | None = None
_lock = threading.Lock()


class LoggingTransport:
    """Logs method, path and status of every Maxio call. Never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = request.url.split('?', 1)[0]
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('maxio %s %s -> %s (%.0f ms)', request.method, path,
                           type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info('maxio %s %s -> %s (%.0f ms)', request.method, path,
                    response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def _environment() -> Environment:
    value = str(settings.MAXIO_ENVIRONMENT).strip().lower()
    try:
        return _ENVIRONMENTS[value]
    except KeyError:
        raise ImproperlyConfigured(
            f'MAXIO_ENVIRONMENT must be one of {sorted(_ENVIRONMENTS)}, got {value!r}') from None


def _server_config(environment: Environment) -> ServerConfigDict:
    base_url = settings.MAXIO_BASE_URL
    if base_url:
        # Used verbatim, instead of the subdomain-derived address.
        if environment == 'eu':
            return {'production': {'eu': {'base_url': base_url}}}
        return {'production': {'us': {'base_url': base_url}}}
    site = settings.MAXIO_SITE_SUBDOMAIN
    if not site:
        raise ImproperlyConfigured('Set MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL).')
    if environment == 'eu':
        return {'production': {'eu': {'site': site}}}
    return {'production': {'us': {'site': site}}}


def build_client(transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    api_key = settings.MAXIO_API_KEY
    if not api_key:
        # Without it the SDK would send every request unauthenticated, silently.
        raise ImproperlyConfigured('Set MAXIO_API_KEY.')
    environment = _environment()
    if transport is None:
        # A custom transport replaces the client's own, so the timeout is set here.
        transport = HttpxClient(timeout=float(settings.MAXIO_TIMEOUT_SECONDS))
    return MaxioAdvancedBillingClient(
        environment=environment,
        server_config=_server_config(environment),
        # Maxio takes the API key as the basic-auth username and "x" as the password.
        basic_auth={'username': api_key, 'password': 'x'},
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
    """Swap the process client (tests, credential rotation); closes the old one."""
    global _client
    with _lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()
