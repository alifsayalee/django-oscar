"""
The one Maxio Advanced Billing client of this process, built from settings.
"""
import atexit
import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing import MaxioAdvancedBillingClient, ServerConfigDict
from maxio_advanced_billing.core import BasicAuthCredentials
from maxio_advanced_billing.server import Environment

_lock = threading.Lock()
_client: MaxioAdvancedBillingClient | None = None


def _server_config() -> tuple[Environment, ServerConfigDict]:
    environment = str(settings.MAXIO_ENVIRONMENT or 'us').strip().lower()
    base_url = settings.MAXIO_BASE_URL
    site = settings.MAXIO_SITE_SUBDOMAIN
    if not base_url and not site:
        raise ImproperlyConfigured('Set MAXIO_SITE_SUBDOMAIN (or MAXIO_BASE_URL).')
    # An explicit map: an unknown region fails here instead of silently
    # falling through to the SDK's default ("us").
    if environment == 'us':
        if base_url:
            return 'us', {'production': {'us': {'base_url': base_url}}}
        return 'us', {'production': {'us': {'site': site}}}
    if environment == 'eu':
        if base_url:
            return 'eu', {'production': {'eu': {'base_url': base_url}}}
        return 'eu', {'production': {'eu': {'site': site}}}
    raise ImproperlyConfigured(
        'MAXIO_ENVIRONMENT must be "us" or "eu", not %r.' % settings.MAXIO_ENVIRONMENT)


def build_client() -> MaxioAdvancedBillingClient:
    api_key = settings.MAXIO_API_KEY
    if not api_key:
        # Without credentials the SDK would send unauthenticated requests.
        raise ImproperlyConfigured('Set MAXIO_API_KEY.')
    environment, server_config = _server_config()
    return MaxioAdvancedBillingClient(
        environment=environment,
        timeout=float(settings.MAXIO_TIMEOUT),
        server_config=server_config,
        # Maxio takes the API key as the user name and "x" as the password.
        basic_auth=BasicAuthCredentials(username=api_key, password='x'),
    )


def get_client() -> MaxioAdvancedBillingClient:
    """
    Return the process-wide client, building it on first use (so it is built
    after a WSGI server forks its workers) and closing its pool at exit.
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                client = build_client()
                atexit.register(client.close)
                _client = client
    return _client


def product_family() -> str:
    family = settings.MAXIO_DEFAULT_PRODUCT_FAMILY
    if not family:
        raise ImproperlyConfigured('Set MAXIO_DEFAULT_PRODUCT_FAMILY.')
    return str(family)
