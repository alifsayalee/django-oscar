"""
The one PayPal SDK client this process uses.

Django runs these views synchronously under WSGI, so this is the SDK's sync
``PaypalClient``. It owns a connection pool and caches the OAuth token, so it
is built once per process, lazily on first use (after any worker fork), and
closed at exit.
"""
import atexit
import logging
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient

logger = logging.getLogger('apps.payments.paypal')

# The environments this SDK declares. The plugin documents only the sandbox
# host; any other environment must name its host through PAYPAL_BASE_URL.
BASE_URLS = {
    'sandbox': 'https://api-m.sandbox.paypal.com',
}

_client = None
_lock = threading.Lock()


class LoggingTransport:
    """
    Wraps the SDK's transport and logs method, path, status and duration.

    Headers and bodies are never logged: the one carries the bearer token and
    the other can carry card details.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning('PayPal %s %s failed: %s', request.method, path, type(exc).__name__)
            raise
        logger.info(
            'PayPal %s %s -> %s (%.0f ms, debug id %s)', request.method, path,
            response.status_code, (time.monotonic() - started) * 1000,
            response.headers.get('paypal-debug-id', '-'))
        return response

    def close(self) -> None:
        self._inner.close()


def base_url() -> str:
    override = getattr(settings, 'PAYPAL_BASE_URL', '')
    if override:
        return override
    environment = settings.PAYPAL_ENVIRONMENT
    try:
        return BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            'PAYPAL_ENVIRONMENT=%r has no known base URL; set PAYPAL_BASE_URL '
            'to the PayPal API host for that environment.' % environment) from None


def build_client(http_client: HttpClient | None = None) -> PaypalClient:
    client_id = settings.PAYPAL_CLIENT_ID
    client_secret = settings.PAYPAL_CLIENT_SECRET
    if not client_id or not client_secret:
        raise ImproperlyConfigured(
            'PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.')
    if http_client is None:
        http_client = LoggingTransport(HttpxClient(timeout=settings.PAYPAL_TIMEOUT))
    return PaypalClient(
        base_url=base_url(),
        custom_http_client=http_client,
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Replace the process client (tests use this with a stub transport)."""
    global _client
    with _lock:
        _client = client
