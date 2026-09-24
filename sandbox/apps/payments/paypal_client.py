"""
The one PayPal SDK client for this process.

The client owns a connection pool and caches the OAuth token, so it is built
once, lazily (after any worker fork), and closed at interpreter exit. Every
setting is read from ``django.conf.settings``; nothing here holds a value.
"""

import atexit
import logging
import threading
import time

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient

logger = logging.getLogger("apps.payments.paypal")

# The PayPal hosts this build knows by environment name. The SDK declares
# only its sandbox server; any other environment needs PAYPAL_BASE_URL.
_BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
}

_client: PaypalClient | None = None
_client_lock = threading.Lock()


def resolve_base_url() -> str:
    override = (getattr(settings, "PAYPAL_BASE_URL", "") or "").strip()
    if override:
        return override
    environment = (getattr(settings, "PAYPAL_ENVIRONMENT", "") or "").strip().lower()
    try:
        return _BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            f"PAYPAL_ENVIRONMENT={environment!r} has no known PayPal host; "
            "set PAYPAL_BASE_URL to the API base address for it."
        ) from None


def _credentials() -> ClientCredentials:
    client_id = (getattr(settings, "PAYPAL_CLIENT_ID", "") or "").strip()
    client_secret = (getattr(settings, "PAYPAL_CLIENT_SECRET", "") or "").strip()
    if not client_id or not client_secret:
        # Without credentials the SDK would silently send unauthenticated calls.
        raise ImproperlyConfigured(
            "PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set in the environment."
        )
    return ClientCredentials(client_id=client_id, client_secret=client_secret)


class LoggingTransport:
    """
    Wraps the SDK transport and logs method, path, status, duration and
    PayPal's debug id. Headers and bodies are never logged: they carry the
    bearer token and card data.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = request.url.split("?", 1)[0]
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "PayPal %s %s failed after %.0f ms: %s",
                request.method,
                path,
                (time.monotonic() - started) * 1000,
                type(exc).__name__,
            )
            raise
        logger.info(
            "PayPal %s %s -> %s (%.0f ms, debug id %s)",
            request.method,
            path,
            response.status_code,
            (time.monotonic() - started) * 1000,
            response.headers.get("paypal-debug-id", "-"),
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(transport: HttpClient | None = None) -> PaypalClient:
    timeout = float(getattr(settings, "PAYPAL_TIMEOUT", 20.0))
    inner = transport if transport is not None else HttpxClient(timeout=timeout)
    return PaypalClient(
        base_url=resolve_base_url(),
        timeout=timeout,
        custom_http_client=LoggingTransport(inner),
        oauth2=_credentials(),
    )


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> PaypalClient | None:
    """Swap the process client (tests inject one built on a stub transport)."""
    global _client
    with _client_lock:
        previous, _client = _client, client
    return previous
