"""
The one PayPal Server SDK client this process uses.

Built lazily on first use (so a forking server builds it after the fork),
reused for every request (it owns the connection pool and the cached OAuth
token) and closed at interpreter exit.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import ClientCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient

logger = logging.getLogger('apps.paypal_payments.paypal')

# The only host the SDK declares. Any other environment must name its host
# through PAYPAL_BASE_URL rather than fall through to a default.
BASE_URLS = {
    'sandbox': 'https://api-m.sandbox.paypal.com',
}


class PayPalNotConfigured(ImproperlyConfigured):  # type: ignore[misc]
    pass


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str
    client_secret: str
    base_url: str
    currency: str
    timeout: float
    reference_prefix: str


def paypal_config() -> PayPalConfig:
    missing = [name for name in (
        'PAYPAL_CLIENT_ID', 'PAYPAL_CLIENT_SECRET', 'PAYPAL_ENVIRONMENT', 'PAYPAL_CURRENCY')
        if not getattr(settings, name, '')]
    if missing:
        raise PayPalNotConfigured('PayPal is not configured: %s unset' % ', '.join(missing))
    base_url = getattr(settings, 'PAYPAL_BASE_URL', '') or ''
    if not base_url:
        environment = settings.PAYPAL_ENVIRONMENT.strip().lower()
        if environment not in BASE_URLS:
            raise PayPalNotConfigured(
                'PAYPAL_ENVIRONMENT=%r has no known API host; set PAYPAL_BASE_URL' % environment)
        base_url = BASE_URLS[environment]
    return PayPalConfig(
        client_id=settings.PAYPAL_CLIENT_ID,
        client_secret=settings.PAYPAL_CLIENT_SECRET,
        base_url=base_url,
        currency=settings.PAYPAL_CURRENCY.strip().upper(),
        timeout=float(getattr(settings, 'PAYPAL_TIMEOUT', 20.0)),
        reference_prefix=getattr(settings, 'PAYPAL_REFERENCE_PREFIX', 'oscar-sandbox'),
    )


class LoggingTransport:
    """Logs method, path, status and latency of every PayPal call.

    Never headers (the bearer token) and never bodies (card details).
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('PayPal %s %s -> %s after %.0f ms', request.method, path,
                           type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info('PayPal %s %s -> %s (%.0f ms, debug id %s)', request.method, path,
                    response.status_code, (time.monotonic() - started) * 1000,
                    response.headers.get('paypal-debug-id', '-'))
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(config: PayPalConfig, transport: HttpClient | None = None) -> PayPalServerSdkClient:
    return PayPalServerSdkClient(
        base_url=config.base_url,
        timeout=config.timeout,
        oauth2=ClientCredentials(client_id=config.client_id, client_secret=config.client_secret),
        # The client's own timeout only reaches its default transport, so the
        # transport we pass carries it.
        custom_http_client=LoggingTransport(transport or HttpxClient(timeout=config.timeout)),
    )


_client: PayPalServerSdkClient | None = None
_client_lock = threading.Lock()


def get_client() -> PayPalServerSdkClient:
    global _client
    if _client is None:
        config = paypal_config()
        with _client_lock:
            if _client is None:
                _client = build_client(config)
                atexit.register(_client.close)
    return _client


def install_client(client: PayPalServerSdkClient | None) -> PayPalServerSdkClient | None:
    """Replace the process client (tests); returns the previous one."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    return previous
