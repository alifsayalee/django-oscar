"""Construction and lifetime of the single PayPal SDK client.

Django runs under WSGI (sync), so we use the sync ``Client`` and hold ONE
long-lived instance: it owns an httpx connection pool and caches the OAuth2
token, both of which must survive across requests. Built lazily on first use
and closed at interpreter shutdown.
"""
from __future__ import annotations

import atexit
import threading

from django.conf import settings
from paypal import Client
from paypal.core import ClientCredentials

_client: Client | None = None
_lock = threading.Lock()

_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
_LIVE_BASE = "https://api-m.paypal.com"


def resolve_base_url() -> str:
    """Return the base URL for every PayPal call (incl. the token fetch).

    ``PAYPAL_BASE_URL`` is an explicit override used verbatim when set;
    otherwise it is derived from ``PAYPAL_ENVIRONMENT``.
    """
    override = (getattr(settings, "PAYPAL_BASE_URL", "") or "").strip()
    if override:
        return override
    environment = (getattr(settings, "PAYPAL_ENVIRONMENT", "sandbox") or "sandbox").strip().lower()
    return _LIVE_BASE if environment == "live" else _SANDBOX_BASE


def _build_client() -> Client:
    client_id = settings.PAYPAL_CLIENT_ID
    client_secret = settings.PAYPAL_CLIENT_SECRET
    if not client_id or not client_secret:
        # Fail fast with a message about deployment, not a later 401.
        raise RuntimeError(
            "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not configured; "
            "set them in the environment before making PayPal calls."
        )
    return Client(
        base_url=resolve_base_url(),
        timeout=float(getattr(settings, "PAYPAL_TIMEOUT", 30.0)),
        oauth2=ClientCredentials(client_id=client_id, client_secret=client_secret),
    )


def get_client() -> Client:
    """Return the process-wide PayPal client, building it once on first use."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = _build_client()
    return _client


@atexit.register
def _close_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        finally:
            _client = None


def reset_client_for_tests() -> None:
    """Drop the cached client (tests that swap settings/transport)."""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # pragma: no cover - best effort in teardown
                pass
        _client = None
