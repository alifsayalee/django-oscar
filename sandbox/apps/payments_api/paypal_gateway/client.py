"""The process-wide PayPal client.

Django runs under WSGI here, so this is the sync ``PaypalClient``. It is built lazily on first use
(so after any worker fork), reused for the life of the process, and closed at interpreter exit.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient

log = logging.getLogger("apps.payments_api.paypal")

# The only host this SDK declares (sdk-map.md, "Servers & auth"). Any other environment must be
# given an explicit PAYPAL_BASE_URL: an unknown environment never falls through to a default.
ENVIRONMENT_BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com"}


class PayPalConfigurationError(Exception):
    """PayPal settings are missing or inconsistent."""


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str
    client_secret: str
    environment: str
    currency: str
    base_url: str | None
    timeout: float = 20.0

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        try:
            return ENVIRONMENT_BASE_URLS[self.environment.lower()]
        except KeyError:
            raise PayPalConfigurationError(
                f"PAYPAL_ENVIRONMENT={self.environment!r} has no known API host; set PAYPAL_BASE_URL"
            ) from None

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("PAYPAL_CLIENT_ID", self.client_id),
                ("PAYPAL_CLIENT_SECRET", self.client_secret),
                ("PAYPAL_CURRENCY", self.currency),
            )
            if not value
        ]
        if missing:
            raise PayPalConfigurationError(f"missing settings: {', '.join(missing)}")
        self.resolved_base_url()


class LoggingTransport:
    """Wraps the SDK transport and logs method, path and status only.

    Headers carry the bearer token and bodies carry card data, so neither is ever logged.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as exc:
            log.warning("paypal %s %s -> %s", request.method, path, type(exc).__name__)
            raise
        log.info(
            "paypal %s %s -> %s (%.0f ms)",
            request.method,
            path,
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_client(config: PayPalConfig, transport: HttpClient | None = None) -> PaypalClient:
    config.validate()
    return PaypalClient(
        base_url=config.resolved_base_url(),
        # A supplied transport owns the timeout: the client's own `timeout=` only builds its default one.
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=config.timeout)),
        oauth2=ClientCredentials(client_id=config.client_id, client_secret=config.client_secret),
    )


_lock = threading.Lock()
_client: PaypalClient | None = None


def get_client(config: PayPalConfig) -> PaypalClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client(config)
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Swap the process client (tests inject one built on a stub transport)."""
    global _client
    with _lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    set_client(None)
