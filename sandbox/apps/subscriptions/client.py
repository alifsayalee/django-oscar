"""Construction and lifetime of the Maxio Advanced Billing SDK client.

The SDK client owns a pooled HTTP transport and is meant to be long-lived, so we build it once
as a lazy module-level singleton (the pragmatic placement for Django under WSGI) and close it at
interpreter shutdown. All configuration is read from Django settings, which in turn read the
credentials from environment variables at runtime — no secret value is ever hard-coded here.
"""

import atexit
import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from maxio_advanced_billing import MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import BasicAuthCredentials
from maxio_advanced_billing.server import ProductionUsConfigDict

_client = None
_lock = threading.Lock()


def _build_client():
    api_key = getattr(settings, 'MAXIO_API_KEY', None)
    if not api_key:
        raise ImproperlyConfigured(
            'MAXIO_API_KEY is not configured; set the MAXIO_API_KEY environment variable.'
        )

    base_url = getattr(settings, 'MAXIO_BASE_URL', None)
    subdomain = getattr(settings, 'MAXIO_SITE_SUBDOMAIN', None)

    # This SDK declares several servers across several environments, so server overrides nest as
    # {server: {environment: {...}}}. The production/us base URL template is
    # ``https://{site}.chargify.com``. MAXIO_BASE_URL, when set, replaces the whole URL verbatim;
    # otherwise we fill the {site} template variable from the site subdomain.
    production_us: ProductionUsConfigDict
    if base_url:
        production_us = {'base_url': base_url}
    elif subdomain:
        production_us = {'site': subdomain}
    else:
        raise ImproperlyConfigured(
            'Either MAXIO_BASE_URL or MAXIO_SITE_SUBDOMAIN must be configured.'
        )

    server_config: ServerConfigDict = {'production': {'us': production_us}}
    return MaxioAdvancedBillingClient(
        basic_auth=BasicAuthCredentials(username=api_key, password='x'),
        environment='us',
        server_config=server_config,
        timeout=float(getattr(settings, 'MAXIO_TIMEOUT', 30.0)),
    )


def get_client():
    """Return the process-wide Maxio client, building it on first use (thread-safe)."""
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
        finally:
            _client = None
