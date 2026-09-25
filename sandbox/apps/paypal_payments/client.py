"""
The one PayPal SDK client this process uses.

Django runs synchronously under WSGI, so this is the SDK's sync client. It is
built lazily on first use - after any worker fork - and reused for the life of
the process (it owns a connection pool and caches the OAuth token), then
closed at interpreter exit.
"""
import atexit
import logging
import threading
import time

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient

logger = logging.getLogger('apps.paypal_payments.http')

# The only server this SDK declares (sdk-map.md, "Servers & auth").
SANDBOX_BASE_URL = 'https://api-m.sandbox.paypal.com'

_lock = threading.Lock()
_client: PaypalClient | None = None


class LoggingTransport:
    """
    Logs method, path, status, PayPal's debug id and duration of every call.
    Never headers (they carry the bearer token) and never bodies (they carry
    card details).
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = request.url.split('?', 1)[0]
        try:
            response = self._inner.send(request)
        except Exception as e:
            logger.warning(
                'PayPal %s %s -> %s after %.0f ms', request.method, path,
                type(e).__name__, (time.monotonic() - started) * 1000)
            raise
        logger.info(
            'PayPal %s %s -> %s debug_id=%s (%.0f ms)', request.method, path,
            response.status_code, response.headers.get('paypal-debug-id', '-'),
            (time.monotonic() - started) * 1000)
        return response

    def close(self) -> None:
        self._inner.close()


def resolve_base_url() -> str:
    """
    PAYPAL_BASE_URL wins verbatim (the token request follows it, since the SDK
    derives the token endpoint from the base URL). Otherwise the environment
    selects the host; the SDK declares no host but the sandbox, so any other
    environment has to name its base URL explicitly.
    """
    override = getattr(settings, 'PAYPAL_BASE_URL', '') or ''
    if override:
        return override
    environment = (getattr(settings, 'PAYPAL_ENVIRONMENT', '') or '').strip().lower()
    if environment == 'sandbox':
        return SANDBOX_BASE_URL
    if not environment:
        raise ImproperlyConfigured('PAYPAL_ENVIRONMENT is not set.')
    raise ImproperlyConfigured(
        "PAYPAL_ENVIRONMENT=%r has no known PayPal host; set PAYPAL_BASE_URL." % environment)


def build_client(transport: HttpClient | None = None) -> PaypalClient:
    client_id = getattr(settings, 'PAYPAL_CLIENT_ID', '') or ''
    client_secret = getattr(settings, 'PAYPAL_CLIENT_SECRET', '') or ''
    if not client_id or not client_secret:
        # Without credentials the SDK would silently send unauthenticated requests.
        raise ImproperlyConfigured('PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.')
    timeout = float(getattr(settings, 'PAYPAL_TIMEOUT', 20.0))
    if transport is None:
        transport = LoggingTransport(HttpxClient(timeout=timeout))
    return PaypalClient(
        base_url=resolve_base_url(),
        timeout=timeout,
        custom_http_client=transport,
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
    """Replace the process client (tests inject one over a stub transport)."""
    global _client
    with _lock:
        _client = client
