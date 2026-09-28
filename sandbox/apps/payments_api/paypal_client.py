"""
The one PayPal SDK client this process uses.

Django here runs under WSGI with sync views, so this is the SDK's sync
``PaypalClient``. It owns a connection pool and caches the OAuth token, so it is
built once -- lazily, on first use, which also means after any worker fork --
and closed at interpreter exit.
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

logger = logging.getLogger(__name__)

# The only server the SDK declares. Any other environment must name its host
# explicitly through PAYPAL_BASE_URL.
BASE_URLS = {
    'sandbox': 'https://api-m.sandbox.paypal.com',
}

_client: PaypalClient | None = None
_client_lock = threading.Lock()


class LoggingTransport:
    """Logs method, path, status and latency of every PayPal request.

    Never headers (they carry the bearer token) and never bodies (they carry
    card details); the query string is dropped as well.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as e:
            logger.warning(
                'PayPal %s %s failed after %.0f ms: %s', request.method, path,
                (time.monotonic() - started) * 1000, type(e).__name__)
            raise
        logger.info(
            'PayPal %s %s -> %s (%.0f ms)', request.method, path,
            response.status_code, (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def base_url() -> str:
    """PAYPAL_BASE_URL verbatim when set, else the environment's host."""
    override = getattr(settings, 'PAYPAL_BASE_URL', '')
    if override:
        return override
    environment = getattr(settings, 'PAYPAL_ENVIRONMENT', '')
    try:
        return BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            'PAYPAL_ENVIRONMENT=%r has no known PayPal host; set '
            'PAYPAL_BASE_URL to the API base address for it.' % environment
        ) from None


def currency() -> str:
    value = getattr(settings, 'PAYPAL_CURRENCY', '')
    if not value:
        raise ImproperlyConfigured('PAYPAL_CURRENCY is not set.')
    return value.upper()


def build_client(transport: HttpClient | None = None) -> PaypalClient:
    client_id = getattr(settings, 'PAYPAL_CLIENT_ID', '')
    client_secret = getattr(settings, 'PAYPAL_CLIENT_SECRET', '')
    if not client_id or not client_secret:
        # Without credentials the SDK would send every request unauthenticated.
        raise ImproperlyConfigured(
            'PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.')
    timeout = float(getattr(settings, 'PAYPAL_TIMEOUT_SECONDS', 20.0))
    if transport is None:
        # We supply the transport, so the timeout is set here: the client's own
        # ``timeout=`` would only reach a transport it builds itself.
        transport = LoggingTransport(HttpxClient(timeout=timeout))
    return PaypalClient(
        base_url=base_url(),
        timeout=timeout,
        custom_http_client=transport,
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Replace the process client (tests inject one over a stub transport)."""
    global _client
    with _client_lock:
        _client = client
