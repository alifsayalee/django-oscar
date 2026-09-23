"""Construction and lifetime of the sync PayPal SDK client.

The sandbox runs under WSGI (sync), so we use ``PaypalClient`` (sync). The SDK
client owns a pooled HTTP transport and caches the OAuth token, so it must be
long-lived: we build one lazily as a module global and close it at interpreter
exit. It is never rebuilt per request.

All configuration is read from ``django.conf.settings`` at build time; no
secret value is ever hard-coded here.
"""

import atexit
import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from paypal import PaypalClient
from paypal.core import ClientCredentials

# Environments this SDK talks to. PAYPAL_BASE_URL overrides this mapping.
_ENVIRONMENT_BASE_URLS = {
    'sandbox': 'https://api-m.sandbox.paypal.com',
    'live': 'https://api-m.paypal.com',
    'production': 'https://api-m.paypal.com',
}

_client = None
_lock = threading.Lock()


def resolve_base_url():
    """Return the base URL for every PayPal call (token request included).

    If ``PAYPAL_BASE_URL`` is set it is used verbatim. Otherwise it is derived
    from ``PAYPAL_ENVIRONMENT`` via an explicit map that fails on an unknown
    value rather than silently falling through to a default.
    """
    override = (settings.PAYPAL_BASE_URL or '').strip()
    if override:
        return override
    environment = (settings.PAYPAL_ENVIRONMENT or '').strip().lower()
    try:
        return _ENVIRONMENT_BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            "PAYPAL_ENVIRONMENT=%r is not one of %s; set PAYPAL_BASE_URL to "
            "override." % (settings.PAYPAL_ENVIRONMENT, sorted(_ENVIRONMENT_BASE_URLS))
        )


def build_client():
    """Build a fresh sync client from settings. Caller owns its lifetime."""
    client_id = settings.PAYPAL_CLIENT_ID
    client_secret = settings.PAYPAL_CLIENT_SECRET
    if not client_id or not client_secret:
        raise ImproperlyConfigured(
            "PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set in the "
            "environment to talk to PayPal."
        )
    return PaypalClient(
        base_url=resolve_base_url(),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
        timeout=30.0,
    )


def get_client():
    """Return the process-wide, lazily-built PayPal client."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


@atexit.register
def _close_client():
    global _client
    if _client is not None:
        try:
            _client.close()
        finally:
            _client = None
