"""
The process-wide Maxio Advanced Billing client.

The sandbox runs under WSGI, so this is the SDK's sync client. It is built lazily
on first use (after any worker fork), shared across threads, and closed at exit.
Configuration comes only from Django settings, which read it from the environment.
"""

import atexit
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import (
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
    RetryOptions,
)

logger = logging.getLogger("apps.subscriptions.maxio")

MaxioEnvironment = Literal["us", "eu"]
_ENVIRONMENTS: dict[str, MaxioEnvironment] = {"us": "us", "eu": "eu"}

# Idempotent methods only (the SDK default method set): POSTs are never resent.
_RETRY_OPTIONS = RetryOptions(max_retries=2, initial_delay=0.5, max_delay=4.0)

_lock = threading.Lock()
_client: MaxioAdvancedBillingClient | None = None


@dataclass(frozen=True)
class MaxioConfig:
    api_key: str
    site_subdomain: str
    environment: MaxioEnvironment
    product_family: str
    base_url: str | None
    timeout: float

    @classmethod
    def from_settings(cls) -> "MaxioConfig":
        missing = [
            name
            for name in ("MAXIO_API_KEY", "MAXIO_DEFAULT_PRODUCT_FAMILY")
            if not getattr(settings, name, "")
        ]
        base_url = getattr(settings, "MAXIO_BASE_URL", None) or None
        if not base_url and not getattr(settings, "MAXIO_SITE_SUBDOMAIN", ""):
            missing.append("MAXIO_SITE_SUBDOMAIN")
        if missing:
            raise ImproperlyConfigured(
                "Maxio billing is not configured; set %s" % ", ".join(missing)
            )
        raw_environment = str(getattr(settings, "MAXIO_ENVIRONMENT", "us")).strip().lower()
        try:
            environment = _ENVIRONMENTS[raw_environment]
        except KeyError:
            raise ImproperlyConfigured(
                "MAXIO_ENVIRONMENT must be one of %s" % ", ".join(sorted(_ENVIRONMENTS))
            ) from None
        timeout = float(getattr(settings, "MAXIO_TIMEOUT", 15.0))
        if not timeout > 0:
            raise ImproperlyConfigured("MAXIO_TIMEOUT must be a positive number of seconds")
        return cls(
            api_key=settings.MAXIO_API_KEY,
            site_subdomain=getattr(settings, "MAXIO_SITE_SUBDOMAIN", ""),
            environment=environment,
            product_family=settings.MAXIO_DEFAULT_PRODUCT_FAMILY,
            base_url=base_url,
            timeout=timeout,
        )


class LoggingTransport:
    """Wraps the SDK transport to log method, path, status and latency (never headers or bodies)."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        path = urlsplit(request.url).path
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "Maxio %s %s failed after %.0f ms: %s",
                request.method, path, (time.monotonic() - started) * 1000, type(exc).__name__,
            )
            raise
        logger.info(
            "Maxio %s %s -> %s (%.0f ms)",
            request.method, path, response.status_code, (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def _server_config(config: MaxioConfig) -> ServerConfigDict:
    """The ``production`` server for the selected environment: MAXIO_BASE_URL verbatim, else the subdomain."""
    if config.environment == "eu":
        if config.base_url:
            return {"production": {"eu": {"base_url": config.base_url}}}
        return {"production": {"eu": {"site": config.site_subdomain}}}
    if config.base_url:
        return {"production": {"us": {"base_url": config.base_url}}}
    return {"production": {"us": {"site": config.site_subdomain}}}


def build_client(config: MaxioConfig, transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    return MaxioAdvancedBillingClient(
        environment=config.environment,
        server_config=_server_config(config),
        retry_options=_RETRY_OPTIONS,
        # The client's own ``timeout=`` only reaches its default transport, so set it here.
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=config.timeout)),
        basic_auth=BasicAuthCredentials(username=config.api_key, password="x"),
    )


def get_client() -> MaxioAdvancedBillingClient:
    """The shared client, built on first use. Raises ImproperlyConfigured if settings are missing."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client(MaxioConfig.from_settings())
    return _client


def product_family() -> str:
    return MaxioConfig.from_settings().product_family


@contextmanager
def override_client(client: MaxioAdvancedBillingClient) -> Iterator[MaxioAdvancedBillingClient]:
    """Swap the shared client (tests); the previous one is restored, the override is closed."""
    global _client
    with _lock:
        previous, _client = _client, client
    try:
        yield client
    finally:
        with _lock:
            _client = previous
        client.close()


@atexit.register
def _close_client() -> None:
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None
