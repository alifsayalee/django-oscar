"""
The one Maxio Advanced Billing client this process uses.

Django here runs under WSGI, so the client is the synchronous one. It owns an httpx connection pool,
so it is built once - lazily, on first use, which also keeps it on the right side of a forking
server - and closed when the process exits.
"""
import atexit
import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import Environment, MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import BasicAuthCredentials
from maxio_advanced_billing.server import ProductionUsConfigDict

# Explicit map: an unknown value is a configuration error, never a silent fall-through to "us".
ENVIRONMENTS: dict[str, Environment] = {"us": "us", "eu": "eu"}

_lock = threading.Lock()
_client: MaxioAdvancedBillingClient | None = None


def build_client() -> MaxioAdvancedBillingClient:
    """Build a client from Django settings; raises ImproperlyConfigured when a setting is missing."""
    api_key = settings.MAXIO_API_KEY
    if not api_key:
        raise ImproperlyConfigured("MAXIO_API_KEY is not set")
    try:
        environment = ENVIRONMENTS[str(settings.MAXIO_ENVIRONMENT).strip().lower()]
    except KeyError:
        raise ImproperlyConfigured(
            "MAXIO_ENVIRONMENT must be one of %s" % ", ".join(sorted(ENVIRONMENTS))) from None

    base_url = settings.MAXIO_BASE_URL
    subdomain = settings.MAXIO_SITE_SUBDOMAIN
    target: ProductionUsConfigDict = {}
    if base_url:
        # Used verbatim: no "{site}" in it, so the subdomain plays no part.
        target["base_url"] = base_url
    elif subdomain:
        target["site"] = subdomain
    else:
        raise ImproperlyConfigured("Set MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL)")
    server_config: ServerConfigDict = (
        {"production": {"us": target}} if environment == "us" else {"production": {"eu": target}})

    return MaxioAdvancedBillingClient(
        environment=environment,
        timeout=float(settings.MAXIO_TIMEOUT),
        server_config=server_config,
        # Maxio's API key is the Basic-auth username; the password is the literal "x".
        basic_auth=BasicAuthCredentials(username=api_key, password="x"),
    )


def get_client() -> MaxioAdvancedBillingClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = build_client()
    return _client


def set_client(client: MaxioAdvancedBillingClient | None) -> None:
    """Replace the process client (tests inject one backed by a stub transport)."""
    global _client
    with _lock:
        old, _client = _client, client
    if old is not None and old is not client:
        old.close()


@atexit.register
def _close_client() -> None:
    if _client is not None:
        _client.close()
