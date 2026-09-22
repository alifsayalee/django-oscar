"""Construction of the long-lived Maxio Advanced Billing SDK client.

The SDK client owns a pooled HTTP transport and is meant to be built once and
reused for the process lifetime (never per request). Under Django's WSGI model
a lazily-initialised module global is the pragmatic placement, so we cache one
sync client and hand it out.

Authentication is HTTP Basic with the Maxio API key as the username and the
literal ``"x"`` as the password (Maxio/Chargify convention: ``curl -u <key>:x``).
Credentials and the site are read from Django settings, which read them from the
environment; no secret value is hard-coded here.
"""

from __future__ import annotations

import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from maxio_advanced_billing import MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import BasicAuthCredentials

_client: MaxioAdvancedBillingClient | None = None
_lock = threading.Lock()


def _build_server_config() -> ServerConfigDict:
    """Resolve the ``production``/``us`` base address for the configured site.

    ``MAXIO_BASE_URL`` wins verbatim when set; otherwise the site subdomain
    fills the ``{site}`` template variable of the default Chargify base URL.
    """
    base_url = settings.MAXIO_BASE_URL
    if base_url:
        return {'production': {'us': {'base_url': base_url}}}

    subdomain = settings.MAXIO_SITE_SUBDOMAIN
    if not subdomain:
        raise ImproperlyConfigured(
            'Maxio is not configured: set MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL).'
        )
    return {'production': {'us': {'site': subdomain}}}


def get_client() -> MaxioAdvancedBillingClient:
    """Return the shared sync Maxio client, building it on first use."""
    global _client
    if _client is not None:
        return _client

    with _lock:
        if _client is not None:
            return _client

        api_key = settings.MAXIO_API_KEY
        if not api_key:
            raise ImproperlyConfigured('Maxio is not configured: set MAXIO_API_KEY.')

        _client = MaxioAdvancedBillingClient(
            basic_auth=BasicAuthCredentials(username=api_key, password='x'),
            environment='us',
            server_config=_build_server_config(),
            timeout=30.0,
        )
        return _client


def reset_client() -> None:
    """Drop the cached client (used by tests that inject a stub transport)."""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        _client = None
