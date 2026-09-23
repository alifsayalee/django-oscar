"""
Construction of the Maxio Advanced Billing client.

Django-free on purpose: the caller hands in a ``MaxioConfig`` built from settings.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials, HttpClient, HttpRequest, HttpResponse, HttpxClient
from maxio_advanced_billing.server import (
    Environment, ProductionConfig, ProductionEuConfig, ProductionUsConfig, ServerConfig)

logger = logging.getLogger(__name__)

# Seconds; bounds connect, read, write and pool waits alike. The SDK default (30 s)
# is too long for a user-facing request.
DEFAULT_TIMEOUT = 10.0

_ENVIRONMENTS: dict[str, Environment] = {"us": "us", "eu": "eu"}


class MaxioConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class MaxioConfig:
    api_key: str
    site_subdomain: str
    product_family: str
    environment: str = "us"
    base_url: str | None = None
    timeout: float = DEFAULT_TIMEOUT

    def validate(self) -> Environment:
        missing = [
            name
            for name, value in (
                ("MAXIO_API_KEY", self.api_key),
                ("MAXIO_DEFAULT_PRODUCT_FAMILY", self.product_family),
            )
            if not value
        ]
        if not self.base_url and not self.site_subdomain:
            missing.append("MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL)")
        if missing:
            raise MaxioConfigurationError("Missing Maxio settings: " + ", ".join(missing))
        try:
            # An unknown value must fail, never fall through to the SDK's silent "us".
            return _ENVIRONMENTS[self.environment.strip().lower()]
        except KeyError:
            raise MaxioConfigurationError(f"Unsupported MAXIO_ENVIRONMENT {self.environment!r}") from None


class LoggingTransport:
    """Logs method, URL, status and latency of each request - never headers or bodies."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def send(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self._inner.send(request)
        except Exception as exc:
            logger.warning(
                "maxio %s %s failed after %.0f ms: %s",
                request.method, request.url, (time.monotonic() - started) * 1000, type(exc).__name__,
            )
            raise
        logger.info(
            "maxio %s %s -> %s (%.0f ms)",
            request.method, request.url, response.status_code, (time.monotonic() - started) * 1000,
        )
        return response

    def close(self) -> None:
        self._inner.close()


def _server_config(environment: Environment, config: MaxioConfig) -> ServerConfig:
    # MAXIO_BASE_URL, when set, is used verbatim as the base address; otherwise the
    # environment's own template is filled with the site subdomain.
    if environment == "eu":
        eu = ProductionEuConfig(base_url=config.base_url) if config.base_url else ProductionEuConfig(
            site=config.site_subdomain)
        return ServerConfig(production=ProductionConfig(eu=eu))
    us = ProductionUsConfig(base_url=config.base_url) if config.base_url else ProductionUsConfig(
        site=config.site_subdomain)
    return ServerConfig(production=ProductionConfig(us=us))


def build_client(config: MaxioConfig, transport: HttpClient | None = None) -> MaxioAdvancedBillingClient:
    environment = config.validate()
    return MaxioAdvancedBillingClient(
        environment=environment,
        server_config=_server_config(environment, config),
        # Maxio authenticates with the API key as the Basic username and "x" as the password.
        basic_auth=BasicAuthCredentials(username=config.api_key, password="x"),
        # A supplied transport carries its own timeout; the client's `timeout=` would not reach it.
        custom_http_client=transport or LoggingTransport(HttpxClient(timeout=config.timeout)),
    )
