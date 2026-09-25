"""
The one PayPal client for this process.

Django runs here under WSGI, so the sync ``PaypalClient`` is used. It is built
lazily on first use (after any worker fork), kept for the life of the process so
the connection pool and the OAuth token cache are reused, and closed at exit.
"""

import atexit
import logging
import threading
import time

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse, HttpxClient

logger = logging.getLogger(__name__)

# The only server the PayPal SDK declares. Any other environment must name its
# host explicitly through PAYPAL_BASE_URL; none is guessed.
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
REQUEST_TIMEOUT = 20.0

_lock = threading.Lock()
_client: PaypalClient | None = None


def resolve_base_url() -> str:
    override = getattr(settings, "PAYPAL_BASE_URL", "") or ""
    if override:
        return str(override)
    environment = (getattr(settings, "PAYPAL_ENVIRONMENT", "") or "").strip().lower()
    if environment == "sandbox":
        return SANDBOX_BASE_URL
    raise ImproperlyConfigured(
        f"PAYPAL_ENVIRONMENT={environment!r} has no known PayPal host; set PAYPAL_BASE_URL explicitly."
    )


def configured_currency() -> str:
    currency = (getattr(settings, "PAYPAL_CURRENCY", "") or "").strip().upper()
    if len(currency) != 3:
        raise ImproperlyConfigured("PAYPAL_CURRENCY must be a three-letter ISO-4217 code.")
    return currency


class LoggingTransport:
    """Logs method, URL, status and timing. Never headers or bodies: the
    authorization header carries a live token and bodies may carry card data."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning("PayPal %s %s -> %s", request.method, _strip_query(request.url), type(exc).__name__)
            raise
        logger.info(
            "PayPal %s %s -> %s (%.0f ms)",
            request.method,
            _strip_query(request.url),
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]


def build_client() -> PaypalClient:
    client_id = getattr(settings, "PAYPAL_CLIENT_ID", "") or ""
    client_secret = getattr(settings, "PAYPAL_CLIENT_SECRET", "") or ""
    if not client_id or not client_secret:
        # Never let the SDK send unauthenticated requests.
        raise ImproperlyConfigured("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.")
    return PaypalClient(
        base_url=resolve_base_url(),
        custom_http_client=LoggingTransport(HttpxClient(timeout=REQUEST_TIMEOUT)),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
                atexit.register(close_client)
    return _client


def close_client() -> None:
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None


def set_client_for_tests(client: PaypalClient | None) -> None:
    """Swap the process client (tests inject one built on a stub transport)."""
    global _client
    with _lock:
        _client = client
