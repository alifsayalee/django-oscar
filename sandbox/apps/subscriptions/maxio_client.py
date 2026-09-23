"""Construction and lifetime of the Maxio Advanced Billing SDK client.

The client owns a pooled HTTP transport and caches nothing per request, so it must be
long-lived: built once, lazily, and reused for the process lifetime (per
python-client-initialization). Credentials are read from Django settings -- which default
to empty strings so importing settings never raises -- and the missing-credential check
lives here, not in settings (per python-authentication).
"""

import atexit
import threading

from django.conf import settings

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials

# Maxio/Chargify Basic auth places the API key in the username and a literal "x" in the
# password (confirmed in the SDK's api-reference.md: ``curl -u <api_key>:x``).
_BASIC_AUTH_PASSWORD = "x"

# The sandbox is a US-hosted Advanced Billing account. Passed explicitly rather than
# relying on the SDK's silent "us" default.
_ENVIRONMENT = "us"

# Requests sit on a user-facing path; 30s (the SDK default) is far too long.
_TIMEOUT_SECONDS = 15.0

_REQUIRED_SETTINGS = ("MAXIO_API_KEY", "MAXIO_SITE_SUBDOMAIN", "MAXIO_DEFAULT_PRODUCT_FAMILY")

_client = None
_lock = threading.Lock()


def _server_config():
    """Point the production/us server at this account.

    ``MAXIO_BASE_URL`` is an optional verbatim override; otherwise the base URL is derived
    from the site subdomain (production/us template ``https://{site}.chargify.com``).
    """
    base_url = getattr(settings, "MAXIO_BASE_URL", "") or ""
    if base_url:
        return {"production": {"us": {"base_url": base_url}}}
    return {"production": {"us": {"site": settings.MAXIO_SITE_SUBDOMAIN}}}


def _build_client():
    missing = [name for name in _REQUIRED_SETTINGS if not getattr(settings, name, "")]
    if missing:
        raise RuntimeError(
            "Maxio integration is not configured; missing settings: " + ", ".join(missing)
        )
    return MaxioAdvancedBillingClient(
        basic_auth=BasicAuthCredentials(
            username=settings.MAXIO_API_KEY, password=_BASIC_AUTH_PASSWORD
        ),
        environment=_ENVIRONMENT,
        server_config=_server_config(),
        timeout=_TIMEOUT_SECONDS,
    )


def get_client():
    """Return the shared, lazily-built SDK client (thread-safe, double-checked)."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = _build_client()
    return _client


@atexit.register
def _close_client():
    global _client
    if _client is not None:
        _client.close()
        _client = None
