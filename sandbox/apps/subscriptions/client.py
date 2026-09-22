"""Construction and lifetime of the Maxio Advanced Billing SDK client.

The SDK client owns a pooled ``httpx`` transport and must be **long-lived** — one
per process, never rebuilt per request. Under Django/WSGI a lazily-initialised
module global is the pragmatic placement: importing this module (as a test run does)
never touches credentials; the client is built on first use and closed at interpreter
exit. The sync client class is used because the sandbox is a synchronous WSGI app.
"""

from __future__ import annotations

import atexit
import threading
from typing import cast

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from maxio_advanced_billing import (
    Environment,
    MaxioAdvancedBillingClient,
    ServerConfigOrDict,
)

# A user-facing request path should not wait 30s (the SDK default). Deliberate bound;
# the SDK performs no retries, so this genuinely caps one call.
DEFAULT_TIMEOUT_SECONDS = 20.0

# Valid Maxio host environments (the SDK's ``environment`` literal).
_VALID_ENVIRONMENTS = frozenset({"us", "eu", "maxio_api_gateway"})

_client: MaxioAdvancedBillingClient | None = None
_lock = threading.Lock()


def _resolve_environment() -> Environment:
    env = (getattr(settings, "MAXIO_ENVIRONMENT", None) or "us").strip().lower()
    if env not in _VALID_ENVIRONMENTS:
        raise ImproperlyConfigured(
            f"MAXIO_ENVIRONMENT={env!r} is not one of {sorted(_VALID_ENVIRONMENTS)}"
        )
    return cast(Environment, env)


def build_client() -> MaxioAdvancedBillingClient:
    """Build a Maxio client from Django settings. Raises if misconfigured.

    Credentials are read here — never at import time — so a deployment missing a
    variable stops the app with a clear message instead of failing at first call.
    """
    api_key = getattr(settings, "MAXIO_API_KEY", None)
    subdomain = getattr(settings, "MAXIO_SITE_SUBDOMAIN", None)
    base_url = getattr(settings, "MAXIO_BASE_URL", None)

    if not api_key:
        raise ImproperlyConfigured("MAXIO_API_KEY is not configured")
    if not base_url and not subdomain:
        raise ImproperlyConfigured(
            "Either MAXIO_BASE_URL or MAXIO_SITE_SUBDOMAIN must be configured"
        )

    environment = _resolve_environment()

    # The production/us base URL template is https://{site}.chargify.com, where
    # {site} defaults to the literal "subdomain" and MUST be overridden. When an
    # explicit MAXIO_BASE_URL is given, use it verbatim instead of deriving one.
    if base_url:
        inner = {"base_url": base_url}
    else:
        inner = {"site": subdomain}
    server_config = cast(ServerConfigOrDict, {"production": {environment: inner}})

    # Maxio HTTP Basic auth: the API key is the username, the password is "x"
    # (per the SDK's api-reference: `curl -u <api_key>:x`).
    return MaxioAdvancedBillingClient(
        basic_auth={"username": api_key, "password": "x"},
        environment=environment,
        server_config=server_config,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def get_client() -> MaxioAdvancedBillingClient:
    """Return the process-wide Maxio client, building it once on first use."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def close_client() -> None:
    """Close the pooled transport. Idempotent; safe to call at shutdown."""
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None


atexit.register(close_client)
