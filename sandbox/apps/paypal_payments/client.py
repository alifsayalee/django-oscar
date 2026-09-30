"""
The process-wide PayPal SDK client.

Built lazily on first use (so after a forking server has forked), reused for
the life of the process - the OAuth token is cached on it - and closed at exit.
"""

from __future__ import annotations

import atexit
import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import ClientCredentials, HttpxClient

from .transport import LoggingTransport

# The SDK declares a single server; any other environment needs PAYPAL_BASE_URL.
BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com"}

TIMEOUT_SECONDS = 30.0

_lock = threading.Lock()
_client: PayPalServerSdkClient | None = None


def resolve_base_url() -> str:
    override = getattr(settings, "PAYPAL_BASE_URL", None)
    if override:
        return str(override)
    environment = str(getattr(settings, "PAYPAL_ENVIRONMENT", "") or "").lower()
    try:
        return BASE_URLS[environment]
    except KeyError:
        raise ImproperlyConfigured(
            f"PAYPAL_ENVIRONMENT={environment!r} has no known PayPal host; set PAYPAL_BASE_URL."
        ) from None


def build_client() -> PayPalServerSdkClient:
    client_id = getattr(settings, "PAYPAL_CLIENT_ID", None)
    client_secret = getattr(settings, "PAYPAL_CLIENT_SECRET", None)
    if not client_id or not client_secret:
        raise ImproperlyConfigured("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.")
    return PayPalServerSdkClient(
        base_url=resolve_base_url(),
        # Default retry policy kept: it only ever resends GET/HEAD/PUT/OPTIONS, so no
        # PayPal write here (all POST/DELETE) is repeated by the SDK.
        custom_http_client=LoggingTransport(HttpxClient(timeout=TIMEOUT_SECONDS)),
        oauth2=ClientCredentials(client_id=str(client_id), client_secret=str(client_secret)),
    )


def get_client() -> PayPalServerSdkClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: PayPalServerSdkClient | None) -> PayPalServerSdkClient | None:
    """Swap the process client (tests inject one built on a stub transport)."""
    global _client
    with _lock:
        previous, _client = _client, client
    return previous


@atexit.register
def _close() -> None:
    if _client is not None:
        _client.close()
