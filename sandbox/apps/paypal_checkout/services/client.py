"""Construction and lifetime of the PayPal SDK client.

The SDK client owns a pooled HTTP transport and caches its OAuth token, so it is
built once and reused for the process lifetime (Django/WSGI is synchronous, so we
use the synchronous ``PaypalClient``). It is closed at interpreter exit.
"""

import atexit
import threading

from django.conf import settings

from paypal import PaypalClient
from paypal.core import ClientCredentials

_client = None
_lock = threading.Lock()


def _build_client():
    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        # Surfaced as a configuration error by the gateway's first call.
        from ..errors import PayPalConfigError

        raise PayPalConfigError(
            "PayPal credentials are not configured (PAYPAL_CLIENT_ID / "
            "PAYPAL_CLIENT_SECRET)."
        )
    # PAYPAL_BASE_URL is an optional override used verbatim (incl. the token
    # request); when empty, base_url=None lets the SDK target its sandbox default.
    base_url = settings.PAYPAL_BASE_URL or None
    return PaypalClient(
        base_url=base_url,
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET,
        ),
        timeout=30.0,
    )


def get_client():
    """Return the process-wide PayPal client, building it lazily and thread-safely."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = _build_client()
                atexit.register(_close_client)
    return _client


def _close_client():
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None


def reset_client_for_tests():
    """Drop the cached client (used by tests that inject a fake transport)."""
    _close_client()
