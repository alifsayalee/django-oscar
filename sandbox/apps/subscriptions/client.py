"""Construction and lifetime of the Maxio Advanced Billing SDK client.

The SDK client owns a pooled HTTP transport and is meant to be long-lived, so we
build it once per process and reuse it (never per request). A sync client is used
because the sandbox runs under WSGI. Credentials and site configuration are read
from Django settings, which in turn read them from the environment; no secret
value is ever hard-coded here.
"""

import atexit
import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials

# Maxio (Chargify) HTTP Basic auth uses the API key as the username and the
# literal "x" as the password (per the SDK README: ``curl -u <api_key>:x``).
_BASIC_AUTH_PASSWORD = "x"

_client_lock = threading.Lock()
_client = None


def build_client():
    """Build a fresh sync Maxio client from Django settings.

    Raises ImproperlyConfigured when the required credentials are missing, so a
    misconfiguration surfaces as a clear error rather than an eventual 401.
    """
    api_key = settings.MAXIO_API_KEY
    subdomain = settings.MAXIO_SITE_SUBDOMAIN
    base_url = settings.MAXIO_BASE_URL

    if not api_key:
        raise ImproperlyConfigured(
            "MAXIO_API_KEY is not set; the subscription-billing app cannot "
            "authenticate to Maxio."
        )
    if not base_url and not subdomain:
        raise ImproperlyConfigured(
            "Either MAXIO_SITE_SUBDOMAIN or MAXIO_BASE_URL must be set so the "
            "Maxio production server URL can be resolved."
        )

    # The `production`/`us` server template variable {site} defaults to the
    # literal string "subdomain" and MUST be overridden. When MAXIO_BASE_URL is
    # given we override the whole base_url verbatim instead of deriving it.
    if base_url:
        production_us = {"base_url": base_url}
    else:
        production_us = {"site": subdomain}

    return MaxioAdvancedBillingClient(
        basic_auth=BasicAuthCredentials(username=api_key, password=_BASIC_AUTH_PASSWORD),
        environment="us",
        server_config={"production": {"us": production_us}},
        timeout=settings.MAXIO_TIMEOUT,
    )


def get_client():
    """Return the process-wide singleton Maxio client, building it on first use.

    Double-checked locking keeps concurrent first calls from building more than
    one client (and, with it, more than one connection pool).
    """
    global _client
    if _client is None:
        with _client_lock:
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
