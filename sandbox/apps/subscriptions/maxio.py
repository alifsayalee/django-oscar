"""
The Maxio Advanced Billing client for this site.

One long-lived sync client per process, built lazily on first use (so it is
created after any worker fork) and closed at interpreter exit.
"""
import atexit
import logging
import secrets
import threading
import time
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from maxio_advanced_billing import Environment, MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import BasicAuthCredentials, HttpRequest, HttpResponse, HttpxClient
from maxio_advanced_billing.server import ProductionUsConfigDict

logger = logging.getLogger('apps.subscriptions.maxio')

# Hosting regions a site API key can talk to. The API-gateway environment needs a
# connector bearer token instead, so it is deliberately not offered here.
ENVIRONMENTS: dict[str, Environment] = {'us': 'us', 'eu': 'eu'}

_client: MaxioAdvancedBillingClient | None = None
_client_lock = threading.Lock()   # guards construction only; not a write guard


class LoggingTransport:
    """Logs method, path, status and latency of every Maxio call — never headers or bodies."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = request.url.split('?', 1)[0]
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('Maxio %s %s -> %s (%.0f ms)', request.method, path,
                           type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info('Maxio %s %s -> %s (%.0f ms)', request.method, path,
                    response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def _setting(name: str) -> str:
    value = getattr(settings, name, '') or ''
    if not isinstance(value, str):
        raise ImproperlyConfigured(f'{name} must be a string')
    return value.strip()


def build_client(**overrides: Any) -> MaxioAdvancedBillingClient:
    """Build a client from Django settings. ``overrides`` exist for tests (custom_http_client)."""
    api_key = _setting('MAXIO_API_KEY')
    if not api_key:
        raise ImproperlyConfigured('MAXIO_API_KEY is not set')
    env_name = _setting('MAXIO_ENVIRONMENT').lower() or 'us'
    try:
        environment = ENVIRONMENTS[env_name]
    except KeyError:
        raise ImproperlyConfigured(
            f'MAXIO_ENVIRONMENT must be one of {sorted(ENVIRONMENTS)}, got {env_name!r}') from None

    # Every operation this app calls lives on the "production" server of the chosen region.
    base_url = _setting('MAXIO_BASE_URL')
    host: ProductionUsConfigDict = {}
    if base_url:
        host['base_url'] = base_url
    else:
        subdomain = _setting('MAXIO_SITE_SUBDOMAIN')
        if not subdomain:
            raise ImproperlyConfigured('Set MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL)')
        host['site'] = subdomain
    server_config: ServerConfigDict = (
        {'production': {'us': host}} if environment == 'us'
        else {'production': {'eu': {**host}}})

    timeout = float(getattr(settings, 'MAXIO_TIMEOUT', 10.0))
    kwargs: dict[str, Any] = {
        'custom_http_client': LoggingTransport(HttpxClient(timeout=timeout)),
    }
    kwargs.update(overrides)
    return MaxioAdvancedBillingClient(
        environment=environment,
        timeout=timeout,
        server_config=server_config,
        basic_auth=BasicAuthCredentials(username=api_key, password='x'),
        **kwargs,
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: MaxioAdvancedBillingClient | None) -> None:
    """Swap the process client (tests, credential rotation). Closes the previous one."""
    global _client
    with _client_lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()


def product_family() -> str:
    family = _setting('MAXIO_DEFAULT_PRODUCT_FAMILY')
    if not family:
        raise ImproperlyConfigured('MAXIO_DEFAULT_PRODUCT_FAMILY is not set')
    return family


def reference_prefix() -> str:
    """
    A prefix unique to this install, so references never collide across installs
    sharing one Maxio site: MAXIO_REFERENCE_PREFIX, else a random id created once
    per database.
    """
    prefix = _setting('MAXIO_REFERENCE_PREFIX')
    if prefix:
        return prefix
    from .models import BillingInstall

    install = BillingInstall.objects.order_by('pk').first()
    if install is None:
        try:
            with transaction.atomic():
                install = BillingInstall.objects.create(pk=1, install_id=secrets.token_hex(6))
        except IntegrityError:            # another process created it first
            install = BillingInstall.objects.get(pk=1)
    return f'oscar-{install.install_id}'
