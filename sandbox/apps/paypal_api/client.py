"""Construction of the long-lived PayPal SDK client.

The client owns a pooled HTTP transport and caches its OAuth token, so it must
be built once and reused for the process lifetime (never per request). We build
it lazily on first use -- importing this module never needs credentials, so the
test suite and management commands import cleanly even when the environment is
unset.
"""
import threading

from django.conf import settings

from paypal import PaypalClient
from paypal.core import ClientCredentials

# Live PayPal API host, used only when PAYPAL_ENVIRONMENT selects production and
# no explicit PAYPAL_BASE_URL override is given. The SDK already defaults to the
# sandbox host, so sandbox needs no base_url at all.
_LIVE_BASE_URL = "https://api-m.paypal.com"

_client = None
_lock = threading.Lock()


def resolve_base_url():
    """Return the base URL to pass to the SDK, or ``None`` for the SDK default.

    When ``PAYPAL_BASE_URL`` is set it is used verbatim for every call (including
    the token request). Otherwise it is derived from ``PAYPAL_ENVIRONMENT``:
    a production environment targets the live host; anything else falls through
    to the SDK's own sandbox default.
    """
    override = (settings.PAYPAL_BASE_URL or "").strip()
    if override:
        return override
    environment = (settings.PAYPAL_ENVIRONMENT or "").strip().lower()
    if environment in ("live", "production"):
        return _LIVE_BASE_URL
    return None


def _validate_credentials():
    missing = [
        name
        for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")
        if not (getattr(settings, name, "") or "").strip()
    ]
    if missing:
        raise RuntimeError(
            "PayPal credentials are not configured: missing "
            + ", ".join(missing)
        )


def build_client():
    """Build a fresh sync PayPal client from Django settings."""
    _validate_credentials()
    return PaypalClient(
        base_url=resolve_base_url(),
        timeout=30.0,
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET,
        ),
    )


def get_client():
    """Return the process-wide singleton PayPal client, building it on first use."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def reset_client():
    """Drop the cached client (used by tests that swap in a stub transport)."""
    global _client
    with _lock:
        old = _client
        _client = None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
