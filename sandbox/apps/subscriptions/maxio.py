"""
Construction and lifetime of the Maxio Advanced Billing client.

The sandbox is a synchronous (WSGI) Django site, so this uses the SDK's sync
client. One client is built lazily on first use -- never at import, so importing
the app (as the test run does) needs no credentials, and a forking server
builds it after the fork -- and is reused for the life of the process: it owns
a pooled HTTP transport.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time

from django.conf import settings
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import (
    BasicAuthCredentials,
    HttpClient,
    HttpRequest,
    HttpResponse,
    HttpxClient,
)
from maxio_advanced_billing.server import (
    Environment,
    ProductionConfig,
    ProductionEuConfig,
    ProductionUsConfig,
    ServerConfig,
)

logger = logging.getLogger("apps.subscriptions.maxio")

# MAXIO_ENVIRONMENT -> the SDK's environment. Explicit, and an unknown value is
# refused: omitting the environment would silently select "us".
ENVIRONMENTS: dict[str, Environment] = {"us": "us", "eu": "eu"}

# Maxio's basic auth takes the API key as the username; the password is unused
# and conventionally "x" (the SDK README's `curl -u <api_key>:x` example).
API_KEY_PASSWORD = "x"


class MaxioConfigError(RuntimeError):
    """The integration is not configured; raised where the client is built."""


class LoggingTransport:
    """
    Wraps the SDK's own transport and logs method, URL, status and latency.
    Headers and bodies are never logged: the auth header carries the API key.
    """

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "maxio %s %s -> %s (%.0f ms)",
                request.method,
                request.url,
                type(exc).__name__,
                (time.monotonic() - started) * 1000,
            )
            raise
        logger.info(
            "maxio %s %s -> %s (%.0f ms)",
            request.method,
            request.url,
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def _production_server(environment: Environment, base_url: str, site: str) -> ProductionConfig:
    """
    The API server for the selected environment: MAXIO_BASE_URL verbatim when
    set, otherwise the environment's own URL template filled with the subdomain.
    """
    if environment == "eu":
        eu = ProductionEuConfig(base_url=base_url.rstrip("/")) if base_url else ProductionEuConfig(site=site)
        return ProductionConfig(eu=eu)
    us = ProductionUsConfig(base_url=base_url.rstrip("/")) if base_url else ProductionUsConfig(site=site)
    return ProductionConfig(us=us)


def build_client(transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    """
    Build a client from Django settings. Missing configuration stops here, by
    setting name, rather than sending an unauthenticated request.
    """
    required = ["MAXIO_API_KEY"]
    if not _setting("MAXIO_BASE_URL"):
        required.append("MAXIO_SITE_SUBDOMAIN")
    missing = [name for name in required if not _setting(name)]
    if missing:
        raise MaxioConfigError("Missing Maxio settings: " + ", ".join(missing))

    environment_name = (_setting("MAXIO_ENVIRONMENT") or "us").lower()
    if environment_name not in ENVIRONMENTS:
        raise MaxioConfigError(
            "Unsupported MAXIO_ENVIRONMENT; expected one of: " + ", ".join(ENVIRONMENTS)
        )
    environment = ENVIRONMENTS[environment_name]

    server_config = ServerConfig(
        production=_production_server(
            environment, _setting("MAXIO_BASE_URL"), _setting("MAXIO_SITE_SUBDOMAIN")
        )
    )

    timeout = float(getattr(settings, "MAXIO_TIMEOUT", 10.0))
    if transport is None:
        # A custom transport owns the timeout: the client's timeout= only
        # configures the SDK's default transport.
        transport = LoggingTransport(HttpxClient(timeout=timeout))

    return MaxioAdvancedBillingClient(
        environment=environment,
        timeout=timeout,
        server_config=server_config,
        custom_http_client=transport,
        basic_auth=BasicAuthCredentials(
            username=_setting("MAXIO_API_KEY"), password=API_KEY_PASSWORD
        ),
    )


_client: MaxioAdvancedBillingClient | None = None
_lock = threading.Lock()


def get_client() -> MaxioAdvancedBillingClient:
    """The process-wide client, built on first use."""
    global _client  # pylint: disable=global-statement
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: MaxioAdvancedBillingClient | None) -> None:
    """Replace the process-wide client (tests, credential rotation)."""
    global _client  # pylint: disable=global-statement
    with _lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    set_client(None)
