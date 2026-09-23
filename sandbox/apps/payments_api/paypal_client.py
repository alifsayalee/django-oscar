"""Construction and lifetime of the PayPal SDK client.

The client owns a pooled HTTP transport and caches its OAuth token, so it must be
long-lived. We build one lazily as a module global on first use (never at import,
so importing this module needs no credentials) and close it at process exit.
"""
import atexit
import logging
import threading

from django.conf import settings

from paypal import PaypalClient
from paypal.core import ClientCredentials

logger = logging.getLogger("paypal")

_client = None
_lock = threading.Lock()


def resolve_base_url() -> str:
    """PAYPAL_BASE_URL verbatim when set, else derived from PAYPAL_ENVIRONMENT."""
    override = (settings.PAYPAL_BASE_URL or "").strip()
    if override:
        return override
    env = (settings.PAYPAL_ENVIRONMENT or "sandbox").strip().lower()
    if env in ("live", "production"):
        return "https://api-m.paypal.com"
    return "https://api-m.sandbox.paypal.com"


def build_client() -> PaypalClient:
    """Build a fresh client. Raises if credentials are missing (stops the request
    with a clear message rather than sending an unauthenticated call)."""
    missing = [
        name
        for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")
        if not getattr(settings, name, "")
    ]
    if missing:
        raise RuntimeError("Missing PayPal credentials: " + ", ".join(missing))
    return PaypalClient(
        base_url=resolve_base_url(),
        timeout=30.0,
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET,
        ),
    )


def get_client() -> PaypalClient:
    """Return the shared, long-lived client, building it on first use."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
                atexit.register(_close_client)
    return _client


def _close_client():
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.warning("error closing PayPal client", exc_info=True)
        _client = None
