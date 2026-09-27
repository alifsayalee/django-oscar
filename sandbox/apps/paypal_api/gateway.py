"""
The one PayPal client for this process, and the transport it talks through.

The client is sync (Django runs this app under WSGI), long-lived and built
lazily on first use, so a forking server builds it after the fork. It is
closed at interpreter exit.
"""

import atexit
import contextvars
import hashlib
import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from paypal import PaypalClient
from paypal.core import (
    ApiError,
    ClientCredentials,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    OAuthProviderError,
)

logger = logging.getLogger(__name__)

# The only server the SDK declares. Any other environment needs PAYPAL_BASE_URL.
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
BASE_URLS = {"sandbox": SANDBOX_BASE_URL}

# Status of the last response this thread received. A decode failure raises a
# plain ValueError with no status on it; this tells a 2xx we could not read
# (outcome unknown) from an error body we could not read (a rejection).
last_status: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "paypal_last_status", default=None
)


class LoggingTransport:
    """Logs method, path, status and duration - never headers or bodies."""

    def __init__(self, inner: HttpxClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        last_status.set(None)
        started = time.monotonic()
        response = self._inner.send(request)
        last_status.set(response.status_code)
        logger.info(
            "PayPal %s %s -> %s (%.0f ms, debug id %s)",
            request.method,
            httpx.URL(request.url).path,
            response.status_code,
            (time.monotonic() - started) * 1000,
            response.headers.get("paypal-debug-id", "-"),
        )
        return response

    def close(self) -> None:
        self._inner.close()


def base_url() -> str:
    override = str(settings.PAYPAL_BASE_URL).strip()
    if override:
        return override
    environment = str(settings.PAYPAL_ENVIRONMENT).strip().lower()
    try:
        return BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            f"PAYPAL_ENVIRONMENT={environment!r} has no known PayPal host; "
            "set PAYPAL_BASE_URL to the API base address for that environment."
        ) from None


def currency() -> str:
    value = str(settings.PAYPAL_CURRENCY).strip().upper()
    if len(value) != 3 or not value.isalpha():
        raise ImproperlyConfigured("PAYPAL_CURRENCY must be a three-letter ISO-4217 code.")
    return value


def reference_prefix() -> str:
    """A prefix unique to this install, stable across restarts."""
    configured = str(settings.PAYPAL_REFERENCE_PREFIX).strip()
    if configured:
        return configured
    seed = f"{settings.SECRET_KEY}|{settings.DATABASES['default']['NAME']}"
    return "osc" + hashlib.sha256(seed.encode()).hexdigest()[:10]


def _build_client() -> PaypalClient:
    client_id = str(settings.PAYPAL_CLIENT_ID).strip()
    client_secret = str(settings.PAYPAL_CLIENT_SECRET).strip()
    if not client_id or not client_secret:
        raise ImproperlyConfigured("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.")
    return PaypalClient(
        base_url=base_url(),
        custom_http_client=LoggingTransport(
            HttpxClient(timeout=float(settings.PAYPAL_TIMEOUT_SECONDS))
        ),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


_client: PaypalClient | None = None
_client_lock = threading.Lock()


def get_client() -> PaypalClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PaypalClient | None) -> None:
    """Swap the process client (tests build one over a stub transport)."""
    global _client
    with _client_lock:
        _client = client


T = TypeVar("T")

# Failures where the request provably never reached PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


def read_with_retry(call: Callable[[], T], *, attempts: int = 3) -> T:
    """Retry an idempotent read on connection failures, 429 and 5xx. Never used for writes."""
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except ApiError as e:
            transient = not isinstance(e.error, OAuthProviderError) and (
                e.status_code == 429 or e.status_code >= 500
            )
            if not transient or attempt == attempts:
                raise
            retry_after = e.response.headers.get("retry-after", "")
            wait = float(retry_after) if retry_after.isdigit() else delay
        except (*NEVER_SENT, httpx.ReadTimeout):
            if attempt == attempts:
                raise
            wait = delay
        time.sleep(min(wait, 5.0))
        delay *= 2
    raise AssertionError("unreachable")
