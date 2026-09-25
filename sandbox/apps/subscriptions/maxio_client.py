"""
The one Maxio Advanced Billing client this process uses.

Built lazily on first use (so a forking server builds it after the fork),
shared across threads, and closed at interpreter exit. Every setting is read
from Django settings, which read the environment.
"""
import atexit
import logging
import threading
import time

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import (
    BasicAuthCredentials, HttpRequest, HttpResponse, HttpxClient)
from maxio_advanced_billing.server import (
    Environment, ProductionConfig, ProductionEuConfig,
    ProductionUsConfig, ServerConfig)

logger = logging.getLogger(__name__)

# MAXIO_ENVIRONMENT names the hosting region of the Maxio site. Anything else
# is a configuration error rather than a silent fall-through to the default.
ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

_lock = threading.Lock()
_client: MaxioAdvancedBillingClient | None = None


class LoggingTransport:
    """Logs method, URL and status of every Maxio request - never headers or bodies."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('Maxio %s %s -> %s (%.0f ms)', request.method, request.url,
                           type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info('Maxio %s %s -> %s (%.0f ms)', request.method, request.url,
                    response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def timeout_seconds() -> float:
    return float(getattr(settings, 'MAXIO_TIMEOUT_SECONDS', 15.0))


def product_family() -> str:
    family: str = getattr(settings, 'MAXIO_DEFAULT_PRODUCT_FAMILY', '')
    if not family:
        raise ImproperlyConfigured('MAXIO_DEFAULT_PRODUCT_FAMILY is not set')
    return family


def build_client() -> MaxioAdvancedBillingClient:
    api_key: str = getattr(settings, 'MAXIO_API_KEY', '')
    if not api_key:
        # Without it the SDK would send every request unauthenticated.
        raise ImproperlyConfigured('MAXIO_API_KEY is not set')

    region = str(getattr(settings, 'MAXIO_ENVIRONMENT', '') or 'us').strip().lower()
    try:
        environment = ENVIRONMENTS[region]
    except KeyError:
        raise ImproperlyConfigured(
            f'MAXIO_ENVIRONMENT must be one of {sorted(ENVIRONMENTS)}, got {region!r}') from None

    return MaxioAdvancedBillingClient(
        environment=environment,
        server_config=server_config(environment),
        custom_http_client=LoggingTransport(HttpxClient(timeout=timeout_seconds())),
        basic_auth=BasicAuthCredentials(username=api_key, password='x'),
    )


def server_config(environment: Environment) -> ServerConfig:
    """
    Point the ``production`` server (the one every Advanced Billing operation
    uses) at the site: MAXIO_BASE_URL verbatim when set, else the subdomain
    substituted into the region's own base-URL template.
    """
    base_url: str = getattr(settings, 'MAXIO_BASE_URL', '')
    subdomain: str = getattr(settings, 'MAXIO_SITE_SUBDOMAIN', '')
    if not base_url and not subdomain:
        raise ImproperlyConfigured('Set MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL)')
    if environment == 'eu':
        eu = ProductionEuConfig(base_url=base_url) if base_url else ProductionEuConfig(site=subdomain)
        return ServerConfig(production=ProductionConfig(eu=eu))
    us = ProductionUsConfig(base_url=base_url) if base_url else ProductionUsConfig(site=subdomain)
    return ServerConfig(production=ProductionConfig(us=us))


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: MaxioAdvancedBillingClient | None) -> None:
    """Replace the process-wide client (tests, credential rotation); closes the old one."""
    global _client
    with _lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()
