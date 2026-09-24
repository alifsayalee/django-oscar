"""
Construction and lifetime of the shared PayPal SDK client.

The sandbox runs under WSGI, so the synchronous ``PaypalClient`` is used. It is
built lazily on first use (never at import, so importing the app or running
tests needs no credentials), shared by every request in the process (the SDK's
token cache is lock-guarded) and closed at interpreter exit.
"""
import atexit
import logging
import threading
import time

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient

logger = logging.getLogger(__name__)

# The only host the PayPal SDK declares (paypal/server/server_config.py). Any
# other environment must be addressed explicitly through PAYPAL_BASE_URL.
BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
}

_lock = threading.Lock()
_client: PaypalClient | None = None


class LoggingTransport:
    """
    Transport wrapper that logs method, path and status of each PayPal call.

    Headers and bodies are never logged: the former carry the bearer token and
    the latter can carry card data.
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


def resolve_base_url() -> str:
    override = getattr(settings, "PAYPAL_BASE_URL", "")
    if override:
        return str(override)
    environment = str(getattr(settings, "PAYPAL_ENVIRONMENT", "") or "sandbox").lower()
    try:
        return BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            "PAYPAL_ENVIRONMENT=%r has no known PayPal host; set PAYPAL_BASE_URL "
            "to the API base address for that environment." % environment
        ) from None


def build_client() -> PaypalClient:
    missing = [
        name
        for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")
        if not getattr(settings, name, "")
    ]
    if missing:
        raise ImproperlyConfigured("Missing PayPal settings: " + ", ".join(missing))
    timeout = float(getattr(settings, "PAYPAL_TIMEOUT", 20.0))
    return PaypalClient(
        base_url=resolve_base_url(),
        custom_http_client=LoggingTransport(HttpxClient(timeout=timeout)),
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET,
        ),
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
    """Swap the shared client (tests inject one built on a stub transport)."""
    global _client
    with _lock:
        _client = client
