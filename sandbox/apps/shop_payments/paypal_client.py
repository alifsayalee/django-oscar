"""
Construction and lifetime of the PayPal SDK client.

The sandbox runs under WSGI with sync views, so this uses the SDK's sync
``PaypalClient``.  One client is built lazily per process (after any fork) and
reused for the life of the process so the OAuth token cache and connection pool
are shared; it is closed at interpreter exit.
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
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse, HttpxClient

logger = logging.getLogger(__name__)

# The only server the SDK declares (sdk-map.md, "Servers & auth"). Any other
# environment has to name its host through PAYPAL_BASE_URL.
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
ENVIRONMENT_BASE_URLS = {"sandbox": SANDBOX_BASE_URL}


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str
    client_secret: str
    base_url: str
    currency: str
    timeout: float


def _required(name: str) -> str:
    value = getattr(settings, name, None)
    if not value:
        raise ImproperlyConfigured(
            f"{name} is not set. Export it in the environment; sandbox/settings.py reads it."
        )
    return str(value)


def resolve_base_url(environment: str | None, override: str | None) -> str:
    """PAYPAL_BASE_URL wins verbatim; otherwise the environment must be one the SDK declares."""
    if override:
        return override
    if not environment:
        raise ImproperlyConfigured("PAYPAL_ENVIRONMENT is not set (and no PAYPAL_BASE_URL).")
    try:
        return ENVIRONMENT_BASE_URLS[environment.strip().lower()]
    except KeyError:
        raise ImproperlyConfigured(
            f"PAYPAL_ENVIRONMENT={environment!r} has no known API host; "
            "set PAYPAL_BASE_URL to the host for that environment."
        ) from None


def get_config() -> PayPalConfig:
    return PayPalConfig(
        client_id=_required("PAYPAL_CLIENT_ID"),
        client_secret=_required("PAYPAL_CLIENT_SECRET"),
        base_url=resolve_base_url(
            getattr(settings, "PAYPAL_ENVIRONMENT", None),
            getattr(settings, "PAYPAL_BASE_URL", None),
        ),
        currency=configured_currency(),
        timeout=float(getattr(settings, "PAYPAL_TIMEOUT", 20.0)),
    )


def configured_currency() -> str:
    return _required("PAYPAL_CURRENCY").strip().upper()


_last_response = threading.local()


def reset_last_status() -> None:
    _last_response.status = None


def last_status() -> int | None:
    """The HTTP status of this thread's most recent PayPal response.

    PayPal error bodies do not always match the SDK's error model; the decode
    failure then carries no status, and this tells a rejection (4xx) apart from
    an unreadable success.
    """
    status = getattr(_last_response, "status", None)
    return status if isinstance(status, int) else None


class LoggingTransport:
    """
    Wraps the SDK's transport to log each PayPal call.

    Only the method, the URL path and the status are logged: headers carry the
    bearer token and bodies can carry card data.
    """

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
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
        _last_response.status = response.status_code
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


def build_client(config: PayPalConfig) -> PaypalClient:
    # The client's own ``timeout=`` only configures its default transport, so the
    # timeout goes on the transport we supply.
    transport = LoggingTransport(HttpxClient(timeout=config.timeout))
    return PaypalClient(
        base_url=config.base_url,
        custom_http_client=transport,
        oauth2=ClientCredentials(client_id=config.client_id, client_secret=config.client_secret),
    )


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def get_client() -> PaypalClient:
    """The process-wide client, built on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client(get_config())
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Replace the process-wide client (tests supply one over a stub transport)."""
    global _client
    with _client_lock:
        _client = client
