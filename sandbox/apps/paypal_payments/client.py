"""
The process-wide PayPal Server SDK client.

Django here runs under WSGI with synchronous views, so this is the sync
client. It owns a connection pool and caches the OAuth token, so it is built
once per process - lazily, on first use, which also keeps it on the right side
of a forking server - and closed when the process exits.
"""

import atexit
import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import ClientCredentials

# The PayPal Server SDK declares a single server - PayPal's sandbox - and no
# environment enum, so it is the only host this integration knows by name. Any
# other environment must name its host through PAYPAL_BASE_URL.
_HOSTS_BY_ENVIRONMENT = {"sandbox": "https://api-m.sandbox.paypal.com"}

# Seconds allowed for each wait (connect, send, each read) of a PayPal call.
_TIMEOUT = 20.0

_client: PayPalServerSdkClient | None = None
_lock = threading.Lock()


def resolve_base_url() -> str:
    override: str = settings.PAYPAL_BASE_URL
    if override:
        return override
    environment: str = settings.PAYPAL_ENVIRONMENT
    try:
        return _HOSTS_BY_ENVIRONMENT[environment.lower()]
    except KeyError:
        raise ImproperlyConfigured(
            f"PAYPAL_ENVIRONMENT={environment!r} has no known PayPal host; "
            "set PAYPAL_BASE_URL to the API base address for it."
        ) from None


def build_client() -> PayPalServerSdkClient:
    client_id: str = settings.PAYPAL_CLIENT_ID
    client_secret: str = settings.PAYPAL_CLIENT_SECRET
    if not client_id or not client_secret:
        # Without credentials the SDK would silently send unauthenticated
        # requests; refuse instead.
        raise ImproperlyConfigured("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must both be set.")
    return PayPalServerSdkClient(
        base_url=resolve_base_url(),
        timeout=_TIMEOUT,
        # The SDK's default retry policy is kept: it repeats GETs on 408/429/5xx
        # and dropped connections, and never repeats a POST or DELETE. Writes are
        # made safe by our own claims plus a stored PayPal-Request-Id instead.
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


def get_client() -> PayPalServerSdkClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
                atexit.register(_client.close)
    return _client


def set_client(client: PayPalServerSdkClient | None) -> None:
    """Replace the process-wide client (tests inject one with a fake transport)."""
    global _client
    with _lock:
        _client = client
