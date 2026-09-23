"""Construction and lifetime of the Maxio Advanced Billing SDK client.

The client owns a pooled HTTP transport and caches auth, so it is built once, lazily, and
reused for the process lifetime (never per request). Credentials are read from Django
settings *here*, at build time — never at import — so importing this module never requires
secrets and never raises. All contract facts (auth pattern, server config shape, environment
values) come from the SDK map / source, recorded in ``maxio-advanced-billing-plan.md``.
"""

import atexit
import logging
import threading
from typing import Any, Literal, Optional

from django.conf import settings

from maxio_advanced_billing import (
    MaxioAdvancedBillingClient,
    ServerConfigDict,
)
from maxio_advanced_billing.core import ApiError, BasicAuthCredentials, RawError, UnsetType

from .exceptions import ConfigurationError

logger = logging.getLogger("maxio.subscriptions")

# Maxio host environments (SDK ``Environment``). The gateway environment authenticates
# with a bearer token and uses a different server shape; it is out of scope here.
_SITE_ENVIRONMENTS = {"us", "eu"}

_lock = threading.Lock()
_client: Optional[MaxioAdvancedBillingClient] = None
# Cache of product-family handle -> numeric id (ids are re-assigned on re-seed, so we
# resolve by handle at runtime rather than trusting a configured id).
_family_id_cache: dict[str, int] = {}


def _resolve_environment() -> Literal["us", "eu"]:
    raw = (getattr(settings, "MAXIO_ENVIRONMENT", "") or "us").strip().lower()
    if raw == "us":
        return "us"
    if raw == "eu":
        return "eu"
    raise ConfigurationError(
        f"Unsupported MAXIO_ENVIRONMENT {raw!r}; supported: {sorted(_SITE_ENVIRONMENTS)}"
    )


def build_client() -> MaxioAdvancedBillingClient:
    """Build a fresh SDK client from settings, or raise ConfigurationError.

    The missing-credential check lives here (not in settings), so an unconfigured
    deployment stops the operation with a clear message instead of failing at the first
    API call.
    """
    api_key = getattr(settings, "MAXIO_API_KEY", "") or ""
    subdomain = getattr(settings, "MAXIO_SITE_SUBDOMAIN", "") or ""
    base_url = getattr(settings, "MAXIO_BASE_URL", "") or ""
    environment = _resolve_environment()

    missing = []
    if not api_key:
        missing.append("MAXIO_API_KEY")
    # A subdomain is only required when no explicit base URL override is given.
    if not subdomain and not base_url:
        missing.append("MAXIO_SITE_SUBDOMAIN")
    if not getattr(settings, "MAXIO_DEFAULT_PRODUCT_FAMILY", ""):
        missing.append("MAXIO_DEFAULT_PRODUCT_FAMILY")
    if missing:
        raise ConfigurationError("Missing Maxio settings: " + ", ".join(missing))

    # Point the selected environment's `production` server at this site. When
    # MAXIO_BASE_URL is set, use it verbatim (it carries no {site} placeholder); otherwise
    # derive the host from the subdomain via the {site} template variable. The nesting is
    # spelled out with literal environment keys so it type-checks against ServerConfigDict.
    server_config: ServerConfigDict
    if environment == "eu":
        server_config = {
            "production": {"eu": {"base_url": base_url} if base_url else {"site": subdomain}}
        }
    else:
        server_config = {
            "production": {"us": {"base_url": base_url} if base_url else {"site": subdomain}}
        }

    # Maxio Basic auth: API key as username, literal "x" as password (SDK README: `curl -u <api_key>:x`).
    return MaxioAdvancedBillingClient(
        basic_auth=BasicAuthCredentials(username=api_key, password="x"),
        environment=environment,
        timeout=float(getattr(settings, "MAXIO_TIMEOUT_SECONDS", 30.0)),
        server_config=server_config,
    )


def get_client() -> MaxioAdvancedBillingClient:
    """Return the process-wide client, building it on first use."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
                atexit.register(_close_client)
    return _client


def _close_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.warning("Error closing Maxio client", exc_info=True)
        _client = None


def reset_client_for_testing() -> None:
    """Drop the cached client and family cache (used by tests that swap the transport)."""
    global _client
    with _lock:
        _client = None
        _family_id_cache.clear()


def resolve_family_id(client: MaxioAdvancedBillingClient) -> int:
    """Resolve the configured product-family handle to its current numeric id.

    Ids are not stable across re-seeds, so we look the family up by handle each process and
    cache the result. Raises CallerError-free ConfigurationError semantics via 404 handling
    in the service layer; here we raise ConfigurationError when the family is absent.
    """
    handle = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    cached = _family_id_cache.get(handle)
    if cached is not None:
        return cached

    families = client.product_families.list_product_families()
    for entry in families:
        family = entry.product_family
        if isinstance(family, UnsetType) or family is None:
            continue
        family_id = family.id
        if family.handle == handle and not isinstance(family_id, UnsetType) and family_id is not None:
            _family_id_cache[handle] = family_id
            return family_id
    raise ConfigurationError(
        f"Configured product family {handle!r} was not found on the Maxio site"
    )


def is_not_found(error: Any) -> bool:
    """True when an ApiError payload is a 404 'not found' (RawError at status 404)."""
    return isinstance(error, RawError) and error.status_code == 404


__all__ = [
    "ApiError",
    "build_client",
    "get_client",
    "is_not_found",
    "reset_client_for_testing",
    "resolve_family_id",
]
